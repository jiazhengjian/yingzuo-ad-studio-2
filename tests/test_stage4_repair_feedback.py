"""Bounded repairs use their current draft; incomplete reviews are not content verdicts."""
import pytest

from app.beat_timing import script_capacity_budget
from app.progressive_planning import ProgressivePlanningError
from app.providers.gateway import ProviderError
from test_progressive_planning import (
    forbid_real_network, install_transport, overburden_first_beat,
    progressive_gateway, reject_first_shot, settings, simplify_first_beat, source,
)


def incomplete_review(review):
    review["alignment"]["shots"][0].pop("temporal_omission")


@pytest.mark.parametrize("failure", ["invalid_correction", "lost_rejection", "already_used"])
def test_incomplete_review_keeps_its_own_error_class_and_request_bound(progressive_gateway, failure):
    state = progressive_gateway
    state.first_review_mutation = incomplete_review
    if failure == "invalid_correction":
        state.second_review_mutation = incomplete_review
    elif failure == "lost_rejection":
        def rejected_and_incomplete(review):
            incomplete_review(review)
            reject_first_shot(review)
        state.first_review_mutation = rejected_and_incomplete
    else:
        state.second_review_mutation = reject_first_shot
        state.third_review_mutation = incomplete_review
    if failure == "already_used":
        plan = state.run()
        assert plan.get("plan_review", {}).get("ready") is not True
    else:
        with pytest.raises(ProviderError, match="这次没生成出来"):
            state.run()
    assert state.calls.count("plan_refinement") == int(failure == "already_used")
    assert state.calls.count("plan_review") == (3 if failure == "already_used" else 2)
    assert sum(context.get("task_kind") == "plan_review_response_repair"
               for context in state.contexts) == 1


def test_complete_content_rejection_does_not_become_an_incomplete_review(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = state.second_review_mutation = reject_first_shot
    plan = state.run()
    assert plan["segments"]
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    assert ProgressivePlanningError("owned failure").error_code == "planning_content_failed"


def test_semantic_repair_receives_current_capacity_and_actual_windows(progressive_gateway):
    state = progressive_gateway
    # The coordinator supplies a different valid draft; never reuse this early
    # capacity report merely because it has already been computed.
    state.draft_mutation = lambda script: script["beats"][0]["timing"].update(visual_seconds=21)
    state.first_review_mutation = reject_first_shot
    state.run()
    repair = state.contexts[3]
    assert repair["repair_capacity"] == script_capacity_budget(repair["draft_storyboard"]["ad_script"], 20)
    assert repair["repair_capacity"]["overrun_seconds"] == 0
    assert state.contexts[1]["draft_capacity"]["overrun_seconds"] > 0
    assert repair["current_execution_windows"] == state.contexts[2]["execution_windows"]
    assert repair["current_execution_window_basis"] and repair["repair_capacity_basis"]
    assert repair["final_execution"]
    assert not {"draft_script", "draft_capacity", "draft_capacity_basis", "draft_validation_issues"} & repair.keys()
    assert repair["review_feedback"]["alignment"]["shots"][0]["passed"] is False
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]


def test_unaccepted_candidate_gets_its_own_capacity_without_stale_windows(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0]["timing"].update(visual_seconds=3)
    state.first_review_mutation = reject_first_shot
    def lower_only(value):
        value["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 0.1
    state.refinement_mutations = [lower_only, lower_only]
    plan = state.run()
    assert plan["segments"]
    first, second = [context for context in state.contexts if context["stage"] == "plan_refinement"]
    assert second["repair_capacity"] == script_capacity_budget(second["draft_storyboard"]["ad_script"], 20)
    assert second["repair_capacity"] != first["repair_capacity"]
    assert second["review_feedback"]["kind"] == "unsupported_timing_reduction"
    assert second["review_feedback"]["previous_feedback"] == first["review_feedback"]
    assert first["current_execution_windows"]
    assert "current_execution_windows" not in second and "final_execution" not in second
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_refinement"]


def test_new_overrun_refreshes_budget_and_retains_unresolved_semantic_feedback(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def overload(value):
        overburden_first_beat(value)
        value["shots"][0]["description"] += "又额外完成多次取放。"
    state.refinement_mutations = [overload, simplify_first_beat]
    state.run()
    first, second = [context for context in state.contexts if context["stage"] == "plan_refinement"]
    assert first["repair_capacity"]["overrun_seconds"] == 0
    assert second["repair_capacity"]["overrun_seconds"] > 0
    assert second["repair_capacity"] == second["review_feedback"]["capacity"]
    assert second["review_feedback"]["previous_review"] == first["review_feedback"]
    assert "current_execution_windows" not in second and "final_execution" not in second
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_refinement", "plan_review"]


def test_capacity_feedback_does_not_unlock_fixed_speech_or_other_content(progressive_gateway):
    state = progressive_gateway
    state.brief = "台词逐字保留“看轮廓。”"
    state.first_review_mutation = reject_first_shot
    state.refinement_mutation = lambda value: value["ad_script"]["beats"][0].update(narration="改过的轮廓。")
    plan = state.run()
    spoken = "".join(beat.get("narration") or "" for beat in plan["ad_script"]["beats"])
    assert "看轮廓。" in spoken and "改过的轮廓" not in spoken
    repair = state.contexts[-1]
    assert repair["repair_capacity"]
    assert repair["fixed_speech"] == ["看轮廓。"]
    assert repair["repair_scope"]["beat_ids"] == ["b0"]
    assert not repair["repair_scope"].get("allow_beat_restructure")
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]
