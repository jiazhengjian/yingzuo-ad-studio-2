"""Durable text recovery through the real API/worker/adapter with offline HTTP."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from app.db import Store
from app.pipeline import Pipeline
from app.providers.gateway import Gateway
from app.strategy import StrategyRecord
from test_conversation_api import env
from test_direction_selection import offer, select, isolated_skill_loader
from test_progressive_planning import director_output, accepted_review, reject_first_shot
from test_providers import chat_output, request_context


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Recovery tests must not use real network or media providers")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    for name in ("create_designs", "submit_video", "synthesize_voice"):
        monkeypatch.setattr(Gateway, name, denied)


@pytest.fixture
def run_case(env, monkeypatch, isolated_skill_loader):
    client, app, pid, asset = env
    settings = app.state.settings
    settings.direction_selection_enabled = settings.progressive_planning_enabled = True
    settings.planning_generation_strategy = "continuity_v1"
    settings.planner_retry_delay_seconds = 0
    gate, _ = offer(env)
    job = select(env, gate).json()["job"]
    store = app.state.store
    strategy = StrategyRecord.model_validate(store.get_job(job["id"])["payload"]["selected_strategy"])
    calls, bodies = [], []
    control = {"failure": None, "refine": False, "interrupted": False}

    def respond(request):
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        bodies.append(json.loads(request.content))
        if stage == "ad_script":
            return chat_output(director_output(strategy)["ad_script"])
        if stage == "storyboard":
            return chat_output(director_output(strategy))
        if stage == "plan_refinement":
            draft = deepcopy(context["draft_storyboard"])
            draft["shots"][0]["description"] += "只保留瓶身静置辨认。"
            return chat_output(draft)
        assert stage == "plan_review"
        if control["refine"] and calls.count("plan_review") == 1:
            review = accepted_review(context["final_plan"])
            reject_first_shot(review)
            return chat_output(review)
        failure = control["failure"]
        if failure == "interrupt" and not control["interrupted"]:
            control["interrupted"] = True
            raise asyncio.CancelledError()
        if failure == "transient_once" and calls.count("plan_review") == 1:
            return httpx.Response(503, text="PRIVATE_HTTP_BODY")
        if failure == "transient":
            return httpx.Response(503, text="PRIVATE_HTTP_BODY")
        if failure == "invalid":
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": "PRIVATE_INVALID_JSON"}}]})
        if failure == "configuration":
            return httpx.Response(401, text="PRIVATE_CREDENTIAL_DETAILS")
        if failure in ("cancel", "change_context"):
            if failure == "cancel":
                store.request_cancel(pid)
            else:
                store.add_message(pid, "user", "改为新的商品需求")
            return httpx.Response(503, text="PRIVATE_RESPONSE")
        return chat_output(accepted_review(context["final_plan"]))

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs:
        original_client(*args, transport=httpx.MockTransport(respond), **kwargs))

    def execute(new_store=None):
        return asyncio.run(Pipeline(new_store or store, Gateway(settings), settings).execute(job["id"]))
    return client, store, pid, job, calls, bodies, control, execute


@pytest.mark.parametrize("refine", [False, True])
def test_restart_replays_completed_stages_and_keeps_repair_history(run_case, refine):
    client, store, pid, job, calls, bodies, control, execute = run_case
    before = store.project_detail(pid)
    control.update(failure="interrupt", refine=refine)
    with pytest.raises(asyncio.CancelledError):
        execute()
    interrupted = store.get_job(job["id"])
    assert interrupted["status"] == "queued"
    saved = interrupted["payload"]["planning_checkpoint"]
    assert saved["entries"][-1]["state"] == "running"
    initial_trace = deepcopy(interrupted["payload"]["skill_trace"])
    restored = Store(store.db_path, store.media_root)
    restored.recover_jobs()
    result = execute(restored)
    assert result["status"] == "ready"
    assert calls.count("ad_script") == calls.count("storyboard") == 1
    assert calls.count("plan_refinement") == int(refine)
    assert calls.count("plan_review") == 2 + int(refine)
    assert result["payload"]["skill_trace"] == initial_trace
    assert result["payload"]["planning_checkpoint"]["transient_retries"] == 1
    assert restored.project_detail(pid)["assets"] == before["assets"]
    assert restored.project_detail(pid)["renders"] == before["renders"]
    assert result["submission_count"] == 0
    labels = [event["label"] for event in restored.events_after(pid)["events"] if event["kind"] == "step"]
    assert labels.count("正在策划广告内容") == labels.count("正在安排画面和台词") == 1
    visible = client.get(f"/api/projects/{pid}").text
    assert "planning_checkpoint" not in visible and "input_hash" not in visible


def test_temporary_failure_continues_same_stage_without_user_action(run_case):
    _, store, pid, job, calls, bodies, control, execute = run_case
    control["failure"] = "transient_once"
    result = execute()
    assert result["status"] == "ready"
    assert calls == ["ad_script", "storyboard", "plan_review", "plan_review"]
    assert bodies[-2] == bodies[-1]
    events = store.events_after(pid)["events"]
    assert any("暂时中断" in event["label"] for event in events)
    assert not any(event["kind"] == "terminal" and event["state"] == "failed" for event in events)


def test_exhaustion_is_durable_and_explicit_retry_reuses_completed_work(run_case):
    client, store, pid, job, calls, bodies, control, execute = run_case
    control["failure"] = "transient"
    failed = execute()
    assert failed["status"] == "failed"
    assert failed["progress"]["error_code"] == "planning_request_failed"
    assert failed["progress"]["retry_action"] == "retry_planning"
    assert calls == ["ad_script", "storyboard"] + ["plan_review"] * 3
    # A worker recreation does not replenish a terminal request's allowance.
    assert execute()["status"] == "failed"
    assert len(calls) == 5
    response = client.post(f"/api/projects/{pid}/retry", json={"job_id": job["id"]})
    assert response.status_code == 200
    assert store.get_project(pid)["work_status"] == "queued"
    assert client.post(f"/api/projects/{pid}/retry", json={"job_id": job["id"]}).status_code == 409
    control["failure"] = None
    assert execute()["status"] == "ready"
    assert calls == ["ad_script", "storyboard"] + ["plan_review"] * 4
    assert "PRIVATE" not in json.dumps(client.get(f"/api/projects/{pid}").json())


@pytest.mark.parametrize("failure,code", [("invalid", "planning_invalid_response"),
                                         ("configuration", "planning_configuration_failed")])
def test_schema_failure_can_be_retried_and_configuration_failure_cannot(run_case, failure, code):
    client, store, pid, job, calls, _, control, execute = run_case
    control["failure"] = failure
    result = execute()
    assert result["status"] == "failed" and result["progress"]["error_code"] == code
    assert calls == ["ad_script", "storyboard", "plan_review"]
    if failure == "invalid":
        assert result["progress"]["retryable"] is True
        assert result["progress"]["retry_action"] == "retry_planning"
        assert client.post(f"/api/projects/{pid}/retry", json={"job_id": job["id"]}).status_code == 200
    else:
        assert result["progress"]["retryable"] is False
        assert client.post(f"/api/projects/{pid}/retry", json={"job_id": job["id"]}).status_code == 409
    assert "PRIVATE" not in json.dumps(client.get(f"/api/projects/{pid}").json())


def test_changed_rules_cannot_reuse_old_stage_results(run_case, monkeypatch):
    _, store, _, job, calls, _, control, execute = run_case
    control["failure"] = "interrupt"
    with pytest.raises(asyncio.CancelledError):
        execute()
    from app import runtime_skills
    original = runtime_skills.stage_skill_context
    def changed(stage, mode=None, **kwargs):
        data = original(stage, mode, **kwargs)
        data["skills"][0]["content"] += "规则已更新。"
        return data
    monkeypatch.setattr(runtime_skills, "stage_skill_context", changed)
    result = execute()
    assert result["status"] == "failed"
    assert result["progress"]["error_code"] == "planning_invalid_response"
    assert len(calls) == 3


@pytest.mark.parametrize("failure,status", [("cancel", "canceled"), ("change_context", "needs_review")])
def test_cancel_or_new_requirement_prevents_automatic_request_retry(run_case, failure, status):
    _, _, _, _, calls, _, control, execute = run_case
    control["failure"] = failure
    assert execute()["status"] == status
    assert calls == ["ad_script", "storyboard", "plan_review"]


def test_repeated_worker_interruptions_do_not_reset_retry_budget(run_case):
    _, store, _, job, calls, _, control, execute = run_case
    for _ in range(3):
        control.update(failure="interrupt", interrupted=False)
        with pytest.raises(asyncio.CancelledError):
            execute()
    assert execute()["status"] == "failed"
    assert calls == ["ad_script", "storyboard"] + ["plan_review"] * 3
    checkpoint = store.get_job(job["id"])["payload"]["planning_checkpoint"]
    assert checkpoint["request_count"] == 5 and checkpoint["transient_retries"] == 2


def test_replaced_original_bytes_cannot_reuse_cached_director(run_case):
    _, store, pid, _, calls, _, control, execute = run_case
    control["failure"] = "interrupt"
    with pytest.raises(asyncio.CancelledError):
        execute()
    original = next(asset for asset in store.project_detail(pid)["assets"] if asset["kind"] == "product")
    Path(original["path"]).write_bytes(b"EXPLICIT_TEST_REPLACED_BYTES")
    result = execute()
    assert result["status"] == "failed" and result["progress"]["error_code"] == "planning_invalid_response"
    assert len(calls) == 3
