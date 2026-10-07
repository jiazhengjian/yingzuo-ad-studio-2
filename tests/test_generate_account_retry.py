"""Chat and button retries after an unpaid account refusal. No provider calls."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import StaleRevision
from app.generate_retry import QUEUED_REPLY, is_generate_retry_intent, no_queue
from app.main import create_app
from test_failed_video_retry import prepared
from test_pipeline import setup  # noqa: F401  # fixture dependency of prepared


def _fail(prepared, code="provider_auth", state="rejected", status="failed", task_id=None, result_code=None):
    store, project, _, _, job = prepared
    assert store.claim_job(job["id"])
    assert store.reserve_submission(job["id"], "video:s0", {"fixture": True}, 2)
    store.update_step(job["id"], "video:s0", state=state, task_id=task_id,
                      result={"error_code": result_code or code, "provider_status": 401})
    progress = {"error_code": code, "retryable": False, "retry_action": None,
                "design_asset_ids": job["payload"]["design_asset_ids"]}
    store.update_job(job["id"], status=status, error="卡片", progress=progress)
    return store, store.get_project(project["id"]), job


def _turn(store, project, content, turn_id="chat-1"):
    before = project["context_version"]
    job = store.create_turn(project["id"], {
        "client_turn_id": turn_id,
        "expected_context_version": before,
        "content": content,
    })
    detail = store.project_detail(project["id"])
    return job, detail, before


def test_retry_phrases_are_only_the_short_ones():
    for text in ("继续生成", "请继续生成。", "重新生成一下", "再试一次吧", "麻烦重新生成视频"):
        assert is_generate_retry_intent(text)
    for text in ("继续生成，把开头改短", "请重新生成方案", "再试一次，换成户外", "你好"):
        assert not is_generate_retry_intent(text)


@pytest.mark.parametrize("text", ["继续生成", "重新生成", "再试一次"])
def test_chat_after_account_failure_queues_the_same_video(prepared, text):
    store, project, job = _fail(prepared)
    plan = json.loads(json.dumps(job["payload"]["plan"]))
    images = list(job["payload"]["design_asset_ids"])
    queued, detail, context = _turn(store, project, text)
    assert queued["id"] == job["id"] and queued["status"] == "queued"
    assert queued["submission_count"] == 0
    assert store.get_step(job["id"], "video:s0") is None
    assert queued["payload"]["plan"] == plan and queued["payload"]["design_asset_ids"] == images
    assert store.get_project(project["id"])["context_version"] == context
    assert store.get_project(project["id"])["revision"] == project["revision"]
    assert all(item["kind"] != "plan" for item in detail["jobs"])
    assistant = [item["content"] for item in detail["messages"] if item["role"] == "assistant"]
    assert assistant == [QUEUED_REPLY]
    assert "会继续推进" not in assistant[0]
    again = store.create_turn(project["id"], {
        "client_turn_id": "chat-1", "expected_context_version": context, "content": text})
    assert again["id"] == job["id"]
    assert [item["content"] for item in store.project_detail(project["id"])["messages"] if item["role"] == "assistant"] == [QUEUED_REPLY]


def test_chat_refuses_when_the_submission_is_unknown_or_rejected(prepared):
    store, project, job = _fail(prepared, code="provider_auth", state="submitted_unknown", status="submitted_unknown")
    returned, detail, context = _turn(store, project, "继续生成")
    assert returned["id"] == job["id"] and returned["status"] == "submitted_unknown"
    assert store.get_step(job["id"], "video:s0")["state"] == "submitted_unknown"
    assert store.get_project(project["id"])["context_version"] == context
    assert all(item["kind"] != "plan" for item in detail["jobs"])
    text = detail["messages"][-1]["content"]
    assert detail["messages"][-1]["role"] == "assistant"
    assert "没有新的任务排队" in text
    assert "已重新排队" not in text and "会继续推进" not in text and "会继续这次制作" not in text


def test_chat_does_not_resubmit_a_rejected_request(prepared):
    store, project, job = _fail(prepared, code="provider_request_rejected", result_code="provider_request_rejected")
    returned, detail, _ = _turn(store, project, "重新生成")
    assert returned["status"] == "failed" and returned["submission_count"] == 1
    assert store.get_step(job["id"], "video:s0")["state"] == "rejected"
    text = [item["content"] for item in detail["messages"] if item["role"] == "assistant"][-1]
    assert "没有新的任务排队" in text and "已重新排队" not in text
    assert all(item["kind"] != "plan" for item in detail["jobs"])


def test_a_longer_request_still_goes_to_planning(prepared):
    store, project, job = _fail(prepared)
    queued, detail, context = _turn(store, project, "继续生成，把开头改短")
    assert queued["kind"] == "plan" and queued["id"] != job["id"]
    assert store.get_job(job["id"])["status"] == "failed"
    assert store.get_step(job["id"], "video:s0")["state"] == "rejected"
    assert store.get_project(project["id"])["context_version"] == context + 1
    assert not any(item["role"] == "assistant" and "已重新排队" in item["content"] for item in detail["messages"])


def test_paid_task_and_unknown_image_are_not_dropped(prepared):
    store, project, job = _fail(prepared, task_id="remote-kept")
    with pytest.raises(StaleRevision):
        store.retry_generate_job(project["id"], job["id"])
    assert store.get_step(job["id"], "video:s0")["task_id"] == "remote-kept"
    assert store.get_job(job["id"])["status"] == "failed"

    payload = dict(store.get_job(job["id"])["payload"])
    payload["creative_confirmed"] = False
    store.update_job(job["id"], payload=payload)
    # The paid step still blocks before any picture receipt is touched.
    folder = store.media_root / project["id"] / job["id"] / "designs"
    folder.mkdir(parents=True)
    receipt = folder / "scene_task.json"
    receipt.write_text(json.dumps({"state": "failed", "task_id": "image-paid", "error_code": "provider_auth"}), encoding="utf-8")
    with pytest.raises(StaleRevision):
        store.retry_generate_job(project["id"], job["id"])
    assert json.loads(receipt.read_text(encoding="utf-8"))["task_id"] == "image-paid"


def test_a_finished_segment_stays_when_the_unpaid_account_step_is_released(prepared):
    store, project, job = _fail(prepared)
    store.update_job(job["id"], status="running")
    assert store.reserve_submission(job["id"], "video:s1", {"fixture": True}, 2)
    store.update_step(job["id"], "video:s1", state="downloaded", task_id="remote-s1",
                      result={"asset_id": "asset-kept"})
    store.update_job(job["id"], status="failed",
                     progress={"error_code": "provider_auth", "retryable": False, "retry_action": None})
    retried = store.retry_generate_job(project["id"], job["id"])
    assert retried["status"] == "queued" and retried["submission_count"] == 1
    assert store.get_step(job["id"], "video:s0") is None
    kept = store.get_step(job["id"], "video:s1")
    assert kept["state"] == "downloaded" and kept["task_id"] == "remote-s1"
    assert retried["payload"]["plan"] == job["payload"]["plan"]


def test_unpaid_image_receipt_is_cleared_only_by_an_explicit_retry(prepared):
    store, project, _, _, job = prepared
    payload = dict(job["payload"])
    payload["creative_confirmed"] = False
    payload["designs_directory"] = str(store.media_root / project["id"] / job["id"] / "designs")
    store.update_job(job["id"], status="failed", payload=payload,
                     progress={"error_code": "provider_balance", "retryable": True, "retry_action": "retry_existing"})
    folder = store.media_root / project["id"] / job["id"] / "designs"
    folder.mkdir(parents=True)
    receipt = folder / "scene_task.json"
    receipt.write_text(json.dumps({"state": "failed", "error_code": "provider_balance"}), encoding="utf-8")
    kept = folder / "scene_done_task.json"
    kept.write_text(json.dumps({"state": "completed", "task_id": "kept", "url": "https://example.invalid/a.png"}), encoding="utf-8")
    retried = store.retry_generate_job(project["id"], job["id"])
    assert retried["status"] == "queued" and retried["id"] == job["id"]
    assert not receipt.exists() and kept.exists()
    assert retried["payload"]["plan"] == job["payload"]["plan"]


def test_retry_and_generate_accept_the_account_failure(prepared):
    store, project, job = _fail(prepared, code="provider_balance")
    images = list(job["payload"]["design_asset_ids"])
    config = SimpleNamespace(database_path=store.db_path, media_root=store.media_root, data_root=store.db_path.parent,
                             tudou_api_key="TEST_ONLY_NOT_REAL", deepseek_api_key="TEST_ONLY_NOT_REAL",
                             ffmpeg_path="unused", max_video_submissions=2, max_active_runs=1, poll_seconds=0.01,
                             provider_timeout_seconds=1, tts_voice="TEST_ONLY", video_model="TEST_ONLY",
                             presenter_verified=False)
    client = TestClient(create_app(config, run_worker=False))
    response = client.post(f"/api/projects/{project['id']}/retry", json={"job_id": job["id"]})
    assert response.status_code == 200, response.text
    body = response.json()["job"]
    assert body["id"] == job["id"] and body["status"] == "queued" and body["submission_count"] == 0
    assert store.get_job(job["id"])["payload"]["design_asset_ids"] == images
    assert store.get_step(job["id"], "video:s0") is None

    store.update_job(job["id"], status="queued")
    assert store.claim_job(job["id"])
    assert store.reserve_submission(job["id"], "video:s0", {"fixture": True}, 2)
    store.update_step(job["id"], "video:s0", state="rejected", result={"error_code": "provider_balance"})
    store.update_job(job["id"], status="failed", error="卡片",
                     progress={"error_code": "provider_balance", "retryable": True, "retry_action": "retry_existing",
                               "design_asset_ids": images})
    generated = client.post(f"/api/projects/{project['id']}/generate",
                            json={"accepted_revision": project["revision"], "pause_for_creative": True})
    assert generated.status_code == 200, generated.text
    again = generated.json()["job"]
    assert again["id"] == job["id"] and again["status"] == "queued"
    assert store.get_job(job["id"])["payload"]["design_asset_ids"] == images
    assert store.get_step(job["id"], "video:s0") is None
    assert store.get_project(project["id"])["revision"] == project["revision"]
    turn = client.post(f"/api/projects/{project['id']}/turns", json={
        "client_turn_id": "from-http", "expected_context_version": project["context_version"], "content": "继续生成"})
    assert turn.status_code == 409
    assert "没有" in turn.json()["detail"] or "仍在进行" in turn.json()["detail"]


def test_refusal_helper_never_claims_a_queue():
    text = no_queue("存在受理状态未知的片段，请先核对原任务")
    assert text.endswith("没有新的任务排队。")
    assert "会继续" not in text
