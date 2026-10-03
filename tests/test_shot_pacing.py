"""Content windows are not edits; pacing estimates gate new, not saved plans.

These are structural and review-contract tests. They do not claim to measure
generated actions, understand a picture, or establish a successful ad.
"""
from copy import deepcopy

import pytest

from app.ad_script import AdScript
from app.av_review import validate_av_review
from app.planning import expression_schedule, validate_expression_schedule
from app.schemas import Plan, Shot
from app.storyboard_repair import can_repair_timing, timing_repair_preserves_contract, repairable_timing_issues
from pydantic import ValidationError
from test_directing_contract import directed_plan_data
from test_speech_spans import plan_data


@pytest.fixture
def long_shot_case(plan_data):
    value = deepcopy(plan_data)
    value["generation_strategy"] = "continuity_v1"
    value["ad_script"]["beats"][1]["narration"] = "这一块，留给你。"
    value["script_review"]["script"] = deepcopy(value["ad_script"])
    script = AdScript.model_validate(value["ad_script"])
    schedule = expression_schedule(script, [10], beat_groups=[[0, 1]])
    segment = value["segments"][0]
    segment["shots"] = [{"id": "together", "start": 0, "end": 10,
        "beat_id": "choice", "beat_ids": ["choice", "ending"], "narration": "",
        "description": "同一中景中，桌边的人看向蛋糕，再将同一盘蛋糕轻推给身旁的人，蛋糕切面始终可见。",
        "purpose": "在同一动作中呈现下午茶选择与邀请", "evidence_ids": ["layers"]}]
    segment["speech_spans"] = [{"id": "voice_" + group["beat_id"], "beat_id": group["beat_id"],
        "text": group["text"], "start": group["speech_start"], "end": group["speech_end"],
        "shot_ids": ["together"], "anchors": [{"text": group["text"],
            "start": group["speech_start"], "end": group["speech_end"], "shot_ids": ["together"],
            "visual_evidence": "相应时段的蛋糕、目光与邀请动作共同承接这句话"}]}
        for group in schedule[0]["groups"]]
    value["script"] = segment["narration"] = "".join(span["text"] for span in segment["speech_spans"])
    return value, schedule


def test_one_shot_can_cover_two_beats_without_moving_either_sentence(long_shot_case):
    value, schedule = long_shot_case
    original = deepcopy(value)
    plan = Plan.model_validate(value, context={"require_beat_ids": True})
    validate_expression_schedule(plan.model_dump(), schedule)
    assert len(plan.segments[0].shots) == 1
    assert [span.shot_ids for span in plan.segments[0].speech_spans] == [["together"], ["together"]]
    assert plan.segments[0].speech_spans[0].end <= plan.segments[0].speech_spans[1].start
    assert value == original


def test_cut_may_follow_action_instead_of_reserved_beat_boundary(long_shot_case):
    value, schedule = long_shot_case
    segment = value["segments"][0]
    boundary = schedule[0]["groups"][0]["end"]
    cut = boundary + .7
    second = deepcopy(segment["shots"][0])
    segment["shots"][0]["end"] = cut
    second.update(id="settle", start=cut, beat_id="ending", beat_ids=["ending"],
                  description="同一盘蛋糕停在同伴面前，保持动作完成后的状态。")
    segment["shots"].append(second)
    span = segment["speech_spans"][1]
    span["shot_ids"] = span["anchors"][0]["shot_ids"] = ["together", "settle"]
    plan = Plan.model_validate(value)
    validate_expression_schedule(plan.model_dump(), schedule)
    assert plan.segments[0].shots[0].end != boundary


@pytest.mark.parametrize("invalid", ["missing_beat", "wrong_primary", "duplicate", "unknown", "reverse", "scene_change"])
def test_long_shot_cannot_hide_missing_content_or_teleport(long_shot_case, invalid):
    value, _ = long_shot_case
    shot = value["segments"][0]["shots"][0]
    if invalid == "missing_beat": shot["beat_ids"] = ["choice"]
    elif invalid == "wrong_primary": shot["beat_id"] = "ending"
    elif invalid == "duplicate": shot["beat_ids"] = ["choice", "choice", "ending"]
    elif invalid == "unknown": shot["beat_ids"].append("invented")
    elif invalid == "reverse": shot.update(beat_id="ending", beat_ids=["ending", "choice"])
    elif invalid == "scene_change":
        value["ad_script"]["beats"][1]["boundary_before"] = "scene_change"
        value["script_review"]["script"] = deepcopy(value["ad_script"])
    with pytest.raises(ValueError):
        Plan.model_validate(value)


def test_declared_beats_must_actually_overlap_the_shot(long_shot_case):
    value, schedule = long_shot_case
    # Both records claim both beats, but the first stops at the actual boundary.
    second = deepcopy(value["segments"][0]["shots"][0])
    boundary = schedule[0]["groups"][0]["end"]
    value["segments"][0]["shots"][0]["end"] = boundary
    second.update(id="second", start=boundary)
    value["segments"][0]["shots"].append(second)
    with pytest.raises(ValueError, match="实际时段相交"):
        validate_expression_schedule(value, schedule)


def test_cross_beat_shot_does_not_relax_reviewed_speech_times(long_shot_case):
    value, schedule = long_shot_case
    value["segments"][0]["speech_spans"][1]["start"] += .2
    with pytest.raises(ValueError, match="说话时间"):
        validate_expression_schedule(value, schedule)


def test_old_plan_dump_and_opt_in_boundary_remain_unchanged(plan_data):
    before = Plan.model_validate(plan_data).model_dump()
    assert "production" not in before
    for segment in before["segments"]:
        assert "production" not in segment and "reference_roles" not in segment
        assert all("beat_ids" not in shot and "character_ids" not in shot for shot in segment["shots"])
        assert all("speaker_id" not in span for span in segment["speech_spans"])
    assert Plan.model_validate(before).model_dump() == before
    with pytest.raises(ValueError, match="beat_ids"):
        Plan.model_validate(plan_data, context={"require_beat_ids": True})


def test_repair_preserves_multi_beat_story_and_frozen_speech(long_shot_case):
    value, schedule = long_shot_case
    args = (schedule, value["ad_script"], {"layers"}, {"photo"})
    assert can_repair_timing(value, *args)
    repaired = deepcopy(value)
    repaired["segments"][0]["speech_spans"][0]["anchors"][0]["start"] += .1
    assert timing_repair_preserves_contract(value, repaired)
    assert can_repair_timing(repaired, *args)
    repaired["segments"][0]["shots"][0]["description"] = "重写为无关的商品特写"
    assert not timing_repair_preserves_contract(value, repaired)
    repaired = deepcopy(value)
    repaired["segments"][0]["speech_spans"][1]["start"] += .1
    assert not timing_repair_preserves_contract(value, repaired)
    assert not can_repair_timing(repaired, *args)


def test_late_cut_is_repairable_without_changing_declared_content_or_speech(long_shot_case):
    value, schedule = long_shot_case
    segment = value["segments"][0]
    boundary = schedule[0]["groups"][0]["end"]
    first = segment["shots"][0]
    first.update(end=boundary + .7, beat_ids=["choice"], description="人物望向办公桌边的蛋糕切块。")
    second = {**deepcopy(first), "id": "invitation", "beat_id": "ending", "beat_ids": ["ending"],
              "start": boundary + .7, "end": 10, "description": "人物把同一盘蛋糕轻推向身旁的人。"}
    segment["shots"].append(second)
    segment["speech_spans"][1]["shot_ids"] = ["invitation"]
    segment["speech_spans"][1]["anchors"][0]["shot_ids"] = ["invitation"]
    with pytest.raises(ValidationError) as error:
        Plan.model_validate(value)
    assert repairable_timing_issues(error.value)[0]["code"] == "speech_coverage"
    args = (schedule, value["ad_script"], {"layers"}, {"photo"})
    # The current cut crosses the wrong content window, but the fixed ordered
    # memberships can be correctly scheduled without rewriting either picture.
    assert can_repair_timing(value, *args)
    repaired = deepcopy(value)
    repaired["segments"][0]["shots"][0]["end"] = boundary
    repaired["segments"][0]["shots"][1]["start"] = boundary
    assert timing_repair_preserves_contract(value, repaired)
    assert can_repair_timing(repaired, *args)
    plan = Plan.model_validate(repaired)
    validate_expression_schedule(plan.model_dump(), schedule)
    assert repaired["segments"][0]["speech_spans"] == segment["speech_spans"]
    assert [shot["description"] for shot in repaired["segments"][0]["shots"]] == [shot["description"] for shot in segment["shots"]]


def test_repair_preflight_rejects_memberships_that_cannot_share_a_cut(long_shot_case):
    value, schedule = long_shot_case
    first = value["segments"][0]["shots"][0]
    first["end"] = 5
    value["segments"][0]["shots"].append({**deepcopy(first), "id": "again", "start": 5, "end": 10})
    # Both adjacent shots claim both beats. They cannot each cross the same
    # beat boundary without overlapping in time; moving cuts cannot fix that.
    assert not can_repair_timing(value, schedule, value["ad_script"], {"layers"}, {"photo"})


def test_extra_director_fields_are_not_swallowed_as_timing_repair(directed_plan_data):
    directed_plan_data["visual_direction"]["camera_style_note"] = "不是Schema字段"
    span = directed_plan_data["segments"][0]["speech_spans"][0]
    span["shot_ids"] = ["detail"]  # Also starts later than the speech window.
    with pytest.raises(ValidationError) as error:
        Plan.model_validate(directed_plan_data)
    assert {item["type"] for item in error.value.errors()} == {"extra_forbidden", "value_error"}
    assert repairable_timing_issues(error.value) == []


def pacing_review(plan, required=5):
    return {"ready": True, "ad_expression_passed": True, "summary": "计划的动作与邀请表达有足够展开时间。",
        "spans": [{"segment_id": segment.id, "span_id": span.id, "passed": True,
                   "note": "相应说话窗口保留完整句并引用实际动作镜头。"}
                  for segment in plan.segments for span in segment.speech_spans or []],
        "shots": [{"segment_id": segment.id, "shot_id": shot.id, "passed": True,
                   "note": "同一商品在完整动作中保持可辨认。", "pacing": {
                       "action_seconds_required": required, "action_timing_passed": True,
                       "cut_necessity_passed": True, "note": "观察与轻推动作有展开时间；连续观察无实际切镜。"}}
                  for segment in plan.segments for shot in segment.shots]}


def test_new_review_requires_action_and_cut_review_but_old_records_stay_readable(long_shot_case):
    plan = Plan.model_validate(long_shot_case[0])
    raw = pacing_review(plan)
    assert validate_av_review(raw, plan, require_pacing=True).ready
    raw["shots"][0].pop("pacing")
    assert validate_av_review(raw, plan).model_dump() == raw
    with pytest.raises(ValueError, match="动作所需时间"):
        validate_av_review(raw, plan, require_pacing=True)


def test_numeric_action_budget_cannot_be_overruled_by_a_pass_label(long_shot_case):
    plan = Plan.model_validate(long_shot_case[0])
    raw = pacing_review(plan, required=12)
    with pytest.raises(ValueError, match="超过镜头时长"):
        validate_av_review(raw, plan, require_pacing=True)
    raw["ready"] = raw["shots"][0]["passed"] = False
    raw["shots"][0]["pacing"]["action_timing_passed"] = False
    assert not validate_av_review(raw, plan, require_pacing=True).ready
    # An overfull action may need more than the provider's 30-second capacity;
    # the review must be able to record that failure instead of truncating it.
    raw["shots"][0]["pacing"]["action_seconds_required"] = 40
    assert not validate_av_review(raw, plan, require_pacing=True).ready


def test_two_seconds_cannot_pass_a_review_that_requires_five_for_multiple_actions(long_shot_case):
    value, _ = long_shot_case
    segment = value["segments"][0]
    first = deepcopy(segment["shots"][0])
    first.update(id="rushed", end=2, beat_ids=["choice"],
                 description="人物拿起叉子、切取一口、送入口中，咀嚼后再将盘子递出。")
    segment["shots"][0]["start"] = 2
    segment["shots"].insert(0, first)
    span = segment["speech_spans"][0]
    span["shot_ids"].insert(0, "rushed")
    span["anchors"][0]["shot_ids"].insert(0, "rushed")
    plan = Plan.model_validate(value)
    raw = pacing_review(plan, required=5)
    raw["shots"][0]["pacing"]["note"] = "取叉、切取、入口、咀嚼、递盘为顺序动作，估计至少五秒，两秒不足。"
    with pytest.raises(ValueError, match="超过镜头时长"):
        validate_av_review(raw, plan, require_pacing=True)
    raw["ready"] = raw["shots"][0]["passed"] = False
    raw["shots"][0]["pacing"]["action_timing_passed"] = False
    assert not validate_av_review(raw, plan, require_pacing=True).ready


def test_cut_without_viewing_value_cannot_pass_whole_review(long_shot_case):
    plan = Plan.model_validate(long_shot_case[0])
    raw = pacing_review(plan)
    raw["shots"][0]["pacing"].update(cut_necessity_passed=False, note="待审镜头反复换近中景但没有增加观察重点。")
    with pytest.raises(ValueError, match="对应镜头不能"):
        validate_av_review(raw, plan, require_pacing=True)
    raw["ready"] = raw["shots"][0]["passed"] = False
    assert not validate_av_review(raw, plan, require_pacing=True).ready


def test_a_brief_glance_is_not_rejected_by_a_fixed_minimum_shot_duration(long_shot_case):
    plan = Plan.model_validate(long_shot_case[0])
    segment = plan.segments[0]
    # A silent insert changes observation, while the underlying beat and speech
    # remain in the same longer supporting shot. No uniform minimum is imposed.
    first = segment.shots[0]
    first.end = 9.5
    segment.shots.append(Shot.model_validate({**first.model_dump(), "id": "glance", "start": 9.5, "end": 10,
        "beat_id": "ending", "beat_ids": ["ending"], "description": "同伴瞥向盘中的蛋糕。"}))
    for span in segment.speech_spans:
        if span.end > 9.5:
            span.shot_ids.append("glance")
            span.anchors[-1].shot_ids.append("glance")
    raw = pacing_review(plan, required=.4)
    assert validate_av_review(raw, plan, require_pacing=True).ready


@pytest.mark.parametrize("required", [float("nan"), float("inf"), -1])
def test_nonfinite_or_negative_action_estimates_are_invalid(long_shot_case, required):
    plan = Plan.model_validate(long_shot_case[0])
    with pytest.raises(ValueError):
        validate_av_review(pacing_review(plan, required), plan, require_pacing=True)
