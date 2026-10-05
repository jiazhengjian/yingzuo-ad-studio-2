"""On-camera speech is lip-synced from the first word. Historical voiceover stays voiceover."""
import pytest

from app.providers.gateway import Gateway, SubmissionError
from app.video_prompt import compile_grouped_video, compiled_video_request
from app.config import Settings


def _segment(*, mode="character_dialogue", delivery=None, ad_form=None):
    production = {
        "version": "1.0.0",
        "product_constraints": [],
        "characters": [{"id": "char_host", "name": "主讲", "description": "短发，灰色上衣"}],
        "speech_mode": mode,
        "speaker_id": "char_host" if mode == "character_dialogue" else "",
        "voice_description": "自然的成年女性普通话",
    }
    if delivery:
        production["speech_delivery"] = delivery
    shots = [
        {"id": "s0", "start": 0, "end": 3, "description": "她看着镜头，手里拿着吹风机。",
         "character_ids": ["char_host"], "product_visible": True},
        {"id": "s1", "start": 3, "end": 6, "description": "吹风机特写，她把机身放下。",
         "character_ids": [], "product_visible": True},
    ]
    span = {"id": "speech_1", "start": 0, "end": 6, "shot_ids": ["s0", "s1"],
            "text": "吹头发的时候跟你说话。",
            "anchors": [{"text": "吹头发的时候跟你说话。", "start": 0, "end": 6, "shot_ids": ["s0", "s1"]}]}
    segment = {
        "duration_seconds": 6,
        "generation_strategy": "continuity_v1",
        "speech_spans": [] if mode == "none" else [span],
        "shots": shots,
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "scene"},
            {"index": 3, "role": "character", "character_id": "char_host"},
        ],
    }
    if mode != "missing":
        segment["production"] = production
    else:
        segment["shots"][0]["character_ids"] = []
        segment["shots"][1]["character_ids"] = []
        segment["reference_roles"] = [{"index": 1, "role": "product"}, {"index": 2, "role": "scene"}]
        segment["speech_spans"] = []
    if ad_form:
        segment["ad_form"] = ad_form
    return segment


def test_on_camera_speech_is_lip_synced_from_the_first_word():
    visible = compile_grouped_video(_segment())
    assert "嘴型与每个字同步" in visible and "从第一句" in visible
    assert "从本镜第一帧开口说话" in visible
    assert "本镜主讲（参考图3）面向镜头说话，嘴型与台词同步。" in visible
    assert "不对口型" not in visible and "声音方式：画外音" not in visible
    assert "手和商品" in visible and "五指自然" in visible
    off = compile_grouped_video(_segment(delivery="on_and_off_screen"))
    assert "嘴型与字同步" in off and "从本镜第一帧开口说话" in off
    assert "不对口型" not in off and "画外音" not in off
    assert "本镜说话角色不入画" in off
    voice = compile_grouped_video(_segment(mode="voiceover"))
    assert "声音方式：画外旁白" in voice and "不张嘴念词" in voice
    historical = compile_grouped_video(_segment(mode="missing"))
    assert "本版使用画外音，不生成真人对口型。" in historical
    with pytest.raises(ValueError, match="尚未提交视频"):
        compile_grouped_video(_segment(mode="missing", ad_form="presenter_pitch"))


def test_new_template_is_traced_and_does_not_ban_a_spoken_cta_word():
    compiled = compiled_video_request(_segment())
    assert compiled["skill_trace"][0]["version"] == "1.5.0"
    assert "CTA" not in compiled["prompt"]
    assert "台词只说出来，不显示成字幕。" in compiled["prompt"]


def test_roommate_back_of_head_does_not_mark_the_speaker_off_camera():
    segment = _segment(delivery="on_and_off_screen", ad_form="scene_story")
    segment["shots"][0]["description"] = "她对着镜子吹发，室友只露后脑和肩头。"
    prompt = compile_grouped_video(segment)
    opening = prompt.split("镜头1 |", 1)[1].split("镜头2 |", 1)[0]
    assert "画外接着说" not in opening
    assert "嘴型与台词同步" in opening
    assert "不转头看镜头" in opening
    assert "看着镜头亲口说" not in prompt
    assert "面向镜头说话" not in opening
    hidden = _segment(delivery="on_and_off_screen", ad_form="scene_story")
    hidden["shots"][0]["description"] = "她背对镜头，只露后脑。"
    hidden_opening = compile_grouped_video(hidden).split("镜头1 |", 1)[1].split("镜头2 |", 1)[0]
    assert "画外接着说" in hidden_opening
    presenter = compile_grouped_video(_segment(delivery="on_and_off_screen"))
    assert "看着镜头亲口说" in presenter


def test_character_dialogue_never_uses_the_legacy_voiceover_prompt():
    segment = {"audio_strategy": "native", "speech_spans": None, "shots": [],
               "production": {"speech_mode": "character_dialogue", "speaker_id": "char_host", "characters": []}}
    with pytest.raises(SubmissionError, match="逐句台词时间表"):
        Gateway(Settings()).prepare_video_request(segment)
