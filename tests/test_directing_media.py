"""Offline media prompt and all-shot review contracts, not generated-film QA.

No settings, provider, production state or environment file is loaded here.
"""
from copy import deepcopy
import json
import socket

import pytest
from pydantic import ValidationError

from app.av_review import AVReview, validate_av_review
from app.design import scene_design_request
from app.directing import direction_context
from app.schemas import Plan
from app.video_prompt import compile_grouped_video, expected_grouped_speech
from test_directing_contract import directed_plan_data
from test_speech_spans import plan_data


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("These media-contract tests must remain offline")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def compiled_segment(plan):
    value = Plan.model_validate(plan)
    segment = value.segments[0].model_dump()
    segment["generation_strategy"] = "continuity_v1"
    segment["director_context"] = direction_context(value)
    return segment


def complete_review(plan):
    return {"ready": True, "ad_expression_passed": True,
        "summary": "桌面场景和多层切面共同支持下午茶选择，最后镜头在同一摆位收束。",
        "spans": [{"segment_id": segment["id"], "span_id": span["id"], "passed": True,
                   "note": "台词所指的蛋糕选择在桌面和切面两个连续画面中可理解。"}
                  for segment in plan["segments"] for span in segment.get("speech_spans") or []],
        "shots": [{"segment_id": segment["id"], "shot_id": shot["id"], "passed": True,
                   "note": "办公桌上的同一块蛋糕保持摆位，镜头承担环境、切面或收束作用。"}
                  for segment in plan["segments"] for shot in segment["shots"]]}


def test_video_compiles_owned_direction_facts_and_each_shot_state(directed_plan_data):
    segment = compiled_segment(directed_plan_data)
    before = deepcopy(segment)
    prompt = compile_grouped_video(segment)
    for key in ("scene", "lighting", "color_palette", "camera_style", "sound"):
        assert directed_plan_data["visual_direction"][key] in prompt
    assert "多层切面" in prompt
    assert "source_kind" not in prompt and "source_id" not in prompt and '"layers"' not in prompt
    for shot in segment["shots"]:
        assert f"{shot['id']} | {shot['start']:g}–{shot['end']:g}秒" in prompt
        for key in ("start_state", "allowed_changes", "end_state", "camera_move"):
            assert shot["continuity"][key] in prompt
        assert shot["continuity"]["entry_reason"] not in prompt
    assert "入镜方式：开场建立画面" in prompt
    assert "入镜方式：延续上一镜状态与动作" in prompt
    assert "只执行本镜允许的状态变化" in prompt
    assert "不在画面上加文字、字幕、价格、尾字卡或水印" in prompt
    assert "CTA" not in prompt
    assert "<node-asset>" not in prompt and "@product_reference" not in prompt
    assert segment == before


def test_video_uses_only_directing_fields_and_final_speech_not_old_freeform_prompt(directed_plan_data):
    segment = compiled_segment(directed_plan_data)
    segment["prompt"] = "OBSOLETE_台词和动作不能覆盖最终分镜"
    segment["director_context"]["private_debug"] = "SECRET_DEBUG_NOT_FOR_MODEL"
    segment["director_context"]["product_anchors"][0]["extra"] = "UNOWNED_FEATURE"
    segment["speech_spans"][0]["anchors"][0]["visual_evidence"] = "SELF_ASSERTION_NOT_INSTRUCTIONS"
    prompt = compile_grouped_video(segment)
    assert not any(value in prompt for value in ("OBSOLETE_", "SECRET_DEBUG", "UNOWNED_FEATURE", "SELF_ASSERTION"))
    assert "0–7秒 | 关联镜头 desk, detail" in prompt
    assert segment["speech_spans"][0]["text"] in prompt
    assert prompt.count(segment["speech_spans"][0]["text"]) == 1


def test_confirmed_voice_direction_is_not_overridden_by_legacy_female_default(directed_plan_data):
    directed_plan_data["visual_direction"]["sound"] = "自然成年男性普通话，低声介绍，背景只保留室内环境声"
    prompt = compile_grouped_video(compiled_segment(directed_plan_data))
    assert directed_plan_data["visual_direction"]["sound"] in prompt
    assert "成年女性" not in prompt
    assert "不依赖未提供的声纹或音频素材" in prompt


def test_legacy_video_prompt_keeps_defaults_and_does_not_invent_directing_data(plan_data):
    segment = Plan.model_validate(plan_data).segments[0].model_dump()
    prompt = compile_grouped_video(segment)
    assert "全片视觉与声音方向" not in prompt and "本镜允许变化" not in prompt
    assert "成年女性普通话" in prompt
    assert "同一表达可以连续跨越关联镜头" in prompt


def test_new_direction_does_not_move_or_duplicate_planned_speech(directed_plan_data):
    segment = compiled_segment(directed_plan_data)
    before = deepcopy(segment)
    timeline = expected_grouped_speech(segment, 240)
    assert [(span["start_frame"], span["end_frame"], span["narration"]) for span in timeline] == [
        (240, 408, segment["speech_spans"][0]["text"])]
    assert [anchor["start_frame"] for anchor in timeline[0]["anchors"]] == [240, 312]
    assert segment == before


def scene_context(request):
    return json.loads(request["prompt"].split("已确认的场景与视觉方案：\n", 1)[1])


def test_scene_compiles_shared_scene_light_palette_camera_without_sound_or_product(directed_plan_data):
    before = deepcopy(directed_plan_data)
    request = scene_design_request({"brief": "UNUSED_STALE_SCENE"}, directed_plan_data)
    context = scene_context(request)
    expected = {key: directed_plan_data["visual_direction"][key]
                for key in ("scene", "lighting", "color_palette")}
    assert context["visual_direction"] == expected
    assert request["scenario"] == "办公室下午茶"
    assert "sound" not in context["visual_direction"] and "product_anchor_ids" not in context["visual_direction"]
    assert directed_plan_data["visual_direction"]["sound"] not in request["prompt"]
    assert directed_plan_data["visual_direction"]["camera_style"] not in request["prompt"]
    assert "不出现商品、包装、人像" in request["prompt"]
    assert "声音方案和视频动作不属于这张环境图" in request["prompt"]
    assert "UNUSED_STALE_SCENE" not in request["prompt"]
    assert directed_plan_data == before


def test_directing_scene_is_available_when_older_brief_has_no_location(directed_plan_data):
    directed_plan_data["strategy"]["brief"]["scenario"] = ""
    for direction in directed_plan_data["strategy"]["directions"]:
        direction["scenario"] = ""
    request = scene_design_request({}, directed_plan_data)
    assert request["scenario"] == directed_plan_data["visual_direction"]["scene"]


def test_new_review_includes_silent_ending_and_preserves_approved_plan(directed_plan_data):
    plan = Plan.model_validate(directed_plan_data)
    before = plan.model_dump()
    review = complete_review(before)
    result = validate_av_review(review, plan)
    assert result.ready and result.model_dump() == review
    assert result.shots[-1].shot_id == "rest"
    assert "rest" not in plan.segments[0].speech_spans[0].shot_ids
    assert plan.model_dump() == before


@pytest.mark.parametrize("invalid", ["absent", "null", "empty", "missing_silent", "foreign", "wrong_segment", "duplicate"])
def test_new_review_rejects_missing_silent_or_foreign_shots(directed_plan_data, invalid):
    review = complete_review(directed_plan_data)
    if invalid == "absent": review.pop("shots")
    elif invalid == "null": review["shots"] = None
    elif invalid == "empty": review["shots"] = []
    elif invalid == "missing_silent": review["shots"].pop()
    elif invalid == "foreign": review["shots"][-1]["shot_id"] = "not_in_plan"
    elif invalid == "wrong_segment": review["shots"][-1]["segment_id"] = "another_segment"
    else: review["shots"][-1] = deepcopy(review["shots"][0])
    with pytest.raises(ValueError, match="镜头"):
        validate_av_review(review, directed_plan_data)


@pytest.mark.parametrize("ad_ok,span_ok,shot_ok", [(True, True, True), (True, True, False),
    (True, False, True), (False, True, True), (False, False, False)])
def test_new_ready_requires_ad_speech_and_every_shot(directed_plan_data, ad_ok, span_ok, shot_ok):
    review = complete_review(directed_plan_data)
    review["ad_expression_passed"] = ad_ok
    review["spans"][0]["passed"] = span_ok
    review["shots"][-1]["passed"] = shot_ok
    review["ready"] = ad_ok and span_ok and shot_ok
    assert validate_av_review(review, directed_plan_data).ready == review["ready"]
    review["ready"] = not review["ready"]
    with pytest.raises(ValidationError, match="结论必须"):
        AVReview.model_validate(review)


def test_failed_silent_shot_is_recordable_without_claiming_automatic_semantic_detection(directed_plan_data):
    # The reviewing model supplies this verdict. This function validates its
    # completeness/consistency; it does not inspect generated images or audio.
    review = complete_review(directed_plan_data)
    review["ready"] = False
    review["shots"][-1].update(passed=False, note="静音收束镜头换成另一个餐盘，实际描述与上一镜保留的状态不相容。")
    assert not validate_av_review(review, directed_plan_data).ready


def test_legacy_review_can_omit_shots_and_retains_identical_serialized_shape(plan_data):
    review = complete_review(plan_data)
    review.pop("shots")
    result = validate_av_review(review, plan_data)
    assert result.model_dump() == review and result.shots is None


def test_legacy_review_with_explicit_shots_must_still_cover_every_shot(plan_data):
    review = complete_review(plan_data)
    assert validate_av_review(review, plan_data).ready
    review["shots"].pop()
    with pytest.raises(ValueError, match="全部实际镜头"):
        validate_av_review(review, plan_data)


def test_shot_ids_are_scoped_to_segments_not_globally():
    plan = {"title": "两个商品镜头", "script": "商品广告。", "audio_strategy": "voiceover",
            "facts": {"name": "商品", "claims": []}, "segments": [
                {"id": sid, "duration_seconds": 10, "purpose": "呈现商品", "prompt": "商品展示",
                 "reference_asset_ids": ["photo"], "shots": [
                     {"id": "same_local_id", "start": 0, "end": 10, "description": "桌面商品"}]}
                for sid in ("part1", "part2")]}
    review = complete_review(plan)
    assert validate_av_review(review, plan).ready
    review["shots"][1]["segment_id"] = "part1"
    with pytest.raises(ValueError, match="不能重复"):
        validate_av_review(review, plan)


@pytest.mark.parametrize("invalid", ["coerced_boolean", "blank_note", "extra_field", "blank_id"])
def test_shot_verdict_is_strict_and_cannot_rewrite_plan(directed_plan_data, invalid):
    review = complete_review(directed_plan_data)
    check = review["shots"][0]
    if invalid == "coerced_boolean": check["passed"] = "true"
    elif invalid == "blank_note": check["note"] = "  "
    elif invalid == "extra_field": check["new_description"] = "添加未知功能"
    else: check["shot_id"] = ""
    with pytest.raises(ValidationError):
        AVReview.model_validate(review)


def test_shot_coverage_does_not_replace_required_speech_coverage(directed_plan_data):
    review = complete_review(directed_plan_data)
    review["spans"] = []
    with pytest.raises(ValueError, match="全部旁白时段"):
        validate_av_review(review, directed_plan_data)
