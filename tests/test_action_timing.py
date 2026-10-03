"""Offline joint action/speech capacity and backward-compatible stage contracts."""
from copy import deepcopy
import math

import pytest
from pydantic import ValidationError

from app.ad_script import (AdScript, BeatTiming, ScriptReview, script_context,
    script_stage_schema, validate_script, validate_script_execution, validate_script_timing)
from app.beat_timing import beat_minimum_ticks, beat_timing_budget
from app.generation_policy import generation_groups
from app.planning import expression_schedule, storyboard_schedule
from test_ad_script import review_for, script, strategy


def timing(visual_seconds=4, *, speech_overlap=True, **changes):
    """Small explicit execution estimate for fixtures; no generated-media claims."""
    return {"visual_seconds": visual_seconds, "speech_overlap": speech_overlap,
        "action_plan": "将叉子落在切面取下一小块，留出辨认停留；" + (
            "可与画外音并行" if speech_overlap else "动作完成后保持结果可见，再说整句台词"),
        "start_state": "蛋糕切块完整地留在盘内，叉子在旁边",
        "end_state": "蛋糕切块留有一个缺口，小块留在叉子上",
        "time_transition": "continuous", **changes}


def timed_script(script, seconds=(4, 4), *, speech_overlap=True):
    result = deepcopy(script)
    for beat, seconds in zip(result["beats"], seconds):
        beat["timing"] = timing(seconds, speech_overlap=speech_overlap)
    return result


def checked_review(script, *, action_capacity=True, state_continuity=True):
    result = review_for(script)
    result["execution_check"] = {"action_capacity": action_capacity,
        "state_continuity": state_continuity, "note": "动作与说话容量已检查，状态按说明承接"}
    result["ready"] = action_capacity and state_continuity
    return result


def test_new_wire_requires_nonnull_timing_and_execution_without_changing_legacy(script):
    historical_script = AdScript.model_json_schema()
    historical_review = ScriptReview.model_json_schema()
    for schema in (script_stage_schema(), script_stage_schema(review=True)):
        beat = schema["$defs"]["AdBeat"]
        assert "timing" in beat["required"]
        assert beat["properties"]["timing"] == {"$ref": "#/$defs/BeatTiming"}
        assert set(schema["$defs"]["BeatTiming"]["required"]) == set(timing())
    reviewed = script_stage_schema(review=True)
    assert "execution_check" in reviewed["required"]
    assert reviewed["properties"]["execution_check"] == {"$ref": "#/$defs/ScriptExecutionCheck"}
    assert AdScript.model_json_schema() == historical_script
    assert ScriptReview.model_json_schema() == historical_review
    assert AdScript.model_validate(script).model_dump() == script
    assert ScriptReview.model_validate(review_for(script)).model_dump() == review_for(script)
    assert "timing" not in historical_script["$defs"]["AdBeat"]["required"]
    assert "execution_check" not in historical_review["required"]


@pytest.mark.parametrize("changes", [
    {"visual_seconds": 0}, {"visual_seconds": -1}, {"visual_seconds": math.inf},
    {"visual_seconds": math.nan}, {"visual_seconds": True}, {"visual_seconds": "4"},
    {"speech_overlap": "true"}, {"speech_overlap": 1}, {"action_plan": " "},
    {"start_state": ""}, {"end_state": ""}, {"time_transition": "jump"},
])
def test_timing_requires_explicit_finite_estimates_and_states(changes):
    with pytest.raises(ValidationError):
        BeatTiming.model_validate(timing(**changes))


@pytest.mark.parametrize("seconds,overlap,expected", [
    (7, True, 7), (2, True, 5.2), (7, False, 12.2), (2.01, False, 7.3),
])
def test_joint_minimum_counts_parallel_or_sequential_work(seconds, overlap, expected):
    beat = {"id": "step", "narration": "字" * 15, "timing": timing(seconds, speech_overlap=overlap)}
    assert beat_timing_budget(beat)["minimum_seconds"] == expected
    assert beat_timing_budget(beat)["speech_seconds"] == 5.2
    assert beat_minimum_ticks({"narration": ""}) == 10
    assert beat_minimum_ticks({"narration": "", "timing": timing(6)}) == 60


def test_silent_actions_consume_real_estimate_in_all_budget_paths(strategy, script):
    value = timed_script(script, (7, 7))
    value["beats"][0]["narration"] = ""
    value["beats"][1]["narration"] = "看这层次。"
    assert validate_script(value, strategy, 10, final=False, generation_strategy="continuity_v1")
    context = script_context(strategy, 10, script=value, generation_strategy="continuity_v1")
    assert not context["schedule_feasible"]
    assert [item["minimum_seconds"] for item in context["draft_beat_timing"]] == [7, 7]
    assert context["schedule_issue"]
    model = AdScript.model_validate(value)
    with pytest.raises(ValueError): generation_groups(model, 10)
    with pytest.raises(ValueError): storyboard_schedule(model, [10])
    with pytest.raises(ValueError): validate_script(value, strategy, 10, generation_strategy="continuity_v1")
    # Timing supplied in a legacy contract still cannot bypass action capacity.
    with pytest.raises(ValueError): validate_script(value, strategy, 10, require_current_version=False)


def test_draft_capacity_failure_can_be_repaired_without_changing_facts_or_core(strategy, script):
    value = timed_script(script, (12, 12))
    checked = validate_script_timing(validate_script(value, strategy, 20, final=False), required=True)
    assert checked.beats[0].timing.visual_seconds == 12
    review = validate_script_execution(checked_review(value, action_capacity=False), required=True)
    assert not review.ready
    repaired = timed_script(script, (4, 4))
    accepted = validate_script_execution(checked_review(repaired), required=True)
    assert validate_script(accepted.script, strategy, 20, generation_strategy="continuity_v1")
    assert accepted.script.evidence_ids == value["evidence_ids"]
    assert accepted.script.core_message == value["core_message"]


@pytest.mark.parametrize("missing", ["timing", "execution_check"])
def test_new_flow_helper_cannot_be_bypassed_by_legacy_compatible_models(script, missing):
    value = checked_review(timed_script(script))
    if missing == "timing": value["script"]["beats"][1].pop("timing")
    else: value.pop("execution_check")
    assert validate_script_execution(value)
    with pytest.raises(ValueError): validate_script_execution(value, required=True)


@pytest.mark.parametrize("field", ["action_capacity", "state_continuity"])
def test_ready_requires_both_execution_checks_and_six_content_checks(script, field):
    value = checked_review(timed_script(script), **{field: False})
    assert not validate_script_execution(value, required=True).ready
    value["ready"] = True
    with pytest.raises(ValidationError): ScriptReview.model_validate(value)
    value["execution_check"][field] = "true"
    with pytest.raises(ValidationError): ScriptReview.model_validate(value)


def test_falsely_positive_execution_verdict_does_not_override_numeric_capacity(strategy, script):
    value = checked_review(timed_script(script, (15, 15)))
    review = validate_script_execution(value, required=True)
    assert review.ready
    with pytest.raises(ValueError):
        validate_script(review.script, strategy, 20, generation_strategy="continuity_v1")


@pytest.mark.parametrize("overlap", [False, True])
def test_director_windows_honor_planned_action_and_speech_relationship(script, overlap):
    value = timed_script(script, (5, 3), speech_overlap=overlap)
    model = AdScript.model_validate(value)
    groups = expression_schedule(model, [20], beat_groups=[[0, 1]])[0]["groups"]
    for beat, group in zip(model.beats, groups):
        assert group["end"] - group["start"] >= beat_minimum_ticks(beat) / 10
        assert group["visual_end"] - group["visual_start"] == pytest.approx(beat.timing.visual_seconds)
        assert group["speech_end"] <= group["end"]
        if overlap:
            assert group["speech_start"] < group["visual_end"]
        else:
            assert group["speech_start"] >= group["visual_end"]
        assert group["timing"] == beat.timing.model_dump()


def test_actions_decide_long_ad_group_capacity_without_breaking_continuity(script):
    value = timed_script(script, (25, 15))
    model = AdScript.model_validate(value)
    groups = generation_groups(model, 40)
    assert groups["durations"] == [25, 15]
    assert groups["beat_groups"] == [[0], [1]]
    assert expression_schedule(model, groups["durations"], beat_groups=groups["beat_groups"])
    value["beats"][1]["boundary_before"] = "continuous"
    with pytest.raises(ValueError, match="连续动作"):
        generation_groups(AdScript.model_validate(value), 40)


def test_explicit_ellipsis_can_preserve_an_explainable_later_state(strategy, script):
    value = timed_script(script, (4, 3))
    value["beats"][1]["timing"] = timing(3, time_transition="ellipsis",
        action_plan="明确省略一段分食过程，叠化到同一盘中剩余切块并留出辨认时间",
        start_state="经过已说明的分食，盘中剩余半块蛋糕，原缺口仍可辨认",
        end_state="同一剩余半块蛋糕保持原有状态，人物放下叉子")
    accepted = validate_script_execution(checked_review(value), required=True)
    final = validate_script(accepted.script, strategy, 10, generation_strategy="continuity_v1")
    schedule = expression_schedule(final, [10], beat_groups=[[0, 1]])
    assert schedule[0]["groups"][1]["timing"]["time_transition"] == "ellipsis"


def test_spare_time_preserves_action_proportions_instead_of_equalizing_beats(script):
    value = timed_script(script, (2, 8))
    for beat in value["beats"]: beat["narration"] = ""
    shots = storyboard_schedule(AdScript.model_validate(value), [20])[0]["shots"]
    assert [(shot["start"], shot["end"]) for shot in shots] == [(0, 4), (4, 20)]
