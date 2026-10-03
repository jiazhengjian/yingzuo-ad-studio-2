"""Authored offline regressions for planned windows, not model/media verdicts."""
from copy import deepcopy

import pytest

from app.av_review import review_execution_windows
from app.beat_timing import execution_action_bounds
from app.plan_review import lower_failed_plan_verdicts, validate_plan_review
from app.progressive_planning import _review_capacity_gaps, compile_director
from test_progressive_planning import (accepted_review, chosen_strategy, director_output,
    forbid_real_network, progressive_gateway)
from test_providers import settings, source, install_transport


def window(*, overlap=True, start=9.3, end=15.0, visual_end=12.1):
    return {"beat_id": "detail", "beat_start": start, "beat_end": end,
        "visual_start": start, "visual_end": visual_end,
        "speech_start": 9.4, "speech_end": 15.0,
        "speech_overlap": overlap, "visual_seconds": 2.8}


def gap_for(shot, beats, required):
    plan = {"segments": [{"id": "seg", "shots": [shot]}]}
    review = {"alignment": {"shots": [{"segment_id": "seg", "shot_id": shot["id"],
        "pacing": {"action_timing_passed": False, "action_seconds_required": required}}]}}
    original = deepcopy((plan, review, beats))
    result = _review_capacity_gaps(review, plan, [{"segment_id": "seg", "beats": beats}])
    assert (plan, review, beats) == original  # A progress metric cannot reverse a verdict.
    return result[tuple(shot["beat_ids"])]


def test_projection_exposes_actual_beat_bounds_without_changing_visual_estimate():
    schedule = [{"segment_index": 0, "duration_seconds": 20, "groups": [
        {"beat_id": "detail", "start": 9.3, "end": 15.0,
         "visual_start": 9.3, "visual_end": 12.1,
         "speech_start": 9.4, "speech_end": 15.0,
         "timing": {"visual_seconds": 2.8, "speech_overlap": True}}]}]
    original = deepcopy(schedule)
    projected = review_execution_windows(schedule, [{"id": "seg", "duration_seconds": 20}])
    assert projected[0]["beats"] == [window()]
    assert execution_action_bounds(projected[0]["beats"][0]) == (9.3, 15.0)
    assert schedule == original


@pytest.mark.parametrize("overlap,expected", [
    (True, (9.3, 15.0)), (False, (9.3, 12.1)), (None, (None, None))])
def test_only_explicit_parallel_policy_uses_the_full_beat(overlap, expected):
    assert execution_action_bounds(window(overlap=overlap)) == expected


def test_parallel_second_shot_uses_actual_allocation_not_estimated_visual_deadline():
    shot = {"id": "bottom", "start": 11.9, "end": 15.0, "beat_ids": ["detail"]}
    assert gap_for(shot, [window()], 2.8) == 0
    # The same shot remains short if the declared action is sequential before speech.
    assert gap_for(shot, [window(overlap=False)], 2.8) == pytest.approx(2.6)


def test_parallel_real_shot_overrun_is_not_hidden_by_the_longer_whole_beat():
    shot = {"id": "return", "start": 17.8, "end": 20.0, "beat_ids": ["detail"]}
    beat = window(start=15.1, end=20.0, visual_end=19.6)
    assert gap_for(shot, [beat], 2.8) == pytest.approx(.6)
    # Merely changing the author's estimate does not manufacture numeric progress.
    beat.update(visual_seconds=1, visual_end=16.1)
    assert gap_for(shot, [beat], 2.8) == pytest.approx(.6)


def test_mixed_beat_shot_counts_each_actual_intersection_without_borrowing():
    sequential = window(overlap=False, start=0, end=5, visual_end=2)
    sequential["beat_id"] = "first"
    parallel = window(start=5, end=10, visual_end=6)
    parallel["beat_id"] = "second"
    unrelated = window(start=0, end=20)
    unrelated["beat_id"] = "unrelated"
    shot = {"id": "bridge", "start": 1, "end": 8, "beat_ids": ["first", "second"]}
    # Available: 1 second before speech in first beat + 3 seconds in second beat.
    assert gap_for(shot, [sequential, parallel, unrelated], 5) == 1


def test_unknown_historical_policy_does_not_acquire_an_action_window():
    legacy = {"beat_id": "old", "beat_start": 0, "beat_end": 10,
        "visual_start": None, "visual_end": None, "speech_overlap": None}
    assert execution_action_bounds(legacy) == (None, None)


@pytest.mark.parametrize("failure", ["natural_speech", "action_capacity", "state_continuity", "temporal_omission"])
def test_corrected_windows_never_promote_an_existing_negative_review(source, failure):
    strategy = chosen_strategy(source)
    plan, schedule, _ = compile_director(director_output(strategy), strategy, 20,
        "continuity_v1", [source["id"]])
    rejected = accepted_review(plan)
    rejected["ready"] = False
    if failure == "natural_speech":
        next(check for check in rejected["checks"] if check["criterion"] == failure)["passed"] = False
    elif failure in ("action_capacity", "state_continuity"):
        rejected["execution_check"][failure] = False
    else:
        rejected["alignment"]["ready"] = False
        rejected["alignment"]["shots"][0].update(passed=False, temporal_omission="unsupported")
    original = deepcopy(rejected)
    windows = review_execution_windows(schedule, plan["segments"])
    assert all(execution_action_bounds(beat) == (beat["beat_start"], beat["beat_end"])
        for segment in windows for beat in segment["beats"])
    checked = validate_plan_review(lower_failed_plan_verdicts(rejected, plan), plan)
    assert not checked.ready
    assert rejected == original


def test_real_gateway_projects_full_beat_and_still_refines_then_reviews_all_content(progressive_gateway):
    state = progressive_gateway
    def reject(review):
        review["ready"] = False
        review["execution_check"]["action_capacity"] = False
        next(check for check in review["checks"] if check["criterion"] == "natural_speech")["passed"] = False
    state.first_review_mutation = reject
    state.refinement_mutation = lambda draft: draft["ad_script"]["beats"][0].update(narration="轮廓很清楚。")
    result = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    reviews = [context for context in state.contexts if context["stage"] == "plan_review"]
    reviewed_speech = []
    for context in reviews:
        beats = context["execution_windows"][0]["beats"]
        assert beats[0]["beat_start"] == 0 and beats[-1]["beat_end"] == 20
        assert all(execution_action_bounds(beat) == (beat["beat_start"], beat["beat_end"]) for beat in beats)
        plan = context["final_plan"]
        texts = [span["text"] for segment in plan["segments"] for span in segment["speech_spans"]]
        script = "".join(texts)
        assert script == "".join(beat["narration"] for beat in plan["ad_script"]["beats"])
        if "script" in plan:
            assert plan["script"] == script
        reviewed_speech.append(texts)
    assert reviewed_speech[0][0] == "看轮廓。"
    assert reviewed_speech[1][0] == "轮廓很清楚。"
    assert reviewed_speech[0][1:] == reviewed_speech[1][1:]
    assert result["script"] == "".join(reviewed_speech[1])
    assert result["plan_review"]["ready"] is True  # Only this new complete mock review passes.
