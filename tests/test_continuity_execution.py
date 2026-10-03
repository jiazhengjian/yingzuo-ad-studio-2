"""Offline continuity migration and execution; never touches a real provider."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import media
from app.db import StaleRevision, Store
from app.main import create_app
from app.pipeline import Pipeline, validate_plan
from app.schemas import Plan
from test_grouped_generation import grouped_segment
from test_pipeline import PNG, FakeGateway


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def denied(*args, **kwargs):
        pytest.fail("Continuity tests cannot access the network")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = Store(tmp_path / "studio.sqlite", tmp_path / "media")
    project = store.create_project("蓝瓶与办公桌配色", "product", duration_seconds=20)
    path = store.media_root / "product.png"
    path.write_bytes(PNG)
    product = store.add_asset(project["id"], "product", "原图", path, "image/png")
    first = grouped_segment(product["id"], "first")
    first["reference_asset_ids"] = [product["id"]]
    second = copy.deepcopy(first)
    second.update(id="second", narration="", speech_spans=[])
    plan = Plan.model_validate({"title": "桌面蓝瓶", "script": first["narration"], "audio_strategy": "native",
                               "facts": {"name": "蓝瓶", "claims": []}, "segments": [first, second]}).model_dump()
    project = store.update_plan(project["id"], plan)
    scene_path = store.media_root / "scene.png"
    scene_path.write_bytes(PNG)
    scene = store.add_asset(project["id"], "scene", "确认过的场景", scene_path, "image/png", project["revision"], {"generated": True})
    settings = SimpleNamespace(database_path=store.db_path, media_root=store.media_root,
        data_root=store.db_path.parent, tudou_api_key="TEST_ONLY", deepseek_api_key="TEST_ONLY",
        ffmpeg_path="TEST_ONLY", max_video_submissions=6, max_active_runs=1, poll_seconds=.01,
        provider_timeout_seconds=1, video_model="TEST_ONLY", tts_voice="TEST_ONLY", presenter_verified=False)
    durations = {}
    class Gateway(FakeGateway):
        async def create_designs(self, *args, **kwargs):
            pytest.fail("The already confirmed scene must be reused")
        async def submit_video(self, segment, assets, key):
            receipt = await super().submit_video(segment, assets, key)
            durations[receipt["task_id"]] = segment["duration_seconds"] if segment.get("generation_strategy") == "continuity_v1" else 10
            return receipt
        async def download_video(self, url, destination):
            destination.write_text(str(durations[url.rsplit("/", 1)[1]]))
    gateway = Gateway(store, plan)
    def probe(path, *args, **kwargs):
        duration = float(Path(path).read_text())
        return {"duration_seconds": duration, "video_duration_seconds": duration, "has_audio": True,
                "has_video": True, "width": 720, "height": 1280, "fps": 24, "frames": round(duration * 24)}
    def compose(manifest, paths, destination, *args, **kwargs):
        destination.write_text(str(manifest["total_frames"] / 24))
        return probe(destination)
    monkeypatch.setattr(media, "probe", probe)
    monkeypatch.setattr(media, "compose", compose)
    job = store.create_job(project["id"], "generate", project["revision"], {
        "pause_for_creative": True, "creative_confirmed": True, "design_asset_ids": [scene["id"]]})
    result = asyncio.run(Pipeline(store, gateway, settings).execute(job["id"]))
    assert result["status"] == "ready", result.get("error")
    return store, store.get_project(project["id"]), result, scene, gateway, settings


def upgrade(prepared, request="offline-upgrade"):
    store, project, source, *_ = prepared
    return store.create_continuity_upgrade(project["id"], project["revision"], source["id"], request)


def test_upgrade_atomic_preserves_old_media_and_receipts_then_submits_one_exact_duration(prepared):
    store, project, source, scene, gateway, settings = prepared
    original_source = copy.deepcopy(store.get_job(source["id"]))
    old_steps = [store.get_step(source["id"], "video:" + s["id"]) for s in source["payload"]["plan"]["segments"]]
    old_render = store.get_render(project["current_render_id"])
    child = upgrade(prepared)
    assert child["revision"] == project["revision"] + 1 and child["submission_count"] == 0
    assert child["payload"]["video_submission_budget"] == 1
    assert child["payload"]["creative_confirmed"] is True
    assert child["payload"]["design_asset_ids"] == [scene["id"]]
    assert child["payload"]["plan"]["generation_strategy"] == "continuity_v1"
    assert store.get_project(project["id"])["current_render_id"] == old_render["id"]
    assert len(child["payload"]["plan"]["segments"]) == 1
    assert len(gateway.submissions) == 2
    result = asyncio.run(Pipeline(store, gateway, settings).execute(child["id"]))
    assert result["status"] == "ready", result.get("error")
    assert result["submission_count"] == 1 and len(gateway.submissions) == 3
    new_spec = result["payload"]["plan"]["segments"][0]
    step = store.get_step(child["id"], "video:" + new_spec["id"])
    assert step["snapshot"]["provider_duration_seconds"] == 20
    assert step["snapshot"]["segment"]["generation_strategy"] == "continuity_v1"
    asset = store.get_asset(step["result"]["asset_id"])
    assert asset["metadata"]["provider_duration_seconds"] == asset["metadata"]["requested_duration_seconds"] == 20
    manifest = result["payload"]["manifest"]
    assert manifest["total_frames"] == 480 and len(manifest["segments"]) == 1
    assert manifest["continuity_review"] == {"voice_identity": "not_verified", "visual_transitions": "not_verified", "generation_groups": 1, "provider_seams": 0}
    assert manifest["manual_review_required"] is True and manifest["audio_alignment_status"] == "not_verified"
    assert store.get_job(source["id"]) == original_source
    assert [store.get_step(source["id"], "video:" + s["id"]) for s in source["payload"]["plan"]["segments"]] == old_steps
    assert store.get_render(old_render["id"]) == old_render
    assert upgrade(prepared)["id"] == child["id"]


def test_upgrade_double_click_allocates_only_one_revision_and_budget(prepared):
    with ThreadPoolExecutor(6) as pool:
        children = list(pool.map(lambda _: upgrade(prepared), range(6)))
    assert len({j["id"] for j in children}) == 1
    store, project, *_ = prepared
    assert store.get_project(project["id"])["revision"] == project["revision"] + 1
    assert len(store.project_detail(project["id"])["jobs"]) == 2


@pytest.mark.parametrize("changed", ["source_status", "context", "confirmation", "scene", "receipt", "unknown", "active", "revision"])
def test_upgrade_rejects_unsafe_sources_without_partial_revision(prepared, changed):
    store, project, source, scene, *_ = prepared
    if changed == "source_status":
        store.update_job(source["id"], status="failed")
    elif changed == "context":
        store.add_message(project["id"], "user", "换成家庭场景")
    elif changed == "confirmation":
        store.update_job(source["id"], payload={**source["payload"], "creative_confirmed": False})
    elif changed == "scene":
        Path(scene["path"]).write_bytes(b"changed")
    elif changed == "receipt":
        store.update_step(source["id"], "video:first", state="submitted")
    elif changed == "unknown":
        store.update_step(source["id"], "video:first", state="submitted_unknown")
    elif changed == "active":
        store.create_job(project["id"], "plan", project["revision"])
    else:
        store.update_plan(project["id"], project["plan"])
    revision_before = store.get_project(project["id"])["revision"]
    jobs_before = len(store.project_detail(project["id"])["jobs"])
    with pytest.raises(ValueError):
        upgrade(prepared)
    assert store.get_project(project["id"])["revision"] == revision_before
    assert len(store.project_detail(project["id"])["jobs"]) == jobs_before


def test_upgrade_rejects_request_id_rebinding(prepared):
    child = upgrade(prepared)
    store, project, source, *_ = prepared
    with pytest.raises(StaleRevision, match="请求编号"):
        store.create_continuity_upgrade(project["id"], project["revision"] + 1, source["id"], "offline-upgrade")
    assert store.get_job(child["id"])["submission_count"] == 0


def test_api_upgrade_is_strict_and_does_not_expose_frozen_input(prepared):
    store, project, source, _, gateway, settings = prepared
    client = TestClient(create_app(settings, gateway, run_worker=False))
    url = f"/api/projects/{project['id']}/continuity"
    body = {"accepted_revision": project["revision"], "source_job_id": source["id"], "request_id": "offline-api"}
    assert client.post(url, json={**body, "accepted_revision": str(project["revision"])}).status_code == 422
    assert client.post(url, json={**body, "skip_checks": True}).status_code == 422
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert "payload" not in response.json()["job"] and "snapshot" not in response.text
    assert client.post(url, json=body).json()["job"]["id"] == response.json()["job"]["id"]


def test_continuity_validation_rejects_legacy_shape_without_changing_legacy(prepared):
    _, project, source, *_ = prepared
    legacy = copy.deepcopy(project["plan"])
    assert validate_plan(legacy, 20) == legacy
    from app.continuity import upgrade_continuity_plan
    upgraded = upgrade_continuity_plan(legacy)
    assert validate_plan(upgraded, 20) == upgraded
    bad = copy.deepcopy(upgraded)
    bad.pop("generation_strategy")
    with pytest.raises(ValueError):
        validate_plan(bad, 20)
    assert source["payload"]["video_submission_budget"] == 2


def test_new_31_second_plan_uses_two_semantic_groups_not_four_fixed_chunks(prepared):
    store, _, source, _, gateway, settings = prepared
    project = store.create_project("31秒分组广告", "product", duration_seconds=31)
    path = store.media_root / "long-product.png"
    path.write_bytes(PNG)
    asset = store.add_asset(project["id"], "product", "商品原图", path, "image/png")
    scene_path = store.media_root / "long-scene.png"
    scene_path.write_bytes(PNG)
    scene = store.add_asset(project["id"], "scene", "已确认场景", scene_path, "image/png", metadata={"generated": True})
    plan = copy.deepcopy(source["payload"]["plan"])
    plan["generation_strategy"] = "continuity_v1"
    for segment, seconds in zip(plan["segments"], (16, 15)):
        segment["duration_seconds"] = seconds
        segment["shots"][-1]["end"] = seconds
        segment["reference_asset_ids"] = [asset["id"]]
    plan = Plan.model_validate(plan).model_dump()
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"], {
        "pause_for_creative": True, "creative_confirmed": True, "design_asset_ids": [scene["id"]]})
    assert job["payload"]["video_submission_budget"] == 2
    result = asyncio.run(Pipeline(store, gateway, settings).execute(job["id"]))
    assert result["status"] == "ready", result.get("error")
    assert result["submission_count"] == 2
    assert [store.get_step(job["id"], "video:" + s["id"])["snapshot"]["provider_duration_seconds"] for s in plan["segments"]] == [16, 15]
    manifest = result["payload"]["manifest"]
    assert [(s["timeline_in_frame"], s["timeline_out_frame"]) for s in manifest["segments"]] == [(0, 384), (384, 744)]
    assert manifest["total_frames"] == 744 and manifest["continuity_review"]["provider_seams"] == 1


def test_provider_10_second_result_cannot_satisfy_20_second_continuity_request(prepared, monkeypatch):
    store, project, _, _, gateway, settings = prepared
    child = upgrade(prepared)
    probe = media.probe
    def too_short(path, *args, **kwargs):
        result = probe(path, *args, **kwargs)
        return {**result, "duration_seconds": 10, "video_duration_seconds": 10, "frames": 240}
    monkeypatch.setattr(media, "probe", too_short)
    result = asyncio.run(Pipeline(store, gateway, settings).execute(child["id"]))
    assert result["status"] == "failed" and "短于计划" in result["error"]
    assert result["submission_count"] == 1
    assert store.get_project(project["id"])["current_render_id"] == project["current_render_id"]
    assert len(store.project_detail(project["id"])["renders"]) == 1
    step = store.get_step(child["id"], "video:" + child["payload"]["plan"]["segments"][0]["id"])
    assert step["state"] == "submitted" and step["task_id"]
