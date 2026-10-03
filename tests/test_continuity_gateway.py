"""Real request compilation and lossless migration, with no network or keys."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.continuity import upgrade_continuity_plan
from app.providers.gateway import Gateway, SubmissionError
from app.schemas import Plan
from test_grouped_generation import grouped_segment
from test_providers import settings, source, install_transport, prepared_original, public_dns


def old_plan(count=2):
    segments = []
    for index in range(count):
        segment = grouped_segment(segment_id=f"s{index}")
        for shot in segment["shots"]:
            shot["beat_id"] = f"beat_{index}"
        segment["speech_spans"][0]["beat_id"] = f"beat_{index}"
        segments.append(segment)
    return Plan.model_validate({"title": "蓝瓶", "facts": {"name": "蓝瓶", "claims": []},
        "language": "zh-CN", "aspect_ratio": "9:16", "audio_strategy": "native",
        "segments": segments, "script": "".join(s["narration"] for s in segments)}).model_dump()


def absolute_timeline(plan):
    shots, spans, cursor = [], [], 0
    for segment in plan["segments"]:
        indices = {shot["id"]: len(shots) + index for index, shot in enumerate(segment["shots"])}
        for shot in segment["shots"]:
            shots.append({**{k: v for k, v in shot.items() if k not in ("id", "start", "end")},
                          "start": round(cursor + shot["start"], 6), "end": round(cursor + shot["end"], 6)})
        for span in segment["speech_spans"]:
            value = deepcopy(span)
            value.pop("id")
            value.update(start=round(cursor + span["start"], 6), end=round(cursor + span["end"], 6),
                         shot_ids=[indices[sid] for sid in span["shot_ids"]])
            for anchor in value["anchors"]:
                anchor.update(start=round(cursor + anchor["start"], 6), end=round(cursor + anchor["end"], 6),
                              shot_ids=[indices[sid] for sid in anchor["shot_ids"]])
            spans.append(value)
        cursor += segment["duration_seconds"]
    return shots, spans


@pytest.mark.parametrize("count", [1, 2, 3])
def test_upgrade_preserves_absolute_visual_and_spoken_timeline(count):
    original = old_plan(count)
    before = deepcopy(original)
    upgraded = upgrade_continuity_plan(original)
    assert original == before
    assert upgraded["generation_strategy"] == "continuity_v1"
    assert [s["duration_seconds"] for s in upgraded["segments"]] == [count * 10]
    assert upgraded["script"] == original["script"]
    assert upgraded["facts"] == original["facts"]
    assert absolute_timeline(upgraded) == absolute_timeline(original)


@pytest.mark.parametrize("invalid", ["long", "different_refs", "already_upgraded", "legacy_speech"])
def test_upgrade_refuses_unsupported_or_creatively_different_inputs(invalid):
    plan = old_plan(4 if invalid == "long" else 2)
    if invalid == "different_refs": plan["segments"][1]["reference_asset_ids"] = ["other_product"]
    if invalid == "already_upgraded": plan = upgrade_continuity_plan(plan)
    if invalid == "legacy_speech":
        for segment in plan["segments"]:
            segment["speech_spans"] = None
    with pytest.raises(ValueError): upgrade_continuity_plan(plan)


def test_twenty_second_native_request_is_one_complete_timeline(settings, source, install_transport, prepared_original, public_dns):
    plan = upgrade_continuity_plan(old_plan())
    segment = {**plan["segments"][0], "audio_strategy": "native", "generation_strategy": "continuity_v1"}
    scene = {"id": "scene", "kind": "scene", "metadata": {"provider_url": "https://cdn.example/scene.png"}}
    calls = []
    def handler(request):
        assert request.method == "POST" and request.url.path == "/v1/videos/generations"
        payload = json.loads(request.content); calls.append(payload)
        assert payload["duration"] == 20
        assert payload["images"] == ["https://canva.ai-tudou.net/library/verified-original.png", "https://cdn.example/scene.png"]
        assert "一次生成完整20秒" in payload["prompt"] and "同一说话人保持声线与口音一致" in payload["prompt"]
        assert "14–20秒" in payload["prompt"] and "10.5–16.5秒" in payload["prompt"]
        assert "此后到10秒" not in payload["prompt"]
        return httpx.Response(200, json={"data": {"id": "single-complete-film"}})
    install_transport(handler)
    result = asyncio.run(Gateway(settings).submit_video(segment, [source, scene], "new-run"))
    assert result["task_id"] == "single-complete-film" and len(calls) == 1


@pytest.mark.parametrize("invalid", ["unknown_model", "legacy_20_seconds", "unknown_policy", "one_second"])
def test_invalid_duration_contract_never_reaches_upload_or_submission(settings, source, monkeypatch, invalid):
    plan = upgrade_continuity_plan(old_plan())
    segment = {**plan["segments"][0], "audio_strategy": "native", "generation_strategy": "continuity_v1"}
    if invalid == "unknown_model": settings.video_model = "unverified-provider"
    if invalid == "legacy_20_seconds": segment.pop("generation_strategy")
    if invalid == "unknown_policy": segment["generation_strategy"] = "invented"
    if invalid == "one_second": segment["duration_seconds"] = 1
    async def forbidden(*args, **kwargs): pytest.fail("invalid plans must stop before uploads or paid submissions")
    monkeypatch.setattr(Gateway, "_original_reference_url", forbidden)
    monkeypatch.setattr(Gateway, "_submit", forbidden)
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(Gateway(settings).submit_video(segment, [source], "forbidden"))
    assert exc.value.uncertain is False
