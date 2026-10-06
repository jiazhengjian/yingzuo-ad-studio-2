"""Invalid review recovery stays on the same plan and never promotes failure."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.db import Store, StaleRevision
from app.pipeline import Pipeline
from app.providers.gateway import Gateway, ProviderError
from app.strategy import StrategyRecord
from test_direction_selection import env, offer, select, isolated_skill_loader, forbid_unmocked_network
from test_progressive_planning import (progressive_gateway, settings, source, install_transport,
    director_output, accepted_review, reject_first_shot)


def incomplete(review):
    review['alignment']['shots'][0].pop('temporal_omission')


def test_known_wire_position_is_repaired_without_another_request(progressive_gateway):
    state = progressive_gateway
    def relocate(review):
        shot = review['alignment']['shots'][-1]
        shot['pacing']['temporal_omission'] = shot.pop('temporal_omission')
    state.first_review_mutation = relocate
    plan = state.run()
    assert plan['plan_review']['ready']
    assert state.calls == ['ad_script', 'storyboard', 'plan_review']


def test_a_continuity_only_failure_is_saved_and_not_reviewed_again(progressive_gateway):
    state = progressive_gateway

    def only_continuity(review):
        review["ready"] = False
        review["execution_check"]["state_continuity"] = False

    state.first_review_mutation = only_continuity
    plan = state.run()
    assert plan["plan_review"]["ready"] is False
    assert plan["plan_review"]["execution_check"]["state_continuity"] is False
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert state.phases.count("refining_storyboard") == 0
    assert not any(context.get("task_kind") == "plan_review_response_repair" for context in state.contexts)


def test_missing_review_field_gets_one_correction_without_reauthoring(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = incomplete
    plan = state.run()
    assert plan['plan_review']['ready']
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review']
    first, corrected = state.contexts[-2:]
    assert corrected['task_kind'] == 'plan_review_response_repair'
    assert first['final_plan'] == corrected['final_plan']
    assert first['execution_windows'] == corrected['execution_windows']
    assert corrected['validation_feedback']
    assert state.phases.count('refining_storyboard') == 0


def test_invalid_correction_stops_after_one_extra_review(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = state.second_review_mutation = incomplete
    plan = state.run()
    assert plan.get('plan_review', {}).get('ready') is not True
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review']


def test_format_correction_cannot_erase_content_failure(progressive_gateway):
    state = progressive_gateway
    def rejected_and_incomplete(review):
        reject_first_shot(review)
        incomplete(review)
    state.first_review_mutation = rejected_and_incomplete
    plan = state.run()
    assert plan.get('plan_review', {}).get('ready') is not True
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review']


def test_preserved_rejection_still_routes_to_content_refinement(progressive_gateway):
    state = progressive_gateway
    def rejected_and_incomplete(review):
        reject_first_shot(review)
        incomplete(review)
    state.first_review_mutation = rejected_and_incomplete
    state.second_review_mutation = reject_first_shot
    plan = state.run()
    assert plan['plan_review']['ready']
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review', 'plan_refinement', 'plan_review']
    assert state.contexts[2]['final_plan'] == state.contexts[3]['final_plan']
    assert state.contexts[3]['final_plan'] != state.contexts[5]['final_plan']


def test_review_correction_allowance_is_shared_across_refined_plans(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = incomplete
    state.second_review_mutation = reject_first_shot
    state.third_review_mutation = incomplete
    plan = state.run()
    assert plan.get('plan_review', {}).get('ready') is not True
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review', 'plan_refinement', 'plan_review']
    assert sum(c.get('task_kind') == 'plan_review_response_repair' for c in state.contexts) == 1


def test_cancel_callback_runs_before_correction_request(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = incomplete
    def cancel(phase):
        if phase == 'reviewing_plan' and state.calls.count('plan_review') == 1:
            raise StaleRevision('request changed')
    with pytest.raises(StaleRevision):
        state.run(cancel)
    assert state.calls == ['ad_script', 'storyboard', 'plan_review']


@pytest.fixture
def interrupted_review(env, monkeypatch, isolated_skill_loader):
    client, app, pid, _ = env
    gate, _ = offer(env)
    job = select(env, gate).json()['job']
    store, config = app.state.store, app.state.settings
    config.progressive_planning_enabled = config.direction_selection_enabled = True
    config.planning_generation_strategy = 'continuity_v1'
    config.planner_retry_delay_seconds = 0
    strategy = StrategyRecord.model_validate(store.get_job(job['id'])['payload']['selected_strategy'])
    state = SimpleNamespace(calls=[], corrected_inputs=[], interrupted=False, all_bad=False)
    async def respond(self, system, context, originals, max_tokens=7000):
        state.calls.append(context['stage'])
        if context['stage'] == 'ad_script': return director_output(strategy)['ad_script']
        if context['stage'] == 'storyboard': return director_output(strategy)
        assert context['stage'] == 'plan_review'
        if context.get('task_kind') == 'plan_review_response_repair':
            state.corrected_inputs.append(deepcopy(context))
            if not state.interrupted:
                state.interrupted = True
                raise asyncio.CancelledError()
            value = accepted_review(context['final_plan'])
            if state.all_bad: incomplete(value)
            return value
        value = accepted_review(context['final_plan']); incomplete(value); return value
    monkeypatch.setattr(Gateway, '_planner_json', respond)
    state.run = lambda target=None: asyncio.run(Pipeline(target or store, Gateway(config), config).execute(job['id']))
    state.store, state.job, state.config = store, job, config
    return state


def test_restart_resumes_only_unfinished_review_correction(interrupted_review):
    state = interrupted_review
    with pytest.raises(asyncio.CancelledError): state.run()
    checkpoint = state.store.get_job(state.job['id'])['payload']['planning_checkpoint']
    assert checkpoint['request_count'] == 4
    assert [x['state'] for x in checkpoint['entries']] == ['ready','ready','ready','running']
    restored = Store(state.store.db_path, state.store.media_root); restored.recover_jobs()
    result = state.run(restored)
    assert result['status'] == 'ready', result.get('error')
    assert state.calls == ['ad_script','storyboard','plan_review','plan_review','plan_review']
    assert state.corrected_inputs[0] == state.corrected_inputs[1]
    assert result['payload']['planning_checkpoint']['request_count'] == 5
    assert result['submission_count'] == 0


def test_restart_keeps_failed_correction_and_shared_request_budget(interrupted_review):
    state = interrupted_review
    with pytest.raises(asyncio.CancelledError): state.run()
    payload = state.store.get_job(state.job['id'])['payload']
    payload['planning_checkpoint']['request_count'] = 12
    state.store.update_job(state.job['id'], payload=payload)
    restored = Store(state.store.db_path, state.store.media_root); restored.recover_jobs()
    result = state.run(restored)
    assert result['status'] == 'failed'
    assert len(state.calls) == 4
    assert result['payload']['planning_checkpoint']['request_count'] == 12
    assert result['submission_count'] == 0
