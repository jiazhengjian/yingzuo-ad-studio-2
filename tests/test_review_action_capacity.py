"""Deterministic action-window bounds, without judging authored action meaning."""
from copy import deepcopy

import pytest

from app.av_review import (_planned_action_limits, _shot_action_window_union,
                           lower_failed_av_verdicts, validate_av_review)
from app.plan_review import (lower_failed_plan_verdicts, plan_review_fingerprint,
                             validate_plan_review)
from app.progressive_planning import compile_director
from app.review_recovery import preserve_review_rejections
from app.schemas import Plan
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import settings, source
from test_review_output import omit_success_notes


def window(beat_id, start, end, *, visual_end=None, parallel=True):
    return {"beat_id": beat_id, "beat_start": start, "beat_end": end,
            "visual_start": start, "visual_end": end if visual_end is None else visual_end,
            "speech_overlap": parallel}


def shot(start, end, *beat_ids):
    return {"id": "shot_02", "start": start, "end": end,
            "beat_id": beat_ids[0], "beat_ids": list(beat_ids)}


def test_regression_eight_second_shot_has_only_four_point_one_action_seconds():
    # Numerical reproduction of the failed review: .7s of a parallel beat,
    # followed by 3.4s of sequential action. The remaining 4s cannot be borrowed.
    current = shot(1.2, 9.3, "first", "second")
    beats = [window("first", 0, 1.9),
             window("second", 1.9, 9.3, visual_end=5.3, parallel=False)]
    assert current["end"] - current["start"] == pytest.approx(8.1)
    assert _shot_action_window_union(current, beats) == pytest.approx(4.1)


@pytest.mark.parametrize("parallel,expected", [(True, 3.1), (False, .2)])
def test_parallel_beat_uses_actual_allocation_not_the_visual_estimate(parallel, expected):
    assert _shot_action_window_union(shot(11.9, 15, "detail"), [
        window("detail", 9.3, 15, visual_end=12.1, parallel=parallel)]) == pytest.approx(expected)


def test_adjacent_beats_and_overlapping_windows_count_only_the_union():
    current = shot(1, 8, "first", "second", "third")
    beats = [window("first", 0, 5, visual_end=2, parallel=False),
             window("second", 5, 7), window("third", 6, 10),
             window("unrelated", 0, 20)]
    # [1,2] + [5,7] union [6,8] = 4, not 5; unrelated windows never count.
    assert _shot_action_window_union(current, beats) == 4


def test_speech_only_part_cannot_borrow_an_earlier_visual_window():
    assert _shot_action_window_union(shot(5, 8, "first"), [
        window("first", 0, 10, visual_end=2, parallel=False)]) == 0


@pytest.mark.parametrize("unknown", ["timing", "missing_beat", "invalid_bound"])
def test_unknown_historical_windows_do_not_invent_a_smaller_limit(unknown):
    beats = [window("first", 0, 5, visual_end=2, parallel=False), window("second", 5, 10)]
    if unknown == "timing":
        beats[1]["speech_overlap"] = None
    elif unknown == "missing_beat":
        beats.pop()
    else:
        beats[1]["beat_end"] = float("nan")
    assert _shot_action_window_union(shot(0, 10, "first", "second"), beats) is None


def compiled(source, *, parallel=False):
    strategy = chosen_strategy(source)
    draft = director_output(strategy, count=2)
    for beat in draft["ad_script"]["beats"]:
        beat["timing"].update(visual_seconds=2, speech_overlap=parallel)
    draft["shots"] = [draft["shots"][0]]
    draft["shots"][0]["coverage"] = [{"beat_id": f"b{i}", "weight": 1} for i in range(2)]
    plan, _, _ = compile_director(draft, strategy, 20, "continuity_v1", [source["id"]])
    return plan


def overrun_review(plan, required=7):
    raw = omit_success_notes(accepted_review(plan))
    raw["alignment"]["shots"][0]["pacing"]["action_seconds_required"] = required
    return raw


def test_fresh_review_keeps_a_pass_when_the_shot_itself_is_long_enough(source):
    # The reserved action window is 4 seconds. The shot is the whole segment.
    # 7 seconds of action fits the shot, so the tighter window must not fail it.
    plan = compiled(source)
    raw = overrun_review(plan)
    before = deepcopy((plan, raw))
    assert _planned_action_limits(plan) == {("seg_1", "shot_0"): 4}
    assert validate_plan_review(raw, plan, require_visual_execution=True).ready
    assert lower_failed_plan_verdicts(raw, plan) == raw
    assert (plan, raw) == before


def test_tighter_intersection_does_not_overturn_a_pass_inside_the_shot(source, monkeypatch):
    plan = compiled(source)
    monkeypatch.setattr("app.av_review._planned_action_limits", lambda _plan: {("seg_1", "shot_0"): 0.1})
    raw = overrun_review(plan, 1.6)
    pacing = lower_failed_av_verdicts(raw["alignment"], plan)["shots"][0]["pacing"]
    assert pacing["action_timing_passed"] is True
    assert validate_plan_review(raw, plan, require_visual_execution=True).ready


def test_action_longer_than_the_shot_still_lowers_a_pass(source):
    plan = compiled(source)
    shot = plan["segments"][0]["shots"][0]
    length = shot["end"] - shot["start"]
    required = length + 1
    raw = overrun_review(plan, required)
    before = deepcopy((plan, raw))
    with pytest.raises(ValueError, match="超过镜头时长"):
        validate_plan_review(raw, plan)
    with pytest.raises(ValueError, match="超过镜头时长"):
        validate_av_review(raw["alignment"], plan, require_pacing=True)
    lowered = lower_failed_plan_verdicts(raw, plan)
    checked = validate_plan_review(lowered, plan)
    assert not checked.ready and not checked.alignment.ready
    check = checked.alignment.shots[0]
    assert not check.passed and not check.pacing.action_timing_passed
    assert check.pacing.action_seconds_required == required
    note = f"程序计算：shot_0动作估计{required:g}秒，镜头实际只有{length:g}秒，超过镜头时长。"
    assert check.note == check.pacing.note == note
    assert "动作窗口并集" not in note
    assert checked.execution_check.action_capacity  # No invented semantic judgment.
    assert (plan, raw) == before
    with pytest.raises(ValueError, match="已有失败"):
        preserve_review_rejections(raw, overrun_review(plan, 1), plan)


@pytest.mark.parametrize("required", [0, 3.9, 4, 4.00000001])
def test_union_upper_bound_does_not_reject_legal_boundary_estimates(source, required):
    plan = compiled(source)
    raw = overrun_review(plan, required)
    assert lower_failed_plan_verdicts(raw, plan) == raw
    assert validate_plan_review(raw, plan).ready


def test_parallel_whole_beat_allows_action_during_speech_without_fabricating_notes(source):
    plan = compiled(source, parallel=True)
    raw = overrun_review(plan, 19)
    assert _planned_action_limits(plan) == {("seg_1", "shot_0"): 20}
    assert lower_failed_plan_verdicts(raw, plan) == raw
    assert validate_plan_review(raw, plan).ready


def test_bound_historical_review_and_saved_plan_remain_readable_but_live_review_is_strict(source):
    plan = compiled(source)
    stored = overrun_review(plan)
    stored.update(reviewed_plan_sha256=plan_review_fingerprint(plan),
                  reviewed_reference_asset_ids={segment["id"]: segment["reference_asset_ids"]
                                               for segment in plan["segments"]})
    assert validate_plan_review(stored, plan).ready
    assert Plan.model_validate({**plan, "plan_review": stored}).plan_review == stored
    # A saved pass stays a pass when the shot is long enough, even on the live wire.
    assert validate_plan_review(stored, plan, require_visual_execution=True).ready
    shot = plan["segments"][0]["shots"][0]
    length = shot["end"] - shot["start"]
    too_long = overrun_review(plan, length + 1)
    too_long.update(reviewed_plan_sha256=stored["reviewed_plan_sha256"],
                    reviewed_reference_asset_ids=stored["reviewed_reference_asset_ids"])
    with pytest.raises(ValueError, match="超过镜头时长"):
        validate_plan_review(too_long, plan, require_visual_execution=True)
    assert not validate_plan_review(lower_failed_plan_verdicts(too_long, plan), plan).ready


def test_actual_legacy_script_without_timing_keeps_whole_shot_guard(source):
    plan = compiled(source)
    for beat in plan["ad_script"]["beats"]:
        beat.pop("timing")
    Plan.model_validate(plan)
    raw = overrun_review(plan)
    assert _planned_action_limits(plan) == {}
    assert lower_failed_plan_verdicts(raw, plan) == raw
    assert validate_plan_review(raw, plan).ready
    too_long = overrun_review(plan, 21)
    lowered = lower_failed_plan_verdicts(too_long, plan)
    assert not lowered["ready"]
    assert "note" not in lowered["alignment"]["shots"][0]["pacing"]
    with pytest.raises(ValueError, match="note"):
        validate_plan_review(lowered, plan)


def test_uncertain_schedule_reconstruction_does_not_guess_a_new_bound(source):
    plan = compiled(source)
    plan["segments"][0]["speech_spans"][0]["start"] += .1
    assert _planned_action_limits(plan) == {}


@pytest.mark.parametrize("failure", ["visual", "cut", "shot"])
def test_math_reason_cannot_fill_missing_reasons_for_other_negative_opinions(source, failure):
    plan = compiled(source)
    raw = overrun_review(plan)
    row = raw["alignment"]["shots"][0]
    if failure == "visual":
        row["visual_execution"]["verdict"] = "unclear"
    elif failure == "cut":
        row["pacing"]["cut_necessity_passed"] = False
    else:
        row["passed"] = False
    with pytest.raises(ValueError, match="note"):
        validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan)


def test_existing_authored_notes_and_other_failures_remain_unchanged(source):
    plan = compiled(source)
    raw = accepted_review(plan)
    row = raw["alignment"]["shots"][0]
    row["pacing"]["action_seconds_required"] = 7
    original = deepcopy(raw)
    lowered = lower_failed_av_verdicts(raw["alignment"], plan)
    assert lowered["shots"][0]["note"] == row["note"]
    assert lowered["shots"][0]["pacing"]["note"] == row["pacing"]["note"]
    assert raw == original
