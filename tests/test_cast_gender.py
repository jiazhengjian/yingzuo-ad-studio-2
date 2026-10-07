"""Gender is chosen once, then the script and the pictures follow it."""
from pathlib import Path

import pytest

from app.design import (
    cast_look_lock,
    character_design_request,
    character_gender,
    settle_cast_appearance,
    state_design_request,
)
from app.planning import STRATEGY_SELECTION_SYSTEM
from app.production import CharacterSpec
from app.strategy import prepare_strategy_ingress
from app.video_prompt import compile_grouped_video


def _direction(description, audience, name="主讲"):
    return prepare_strategy_ingress({
        "kind": "directions",
        "message": "三个方向",
        "brief": {"product_name": "降噪耳机", "ad_form": "brand_tvc", "audience": audience, "scenario": "通勤",
                  "facts": [{"id": "f1", "text": audience, "source_kind": "merchant", "source_id": "brief"}]},
        "directions": [{
            "id": "d1",
            "audience": audience,
            "scenario": "通勤",
            "production_notes": ["不编功效"],
            "hook_type": "mechanism",
            "production": {"characters": [{"id": "char_host", "name": name, "description": description}]},
        }],
    })


def _person(offered):
    return offered["directions"][0]["production"]["characters"][0]


@pytest.mark.parametrize("audience,description,gender,kept", [
    ("年轻女性", "一个男生", "female", "一个女生"),
    ("宝妈", "她", "female", "她"),
    ("男生", "一个女生", "male", "一个男生"),
])
def test_merchant_gender_is_written_without_adding_appearance(audience, description, gender, kept):
    person = _person(_direction(description, audience))
    assert person["gender"] == gender
    assert person["description"] == kept
    assert "短发" not in person["description"]
    assert "东亚" not in person["description"]
    assert "衣服" not in person["description"]


def test_a_dad_keeps_his_role_when_the_audience_is_women():
    person = _person(_direction("爸爸，坐在旁边", "年轻女性", name="爸爸"))
    assert person["gender"] == "male"
    assert person["description"] == "爸爸，坐在旁边"


def test_a_baby_does_not_inherit_the_mothers_gender():
    person = _person(_direction("婴儿", "宝妈", name="宝宝"))
    assert "gender" not in person
    assert person["description"] == "婴儿"


def test_direction_gender_fixes_shot_pronouns_for_every_form():
    for form in ("brand_tvc", "presenter_pitch", "scene_story"):
        person = {"id": "char_host", "name": "主讲", "description": "一个女生"}
        shot = {"id": "s1", "description": "他举起耳机。", "character_ids": ["char_host"],
                "continuity": {"start_state": "他拿着耳机", "end_state": "耳机在他手里", "allowed_changes": "他松开手"}}
        plan = {"ad_form": form, "production": {"characters": [person], "speaker_id": "char_host"},
                "segments": [{"ad_form": form, "shots": [shot], "speech_spans": [
                    {"text": "他看完了。", "shot_ids": ["s1"]}]}]}
        assert settle_cast_appearance(plan) is True
        assert person["gender"] == "female"
        assert person["description"] == "一个女生"
        assert shot["description"] == "她举起耳机。"
        assert shot["continuity"]["start_state"] == "她拿着耳机"
        assert plan["segments"][0]["speech_spans"][0]["text"] == "她看完了。"
        assert "东亚" not in person["description"]


def test_stored_gender_wins_over_shot_text_in_pictures():
    person = {"id": "char_host", "name": "主讲", "description": "他", "gender": "male"}
    shot = {"id": "s1", "description": "她摘下耳机。", "character_ids": ["char_host"]}
    plan = {"facts": {"name": "降噪耳机"}, "production": {"characters": [person]},
            "segments": [{"shots": [shot]}]}
    assert character_gender(plan, person) == "male"
    assert settle_cast_appearance(plan) is True
    assert shot["description"] == "他摘下耳机。"
    sheet = character_design_request({}, plan, person)["prompt"]
    assert "这个人是男性，不要画成女性。" in sheet
    assert "按镜头" not in sheet
    assert "东亚女性" not in sheet
    state = state_design_request({}, plan, {
        "phase": "start", "label": "湿", "subject": "头发", "shot_id": "s1",
        "character_id": "char_host", "state_text": "湿"})
    assert "不要画成女性" in state["prompt"]
    assert "不要画成男性" not in state["prompt"]
    video = compile_grouped_video({
        "duration_seconds": 4, "generation_strategy": "single_clip", "ad_form": "brand_tvc",
        "production": {"characters": [person], "speech_mode": "voiceover",
                       "voice_description": "自然的成年男声"},
        "shots": [{**shot, "start": 0.0, "end": 4.0}], "speech_spans": [],
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_host", "sheet_layout": "horizontal_16x9"},
        ],
    })
    assert "他摘下耳机" in video
    assert "不要画成女性" in video
    assert "她摘下耳机" not in video


def test_an_old_plan_with_no_gender_still_falls_back_to_the_shots():
    person = {"id": "char_host", "name": "主讲", "description": "穿深色上衣"}
    shot = {"id": "s1", "description": "她把耳机举起。", "character_ids": ["char_host"]}
    plan = {"facts": {"name": "降噪耳机"}, "production": {"characters": [person]},
            "segments": [{"shots": [shot]}]}
    assert character_gender(plan, person) == "female"
    assert settle_cast_appearance(plan) is False
    assert shot["description"] == "她把耳机举起。"
    assert person["description"] == "穿深色上衣"
    assert "gender" not in person
    assert "不要画成男性" in cast_look_lock(plan, person)


def test_direction_prompts_ask_for_gender_and_not_wardrobe():
    assert "一个女生" in STRATEGY_SELECTION_SYSTEM
    assert "不要写年龄、发型、服装" in STRATEGY_SELECTION_SYSTEM
    assert "东亚面孔" not in STRATEGY_SELECTION_SYSTEM
    assert "稍后披上" not in STRATEGY_SELECTION_SYSTEM
    sound = Path("app/skills/ads-video-sound-direction/SKILL.md").read_text()
    assert "一个女生" in sound
    assert "年龄段、发型和服装" not in sound
    assert "东亚面孔" not in sound
    assert "partial_only" in sound
    spec = CharacterSpec.model_fields["description"].description
    assert "一个女生" in spec
    assert "不要写年龄、发型、服装" in spec
    sheet = Path("app/skills/ads-video-asset-prompts/references/character-template.txt").read_text()
    assert "日常大陆长相" in sheet
    assert "同一套服装" in sheet
