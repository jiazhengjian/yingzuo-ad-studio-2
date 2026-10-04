"""Delivery behavior for advice, real rejections and versioned live reviews."""
from copy import deepcopy

import pytest

from app.plan_review import validate_plan_review
from app.planning_session import PlanningSession, PlanningSessionError
from app.progressive_planning import compile_director
from app.providers import ProviderError
from app.review_policy import REVIEW_POLICY_VERSION
from test_progressive_planning import (
    director_output, forbid_real_network, install_transport, progressive_gateway,
    settings, source,
)


def advice(raw):
    raw['suggestions'] = [{'kind': 'expression_quality',
        'target': '/segments/0/shots/0/description', 'quote': '办公桌角',
        'note': '可进一步丰富观看展开；当前商品信息与画面可以成立。'}]


def fact_failure(raw):
    row = next(item for item in raw['checks'] if item['criterion'] == 'factual_support')
    row.update(passed=False, note='离线负例：实际台词含来源未支持的功效。')
    raw['ready'] = False
    advice(raw)


def test_editorial_advice_is_saved_without_regeneration_or_content_mutation(progressive_gateway, source):
    state = progressive_gateway
    state.first_review_mutation = advice
    expected = compile_director(director_output(state.strategy), state.strategy,
        state.duration, 'continuity_v1', [source['id']])[0]
    plan = state.run()
    assert state.calls == ['ad_script', 'storyboard', 'plan_review']
    assert plan['plan_review']['ready']
    assert plan['plan_review']['policy_version'] == REVIEW_POLICY_VERSION
    assert len(plan['plan_review']['suggestions']) == 1
    for key in ('ad_script', 'segments', 'facts', 'production'):
        assert plan[key] == expected[key]
    assert state.contexts[-1]['review_policy_version'] == REVIEW_POLICY_VERSION
    assert validate_plan_review(plan['plan_review'], plan, require_review_policy=True).ready


def test_advice_cannot_release_repeated_factual_rejection(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = state.second_review_mutation = fact_failure
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_refinement', 'plan_review']
    feedback = state.contexts[3]['review_feedback']
    assert feedback['suggestions'] and not feedback['ready']
    assert next(c for c in feedback['checks'] if c['criterion'] == 'factual_support')['passed'] is False


def test_format_repair_cannot_relabel_a_failure_as_advice(progressive_gateway):
    state = progressive_gateway
    def malformed_failure(raw):
        fact_failure(raw)
        raw['alignment']['shots'][0].pop('visual_execution')
    state.first_review_mutation = malformed_failure
    state.second_review_mutation = advice  # Supplier attempts to return all true plus an advisory.
    with pytest.raises(ProviderError, match='完整检查尚未完成'):
        state.run()
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review']
    assert state.contexts[-1]['task_kind'] == 'plan_review_response_repair'


@pytest.mark.parametrize('correction_complete', [True, False])
def test_live_provider_cannot_reuse_legacy_binding_to_omit_grading(progressive_gateway, source, correction_complete):
    state = progressive_gateway
    expected = compile_director(director_output(state.strategy), state.strategy,
        state.duration, 'continuity_v1', [source['id']])[0]
    old_records = []
    def legacy(raw):
        raw.pop('policy_version'); raw.pop('suggestions')
        bound = validate_plan_review(raw, expected).model_dump()
        assert 'policy_version' not in bound and 'suggestions' not in bound
        assert validate_plan_review(bound, expected).ready
        raw.clear(); raw.update(bound); old_records.append(deepcopy(bound))
    state.first_review_mutation = legacy
    if correction_complete:
        plan = state.run()
        assert plan['plan_review']['policy_version'] == REVIEW_POLICY_VERSION
    else:
        state.second_review_mutation = legacy
        with pytest.raises(ProviderError, match='完整检查尚未完成'):
            state.run()
    assert state.calls == ['ad_script', 'storyboard', 'plan_review', 'plan_review']
    assert state.contexts[-1]['final_plan'] == state.contexts[-2]['final_plan']
    assert state.contexts[-1]['previous_review'] == old_records[0]


@pytest.mark.asyncio
async def test_review_policy_change_does_not_replay_an_old_pass():
    old = PlanningSession(None, None, {'same_project': True})
    async def old_result():
        return {'ready': True, 'legacy': True}
    await old.call('plan_review', {'stage': 'plan_review', 'final_plan': 'unchanged'}, old_result)
    restored = PlanningSession(old.state, None, {'same_project': True})
    called = False
    async def recompute():
        nonlocal called
        called = True
        return {'ready': True, 'recomputed': True}
    result = await restored.call('plan_review', {'stage': 'plan_review', 'final_plan': 'unchanged',
        'review_policy_version': REVIEW_POLICY_VERSION}, recompute)
    assert called and result == {'ready': True, 'recomputed': True}
    assert restored.state['entries'][0]['result']['recomputed'] is True
