"""Final AV review sees exact existing action/speech reservations, compactly."""
from copy import deepcopy

import pytest

from app.ad_script import AdScript
from app.av_review import AVReview, review_execution_windows
from app.planning import expression_schedule
from test_ad_script import script
from test_action_timing import timed_script
from test_draft_enum_review import enum_gateway
import test_draft_enum_review as draft_fixture
from test_providers import settings, source, strategy_stage, install_transport


WINDOW_FIELDS = {"beat_id", "beat_start", "beat_end", "visual_start", "visual_end", "speech_start", "speech_end",
                 "speech_overlap", "visual_seconds"}


def scheduled_script(script, duration=20, *, overlap=False, silent=False):
    value = timed_script(script, (2.05, 1.7), speech_overlap=overlap)
    for beat in value["beats"]:
        beat["narration"] = "" if silent else "看切面。"
    return expression_schedule(AdScript.model_validate(value), [duration], beat_groups=[[0, 1]])


@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("silent", [False, True])
def test_projection_keeps_serial_parallel_and_silent_windows_exactly(script, overlap, silent):
    schedule = scheduled_script(script, overlap=overlap, silent=silent)
    segments = [{"id": "final_segment", "duration_seconds": 20}]
    original_schedule, original_segments = deepcopy(schedule), deepcopy(segments)
    projected = review_execution_windows(schedule, segments)
    assert set(projected[0]) == {"segment_id", "beats"}
    assert projected[0]["segment_id"] == "final_segment"
    for window, group in zip(projected[0]["beats"], schedule[0]["groups"]):
        assert set(window) == WINDOW_FIELDS
        assert window["beat_id"] == group["beat_id"]
        assert (window["beat_start"], window["beat_end"]) == (group["start"], group["end"])
        for key in ("visual_start", "visual_end", "speech_start", "speech_end"):
            assert window[key] == group.get(key)
        assert window["visual_seconds"] == group["timing"]["visual_seconds"]
        assert window["speech_overlap"] is overlap
        if silent:
            assert window["speech_start"] is window["speech_end"] is None
        elif overlap:
            assert window["speech_start"] < window["visual_end"]
        else:
            assert window["speech_start"] >= window["visual_end"]
    projected[0]["beats"][0]["visual_end"] = 99
    assert schedule == original_schedule and segments == original_segments


@pytest.mark.parametrize("duration", [10, 20, 30])
def test_larger_content_allocation_does_not_scale_reserved_action_or_original_estimate(script, duration):
    schedule = scheduled_script(script, duration)
    windows = review_execution_windows(schedule, [{"id": "one", "duration_seconds": duration}])[0]["beats"]
    assert schedule[0]["groups"][-1]["end"] == duration
    assert windows[0]["visual_seconds"] == 2.05
    assert windows[0]["visual_end"] - windows[0]["visual_start"] == pytest.approx(2.1)
    assert windows[1]["visual_seconds"] == 1.7
    assert windows[1]["visual_end"] - windows[1]["visual_start"] == pytest.approx(1.7)
    assert windows[1]["visual_start"] == schedule[0]["groups"][1]["visual_start"]


def test_segment_ids_and_segment_local_times_follow_actual_compilation_order(script):
    model = AdScript.model_validate(timed_script(script, (3, 4)))
    schedule = expression_schedule(model, [10, 15], beat_groups=[[0], [1]])
    segments = [{"id": "take_b", "duration_seconds": 10}, {"id": "take_a", "duration_seconds": 15}]
    windows = review_execution_windows(schedule, segments)
    assert [item["segment_id"] for item in windows] == ["take_b", "take_a"]
    assert [item["beats"][0]["visual_start"] for item in windows] == [0, 0]
    assert [item["beats"][0]["visual_end"] for item in windows] == [3, 4]


def test_no_timing_continuation_does_not_gain_an_invented_window():
    schedule = [{"segment_index": 0, "duration_seconds": 10,
        "groups": [{"beat_id": "continued", "start": 0, "end": 10,
                    "text": "", "visual_role": "旧分组的同拍静音延续"}]}]
    window = review_execution_windows(schedule, [{"id": "one", "duration_seconds": 10}])[0]["beats"][0]
    assert window == {"beat_id": "continued", "beat_start": 0, "beat_end": 10,
        **{field: None for field in WINDOW_FIELDS - {"beat_id", "beat_start", "beat_end"}}}


@pytest.mark.parametrize("mismatch", ["missing", "extra", "index", "duration"])
def test_projection_rejects_segment_mismatch_instead_of_silently_truncating(script, mismatch):
    schedule = scheduled_script(script)
    segments = [{"id": "one", "duration_seconds": 20}]
    if mismatch == "missing": segments = []
    elif mismatch == "extra": segments.append({"id": "two", "duration_seconds": 20})
    elif mismatch == "index": schedule[0]["segment_index"] = 1
    else: segments[0]["duration_seconds"] = 19
    with pytest.raises(ValueError, match="审核执行时窗"):
        review_execution_windows(schedule, segments)


@pytest.mark.parametrize("overlap", [False, True])
def test_new_gateway_forwards_existing_windows_without_altering_review_schema_or_plan(enum_gateway, overlap, monkeypatch):
    enum_gateway.enum_field = None
    enum_gateway.initial_mutation = lambda raw: raw["beats"][0]["timing"].update(speech_overlap=overlap)
    original_compiler = draft_fixture.compiled_from_reservations
    def continuous_observation(context):
        result = original_compiler(context)
        for segment in result["segments"]:
            # This projection test has one beat. Keep its action and subsequent
            # speech in one shot instead of inventing a second action after
            # the non-parallel action reservation has already ended.
            first = segment["shots"][0]
            first["end"] = segment["duration_seconds"]
            first["continuity"]["end_state"] = segment["shots"][-1]["continuity"]["end_state"]
            segment["shots"] = [first]
            for span in segment["speech_spans"]:
                span["shot_ids"] = [first["id"]]
                for anchor in span["anchors"]:
                    anchor["shot_ids"] = [first["id"]]
        return result
    monkeypatch.setattr(draft_fixture, "compiled_from_reservations", continuous_observation)
    schema_before = AVReview.model_json_schema()
    result = enum_gateway.run()
    storyboard, alignment = enum_gateway.contexts[2:]
    group = storyboard["expression_schedule"][0]["groups"][0]
    window = alignment["execution_windows"][0]["beats"][0]
    assert enum_gateway.calls == ["ad_script", "script_review", "storyboard", "alignment_review"]
    assert alignment["execution_windows"][0]["segment_id"] == result["segments"][0]["id"]
    assert window == {**{key: group.get(key) for key in WINDOW_FIELDS - {"speech_overlap", "visual_seconds", "beat_start", "beat_end"}},
        "beat_start": group["start"], "beat_end": group["end"],
        "speech_overlap": overlap, "visual_seconds": group["timing"]["visual_seconds"]}
    assert "不能拿整镜时长或语音时段" in alignment["execution_window_rules"]
    assert "execution_windows" not in result
    assert result["ad_script"] == storyboard["approved_ad_script"]
    assert AVReview.model_json_schema() == schema_before


def test_one_actual_shot_preserves_separate_execution_windows_for_multiple_beats(enum_gateway, monkeypatch):
    enum_gateway.enum_field = None
    def two_beats(raw):
        first = raw["beats"][0]
        first["narration"] = "看这外观。"
        first["timing"]["speech_overlap"] = False
        second = deepcopy(first)
        second.update(id="second", narration="桌角多点喜欢。")
        second["timing"]["speech_overlap"] = True
        raw["beats"].append(second)
    enum_gateway.initial_mutation = two_beats
    original_compiler = draft_fixture.compiled_from_reservations
    def one_shot(context):
        result = original_compiler(context)
        for segment, scheduled in zip(result["segments"], context["expression_schedule"]):
            shot = deepcopy(segment["shots"][0])
            shot["end"] = segment["duration_seconds"]
            shot["beat_ids"] = [group["beat_id"] for group in scheduled["groups"]]
            shot["continuity"]["end_state"] = segment["shots"][-1]["continuity"]["end_state"]
            segment["shots"] = [shot]
            for span in segment["speech_spans"]:
                span["shot_ids"] = [shot["id"]]
                for anchor in span["anchors"]: anchor["shot_ids"] = [shot["id"]]
        return result
    monkeypatch.setattr(draft_fixture, "compiled_from_reservations", one_shot)
    result = enum_gateway.run()
    alignment = enum_gateway.contexts[-1]
    windows = alignment["execution_windows"][0]["beats"]
    assert len(result["segments"][0]["shots"]) == 1 and len(windows) == 2
    assert [window["beat_id"] for window in windows] == result["segments"][0]["shots"][0]["beat_ids"]
    assert [window["speech_overlap"] for window in windows] == [False, True]
    assert windows[0]["visual_end"] <= windows[0]["speech_start"] < windows[1]["visual_start"]


def test_legacy_alignment_input_keeps_its_existing_shape(enum_gateway):
    enum_gateway.new_flow = False
    enum_gateway.enum_field = None
    assert enum_gateway.run()["alignment_review"]["ready"]
    alignment = enum_gateway.contexts[-1]
    assert alignment["stage"] == "alignment_review"
    assert "execution_windows" not in alignment and "execution_window_rules" not in alignment
