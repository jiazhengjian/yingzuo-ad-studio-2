"""A bound review can justify one unchanged silent shot's smaller estimate.

Only offline fixtures are used. Passing these tests verifies authorization and
repair control flow, not a model's timing judgment or generated media quality.
"""
from copy import deepcopy

import pytest

from app.plan_review import PlanReview, plan_review_fingerprint, validate_plan_review
from app.progressive_planning import (
    PROGRESSIVE_REFINEMENT_POLICY_VERSION, TimingReductionError,
    _reviewed_timing_allows, _reviewed_timing_authorization,
    capacity_repair_scope, compile_director, validate_local_refinement,
)
from app.providers.gateway import ProviderError
from test_progressive_planning import (
    accepted_review, chosen_strategy, director_output, forbid_real_network,
    progressive_gateway,
)
from test_providers import install_transport, settings, source


def reject_other_shot(review, *, required=3.0):
    review["ready"] = review["alignment"]["ready"] = False
    review["execution_check"]["action_capacity"] = False
    review["alignment"]["shots"][0]["pacing"]["action_seconds_required"] = required
    other = review["alignment"]["shots"][2]
    other["passed"] = False
    other["pacing"].update(action_timing_passed=False, action_seconds_required=12)
    other["note"] = "本离线夹具要求精简末镜实际动作。"


def shorten_reviewed_and_repair_other(value, *, seconds=3.0):
    value["ad_script"]["beats"][0]["timing"]["visual_seconds"] = seconds
    value["shots"][2]["description"] = "商品与办公者同在固定构图内，省去反复取放与镜头上下移动。"


@pytest.fixture
def case(source):
    strategy = chosen_strategy(source, "none")
    raw = director_output(strategy)
    raw["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 3.4
    plan, _, before = compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])
    raw_review = accepted_review(plan)
    reject_other_shot(raw_review)
    review = validate_plan_review(raw_review, plan)
    after = deepcopy(before)
    shorten_reviewed_and_repair_other(after)
    return {"strategy": strategy, "plan": plan, "before": before, "after": after,
            "raw_review": raw_review, "review": review, "scope": capacity_repair_scope(before),
            "assets": [source["id"]]}


def authorization(case):
    return _reviewed_timing_authorization(case["before"], case["plan"], case["review"])


def test_policy_version_and_exact_reviewed_floor(case):
    assert PROGRESSIVE_REFINEMENT_POLICY_VERSION == 1
    auth = authorization(case)
    assert auth is not None and auth.minimum_ticks["b0"] == 30
    assert _reviewed_timing_allows(case["before"], case["after"], "b0", auth)
    validate_local_refinement(case["before"], case["after"], case["scope"], reviewed_timing=auth)
    plan, _, _ = compile_director(case["after"], case["strategy"], 20, "continuity_v1", case["assets"])
    assert plan["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 3
    assert sum(segment["duration_seconds"] for segment in plan["segments"]) == 20
    assert not plan.get("plan_review")  # Prior verdict never becomes a new verdict.
    assert plan_review_fingerprint(plan) != case["review"].reviewed_plan_sha256


@pytest.mark.parametrize("evidence", ["missing", "wire_dict", "unbound", "forged_feedback"])
def test_model_feedback_cannot_authorize_an_estimate_reduction(case, evidence):
    supplied = {"missing": None, "wire_dict": case["raw_review"],
        "unbound": PlanReview.model_validate(case["raw_review"]),
        "forged_feedback": {"minimum_ticks": {"b0": 1}, "before": case["before"]}}[evidence]
    assert _reviewed_timing_authorization(case["before"], case["plan"], supplied) is None
    with pytest.raises(TimingReductionError) as exc:
        validate_local_refinement(case["before"], case["after"], case["scope"], reviewed_timing=supplied)
    assert exc.value.beat_ids == ["b0"]


@pytest.mark.parametrize("changed", ["plan", "before", "bound_hash", "baseline_after_authorization"])
def test_authorization_is_bound_to_exact_old_content(case, changed):
    auth = authorization(case)
    if changed == "plan":
        case["plan"]["segments"][0]["shots"][0]["description"] += "改变了实际构图。"
    elif changed == "before":
        case["before"]["shots"][0]["continuity"]["camera_move"] += "再后退。"
    elif changed == "bound_hash":
        case["review"] = case["review"].model_copy(update={"reviewed_plan_sha256": "0" * 64})
    else:
        case["before"]["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 3.5
        assert not _reviewed_timing_allows(case["before"], case["after"], "b0", auth)
    assert _reviewed_timing_authorization(case["before"], case["plan"], case["review"]) is None


@pytest.mark.parametrize("issue", ["zero", "target_failed", "cut_failed", "temporal_unclear", "content_failed", "state_failed"])
def test_incomplete_or_failed_review_grants_no_target_bound(case, issue):
    raw = deepcopy(case["raw_review"])
    shot = raw["alignment"]["shots"][0]
    if issue == "zero":
        shot["pacing"]["action_seconds_required"] = 0
    elif issue == "target_failed":
        shot["passed"] = shot["pacing"]["action_timing_passed"] = False
    elif issue == "cut_failed":
        shot["passed"] = shot["pacing"]["cut_necessity_passed"] = False
    elif issue == "temporal_unclear":
        shot.update(passed=False, temporal_omission="unclear")
    elif issue == "content_failed":
        raw["checks"][0]["passed"] = False
    elif issue == "state_failed":
        raw["execution_check"]["state_continuity"] = False
    bound = validate_plan_review(raw, case["plan"])
    auth = _reviewed_timing_authorization(case["before"], case["plan"], bound)
    assert auth is None or "b0" not in auth.minimum_ticks


def test_reviewer_required_time_rounds_up_to_allocation_tick(case):
    raw = deepcopy(case["raw_review"])
    raw["alignment"]["shots"][0]["pacing"]["action_seconds_required"] = 3.01
    case["review"] = validate_plan_review(raw, case["plan"])
    auth = authorization(case)
    assert auth.minimum_ticks["b0"] == 31
    with pytest.raises(TimingReductionError):
        validate_local_refinement(case["before"], case["after"], case["scope"], reviewed_timing=auth)
    case["after"]["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 3.1
    validate_local_refinement(case["before"], case["after"], case["scope"], reviewed_timing=auth)


@pytest.mark.parametrize("invalid", ["missing_pacing", "non_finite"])
def test_bound_record_is_revalidated_before_granting_any_allowance(case, invalid):
    record = deepcopy(case["review"])
    if invalid == "missing_pacing":
        record.alignment.shots[0].pacing = None
    else:
        record.alignment.shots[0].pacing.action_seconds_required = float("nan")
    assert _reviewed_timing_authorization(case["before"], case["plan"], record) is None


@pytest.mark.parametrize("change", ["camera", "speech_overlap", "action_plan", "narration", "transition",
                                    "shot_order", "beat_order", "coverage", "neighbor_state", "neighbor_beat_state"])
def test_exception_does_not_cover_changed_execution_or_mapping(case, change):
    auth = authorization(case)
    after = case["after"]
    beat, shot = after["ad_script"]["beats"][0], after["shots"][0]
    if change == "camera":
        shot["continuity"]["camera_move"] = "快速环绕一周。"
    elif change == "speech_overlap":
        beat["timing"]["speech_overlap"] = False
    elif change == "action_plan":
        beat["timing"]["action_plan"] = "省去入场。"
    elif change == "narration":
        beat["narration"] = "新增一句话。"
    elif change == "transition":
        beat["timing"]["time_transition"] = "ellipsis"
    elif change == "shot_order":
        after["shots"][1:] = list(reversed(after["shots"][1:]))
    elif change == "beat_order":
        after["ad_script"]["beats"][1:] = list(reversed(after["ad_script"]["beats"][1:]))
    elif change == "coverage":
        shot["coverage"][0]["weight"] = 2
    elif change == "neighbor_state":
        after["shots"][1]["continuity"]["start_state"] = "商品已离开桌面。"
    else:
        after["ad_script"]["beats"][1]["timing"]["start_state"] = "商品已离开桌面。"
    assert not _reviewed_timing_allows(case["before"], after, "b0", auth)


@pytest.mark.parametrize("mapping", ["one_beat_many_shots", "one_shot_many_beats", "non_unit_weight", "spoken"])
def test_only_full_silent_one_to_one_shot_has_a_reviewed_floor(source, mapping):
    strategy = chosen_strategy(source, "voiceover" if mapping == "spoken" else "none")
    raw = director_output(strategy)
    raw["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 6
    if mapping == "one_beat_many_shots":
        split = deepcopy(raw["shots"][0])
        split["id"] = "split"
        split["continuity"]["entry_transition"] = "continuous"
        raw["shots"].insert(1, split)
    elif mapping == "one_shot_many_beats":
        raw["shots"][0]["coverage"].extend(raw["shots"].pop(1)["coverage"])
    elif mapping == "non_unit_weight":
        raw["shots"][0]["coverage"][0]["weight"] = 2
    plan, _, before = compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])
    review = validate_plan_review(accepted_review(plan), plan)
    auth = _reviewed_timing_authorization(before, plan, review)
    assert auth is None or "b0" not in auth.minimum_ticks


def test_global_estimate_only_reduction_still_fails(case):
    after = deepcopy(case["before"])
    after["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 3
    with pytest.raises(TimingReductionError) as exc:
        validate_local_refinement(case["before"], after, case["scope"], reviewed_timing=authorization(case))
    assert exc.value.whole_plan is True
    assert exc.value.beat_ids == []  # Shot bound is valid; whole-film guard remains.


def test_scope_guard_still_runs_before_estimate_guard(case):
    scope = deepcopy(case["scope"])
    scope.update(beat_ids=["b0"], shot_ids=["shot_0"], allow_beat_restructure=False)
    with pytest.raises(ValueError, match="未受影响镜头"):
        validate_local_refinement(case["before"], case["after"], scope, reviewed_timing=authorization(case))


def configure_gateway(state, source):
    state.strategy = chosen_strategy(source, "none")
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0]["timing"].update(visual_seconds=3.4)
    state.first_review_mutation = reject_other_shot
    state.refinement_mutation = shorten_reviewed_and_repair_other


def test_gateway_recompiles_and_independently_reviews_authorized_change(progressive_gateway, source):
    state = progressive_gateway
    configure_gateway(state, source)
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    assert plan["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 3
    review_context = state.contexts[-1]
    assert review_context["final_plan"]["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 3
    assert review_context["final_plan"]["segments"][0]["shots"][2]["description"] == plan["segments"][0]["shots"][2]["description"]
    assert len(plan["plan_review"]["alignment"]["shots"]) == 3
    assert plan["plan_review"]["ready"] is True
    assert plan["plan_review"]["reviewed_plan_sha256"] == plan_review_fingerprint(plan)
    old = director_output(state.strategy)
    state.first_director_mutation(old)
    old_plan, _, _ = compile_director(old, state.strategy, 20, "continuity_v1", [source["id"]])
    assert plan["plan_review"]["reviewed_plan_sha256"] != plan_review_fingerprint(old_plan)
    assert not {"reviewed_timing", "minimum_ticks", "bound_review"}.intersection(state.contexts[3])
    assert "reviewed_plan_sha256" not in state.contexts[3]["review_feedback"]


def test_failed_new_review_cannot_deliver(progressive_gateway, source):
    state = progressive_gateway
    configure_gateway(state, source)
    state.second_review_mutation = reject_other_shot
    with pytest.raises(ProviderError, match="没有产生有效的内容变化"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review", "plan_refinement"]


def test_fixed_speech_still_blocks_refinement(progressive_gateway, source):
    state = progressive_gateway
    state.brief = "固定台词“看桌角。”，其余只需展示。"
    state.first_director_mutation = lambda value: (
        value["ad_script"]["beats"][0].update(narration=""),
        value["ad_script"]["beats"][0]["timing"].update(visual_seconds=3.4))
    state.first_review_mutation = reject_other_shot
    def remove_fixed_line(value):
        shorten_reviewed_and_repair_other(value)
        value["ad_script"]["beats"][1]["narration"] = ""
    state.refinement_mutation = remove_fixed_line
    with pytest.raises(ProviderError, match="已确认要求"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]
