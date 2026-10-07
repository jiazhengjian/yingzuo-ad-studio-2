"""Offline fault injection only. Synthetic fixtures are never a production fallback."""
import asyncio
import base64
import copy
import json
import shutil
import subprocess
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Store, StaleRevision
from app.pipeline import Pipeline
from app import media


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6i8AAAAASUVORK5CYII=")


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "media")
    project = store.create_project("测试商品，蓝色瓶身，不增加功效承诺", "product")
    path = store.media_root / "product.png"
    path.write_bytes(PNG)
    asset = store.add_asset(project["id"], "product", "用户商品图", path, "image/png")
    plan = {"title": "测试方案", "script": "展示商品。了解详情。", "language": "zh-CN",
            "aspect_ratio": "9:16", "audio_strategy": "voiceover", "facts": {"name": "测试商品", "claims": []},
            "segments": [{"id": f"s{i}", "duration_seconds": 10, "purpose": "展示", "prompt": "真实商品展示",
                          "shots": [{"start": 0, "end": 10, "description": f"商品第{i + 1}处"}], "reference_asset_ids": [asset["id"]]} for i in range(2)]}
    project = store.update_plan(project["id"], plan)
    settings = SimpleNamespace(ffmpeg_path="unused_in_orchestration_tests", max_video_submissions=2,
                               max_active_runs=1, poll_seconds=.01, provider_timeout_seconds=1,
                               tts_voice="TEST_ONLY", video_model="TEST_ONLY", presenter_verified=False)
    return store, project, plan, settings


class FakeGateway:
    """Explicit test injection; never imported by app modules."""
    def __init__(self, store, plan):
        self.store, self.plan_data = store, plan
        self.submissions = []
        self.polls = []
        self.downloads = 0
        self.voices = 0
        self.fail_download_once = False
        self.uncertain = False
        self.reject = False
        self.cancel_on_submit = False

    async def plan(self, project, assets, messages):
        return copy.deepcopy(self.plan_data)

    async def submit_video(self, segment, assets, idempotency_key):
        jid, _ = idempotency_key.split(":", 1)
        step = self.store.get_step(jid, f"video:{segment['id']}")
        assert step["state"] == "submitting" and step["snapshot"]["segment"] == segment
        self.submissions.append(idempotency_key)
        if self.uncertain or self.reject:
            exc = RuntimeError("TEST injected submit failure")
            exc.uncertain = self.uncertain
            raise exc
        if self.cancel_on_submit:
            self.store.request_cancel(self.store.get_job(jid)["project_id"])
        return {"task_id": "remote-" + segment["id"], "provider": "EXPLICIT_TEST_FAKE"}

    async def poll_video(self, task_id):
        self.polls.append(task_id)
        return {"status": "succeeded", "download_url": "https://fixture.invalid/" + task_id}

    async def download_video(self, url, destination):
        self.downloads += 1
        if self.fail_download_once:
            self.fail_download_once = False
            raise OSError("TEST injected temporary download error")
        destination.write_bytes(b"TEST_FAKE_VIDEO")

    async def synthesize_voice(self, text, destination, *, target_duration_seconds=None):
        self.voices += 1
        destination.write_bytes(b"TEST_FAKE_AUDIO")
        return {"duration_seconds": 1, "provider": "EXPLICIT_TEST_FAKE", "captions": [
            {"start": 0, "end": .5, "text": "展示商品。"}, {"start": .5, "end": 1, "text": "了解详情。"}]}


@pytest.fixture
def fake_media(monkeypatch):
    def probe(path, *args, **kwargs):
        assert Path(path).is_file()
        video = str(path).endswith("mp4")
        return {"duration_seconds": 10 if video else 1, "has_audio": True, "has_video": video,
                "width": 720, "height": 1280, "fps": 24, "frames": 240 if video else None, "video_duration_seconds": 10 if video else None}

    def compose(manifest, paths, destination, *args, **kwargs):
        assert "cta" not in manifest
        assert all(Path(p).is_file() for p in paths.values())
        destination.write_bytes(b"TEST_FAKE_COMPOSITE")
        return {"duration_seconds": manifest["total_frames"] / 24, "frames": manifest["total_frames"],
                "width": 720, "height": 1280, "fps": 24, "has_audio": True, "has_video": True}

    monkeypatch.setattr(media, "probe", probe)
    monkeypatch.setattr(media, "compose", compose)


def execute(store, project, settings, gateway, job=None):
    job = job or store.create_job(project["id"], "generate", project["revision"])
    return asyncio.run(Pipeline(store, gateway, settings).execute(job["id"]))


def selected_duration_project(setup, duration):
    store, _, original_plan, settings = setup
    project = store.create_project("测试用户所选时长", "product", duration_seconds=duration)
    path = store.media_root / f"product_{duration}.png"
    path.write_bytes(PNG)
    asset = store.add_asset(project["id"], "product", "真实商品", path, "image/png")
    durations = [10] * (duration // 10) + ([duration % 10] if duration % 10 else [])
    plan = copy.deepcopy(original_plan)
    plan["segments"] = [{**copy.deepcopy(original_plan["segments"][0]), "id": f"s{i}", "duration_seconds": seconds,
        "shots": [{"start": 0, "end": seconds, "description": f"本段完整镜头{i + 1}"}],
        "reference_asset_ids": [asset["id"]]} for i, seconds in enumerate(durations)]
    return store, store.update_plan(project["id"], plan), plan, settings


@pytest.mark.parametrize("duration", [10, 15, 60])
def test_selected_duration_controls_manifest_source_trim_and_submission_budget(setup, fake_media, monkeypatch, duration):
    store, project, plan, settings = selected_duration_project(setup, duration)
    settings.max_video_submissions = 6
    gateway = FakeGateway(store, plan)
    reserve, budgets = store.reserve_submission, []
    def capture_budget(*args):
        budgets.append(args[-1])
        return reserve(*args)
    monkeypatch.setattr(store, "reserve_submission", capture_budget)
    result = execute(store, project, settings, gateway)
    count = (duration + 9) // 10
    assert result["status"] == "ready", result.get("error")
    assert result["submission_count"] == count and len(gateway.submissions) == gateway.downloads == count
    assert gateway.voices == 1 and budgets == [count] * count
    assert result["payload"]["video_submission_budget"] == count
    manifest = store.get_render(store.get_project(project["id"])["current_render_id"])["manifest"]
    assert manifest["total_frames"] == duration * 24 and len(manifest["segments"]) == count
    assert manifest["segments"][-1]["timeline_out_frame"] == duration * 24 and "cta" not in manifest
    for expected, segment in zip(plan["segments"], manifest["segments"]):
        assert segment["source_in_frame"] == 0 and segment["source_out_frame"] == expected["duration_seconds"] * 24
        source = store.get_asset(segment["asset_id"])
        assert source["metadata"]["video_duration_seconds"] == 10
        assert source["metadata"]["requested_duration_seconds"] == expected["duration_seconds"]
        assert store.get_step(result["id"], f"video:{expected['id']}")["snapshot"]["provider_duration_seconds"] == 10
    if duration == 15:
        assert manifest["segments"][-1]["source_out_frame"] == 120


def test_selected_duration_cannot_bypass_lower_configured_budget(setup, fake_media):
    store, project, plan, settings = selected_duration_project(setup, 60)
    settings.max_video_submissions = 2
    gateway = FakeGateway(store, plan)
    result = execute(store, project, settings, gateway)
    assert result["status"] == "failed" and "提交上限" in result["error"]
    assert not gateway.submissions and gateway.voices == gateway.downloads == 0


def test_generation_rejects_mismatched_duration_snapshot_before_spending(setup, fake_media):
    store, project, plan, settings = setup
    job = store.create_job(project["id"], "generate", project["revision"])
    payload = copy.deepcopy(job["payload"])
    payload["plan"]["segments"] = payload["plan"]["segments"][:1]
    job = store.update_job(job["id"], payload=payload)
    gateway = FakeGateway(store, plan)
    result = execute(store, project, settings, gateway, job)
    assert result["status"] == "failed" and "所选时长不一致" in result["error"]
    assert not gateway.submissions and gateway.voices == gateway.downloads == 0


def test_completed_bad_response_stops_polling_and_keeps_receipts(setup, fake_media):
    store, project, plan, settings = setup

    class CompletedResponseError(RuntimeError):
        retryable = False
        safe_to_display = True

    class BadResponseGateway(FakeGateway):
        async def poll_video(self, task_id):
            self.polls.append(task_id)
            raise CompletedResponseError("模型已完成但未返回可用的媒体地址")

    gateway = BadResponseGateway(store, plan)
    job = execute(store, project, settings, gateway)
    assert job["status"] == "failed"
    assert job["error"] == "模型已完成但未返回可用的媒体地址"
    # The malformed first result must not prevent collecting the other paid task.
    assert gateway.polls == ["remote-s0", "remote-s1"]
    assert len(gateway.submissions) == 2
    for segment in ("s0", "s1"):
        step = store.get_step(job["id"], f"video:{segment}")
        assert step["state"] == "submitted" and step["task_id"] == "remote-" + segment
    assert gateway.downloads == 0


def test_atomic_claim_and_submission_budget(setup):
    store, project, plan, settings = setup
    job = store.create_job(project["id"], "generate", project["revision"])
    with ThreadPoolExecutor(8) as pool:
        claims = list(pool.map(lambda _: store.claim_job(job["id"]), range(8)))
    assert sum(claims) == 1
    with ThreadPoolExecutor(8) as pool:
        reservations = list(pool.map(lambda _: store.reserve_submission(job["id"], "video:s0", {"test": True}, 2), range(8)))
    assert sum(reservations) == 1
    assert store.reserve_submission(job["id"], "video:s1", {}, 2)
    with pytest.raises(ValueError):
        store.reserve_submission(job["id"], "video:s2", {}, 2)
    assert store.get_job(job["id"])["submission_count"] == 2


@pytest.mark.parametrize("uncertain", [True, False])
def test_no_blind_post_after_uncertain_or_rejected(setup, fake_media, uncertain):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    gateway.uncertain, gateway.reject = uncertain, not uncertain
    job = execute(store, project, settings, gateway)
    assert job["status"] == ("submitted_unknown" if uncertain else "failed")
    execute(store, project, settings, gateway, job)
    assert len(gateway.submissions) == 1
    assert store.get_job(job["id"])["submission_count"] == 1
    assert store.get_step(job["id"], "video:s1") is None
    assert not gateway.polls
    assert store.create_job(project["id"], "generate", project["revision"])["id"] == job["id"]


def test_both_receipts_are_durable_before_first_poll(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    job = store.create_job(project["id"], "generate", project["revision"])
    events = []
    submit, poll = gateway.submit_video, gateway.poll_video

    async def ordered_submit(spec, assets, key):
        if spec["id"] == "s1":
            first = store.get_step(job["id"], "video:s0")
            assert first["state"] == "submitted" and first["task_id"] == "remote-s0"
        events.append("submit:" + spec["id"])
        return await submit(spec, assets, key)

    async def receipts_before_poll(task_id):
        for spec in plan["segments"]:
            receipt = store.get_step(job["id"], "video:" + spec["id"])
            assert receipt["state"] in ("submitted", "downloaded")
            assert receipt["task_id"] == "remote-" + spec["id"]
        events.append("poll:" + task_id)
        return await poll(task_id)

    gateway.submit_video, gateway.poll_video = ordered_submit, receipts_before_poll
    result = execute(store, project, settings, gateway, job)
    assert result["status"] == "ready"
    assert events[:3] == ["submit:s0", "submit:s1", "poll:remote-s0"]
    assert result["submission_count"] == 2


def test_revision_change_after_first_receipt_stops_second_submit(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    submit = gateway.submit_video

    async def change_revision(spec, assets, key):
        receipt = await submit(spec, assets, key)
        store.update_plan(project["id"], {**plan, "title": "新方案"})
        return receipt

    gateway.submit_video = change_revision
    result = execute(store, project, settings, gateway)
    assert result["status"] == "needs_review"
    assert len(gateway.submissions) == 1 and not gateway.polls
    assert store.get_step(result["id"], "video:s0")["task_id"] == "remote-s0"
    assert store.get_step(result["id"], "video:s1") is None


def test_pre_submitted_second_segment_is_reused_on_resume(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    job = store.create_job(project["id"], "generate", project["revision"])
    assert store.claim_job(job["id"])
    store.reserve_submission(job["id"], "video:s0", {"test": True}, 2)
    store.update_step(job["id"], "video:s0", state="submitted", task_id="remote-s0")
    pipeline = Pipeline(store, gateway, settings)
    second = plan["segments"][1]
    assets = pipeline._references(project["id"], plan)[second["id"]]
    first_receipt = asyncio.run(pipeline._submit_video(job, second, assets))
    again = asyncio.run(pipeline._submit_video(job, second, assets))
    assert first_receipt["task_id"] == again["task_id"] == "remote-s1"
    assert len(gateway.submissions) == 1 and not gateway.polls
    store.update_job(job["id"], status="queued")
    result = execute(store, project, settings, gateway, job)
    assert result["status"] == "ready" and result["submission_count"] == 2
    assert len(gateway.submissions) == 1


def test_download_retry_keeps_cloud_tasks_and_assets(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    gateway.fail_download_once = True
    job = execute(store, project, settings, gateway)
    assert job["status"] == "failed" and len(gateway.submissions) == 2
    job = execute(store, project, settings, gateway, job)
    assert job["status"] == "ready" and len(gateway.submissions) == 2
    assert gateway.submissions[0] != gateway.submissions[1]
    assert gateway.voices == 1
    assert gateway.polls.count("remote-s0") == 2
    assert store.create_job(project["id"], "generate", project["revision"])["id"] == job["id"]


def test_restart_queries_known_task_and_unknown_pauses(setup, fake_media):
    store, project, plan, settings = setup
    job = store.create_job(project["id"], "generate", project["revision"])
    assert store.claim_job(job["id"])
    store.reserve_submission(job["id"], "video:s0", {"test": True}, 2)
    store.update_step(job["id"], "video:s0", state="submitted", task_id="remote-s0")
    assert [j["id"] for j in store.recover_jobs()] == [job["id"]]
    gateway = FakeGateway(store, plan)
    result = execute(store, project, settings, gateway, job)
    assert result["status"] == "ready"
    assert len(gateway.submissions) == 1 and gateway.submissions[0].endswith(":s1")
    # New revision: kill in the unresolved POST window.
    p2 = store.update_plan(project["id"], plan)
    j2 = store.create_job(project["id"], "generate", p2["revision"])
    store.claim_job(j2["id"])
    store.reserve_submission(j2["id"], "video:s0", {}, 2)
    assert store.recover_jobs() == []
    assert store.get_job(j2["id"])["status"] == "submitted_unknown"


@pytest.mark.parametrize("kind", ["recompose", "cta"])
def test_recompose_and_legacy_cta_remove_tail_layer_with_zero_generation(setup, fake_media, kind):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    first = execute(store, project, settings, gateway)
    p = store.get_project(project["id"])
    before = store.get_render(p["current_render_id"])
    assert "cta" not in before["manifest"]
    # Simulate a persisted render created by the retired implementation.
    before["manifest"]["cta"] = "TEST_RETIRED_TAIL_TEXT"
    with store._connection(True) as connection:
        connection.execute("UPDATE renders SET manifest=? WHERE id=?", (json.dumps(before["manifest"]), before["id"]))
    old_asset = store.get_asset(before["asset_id"])
    old_bytes = Path(old_asset["path"]).read_bytes()
    job = store.create_job(p["id"], "recompose", p["revision"], {"base_render_id": before["id"]})
    if kind == "cta":
        # New CTA jobs are forbidden; this record represents a pre-upgrade failed job.
        legacy_payload = {**job["payload"], "text": "TEST_NEVER_RESTORE_TAIL_TEXT"}
        with store._connection(True) as connection:
            connection.execute("UPDATE jobs SET kind='cta',status='failed',payload=? WHERE id=?", (json.dumps(legacy_payload), job["id"]))
        job = store.get_job(job["id"])
    assert job["revision"] == p["revision"] + 1
    result = execute(store, project, settings, gateway, job)
    after = store.get_render(store.get_project(p["id"])["current_render_id"])
    assert result["status"] == "ready" and result["submission_count"] == 0
    assert len(gateway.submissions) == 2 and gateway.voices == 1 and gateway.downloads == 2
    assert after["manifest"]["segments"] == before["manifest"]["segments"]
    assert after["manifest"]["audio_asset_id"] == before["manifest"]["audio_asset_id"]
    assert after["manifest"]["captions"] == before["manifest"]["captions"]
    assert "cta" not in after["manifest"] and "cta" not in result["payload"]["manifest"]
    assert after["manifest"]["revision"] == job["revision"] and after["manifest"]["base_render_id"] == before["id"]
    assert after["asset_id"] != before["asset_id"] and Path(old_asset["path"]).read_bytes() == old_bytes
    assert store.get_render(before["id"])["manifest"] == before["manifest"]
    assert store.get_project(p["id"])["plan"]["script"] == plan["script"]


def test_late_render_cannot_replace_current_revision(setup, fake_media, monkeypatch):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    original_compose = media.compose

    def late(*args, **kwargs):
        store.update_plan(project["id"], {**plan, "title": "新的方案"})
        return original_compose(*args, **kwargs)

    monkeypatch.setattr(media, "compose", late)
    result = execute(store, project, settings, gateway)
    assert result["status"] == "needs_review"
    assert store.get_project(project["id"])["current_render_id"] is None
    assert store.project_detail(project["id"])["renders"][0]["status"] == "needs_review"


def test_cancel_after_acceptance_keeps_receipt_and_stops_next_post(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    gateway.cancel_on_submit = True
    result = execute(store, project, settings, gateway)
    assert result["status"] == "canceled"
    assert len(gateway.submissions) == 1
    assert store.get_step(result["id"], "video:s0")["task_id"] == "remote-s0"
    assert store.get_project(project["id"])["current_render_id"] is None


def test_missing_asset_never_spends(setup, fake_media):
    store, project, plan, settings = setup
    Path(store.get_asset(plan["segments"][1]["reference_asset_ids"][0])["path"]).unlink()
    gateway = FakeGateway(store, plan)
    result = execute(store, project, settings, gateway)
    assert result["status"] == "failed" and gateway.submissions == [] and gateway.voices == 0


def test_generated_designs_are_real_assets_and_original_refs_retained(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    extra = store.media_root / "unconfirmed_later_upload.png"
    extra.write_bytes(PNG)
    late_asset = store.add_asset(project["id"], "product", "后补图", extra, "image/png")

    async def designs(project, assets, plan, folder, cancelled=None):
        assert not cancelled()
        assert late_asset["id"] not in [a["id"] for a in assets]
        path = folder / "TEST_FAKE_DESIGN.png"
        path.write_bytes(PNG)
        return [{"source_path": path, "kind": "scene", "name": "TEST generated scene", "metadata": {"provider_url": "https://fixture.invalid/design.png"}}]

    gateway.create_designs = designs
    result = execute(store, project, settings, gateway)
    assert result["status"] == "ready"
    design_id = result["payload"]["design_asset_ids"][0]
    asset = store.get_asset(design_id)
    assert asset["metadata"]["generated"] and Path(asset["path"]).exists()
    for s in result["payload"]["plan"]["segments"]:
        assert design_id in s["reference_asset_ids"] and plan["segments"][0]["reference_asset_ids"][0] in s["reference_asset_ids"]


def test_design_cancellation_stops_video_posts(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)

    async def designs(project, assets, plan, folder, cancelled=None):
        assert not cancelled()
        store.request_cancel(project["id"])
        assert cancelled()
        raise RuntimeError("TEST canceled during image poll")

    gateway.create_designs = designs
    result = execute(store, project, settings, gateway)
    assert result["status"] == "canceled" and not gateway.submissions


@pytest.mark.parametrize("safe", [False, True])
def test_design_error_only_exposes_opted_in_safe_message(setup, fake_media, safe):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)

    async def designs(*args, **kwargs):
        exc = RuntimeError("TEST visible only when safe")
        exc.safe_to_display = safe
        raise exc

    gateway.create_designs = designs
    result = execute(store, project, settings, gateway)
    assert result["status"] == "failed" and not gateway.submissions
    assert ("TEST visible only when safe" in result["error"]) is safe


def test_compose_retry_reuses_both_segments_and_voice(setup, fake_media, monkeypatch):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    original = media.compose

    def broken(*args, **kwargs):
        raise media.MediaError("TEST compose failure")

    monkeypatch.setattr(media, "compose", broken)
    result = execute(store, project, settings, gateway)
    assert result["status"] == "failed" and len(gateway.submissions) == 2
    monkeypatch.setattr(media, "compose", original)
    result = execute(store, project, settings, gateway, result)
    assert result["status"] == "ready" and len(gateway.submissions) == 2
    assert gateway.voices == 1 and gateway.downloads == 2


def test_changed_segment_cannot_silently_replace_recompose_source(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)
    execute(store, project, settings, gateway)
    p = store.get_project(project["id"])
    before = store.get_render(p["current_render_id"])
    source = store.get_asset(before["manifest"]["segments"][0]["asset_id"])
    Path(source["path"]).write_bytes(b"TEST_MUTATED_SOURCE")
    job = store.create_job(p["id"], "recompose", p["revision"], {"base_render_id": before["id"]})
    result = execute(store, project, settings, gateway, job)
    assert result["status"] == "failed" and "已改变" in result["error"]
    assert store.get_project(p["id"])["current_render_id"] == before["id"]
    assert len(gateway.submissions) == 2 and gateway.voices == 1


def test_invalid_measured_caption_never_submits_video(setup, fake_media):
    store, project, plan, settings = setup
    gateway = FakeGateway(store, plan)

    async def voice(text, destination, *, target_duration_seconds=None):
        destination.write_bytes(b"TEST_FAKE_AUDIO")
        return {"provider": "TEST", "captions": [{"start": .9, "end": .2, "text": "invalid"}]}

    gateway.synthesize_voice = voice
    result = execute(store, project, settings, gateway)
    assert result["status"] == "failed" and "时轴" in result["error"]
    assert not gateway.submissions


def test_ass_escapes_override_injection(tmp_path):
    path = tmp_path / "captions.ass"
    media.write_ass(path, [{"start_frame": 0, "end_frame": 24, "text": r"{\pos(0,0)}商品"}], 48)
    text = path.read_text()
    assert r"{\pos" not in text
    assert "｛＼pos(0,0)｝商品" in text


def test_real_ffmpeg_trims_ten_second_sources_to_fifteen_without_tail_layer(tmp_path):
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    features = subprocess.run([ffmpeg, "-filters"], capture_output=True, text=True).stdout
    if " ass " not in features:
        pytest.skip("This FFmpeg build has no ASS filter")
    paths = {}
    for i, color in enumerate(("red", "blue")):
        path = tmp_path / f"fixture{i}.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                        f"color={color}:s=180x320:r=24:d=10", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
        paths[f"s{i}"] = path
    audio = tmp_path / "test_silence.wav"
    with wave.open(str(audio), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(44100)
        f.writeframes(b"\0\0" * 66150)
    paths["voice"] = audio
    manifest = {"fps": 24, "width": 720, "height": 1280, "total_frames": 360, "audio_strategy": "voiceover",
                "audio_asset_id": "voice", "captions": [{"start_frame": 0, "end_frame": 36, "text": "测试商品，中文覆盖层。"}],
                "cta": "了解详情", "segments": [{"asset_id": "s0", "source_in_frame": 0, "source_out_frame": 240,
                                                  "timeline_in_frame": 0, "timeline_out_frame": 240},
                                                 {"asset_id": "s1", "source_in_frame": 0, "source_out_frame": 120,
                                                  "timeline_in_frame": 240, "timeline_out_frame": 360}]}
    result = media.compose(manifest, paths, tmp_path / "export" / "result.mp4", ffmpeg)
    assert result["frames"] == 360 and result["duration_seconds"] == 15 and result["has_audio"] and result["width"] == 720
    saved_manifest = json.loads((tmp_path / "export" / "manifest.json").read_text())
    assert "cta" not in saved_manifest and manifest["cta"] == "了解详情"
    captions = (tmp_path / "export" / "compose_work" / "captions.ass").read_text()
    assert "测试商品，中文覆盖层。" in captions
    assert "Style: CTA" not in captions and ",CTA," not in captions and "了解详情" not in captions


@pytest.mark.skipif(not Path("/usr/bin/say").exists(), reason="Real local speech integration requires macOS")
def test_real_macos_gateway_voice_through_pipeline_with_explicit_video_fixtures(setup, tmp_path, monkeypatch):
    """Real Chinese TTS + real compose; video provider is explicitly replaced with fixtures."""
    import imageio_ffmpeg
    import httpx
    from app.providers import Gateway

    # This test must never make a paid or network request, even after future refactors.
    async def no_network(*args, **kwargs):
        raise AssertionError("External HTTP is forbidden in the local audio integration test")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_network)
    voices = subprocess.run(["/usr/bin/say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    if not any(line.startswith("Tingting ") for line in voices.splitlines()):
        pytest.skip("Tingting voice is not installed")
    store, project, plan, settings = setup
    settings.ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    settings.tts_voice = "Tingting"
    settings.provider_timeout_seconds = 60
    plan["script"] = "这是本地集成测试。请核对中文字幕。"
    for segment in plan["segments"]:
        segment["duration_seconds"] = 10
        segment["shots"] = [{"start": 0, "end": 10, "description": "TEST fixture, not AI video"}]
    project = store.update_plan(project["id"], plan)
    fixture = tmp_path / "EXPLICIT_TEST_VIDEO_ONLY.mp4"
    subprocess.run([settings.ffmpeg_path, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                    "color=navy:s=180x320:r=24:d=10", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(fixture)], check=True)
    gateway = FakeGateway(store, plan)
    real_gateway = Gateway(settings)

    async def voice(text, destination, *, target_duration_seconds=None):
        gateway.voices += 1
        return await real_gateway.synthesize_voice(text, destination, target_duration_seconds=target_duration_seconds)

    async def download(url, destination):
        gateway.downloads += 1
        shutil.copyfile(fixture, destination)

    gateway.synthesize_voice = voice
    gateway.download_video = download
    result = execute(store, project, settings, gateway)
    assert result["status"] == "ready", result.get("error")
    assert len(gateway.submissions) == 2 and gateway.voices == 1
    audio = store.get_asset(result["payload"]["audio_asset_id"])
    with wave.open(audio["path"], "rb") as handle:
        duration = handle.getnframes() / handle.getframerate()
        pcm = handle.readframes(handle.getnframes())
    assert duration > 1 and any(pcm)
    assert audio["metadata"]["alignment"] == "measured_sentence_timing"
    cues = audio["metadata"]["captions"]
    assert len(cues) == 2 and cues[0]["start_frame"] == 0
    assert abs(cues[-1]["end_frame"] / 24 - duration) <= 1 / 24
    render = store.get_render(store.get_project(project["id"])["current_render_id"])
    exported = store.get_asset(render["asset_id"])
    info = media.probe(Path(exported["path"]), settings.ffmpeg_path)
    assert info["frames"] == 480 and info["has_audio"] and info["duration_seconds"] == 20
    assert exported["metadata"]["manual_review_required"]
