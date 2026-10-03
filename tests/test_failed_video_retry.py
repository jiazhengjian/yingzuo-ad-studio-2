"""Fault injection for terminal failures; no network or production media."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import StaleRevision
from app.main import create_app
from app.pipeline import Pipeline
from app.schemas import RetryInput
from test_pipeline import PNG, FakeGateway, execute, fake_media, setup


class FailedVideoGateway(FakeGateway):
    def __init__(self, store, plan, failing=("remote-s0",), code="reference_image_unreadable"):
        super().__init__(store, plan)
        self.failing = set(failing)
        self.code = code

    async def poll_video(self, task_id):
        if task_id in self.failing:
            self.polls.append(task_id)
            return {"status": "failed", "error_code": self.code,
                    "error": "UNTRUSTED PROVIDER RAW TEXT https://private.invalid/?secret=do-not-persist"}
        return await super().poll_video(task_id)


@pytest.fixture
def prepared(setup):
    store, project, plan, settings = setup
    path = store.media_root / "confirmed-scene.png"
    path.write_bytes(PNG)
    scene = store.add_asset(project["id"], "scene", "已确认场景", path, "image/png", project["revision"], {"generated": True})
    job = store.create_job(project["id"], "generate", project["revision"], payload={
        "pause_for_creative": True, "creative_confirmed": True, "design_asset_ids": [scene["id"]]})
    return store, project, plan, settings, job


def run(prepared, gateway, job=None):
    store, project, _, settings, original = prepared
    return execute(store, project, settings, gateway, job or original)


def api(prepared, gateway):
    store, _, _, settings, _ = prepared
    config = SimpleNamespace(**vars(settings), database_path=store.db_path, media_root=store.media_root,
                             data_root=store.db_path.parent, tudou_api_key="TEST_ONLY_NOT_REAL", deepseek_api_key="")
    return TestClient(create_app(config, gateway, run_worker=False))


def seed_receipts(prepared, states):
    store, _, _, _, job = prepared
    assert store.claim_job(job["id"])
    for index, state in enumerate(states):
        if state is None:
            continue
        key = f"video:s{index}"
        assert store.reserve_submission(job["id"], key, {"fixture": True}, 2)
        store.update_step(job["id"], key, state=state,
                          task_id=f"remote-s{index}" if state not in ("submitting", "submitted_unknown", "rejected") else None)
    return store.update_job(job["id"], status="failed")


def test_first_terminal_failure_still_downloads_second_segment(prepared, fake_media):
    store, _, plan, _, job = prepared
    gateway = FailedVideoGateway(store, plan)
    result = run(prepared, gateway)
    assert result["status"] == "failed"
    assert gateway.polls == ["remote-s0", "remote-s1"]
    assert gateway.downloads == 1 and len(gateway.submissions) == 2
    assert store.get_step(job["id"], "video:s1")["state"] == "downloaded"
    assert result["progress"]["failed_segment_ids"] == ["s0"]
    assert result["progress"]["retry_action"] == "retry_failed_segments"
    assert result["progress"]["error_code"] == "reference_image_unreadable"
    assert "UNTRUSTED" not in str(result) + str(store.get_step(job["id"], "video:s0"))
    assert not store.project_detail(job["project_id"])["renders"]


def test_explicit_child_only_retries_failure_and_reuses_scene_voice_success(prepared, fake_media):
    store, project, plan, _, original = prepared
    gateway = FailedVideoGateway(store, plan)
    failed = run(prepared, gateway)
    old_step = copy.deepcopy(store.get_step(original["id"], "video:s0"))
    successful = store.get_step(original["id"], "video:s1")
    child = store.create_failed_video_retry(project["id"], original["id"])
    assert child["id"] != original["id"] and child["revision"] == original["revision"]
    assert child["payload"]["plan"] == failed["payload"]["plan"]
    assert child["payload"]["design_asset_ids"] == failed["payload"]["design_asset_ids"]
    assert child["progress"]["design_asset_ids"] == failed["payload"]["design_asset_ids"]
    assert child["payload"]["video_submission_budget"] == 1 and child["submission_count"] == 0
    assert store.get_step(child["id"], "video:s0") is None
    assert store.get_step(child["id"], "video:s1")["result"] == successful["result"]
    gateway.failing.clear()
    result = run(prepared, gateway, child)
    assert result["status"] == "ready" and result["submission_count"] == 1
    assert len(gateway.submissions) == 3 and gateway.submissions[-1] == child["id"] + ":s0"
    assert gateway.voices == 1 and gateway.downloads == 2
    assert store.get_step(original["id"], "video:s0") == old_step
    assert store.get_job(original["id"])["status"] == "failed"
    assert store.create_failed_video_retry(project["id"], original["id"])["id"] == child["id"]


def test_retry_double_click_allocates_one_child(prepared, fake_media):
    store, project, plan, _, job = prepared
    run(prepared, FailedVideoGateway(store, plan))
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda _: store.create_failed_video_retry(project["id"], job["id"])["id"], range(8)))
    assert len(set(ids)) == 1
    assert len(store.project_detail(project["id"])["jobs"]) == 2


def test_submitted_receipt_is_reused_not_resubmitted(prepared, fake_media):
    store, project, plan, _, job = prepared
    seed_receipts(prepared, ("failed", "submitted"))
    child = store.create_failed_video_retry(project["id"], job["id"])
    gateway = FailedVideoGateway(store, plan, failing=())
    result = run(prepared, gateway, child)
    assert result["status"] == "ready"
    assert gateway.submissions == [child["id"] + ":s0"]
    assert "remote-s1" in gateway.polls


@pytest.mark.parametrize("state", [None, "submitting", "submitted_unknown"])
def test_unsubmitted_or_uncertain_step_cannot_get_new_budget(prepared, state):
    store, project, _, _, job = prepared
    seed_receipts(prepared, ("failed", state))
    with pytest.raises(StaleRevision):
        store.create_failed_video_retry(project["id"], job["id"])
    assert len(store.project_detail(project["id"])["jobs"]) == 1


@pytest.mark.parametrize("field", ["revision", "context_version"])
def test_stale_context_or_revision_blocks_failed_retry(prepared, field):
    store, project, _, _, job = prepared
    seed_receipts(prepared, ("failed", "submitted"))
    with store._connection(True) as connection:
        connection.execute(f"UPDATE projects SET {field}={field}+1 WHERE id=?", (project["id"],))
    with pytest.raises(StaleRevision):
        store.create_failed_video_retry(project["id"], job["id"])


def test_old_source_cannot_mint_another_budget_when_child_fails(prepared, fake_media):
    store, project, plan, _, job = prepared
    gateway = FailedVideoGateway(store, plan)
    run(prepared, gateway)
    child = store.create_failed_video_retry(project["id"], job["id"])
    assert run(prepared, gateway, child)["status"] == "failed"
    assert store.create_failed_video_retry(project["id"], job["id"])["id"] == child["id"]
    grandchild = store.create_failed_video_retry(project["id"], child["id"])
    assert grandchild["id"] != child["id"]
    assert grandchild["payload"]["video_submission_budget"] == 1


def test_normal_retry_rejects_terminal_failure_and_explicit_api_creates_child(prepared, fake_media):
    store, project, plan, _, job = prepared
    gateway = FailedVideoGateway(store, plan)
    run(prepared, gateway)
    client = api(prepared, gateway)
    url = f"/api/projects/{project['id']}/retry"
    assert client.post(url, json={"job_id": job["id"]}).status_code == 409
    response = client.post(url, json={"job_id": job["id"], "retry_failed_segments": True})
    assert response.status_code == 200
    child_id = response.json()["job"]["id"]
    assert child_id != job["id"] and len(gateway.submissions) == 2
    assert client.post(url, json={"job_id": job["id"], "retry_failed_segments": True}).json()["job"]["id"] == child_id


def test_normal_retry_collects_pending_without_resubmitting_failed(prepared, fake_media):
    store, project, plan, _, job = prepared
    seed_receipts(prepared, ("failed", "submitted"))
    gateway = FailedVideoGateway(store, plan, failing=())
    client = api(prepared, gateway)
    response = client.post(f"/api/projects/{project['id']}/retry", json={"job_id": job["id"]})
    assert response.status_code == 200
    result = run(prepared, gateway, store.get_job(job["id"]))
    assert result["status"] == "failed" and not gateway.submissions
    assert gateway.polls == ["remote-s1"]
    assert result["progress"]["retry_action"] == "retry_failed_segments"


def test_invalid_error_code_is_not_exposed(prepared, fake_media):
    store, _, plan, _, job = prepared
    result = run(prepared, FailedVideoGateway(store, plan, code="TOKEN-DO-NOT-SAVE"))
    assert result["progress"]["error_code"] == "video_generation_failed"
    assert store.get_step(job["id"], "video:s0")["result"]["error_code"] == "provider_video_failed"


def test_retry_opt_in_requires_boolean():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RetryInput(job_id="job", retry_failed_segments="true")


def test_retry_reservation_rejects_non_failed_segment(prepared):
    store, project, _, _, job = prepared
    seed_receipts(prepared, ("failed", "submitted"))
    child = store.create_failed_video_retry(project["id"], job["id"])
    assert store.claim_job(child["id"])
    with pytest.raises(ValueError, match="只批准"):
        store.reserve_submission(child["id"], "video:unapproved", {}, 2)


def test_second_submission_rejected_still_collects_first(prepared, fake_media):
    store, _, plan, _, job = prepared

    class RejectSecond(FailedVideoGateway):
        async def submit_video(self, segment, assets, idempotency_key):
            if segment["id"] == "s1":
                error = ValueError("fixture local preflight")
                error.uncertain = False
                error.error_code = "reference_image_unreadable"
                raise error
            return await super().submit_video(segment, assets, idempotency_key)

    gateway = RejectSecond(store, plan, failing=())
    result = run(prepared, gateway)
    assert result["status"] == "failed" and gateway.polls == ["remote-s0"]
    assert store.get_step(job["id"], "video:s0")["state"] == "downloaded"
    rejected = store.get_step(job["id"], "video:s1")
    assert rejected["state"] == "rejected"
    assert rejected["result"]["error_code"] == "reference_image_unreadable"
    assert rejected["error"] == "参考图片检查未通过，视频尚未提交"
    assert result["progress"]["retry_action"] == "retry_failed_segments"


def test_download_error_in_first_still_saves_second(prepared, fake_media):
    store, _, plan, _, job = prepared
    gateway = FailedVideoGateway(store, plan, failing=())
    gateway.fail_download_once = True
    result = run(prepared, gateway)
    assert result["status"] == "failed"
    assert store.get_step(job["id"], "video:s1")["state"] == "downloaded"
    assert len(gateway.submissions) == 2


def test_second_submission_unknown_still_collects_first_without_resubmission(prepared, fake_media):
    store, project, plan, _, job = prepared

    class UnknownSecond(FailedVideoGateway):
        async def submit_video(self, segment, assets, idempotency_key):
            self.uncertain = segment["id"] == "s1"
            return await super().submit_video(segment, assets, idempotency_key)

    gateway = UnknownSecond(store, plan, failing=())
    result = run(prepared, gateway)
    assert result["status"] == "submitted_unknown"
    assert result["phase"] == "needs_reconcile"
    assert gateway.polls == ["remote-s0"] and gateway.downloads == 1
    assert store.get_step(job["id"], "video:s0")["state"] == "downloaded"
    unknown = store.get_step(job["id"], "video:s1")
    assert unknown["state"] == "submitted_unknown" and unknown["task_id"] is None
    assert not store.project_detail(project["id"])["renders"]
    assert len(gateway.submissions) == 2
    run(prepared, gateway, result)
    assert len(gateway.submissions) == 2
    assert store.get_step(job["id"], "video:s1") == unknown
    with pytest.raises(StaleRevision):
        store.create_failed_video_retry(project["id"], job["id"])
