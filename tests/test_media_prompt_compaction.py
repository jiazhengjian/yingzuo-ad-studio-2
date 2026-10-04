"""Compiler checks use approved data only; no provider or environment loading."""
from copy import deepcopy

import pytest

from app.design import character_design_request, scene_design_request
from app.video_prompt import compile_grouped_video
from test_directing_media import compiled_segment
from test_directing_contract import directed_plan_data
from test_speech_spans import plan_data


def production(mode="voiceover"):
    return {"version": "1.0.0",
            "product_constraints": [{"text": "保留单块楔形切片，不变成圆形完整蛋糕", "evidence_ids": ["private_fact_id"]}],
            "characters": [{"id": "char_colleague", "name": "同事", "description": "短发，灰色衬衫，深色长裤"}],
            "speech_mode": mode, "speaker_id": "char_colleague" if mode == "character_dialogue" else "",
            "voice_description": "自然低沉的成年男性普通话" if mode != "none" else ""}


def with_production(plan, mode="voiceover"):
    segment = compiled_segment(plan)
    segment["production"] = production(mode)
    segment["reference_roles"] = [{"index": 1, "role": "scene"}, {"index": 2, "role": "product"},
                                  {"index": 3, "role": "character", "character_id": "char_colleague"}]
    segment["shots"][0]["character_ids"] = ["char_colleague"]
    return segment


def test_image_prompt_keeps_confirmed_environment_and_omits_actions_and_speech(directed_plan_data):
    plan = deepcopy(directed_plan_data)
    plan["segments"][0]["prompt"] = "EXCLUDE_SEGMENT_ACTION"
    plan["segments"][0]["shots"][0]["description"] = "EXCLUDE_SHOT_ACTION"
    plan["segments"][0]["speech_spans"][0]["text"] = "EXCLUDE_SPEECH"
    request = scene_design_request({"brief": "EXCLUDE_OLD_BRIEF"}, plan)
    for text in ("EXCLUDE_SEGMENT_ACTION", "EXCLUDE_SHOT_ACTION", "EXCLUDE_SPEECH", "EXCLUDE_OLD_BRIEF"):
        assert text not in request["prompt"]
    for key in ("scene", "lighting", "color_palette"):
        assert plan["visual_direction"][key] in request["prompt"]


def test_each_approved_character_gets_one_four_view_sheet(directed_plan_data):
    plan = deepcopy(directed_plan_data)
    plan["production"] = production()
    before = deepcopy(plan)
    character = plan["production"]["characters"][0]
    request = character_design_request({}, plan, character)
    assert request["character_id"] == character["id"]
    assert character["description"] in request["prompt"]
    assert all(view in request["prompt"] for view in ("脸部特写", "正面全身", "侧面全身", "背面全身"))
    assert "16:9" in request["prompt"] and "左侧约40%" in request["prompt"]
    assert "2×2" not in request["prompt"]
    assert "默认出镜为中国人，East Asian Chinese appearance，日常大陆长相。" in request["prompt"]
    assert request["aspect_ratio"] == "16:9" and request["sheet_layout"] == "horizontal_16x9"
    assert request["skill_trace"][0]["compiled_resource"] == "references/character-template.txt"
    assert plan["segments"][0]["speech_spans"][0]["text"] not in request["prompt"]
    assert plan == before
    with pytest.raises(ValueError, match="已确认"):
        character_design_request({}, plan, {**character, "description": "未经确认的新人物"})


@pytest.mark.parametrize("mode", ["voiceover", "character_dialogue", "none"])
def test_voice_decision_and_actual_reference_order_are_preserved(directed_plan_data, mode):
    segment = with_production(directed_plan_data, mode)
    segment["director_context"]["sound"] = "只保留办公室环境声"
    if mode == "none":
        segment["speech_spans"] = []
    prompt = compile_grouped_video(segment)
    assert "参考图1：场景参考" in prompt and "参考图2：商品原图" in prompt
    assert "参考图3：角色“同事”的四视图" in prompt
    assert "出镜角色：同事（参考图3）" in prompt
    assert segment["production"]["product_constraints"][0]["text"] in prompt
    assert "private_fact_id" not in prompt and "@element" not in prompt
    assert "成年女性" not in prompt
    if mode == "character_dialogue":
        assert "人物对白，由“同事（参考图3）”" in prompt and "与其画面口型配合" in prompt
        assert "不生成真人对口型" not in prompt and "画面中的角色不对口型" not in prompt
    elif mode == "voiceover":
        assert "声音方式：画外音" in prompt and "画面中的角色不对口型" in prompt
    else:
        assert "声音方式：无人声" in prompt and "本段无台词" in prompt


@pytest.mark.parametrize("invalid", ["unbound", "unknown_character", "missing_character", "wrong_order"])
def test_character_reference_is_never_invented_from_its_name(directed_plan_data, invalid):
    segment = with_production(directed_plan_data)
    if invalid == "unbound":
        segment.pop("reference_roles")
    elif invalid == "unknown_character":
        segment["reference_roles"][-1]["character_id"] = "char_unknown"
    elif invalid == "missing_character":
        segment["reference_roles"].pop()
    else:
        segment["reference_roles"][0]["index"] = 2
    with pytest.raises(ValueError):
        compile_grouped_video(segment)


def test_speech_text_time_and_key_states_survive_without_internal_provenance(directed_plan_data):
    segment = compiled_segment(directed_plan_data)
    before = deepcopy(segment)
    prompt = compile_grouped_video(segment)
    for shot in segment["shots"]:
        assert shot["description"] in prompt
        assert f"{shot['start']:g}–{shot['end']:g}秒" in prompt
        for field in ("start_state", "allowed_changes", "end_state"):
            assert shot["continuity"][field] in prompt
    for span in segment["speech_spans"]:
        assert prompt.count(span["text"]) == 1
        assert f"{span['start']:g}–{span['end']:g}秒" in prompt
        cursor = 0
        for anchor in span["anchors"]:
            end = cursor + len(anchor["text"])
            assert f"第{cursor + 1}–{end}字符 {anchor['start']:g}–{anchor['end']:g}秒" in prompt
            cursor = end
    assert all(key not in prompt for key in ("source_id", "source_kind", "evidence_ids", "entry_reason"))
    assert segment == before


def test_long_approved_text_is_not_truncated_to_meet_a_prompt_budget(directed_plan_data):
    segment = compiled_segment(directed_plan_data)
    description = "保留原图的楔形轮廓以及真实切面。" * 100
    segment["shots"][0]["description"] = description
    assert description in compile_grouped_video(segment)


def test_none_mode_cannot_silently_discard_approved_speech(directed_plan_data):
    segment = with_production(directed_plan_data, "none")
    with pytest.raises(ValueError, match="不能包含台词"):
        compile_grouped_video(segment)


def test_default_cast_is_chinese_and_an_explicit_look_replaces_it(directed_plan_data):
    from app.production import DEFAULT_CAST_LINE
    plan = deepcopy(directed_plan_data)
    plan["production"] = production()
    plain = character_design_request({}, plan, plan["production"]["characters"][0])
    assert DEFAULT_CAST_LINE in plain["prompt"]
    requested = deepcopy(plan["production"]["characters"][0])
    requested["description"] = "成年欧美女性，金色长发，白衬衫"
    plan["production"]["characters"][0] = requested
    overridden = character_design_request({}, plan, requested)
    assert DEFAULT_CAST_LINE not in overridden["prompt"]
    assert "欧美女性" in overridden["prompt"]
    assert "左侧约40%" in overridden["prompt"]
    segment = with_production(directed_plan_data)
    segment["reference_roles"][-1]["sheet_layout"] = "horizontal_16x9"
    prompt = compile_grouped_video(segment)
    assert "左侧是脸部和上胸特写，右侧同一排依次是正面、侧面、背面全身" in prompt
    assert DEFAULT_CAST_LINE in prompt
    segment["production"]["characters"][0]["description"] = "成年欧美女性，金色长发，白衬衫"
    overridden_video = compile_grouped_video(segment)
    assert DEFAULT_CAST_LINE not in overridden_video
    assert "欧美女性" in overridden_video
    assert "角色“同事”的四视图，左侧是脸部和上胸特写" in overridden_video
    assert "四格版式不进入视频" not in overridden_video
    legacy = with_production(directed_plan_data)
    legacy_prompt = compile_grouped_video(legacy)
    assert "角色“同事”的四视图，统一其外貌与服装；四格版式不进入视频。" in legacy_prompt
    assert "角色“同事”的四视图，左侧是脸部和上胸特写" not in legacy_prompt


def test_explicit_null_new_fields_keep_historical_reference_behavior(directed_plan_data):
    segment = compiled_segment(directed_plan_data)
    original = compile_grouped_video(segment)
    segment.update(production=None, reference_roles=None)
    assert compile_grouped_video(segment) == original
