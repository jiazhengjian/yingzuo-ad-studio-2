"""Only a confirmed no-speech choice permits missing provider audio."""
import asyncio
from copy import deepcopy

import pytest

from app import media
from app.pipeline import Pipeline
from app.schemas import Plan
from test_production_pipeline import CastGateway, cast_setup, confirm, execute
from test_pipeline import fake_media
from test_speech_spans import plan_data


def replace_speech_mode(setup, mode):
    store, project, original, settings, old_job = setup
    plan = deepcopy(original)
    plan["production"].update(speech_mode=mode,
        speaker_id="char_worker" if mode == "character_dialogue" else "",
        voice_description="" if mode == "none" else "自然中文角色声音")
    if mode == "none":
        plan.update(script="", ad_script=None, script_review=None)
        for segment in plan["segments"]:
            segment.update(narration="", speech_spans=[])
    elif mode == "character_dialogue":
        for segment in plan["segments"]:
            for shot in segment["shots"]:
                shot["character_ids"] = ["char_worker"]
            for span in segment["speech_spans"]:
                span["speaker_id"] = "char_worker"
    plan = Plan.model_validate(plan).model_dump()
    store.update_job(old_job["id"], status="canceled")
    # 对口型对白属于剧情，不能再用产品展示的类型去放行。
    if mode == "character_dialogue":
        plan["strategy"]["brief"]["ad_form"] = "scene_story"
        with store._connection(True) as connection:
            connection.execute("UPDATE projects SET ad_form='scene_story' WHERE id=?", (project["id"],))
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"])
    return store, project, plan, settings, job


@pytest.mark.parametrize("has_provider_audio", [False, True])
def test_no_speech_pipeline_keeps_actual_audio_metadata_and_unverified_review_status(
        cast_setup, fake_media, monkeypatch, has_provider_audio):
    setup = replace_speech_mode(cast_setup, "none")
    store, project, plan, settings, _ = setup
    probe, compose = media.probe, media.compose
    def provider_probe(path, *args, **kwargs):
        info = probe(path, *args, **kwargs)
        info["has_audio"] = has_provider_audio
        return info
    def completed_compose(manifest, paths, destination, *args, **kwargs):
        assert manifest["production"] == {"speech_mode": "none"}
        info = compose(manifest, paths, destination, *args, **kwargs)
        info["local_silence_asset_ids"] = [] if has_provider_audio else [s["asset_id"] for s in manifest["segments"]]
        return info
    monkeypatch.setattr(media, "probe", provider_probe)
    monkeypatch.setattr(media, "compose", completed_compose)
    gateway = CastGateway(store, plan)
    waiting = execute(setup, gateway)
    result = execute(setup, gateway, confirm(setup, waiting))
    assert result["status"] == "ready", result.get("error")
    assert gateway.voices == 0 and len(gateway.submissions) == 1
    manifest = result["payload"]["manifest"]
    assert manifest["production"] == {"speech_mode": "none"}
    assert manifest["audio_alignment_status"] == "not_applicable"
    assert manifest["caption_alignment"] == "no_speech_requested"
    assert manifest["captions"] == manifest["expected_speech_timeline"] == []
    assert manifest["continuity_review"]["voice_identity"] == "not_applicable"
    assert all(manifest["continuity_review"][key] == "not_verified"
               for key in ("visual_transitions", "environmental_audio", "unexpected_speech"))
    assert manifest["manual_review_required"] is True
    assert "无台词" in manifest["review_note"]
    segment_asset = store.get_asset(manifest["segments"][0]["asset_id"])
    assert segment_asset["metadata"]["has_audio"] is has_provider_audio
    assert manifest["local_silence_asset_ids"] == ([] if has_provider_audio else [segment_asset["id"]])
    render = store.get_render(store.get_project(project["id"])["current_render_id"])
    assert render["manifest"] == manifest
    events = store.events_after(project["id"])["events"]
    assert all("Video Review" not in str(event) and "成片审核通过" not in str(event) for event in events)
    messages = store.project_detail(project["id"])["messages"]
    assert "无人声方案导出" in messages[-1]["content"]
    assert "保留原生声音" not in messages[-1]["content"]
    # Durable download resumption also accepts the true missing-track metadata.
    cached = asyncio.run(Pipeline(store, gateway, settings)._video(
        result, gateway.requests[0]["spec"], gateway.requests[0]["assets"]))
    assert cached["id"] == segment_asset["id"] and gateway.downloads == 1


@pytest.mark.parametrize("speech_mode", ["voiceover", "character_dialogue"])
def test_spoken_modes_still_reject_missing_audio_before_render(cast_setup, fake_media, monkeypatch, speech_mode):
    setup = replace_speech_mode(cast_setup, speech_mode)
    store, project, plan, _, _ = setup
    probe = media.probe
    def missing_audio(path, *args, **kwargs):
        return {**probe(path, *args, **kwargs), "has_audio": False}
    monkeypatch.setattr(media, "probe", missing_audio)
    monkeypatch.setattr(media, "compose", lambda *a, **k: pytest.fail("Missing expected speech cannot be rendered as silence"))
    gateway = CastGateway(store, plan)
    waiting = execute(setup, gateway)
    result = execute(setup, gateway, confirm(setup, waiting))
    assert result["status"] == "failed" and "没有原生音轨" in result["error"]
    assert store.get_project(project["id"])["current_render_id"] is None
    assert gateway.voices == 0 and len(gateway.submissions) == 1
