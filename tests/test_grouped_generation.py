"""Exercise the real adapter and pipeline with offline grouped-speech inputs."""
import asyncio
import copy
import json

import httpx
import pytest

from app.providers.gateway import Gateway, SubmissionError
from app.schemas import Plan
from test_providers import settings, source, install_transport, prepared_original, public_dns
from test_conversation_pipeline import setup, fake_media, OfflineGateway, execute


def grouped_segment(original_id="original-product", segment_id="s0"):
    text = "这只蓝瓶，让桌面多一抹蓝。"
    return {
        "id": segment_id, "duration_seconds": 10, "purpose": "蓝瓶融入桌面配色",
        "prompt": "OLD_PROMPT_必须念旧台词_不要跨镜头", "audio_strategy": "native", "narration": text,
        "reference_asset_ids": [original_id, "scene"],
        "shots": [
            {"id": "wide", "start": 0, "end": 4, "description": "办公桌上摆放蓝瓶", "narration": "", "beat_id": "blue"},
            {"id": "detail", "start": 4, "end": 10, "description": "同一桌面靠近蓝色瓶身", "narration": "", "beat_id": "blue"},
        ],
        "speech_spans": [{"id": "blue-line", "beat_id": "blue", "text": text, "start": .5, "end": 6.5,
                          "shot_ids": ["wide", "detail"], "anchors": [
                              {"text": text, "start": .5, "end": 6.5, "shot_ids": ["wide", "detail"],
                               "visual_evidence": "蓝瓶与桌面的配色关系连续可见"}]}],
    }


@pytest.mark.parametrize("duration", [10, 7])
def test_real_video_submit_compiles_one_cross_shot_speech_schedule_without_stale_prompt(settings, source, install_transport, prepared_original, public_dns, duration):
    segment = grouped_segment(source["id"])
    segment["duration_seconds"] = duration
    segment["shots"][-1]["end"] = duration
    segment["speech_spans"][0]["anchors"][0]["visual_evidence"] = "DIRECTOR_SELF_ASSERTION_不应变成另一套动作指令"
    original = copy.deepcopy(segment)
    scene = {"id": "scene", "kind": "scene", "metadata": {"provider_url": "https://cdn.example/scene.png"}}
    submitted = []
    def handle(request):
        assert request.url.path == "/v1/videos/generations" and request.method == "POST"
        payload = json.loads(request.content)
        submitted.append(payload)
        prompt = payload["prompt"]
        assert "切镜不打断连续表达" in prompt
        assert "同一表达可以连续跨越关联镜头" in prompt
        assert "0.5–6.5秒 | 关联镜头 wide, detail" in prompt
        assert "每句台词仅按原文说一次" in prompt
        assert prompt.count(segment["narration"]) == 1
        assert segment["narration"] in prompt
        assert "OLD_PROMPT" not in prompt and "必须念旧台词" not in prompt
        assert "DIRECTOR_SELF_ASSERTION" not in prompt
        assert "该时段唯一台词" not in prompt and "不跨镜头" not in prompt
        assert "不吞字，不改写或补充商品主张" in prompt
        assert payload["duration"] == 10
        if duration < 10:
            assert "前7秒完成动作和所有旁白" in prompt and "成片只使用前7秒，不截断发音" in prompt
        assert payload["images"] == ["https://canva.ai-tudou.net/library/verified-original.png", "https://cdn.example/scene.png"]
        return httpx.Response(200, json={"data": {"id": "offline-grouped-video"}})
    install_transport(handle)
    result = asyncio.run(Gateway(settings).submit_video(segment, [source, scene], "offline-only"))
    assert result["task_id"] == "offline-grouped-video" and len(submitted) == 1
    assert segment == original


def test_real_video_submit_preserves_explicit_silent_span_schedule(settings, source, install_transport, prepared_original, public_dns):
    segment = grouped_segment(source["id"])
    segment.update(narration="", speech_spans=[])
    scene = {"id": "scene", "kind": "scene", "metadata": {"provider_url": "https://cdn.example/scene.png"}}
    def handle(request):
        prompt = json.loads(request.content)["prompt"]
        assert "本段无台词" in prompt and "不新增台词" in prompt
        assert "OLD_PROMPT" not in prompt and "这只蓝瓶，让桌面多一抹蓝。" not in prompt
        return httpx.Response(200, json={"data": {"id": "offline-silent"}})
    install_transport(handle)
    assert asyncio.run(Gateway(settings).submit_video(segment, [source, scene], "offline-silent"))["task_id"] == "offline-silent"


@pytest.mark.parametrize("invalid", ["unknown_shot", "reverse_shots", "cross_beat", "unknown_anchor", "anchor_text", "short_window", "mixed_legacy_words", "segment_words"])
def test_invalid_grouped_speech_is_blocked_before_reference_upload_and_video_submit(settings, source, monkeypatch, invalid):
    segment = grouped_segment(source["id"])
    span = segment["speech_spans"][0]
    if invalid == "unknown_shot": span["shot_ids"][-1] = "not-present"
    elif invalid == "reverse_shots": span["shot_ids"].reverse()
    elif invalid == "cross_beat": segment["shots"][1]["beat_id"] = "other-message"
    elif invalid == "unknown_anchor": span["anchors"][0]["shot_ids"] = ["not-present"]
    elif invalid == "anchor_text": span["anchors"][0]["text"] = "另一个意思。"
    elif invalid == "short_window": span["end"] = 1
    elif invalid == "mixed_legacy_words": segment["shots"][0]["narration"] = "重复口播。"
    else: segment["narration"] = "与独立旁白不同的台词。"
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid speech associations cannot trigger upload or paid submission")
    monkeypatch.setattr(Gateway, "_original_reference_url", forbidden)
    monkeypatch.setattr(Gateway, "_submit", forbidden)
    with pytest.raises(SubmissionError, match="尚未提交供应商") as exc:
        asyncio.run(Gateway(settings).submit_video(segment, [source], "invalid-offline-only"))
    assert exc.value.uncertain is False


def test_pipeline_exports_speech_windows_independently_of_cuts_and_keeps_silent_segments(setup, fake_media):
    store, project, asset, _, pipeline_settings = setup
    first = grouped_segment(asset["id"])
    first["reference_asset_ids"] = [asset["id"]]
    second = copy.deepcopy(first)
    second.update(id="s1", narration="", speech_spans=[])
    plan = Plan.model_validate({"title": "桌面蓝瓶", "script": first["narration"], "audio_strategy": "native",
                               "facts": {"name": "蓝瓶", "claims": []}, "segments": [first, second]}).model_dump()
    saved = store.update_plan(project["id"], plan)
    gateway = OfflineGateway([])
    job = store.create_job(project["id"], "generate", saved["revision"], {"pause_for_creative": True})
    waiting = execute(store, pipeline_settings, gateway, job)
    assert waiting["status"] == "awaiting_input", waiting.get("error")
    gate = store.pending_gate(project["id"], "creative")
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
                                   "option_id": "current", "client_turn_id": "offline-grouped-image-confirmation"})
    result = execute(store, pipeline_settings, gateway, queued)
    assert result["status"] == "ready", result.get("error")
    manifest = result["payload"]["manifest"]
    assert manifest["expected_speech_timeline"] == [{
        "start_frame": 12, "end_frame": 156, "segment_id": "s0", "speech_span_id": "blue-line",
        "shot_ids": ["wide", "detail"], "narration": first["narration"],
        "visual_description": "办公桌上摆放蓝瓶；同一桌面靠近蓝色瓶身",
        "anchors": [{"text": first["narration"], "start_frame": 12, "end_frame": 156,
                     "shot_ids": ["wide", "detail"], "visual_evidence": "蓝瓶与桌面的配色关系连续可见"}],
    }]
    assert manifest["audio_alignment_status"] == "not_verified" and manifest["manual_review_required"] is True
    assert manifest["total_frames"] == 480 and manifest["captions"] == []
    assert gateway.voice_calls == 0 and len(gateway.submissions) == 2


def test_exported_speech_and_anchors_use_segment_offset_without_changing_scheduled_times():
    from app.video_prompt import expected_grouped_speech
    segment = grouped_segment(segment_id="tail")
    before = copy.deepcopy(segment)
    timeline = expected_grouped_speech(segment, 240)
    assert timeline[0]["start_frame"] == 252 and timeline[0]["end_frame"] == 396
    assert timeline[0]["anchors"][0]["start_frame"] == 252
    assert timeline[0]["anchors"][0]["end_frame"] == 396
    assert segment == before
