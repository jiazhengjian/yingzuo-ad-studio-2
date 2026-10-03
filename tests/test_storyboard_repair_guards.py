"""Timing retries cannot rewrite approved content or hide unknown evidence."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.storyboard_repair import can_repair_timing, timing_repair_preserves_contract


@pytest.fixture
def repair_case():
    text = "午后这块蛋糕，放在桌上也好看。"
    raw = {"title": "下午茶", "audio_strategy": "native", "language": "zh-CN", "aspect_ratio": "9:16",
           "segments": [{"id": "segment_1", "duration_seconds": 10,
                         "reference_asset_ids": ["product_1", "product_2"],
                         "shots": [
                             {"id": "wide", "beat_id": "b1", "start": 0, "end": 3,
                              "narration": "", "description": "桌上的蛋糕", "evidence_ids": ["f1"]},
                             {"id": "detail", "beat_id": "b1", "start": 3, "end": 6,
                              "narration": "", "description": "蛋糕切面", "evidence_ids": ["f1"]},
                             {"id": "hold", "beat_id": "b2", "start": 6, "end": 10,
                              "narration": "", "description": "完整商品", "evidence_ids": ["f1"]}],
                         "speech_spans": [{"id": "speech_1", "beat_id": "b1", "text": text,
                                           "start": .2, "end": 5.8, "shot_ids": ["wide", "detail"],
                                           "anchors": [
                                               {"text": "午后这块蛋糕，", "start": .2, "end": 2.6,
                                                "shot_ids": ["wide"], "visual_evidence": "桌上的商品"},
                                               {"text": "放在桌上也好看。", "start": 2.6, "end": 5.8,
                                                "shot_ids": ["detail"], "visual_evidence": "外观"}]}]}]}
    schedule = [{"duration_seconds": 10, "groups": [
        {"beat_id": "b1", "start": 0, "end": 6, "text": text, "speech_start": .2, "speech_end": 5.8},
        {"beat_id": "b2", "start": 6, "end": 10, "text": ""}]}]
    script = {"beats": [{"id": "b1", "narration": text, "evidence_ids": ["f1"]},
                        {"id": "b2", "narration": "", "evidence_ids": ["f1"]}]}
    return raw, schedule, script, {"f1"}, {"product_1", "product_2"}


def test_preflight_allows_only_unresolved_internal_timing_and_references(repair_case):
    raw, schedule, script, facts, assets = repair_case
    before = deepcopy(raw)
    # Anchor begins before its sole referenced shot: the actual retry trigger.
    assert can_repair_timing(*repair_case)
    raw["segments"][0]["shots"][0]["end"] = 2.8  # Internal gap also remains repairable.
    assert can_repair_timing(raw, schedule, SimpleNamespace(model_dump=lambda: script), facts, assets)
    assert before["segments"][0]["shots"][0]["end"] == 3


@pytest.mark.parametrize("invalid", ["unknown_fact", "unknown_asset", "unknown_beat", "wrong_order",
    "missing_beat", "missing_span", "duplicate_span", "changed_words", "split_words", "shifted_speech",
    "shifted_group", "legacy_shot_words", "duration", "native", "missing_evidence", "anchor_words",
    "repeated_segment", "nonfinite_time"])
def test_preflight_does_not_retry_content_or_reserved_window_changes(repair_case, invalid):
    raw, schedule, script, facts, assets = repair_case
    segment = raw["segments"][0]
    shots, spans = segment["shots"], segment["speech_spans"]
    if invalid == "unknown_fact": shots[0]["evidence_ids"] = ["invented"]
    elif invalid == "unknown_asset": segment["reference_asset_ids"] = ["invented"]
    elif invalid == "unknown_beat": shots[0]["beat_id"] = "invented"
    elif invalid == "wrong_order": shots.reverse()
    elif invalid == "missing_beat": shots[-1]["beat_id"] = "b1"
    elif invalid == "missing_span": spans.clear()
    elif invalid == "duplicate_span": spans.append(deepcopy(spans[0]))
    elif invalid == "changed_words": spans[0]["text"] = "另一句话。"
    elif invalid == "split_words":
        split = deepcopy(spans[0])
        spans[0]["text"], split["text"] = "午后这块蛋糕，", "放在桌上也好看。"
        split["id"] = "split"
        spans.append(split)
    elif invalid == "shifted_speech": spans[0]["start"] += .1
    elif invalid == "shifted_group":
        shots[1]["end"] += .2
        shots[2]["start"] += .2
    elif invalid == "legacy_shot_words": shots[0]["narration"] = "另说一句。"
    elif invalid == "duration": segment["duration_seconds"] = 9
    elif invalid == "native": raw["audio_strategy"] = "voiceover"
    elif invalid == "missing_evidence": shots[-1]["evidence_ids"] = []
    elif invalid == "anchor_words": spans[0]["anchors"][0]["text"] += "新增"
    elif invalid == "repeated_segment": raw["segments"].append(deepcopy(segment))
    elif invalid == "nonfinite_time": spans[0]["start"] = float("nan")
    assert not can_repair_timing(raw, schedule, script, facts, assets)


def test_repair_can_move_a_cut_while_preserving_approved_expression(repair_case):
    original = repair_case[0]
    repaired = deepcopy(original)
    shots = repaired["segments"][0]["shots"]
    shots[0]["end"] = shots[1]["start"] = 2.6
    shots[1]["description"] = "商品已完整出现在台面上"
    repaired["segments"][0]["speech_spans"][0]["anchors"][1]["visual_evidence"] = "商品与台面搭配"
    assert timing_repair_preserves_contract(original, repaired)
    assert can_repair_timing(repaired, *repair_case[1:])


@pytest.mark.parametrize("invalid", ["title", "audio_strategy", "language", "aspect_ratio", "segment_id",
    "duration", "assets", "asset_order", "speech_id", "beat_id", "text", "start", "end", "speech_added",
    "group_boundary", "beat_removed", "segment_added"])
def test_repair_cannot_mutate_frozen_contract(repair_case, invalid):
    original = repair_case[0]
    repaired = deepcopy(original)
    segment = repaired["segments"][0]
    span = segment["speech_spans"][0]
    if invalid in ("title", "audio_strategy", "language", "aspect_ratio"): repaired[invalid] = "changed"
    elif invalid == "segment_id": segment["id"] = "changed"
    elif invalid == "duration": segment["duration_seconds"] = 9
    elif invalid == "assets": segment["reference_asset_ids"] = ["product_1"]
    elif invalid == "asset_order": segment["reference_asset_ids"].reverse()
    elif invalid == "speech_id": span["id"] = "changed"
    elif invalid in ("beat_id", "text"): span[invalid] = "changed"
    elif invalid in ("start", "end"): span[invalid] += .01
    elif invalid == "speech_added": segment["speech_spans"].append(deepcopy(span))
    elif invalid == "group_boundary":
        segment["shots"][1]["end"] = 5.9
        segment["shots"][2]["start"] = 5.9
    elif invalid == "beat_removed": segment["shots"][-1]["beat_id"] = "b1"
    elif invalid == "segment_added": repaired["segments"].append(deepcopy(segment))
    assert not timing_repair_preserves_contract(original, repaired)


@pytest.mark.parametrize("malformed", [None, [], {}, {"segments": None}, {"segments": [None]}])
def test_guards_fail_closed_on_malformed_drafts(repair_case, malformed):
    assert not can_repair_timing(malformed, *repair_case[1:])
    assert not timing_repair_preserves_contract(repair_case[0], malformed)
