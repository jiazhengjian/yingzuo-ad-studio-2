"""Joint authoring still compiles, independently reviews and bounds repairs offline."""
import asyncio
from copy import deepcopy

import pytest

from app.plan_review import plan_review_fingerprint
from app.planning_session import PlanningSessionError
from app.providers.gateway import Gateway, ProviderError
from app.schemas import Plan
from test_progressive_planning import (
    forbid_real_network, install_transport, progressive_gateway, reject_first_shot,
    reject_shot, settings, simplify_shot, source,
)


@pytest.fixture(autouse=True)
def forbid_media(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Text planning must not upload, generate or download media")

    for name in ("create_designs", "submit_video", "poll_video", "download_video",
                 "synthesize_voice", "_submit", "_uploaded_reference_url"):
        monkeypatch.setattr(Gateway, name, denied)


@pytest.fixture
def joint_gateway(progressive_gateway, settings):
    settings.joint_planning_enabled = True
    return progressive_gateway


def test_joint_authoring_completes_in_two_requests_with_real_compile_and_review(joint_gateway):
    state = joint_gateway
    plan = state.run()

    assert state.calls == ["storyboard", "plan_review"]
    initial, review = state.contexts
    assert initial["authoring_mode"] == "joint"
    assert not {"draft_script", "draft_capacity", "draft_capacity_basis",
                "draft_validation_issues"}.intersection(initial)
    assert initial["fixed_speech"] == []
    assert plan["strategy"] == state.strategy.model_dump()
    assert Plan.model_validate(plan).plan_review["ready"] is True
    assert plan["plan_review"]["reviewed_plan_sha256"] == plan_review_fingerprint(plan)
    assert review["execution_windows"]
    assert sum(segment["duration_seconds"] for segment in plan["segments"]) == state.duration
    for segment in plan["segments"]:
        assert segment["shots"][0]["start"] == 0
        assert segment["shots"][-1]["end"] == segment["duration_seconds"]
        for span in segment["speech_spans"]:
            assert 0 <= span["start"] < span["end"] <= segment["duration_seconds"]
    assert {item["stage"] for item in plan["skill_trace"]} == {"storyboard", "plan_review"}


def test_disabled_joint_mode_keeps_the_previous_three_requests(progressive_gateway, settings):
    settings.joint_planning_enabled = False
    state = progressive_gateway
    plan = state.run()

    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert state.contexts[1].get("authoring_mode") != "joint"
    assert state.contexts[1]["draft_script"]
    assert state.contexts[1]["draft_capacity"]
    assert plan["plan_review"]["reviewed_plan_sha256"] == plan_review_fingerprint(plan)


def test_joint_review_failure_still_scopes_repair_and_reviews_the_whole_result(joint_gateway):
    state = joint_gateway
    state.first_review_mutation = reject_first_shot
    plan = state.run()

    assert state.calls == ["storyboard", "plan_review", "plan_refinement", "plan_review"]
    repair = state.contexts[2]
    assert repair["repair_scope"]["beat_ids"] == ["b0"]
    assert repair["repair_scope"]["shot_ids"] == ["shot_0"]
    assert repair["repair_capacity"]
    assert repair["review_feedback"]["alignment"]["shots"][0]["passed"] is False
    assert repair["current_execution_windows"]
    assert plan["segments"][0]["shots"][0]["description"].endswith("仅保留静置辨认。")
    assert len(plan["plan_review"]["alignment"]["shots"]) == 3
    assert len(plan["plan_review"]["alignment"]["spans"]) == 3
    assert plan["plan_review"]["reviewed_plan_sha256"] == plan_review_fingerprint(plan)


def test_joint_repeated_unimproved_content_is_not_given_an_extra_round(joint_gateway):
    state = joint_gateway
    state.first_review_mutation = state.second_review_mutation = reject_first_shot
    plan = state.run()
    assert plan["segments"]
    assert state.calls == ["storyboard", "plan_review", "plan_refinement", "plan_review"]


def test_joint_authoring_keeps_the_existing_two_repair_limit(joint_gateway):
    state = joint_gateway
    state.first_review_mutation = lambda review: reject_shot(review, 0)
    state.second_review_mutation = lambda review: reject_shot(review, 2)
    state.third_review_mutation = lambda review: reject_shot(review, 1)
    state.refinement_mutations = [simplify_shot(0), simplify_shot(2)]
    plan = state.run()
    assert plan["segments"]
    assert state.calls == ["storyboard", "plan_review", "plan_refinement", "plan_review",
                           "plan_refinement", "plan_review"]


@pytest.mark.parametrize("change_during", ["initial", "repair"])
def test_joint_authoring_cannot_change_a_fixed_merchant_line(joint_gateway, change_during):
    state = joint_gateway
    state.brief = "保留台词“看轮廓。”，继续安排。"

    def change(value):
        value["ad_script"]["beats"][0]["narration"] = "看外形。"

    if change_during == "initial":
        state.first_director_mutation = change
        with pytest.raises(ProviderError):
            state.run()
        assert state.calls == ["storyboard"]
    else:
        state.first_review_mutation = reject_first_shot
        state.refinement_mutation = change
        plan = state.run()
        spoken = "".join(beat.get("narration") or "" for beat in plan["ad_script"]["beats"])
        assert "看轮廓。" in spoken and "看外形" not in spoken
        assert state.calls == ["storyboard", "plan_review", "plan_refinement"]
        assert state.contexts[-1]["fixed_speech"] == ["看轮廓。"]
    assert state.contexts[0]["fixed_speech"] == ["看轮廓。"]


def test_joint_incomplete_review_stops_without_turning_it_into_content_repair(joint_gateway):
    state = joint_gateway

    def incomplete(review):
        review["alignment"]["shots"][0].pop("temporal_omission")

    state.first_review_mutation = state.second_review_mutation = incomplete
    plan = state.run()
    assert plan["segments"]
    assert plan.get("plan_review", {}).get("ready") is not True
    assert state.calls == ["storyboard", "plan_review", "plan_review"]


@pytest.mark.parametrize("before", [False, True])
def test_joint_mode_is_part_of_durable_scope_and_cannot_replay_another_route(
        progressive_gateway, settings, source, before):
    state = progressive_gateway
    settings.joint_planning_enabled = before
    checkpoint = None

    def save(value):
        nonlocal checkpoint
        checkpoint = deepcopy(value)

    def run(saved=None):
        return asyncio.run(Gateway(settings).decide(
            {"brief": state.brief, "duration_seconds": state.duration}, [source],
            state.conversation, [], state.current_plan,
            selected_strategy=state.strategy.model_dump(), planning_checkpoint=saved,
            on_checkpoint=save))

    first = run()
    assert first["plan"]["plan_review"]["ready"] is True
    assert checkpoint["request_count"] == (2 if before else 3)
    expected_calls = list(state.calls)
    frozen_checkpoint = deepcopy(checkpoint)
    replayed = run(checkpoint)
    assert replayed["plan"] == first["plan"]
    assert state.calls == expected_calls

    settings.joint_planning_enabled = not before
    with pytest.raises(PlanningSessionError) as error:
        run(checkpoint)
    assert error.value.error_code == "planning_invalid_response"
    assert error.value.retryable is False
    assert state.calls == expected_calls
    assert checkpoint == frozen_checkpoint


def test_refinement_policy_change_cannot_replay_previous_planning_verdicts(
        joint_gateway, settings, source, monkeypatch):
    import app.providers.gateway as gateway_module

    state = joint_gateway
    checkpoint = None

    def save(value):
        nonlocal checkpoint
        checkpoint = deepcopy(value)

    def run(saved=None):
        return asyncio.run(Gateway(settings).decide(
            {"brief": state.brief, "duration_seconds": state.duration}, [source],
            state.conversation, [], state.current_plan,
            selected_strategy=state.strategy.model_dump(), planning_checkpoint=saved,
            on_checkpoint=save))

    first = run()
    assert first["plan"]["plan_review"]["ready"] is True
    saved = deepcopy(checkpoint)
    expected_calls = list(state.calls)
    monkeypatch.setattr(gateway_module, "PROGRESSIVE_REFINEMENT_POLICY_VERSION",
                        gateway_module.PROGRESSIVE_REFINEMENT_POLICY_VERSION + 1)
    result = run(saved)
    assert result["plan"]["plan_review"]["ready"] is True
    assert len(state.calls) > len(expected_calls)
    assert checkpoint != saved
