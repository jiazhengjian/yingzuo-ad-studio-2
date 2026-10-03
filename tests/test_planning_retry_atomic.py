"""Concurrent retries may not rewind a durable planner request or its budget."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier, Event, Thread, current_thread

import pytest

from app.db import Store, StaleRevision
from app.planning_session import fingerprint


@pytest.fixture
def failed_plan(tmp_path):
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    project = store.create_project("蛋糕广告", "product")
    job = store.create_turn(project["id"], {
        "client_turn_id": "plan-1", "expected_context_version": 0, "content": "优化广告方案"})
    checkpoint = {"version": 1, "scope": fingerprint({"project": project["id"]}),
        "request_count": 4, "transient_retries": 2, "empty_retries": 0, "manual_retries": 0,
        "entries": [
            {"stage": "strategy", "input_hash": fingerprint("strategy"), "state": "ready",
             "attempts": 1, "result": {"completed": "saved direction"}},
            {"stage": "ad_script", "input_hash": fingerprint("ad_script"), "state": "terminal",
             "attempts": 3, "failure": {"message": "连接暂时中断", "error_code": "planning_request_failed",
                                        "retryable": True, "retry_action": "retry_planning"}}]}
    job = store.update_job(job["id"], status="failed", error="连接暂时中断",
        payload={**job["payload"], "planning_checkpoint": checkpoint},
        progress={"error_code": "planning_request_failed", "retryable": True,
                  "retry_action": "retry_planning", "issue_summary": [], "completed_stages": 1})
    store.set_project_status(project["id"], job["revision"], "failed")
    return store, project["id"], job


def test_retry_preserves_completed_calls_and_extends_only_manual_budget(failed_plan):
    store, pid, job = failed_plan
    before = deepcopy(job["payload"]["planning_checkpoint"])
    retried = store.retry_planning_job(pid, job["id"])
    checkpoint = retried["payload"]["planning_checkpoint"]
    assert retried["status"] == retried["phase"] == "queued"
    assert retried["error"] is None and retried["lease_until"] == 0
    assert retried["progress"] == {"completed_stages": 1}
    assert checkpoint["entries"][0] == before["entries"][0]
    assert checkpoint["entries"][1]["state"] == "pending"
    assert checkpoint["entries"][1]["attempts"] == 3
    assert "failure" not in checkpoint["entries"][1]
    assert checkpoint["request_count"] == before["request_count"]
    assert checkpoint["transient_retries"] == before["transient_retries"]
    assert checkpoint["manual_retries"] == 1
    assert store.get_project(pid)["work_status"] == "queued"
    events = store.project_detail(pid)["events"]
    assert events[-1]["job_id"] == job["id"] and events[-1]["state"] == "waiting"


def test_simultaneous_retry_requests_enqueue_and_extend_budget_once(failed_plan):
    store, pid, job = failed_plan
    barrier = Barrier(2)

    def retry():
        barrier.wait(timeout=5)
        try:
            return store.retry_planning_job(pid, job["id"])["status"]
        except StaleRevision:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(retry) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == ["conflict", "queued"]
    final = store.get_job(job["id"])
    assert final["payload"]["planning_checkpoint"]["manual_retries"] == 1
    assert final["payload"]["planning_checkpoint"]["request_count"] == 4
    queued_events = [event for event in store.project_detail(pid)["events"]
                     if event["label"] == "策划已重新排队，将继续未完成的步骤"]
    assert len(queued_events) == 1


def test_delayed_duplicate_cannot_overwrite_workers_new_checkpoint(failed_plan, monkeypatch):
    store, pid, job = failed_plan
    original_connection = store._connection
    duplicate_waiting, release_duplicate = Event(), Event()
    outcome = []

    @contextmanager
    def delayed_connection(write=False):
        if write and current_thread().name == "delayed-retry":
            duplicate_waiting.set()
            assert release_duplicate.wait(timeout=5)
        with original_connection(write) as connection:
            yield connection

    monkeypatch.setattr(store, "_connection", delayed_connection)

    def duplicate():
        try:
            store.retry_planning_job(pid, job["id"])
        except StaleRevision:
            outcome.append("conflict")

    thread = Thread(target=duplicate, name="delayed-retry")
    thread.start()
    try:
        assert duplicate_waiting.wait(timeout=5)
        store.retry_planning_job(pid, job["id"])
        assert store.claim_job(job["id"])
        running = store.get_job(job["id"])
        checkpoint = deepcopy(running["payload"]["planning_checkpoint"])
        checkpoint["request_count"] += 1
        checkpoint["entries"][-1].update(state="running", attempts=4)
        store.update_job(job["id"], payload={**running["payload"], "planning_checkpoint": checkpoint})
    finally:
        release_duplicate.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and outcome == ["conflict"]
    final = store.get_job(job["id"])
    assert final["status"] == "running"
    assert final["payload"]["planning_checkpoint"] == checkpoint
    assert final["payload"]["planning_checkpoint"]["request_count"] == 5


@pytest.mark.parametrize("change", ["context", "revision", "canceled", "busy", "content_failure", "no_checkpoint"])
def test_retry_rechecks_current_state_without_mutating_on_rejection(failed_plan, change):
    store, pid, job = failed_plan
    if change == "context":
        store.add_message(pid, "user", "新增要求")
    elif change == "revision":
        store.update_plan(pid, {"segments": [{"duration_seconds": 10}, {"duration_seconds": 10}]})
    elif change == "canceled":
        with store._connection(True) as c:
            c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job["id"],))
    elif change == "busy":
        store.create_job(pid, "plan", job["revision"])
    elif change == "content_failure":
        store.update_job(job["id"], progress={"retryable": False, "retry_action": "edit_plan"})
    elif change == "no_checkpoint":
        store.update_job(job["id"], payload={key: value for key, value in job["payload"].items()
                                            if key != "planning_checkpoint"})
    before = store.project_detail(pid)
    with pytest.raises(StaleRevision):
        store.retry_planning_job(pid, job["id"])
    assert store.project_detail(pid) == before


def test_retry_cannot_target_another_project(failed_plan):
    store, pid, job = failed_plan
    other = store.create_project("其他项目", "product")
    before = store.project_detail(pid)
    with pytest.raises(KeyError):
        store.retry_planning_job(other["id"], job["id"])
    assert store.project_detail(pid) == before
