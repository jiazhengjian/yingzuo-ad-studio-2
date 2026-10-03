"""Offline continuity grouping and retained content/timing contracts."""
from copy import deepcopy

import pytest

from app.ad_script import AdScript, script_context, validate_script
from app.generation_policy import generation_durations, generation_groups, validate_generation_durations
from app.planning import expression_schedule
from app.schemas import Plan
from test_ad_script import script, strategy
from test_speech_spans import plan_data


def beats(count):
    return {"beats": [{"id": f"b{i}", "narration": "这是一句商品说明。", "boundary_before": "complete", "continuity_group": ""} for i in range(count)]}


@pytest.mark.parametrize("duration", [10, 11, 19, 20, 29, 30])
def test_short_ad_has_many_beats_but_one_generation_request(duration):
    result = generation_groups(beats(2), duration)
    assert result["durations"] == [duration]
    assert result["beat_groups"] == [[0, 1]]
    assert result["voice_continuity"] == "single_request_unverified"


@pytest.mark.parametrize("duration", [31, 32, 39, 40, 59, 60])
def test_long_ad_keeps_whole_beats_and_never_makes_one_second_tail(duration):
    result = generation_groups(beats(6), duration)
    assert sum(result["durations"]) == duration
    assert all(2 <= value <= 30 for value in result["durations"])
    assert [i for group in result["beat_groups"] for i in group] == list(range(6))
    assert len(result["durations"]) == 2
    assert result["voice_continuity"] == "cross_request_unverified"


def test_scene_boundary_beats_balanced_but_arbitrary_mid_action_split():
    data = beats(6)
    data["beats"][2]["boundary_before"] = "scene_change"
    result = generation_groups(data, 40)
    assert result["beat_groups"] == [[0, 1], [2, 3, 4, 5]]
    assert result["split_reasons"] == ["scene_change"]


@pytest.mark.parametrize("kind", ["continuous", "same_group"])
def test_continuous_content_cannot_be_split_to_fit_provider_limit(kind):
    data = beats(4)
    for beat in data["beats"]:
        if kind == "continuous": beat["boundary_before"] = "continuous"
        else: beat["continuity_group"] = "one_unfinished_action"
    with pytest.raises(ValueError, match="连续动作"):
        generation_groups(data, 40)
    assert generation_groups(data, 20)["beat_groups"] == [[0, 1, 2, 3]]


def test_selector_respects_action_lock_even_when_it_would_balance_better():
    data = beats(6)
    data["beats"][3]["boundary_before"] = "continuous"
    data["beats"][2]["continuity_group"] = data["beats"][3]["continuity_group"] = "pour"
    data["beats"][4]["boundary_before"] = "scene_change"
    assert generation_groups(data, 40)["beat_groups"] == [[0, 1, 2, 3], [4, 5]]


def test_legacy_time_partition_and_serialization_remain_unchanged(plan_data, script):
    assert generation_durations(31, "legacy_10s") == [10, 10, 10, 1]
    assert "generation_strategy" not in Plan.model_validate(plan_data).model_dump()
    assert AdScript.model_validate(script).model_dump() == script
    validate_generation_durations([10, 10, 10, 1], "legacy_10s")
    with pytest.raises(ValueError): validate_generation_durations([30, 1], "continuity_v1")
    with pytest.raises(ValueError): validate_generation_durations([10, 10], "continuity_v1")


def test_fixed_semantic_groups_are_not_rebalanced_by_director(script):
    data = AdScript.model_validate(script)
    schedule = expression_schedule(data, [15, 25], beat_groups=[[0], [1]])
    assert [s["duration_seconds"] for s in schedule] == [15, 25]
    assert [[g["beat_id"] for g in s["groups"]] for s in schedule] == [["opening"], ["product"]]
    with pytest.raises(ValueError, match="完整覆盖"):
        expression_schedule(data, [20], beat_groups=[[1, 0]])


def test_new_script_uses_generation_groups_without_removing_fact_guards(strategy, script):
    context = script_context(strategy, 20, script=script, generation_strategy="continuity_v1")
    assert context["segment_durations"] == [20]
    assert context["draft_generation_groups"]["beat_groups"] == [[0, 1]]
    assert context["schedule_feasible"] is True
    assert validate_script(script, strategy, 20, generation_strategy="continuity_v1")
    script["beats"][1]["evidence_ids"] = ["invented"]
    with pytest.raises(ValueError, match="已有商品事实"):
        validate_script(script, strategy, 20, generation_strategy="continuity_v1")


def test_twenty_second_schema_accepts_time_after_ten_seconds_without_dropping_speech_guard(plan_data):
    value = deepcopy(plan_data)
    value["generation_strategy"] = "continuity_v1"
    segment = value["segments"][0]
    segment["duration_seconds"] = 20
    segment["shots"][-1]["end"] = 20
    valid = Plan.model_validate(value)
    assert valid.generation_strategy == "continuity_v1"
    assert valid.segments[0].shots[-1].end == 20
    segment["speech_spans"][0]["end"] = 12
    with pytest.raises(ValueError, match="对应镜头覆盖"):
        Plan.model_validate(value)


def test_many_beats_can_be_in_one_long_request_without_inventing_extra_requests(script):
    data = deepcopy(script)
    data["beats"] = [{**deepcopy(script["beats"][0]), "id": f"b{i}", "narration": "", "evidence_ids": []} for i in range(8)]
    model = AdScript.model_validate(data)
    result = generation_groups(model, 20)
    schedule = expression_schedule(model, result["durations"], beat_groups=result["beat_groups"])
    assert len(schedule) == 1 and len(schedule[0]["groups"]) == 8


def test_plan_cannot_override_chosen_beat_boundaries(plan_data):
    # Build a structurally valid 40-second two-beat plan, then move only the
    # request boundary. Both keep copy intact, but only the selected grouping
    # capacities are admissible for a fresh continuity plan.
    value = deepcopy(plan_data)
    value["generation_strategy"] = "continuity_v1"
    original = value["segments"][0]
    first = deepcopy(original)
    first["shots"] = first["shots"][:2]
    second = deepcopy(original)
    second.update(id="seg2", narration="", speech_spans=[], shots=[second["shots"][2]])
    group = generation_groups(AdScript.model_validate(value["ad_script"]), 40)
    first["duration_seconds"], second["duration_seconds"] = group["durations"]
    first["shots"][-1]["end"] = first["duration_seconds"]
    second["shots"][0].update(start=0, end=second["duration_seconds"])
    value["segments"] = [first, second]
    assert Plan.model_validate(value)
    first["duration_seconds"] -= 1
    second["duration_seconds"] += 1
    first["shots"][-1]["end"] -= 1
    second["shots"][-1]["end"] += 1
    with pytest.raises(ValueError, match="连续性分组"):
        Plan.model_validate(value)
