"""Long non-stream disconnects and a changed planner route recover inside the job.

No provider key is used. Image and video submission are not called.
"""
import asyncio
from dataclasses import replace
import json
import logging

import httpx
import pytest

from app.planning_session import PlanningSession
from app.providers import gateway as gateway_module
from app.providers.gateway import Gateway
from test_conversation_api import env
from test_direction_selection import offer, select
from test_planner_transport import completion, settings, transport


@pytest.fixture(autouse=True)
def reset_idle_memory():
    gateway_module._NONSTREAM_IDLE_CUT.clear()
    gateway_module._UNRELIABLE_STREAM_PROVIDERS.clear()
    yield
    gateway_module._NONSTREAM_IDLE_CUT.clear()
    gateway_module._UNRELIABLE_STREAM_PROVIDERS.clear()


def planner_metrics(caplog):
    return [json.loads(record.getMessage().split(": ", 1)[1]) for record in caplog.records
            if record.getMessage().startswith("Planner request metrics:")]


@pytest.mark.parametrize("failure", ["disconnect", "http502"])
def test_two_hundred_second_drops_retry_once_then_use_the_fallback(
        settings, transport, monkeypatch, caplog, failure):
    clock = {"t": 0.0}
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: clock["t"])
    calls, phases = [], []

    def respond(request):
        calls.append((str(request.url), json.loads(request.content)["model"]))
        if "ai-tudou.net" in str(request.url):
            clock["t"] += 102
            if failure == "disconnect":
                raise httpx.RemoteProtocolError("offline close")
            return httpx.Response(502, json={"error": "offline"})
        assert "/images" not in str(request.url) and "/tasks" not in str(request.url)
        return completion({"ok": True})

    routed = replace(settings, planner_stream="off", planner_request_deadline_seconds=270,
                     planner_fallback_provider="deepseek", planner_fallback_model="deepseek-flash",
                     planner_fallback_on=("timeout", "504"))
    transport(respond)
    gateway = Gateway(routed)
    gateway._planner_notice = phases.append
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        assert asyncio.run(gateway._planner_json("JSON", {"stage": "strategy"}, [])) == {"ok": True}
    assert calls == [
        ("https://api.ai-tudou.net/v1/chat/completions", "gpt-6-sol"),
        ("https://api.ai-tudou.net/v1/chat/completions", "gpt-6-sol"),
        ("https://api.deepseek.com/chat/completions", "deepseek-flash"),
    ]
    assert phases == ["retrying_planner_request", "using_backup_planner"]
    assert "deciding" not in phases
    records = [json.loads(path.read_text()) for path in (routed.data_root / "diagnostics").glob("*.json")]
    assert len(records) == 2
    expected_error = "RemoteProtocolError" if failure == "disconnect" else "HTTP502"
    assert {record["error_class"] for record in records} == {expected_error}
    assert {record["stage"] for record in records} == {"strategy"}
    assert {record["model"] for record in records} == {"gpt-6-sol"}
    assert all(record["recovery"] == "interrupted" and record["elapsed_ms"] >= 100_000 for record in records)
    assert any(item.get("recovery") == "interrupted" for item in planner_metrics(caplog))
    assert planner_metrics(caplog)[-1]["recovery"] == "fallback"
    calls.clear()
    phases.clear()
    assert asyncio.run(Gateway(routed)._planner_json("JSON", {"stage": "direction_review"}, [])) == {"ok": True}
    assert calls == [("https://api.deepseek.com/chat/completions", "deepseek-flash")]


def test_stage_budget_skips_another_primary_when_the_cut_repeats_near_the_deadline(
        settings, transport, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: clock["t"])
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if "ai-tudou.net" in str(request.url):
            clock["t"] += 100
            raise httpx.ReadError("offline reset", request=request)
        return completion({"ok": True})

    routed = replace(settings, planner_request_deadline_seconds=270,
                     planner_fallback_provider="deepseek", planner_fallback_model="deepseek-flash",
                     planner_fallback_on=("timeout",))
    transport(respond)
    gateway = Gateway(routed)
    gateway._planner_stage_started = 0.0
    gateway._planner_stage_budget = 120
    assert asyncio.run(gateway._planner_json("JSON", {"stage": "strategy"}, [])) == {"ok": True}
    assert calls == [
        "https://api.ai-tudou.net/v1/chat/completions",
        "https://api.deepseek.com/chat/completions",
    ]


def test_changed_signature_keeps_earlier_entries_and_recomputes_from_the_mismatch(caplog):
    saved = []
    session = PlanningSession(None, saved.append, {"scope": "same"}, retry_delay=0)

    async def perform(value):
        return value

    asyncio.run(session.call("ad_script", {"model": "old"}, lambda: perform({"script": "kept"})))
    asyncio.run(session.call("storyboard", {"model": "old"}, lambda: perform({"board": "old"})))
    resumed = PlanningSession(saved[-1], saved.append, {"scope": "same"}, retry_delay=0)
    replayed = asyncio.run(resumed.call("ad_script", {"model": "old"}, lambda: perform({"script": "unused"})))
    assert replayed == {"script": "kept"}
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        board = asyncio.run(resumed.call("storyboard", {"model": "deepseek-flash"}, lambda: perform({"board": "new"})))
    assert board == {"board": "new"}
    assert resumed.state["entries"][0]["result"] == {"script": "kept"}
    assert resumed.state["entries"][1]["result"] == {"board": "new"}
    assert len(resumed.state["entries"]) == 2
    metric = planner_metrics(caplog)[-1]
    assert metric["recovery"] == "checkpoint_recompute" and metric["dropped_entries"] == 1
    assert metric["stage"] == "storyboard"


def test_resume_planning_queues_the_saved_brief_and_chosen_direction(env):
    client, app, pid, _ = env
    gate, _ = offer(env)
    queued = select(env, gate).json()["job"]
    store = app.state.store
    chosen = store.get_job(queued["id"])["payload"]["selected_strategy"]["selected_direction_id"]
    brief = store.get_project(pid)["brief"]
    store.update_job(queued["id"], status="failed", error="offline")
    response = client.post(f"/api/projects/{pid}/resume-planning")
    assert response.status_code == 200
    job = response.json()["job"]
    assert job["kind"] == "plan" and job["status"] == "queued" and job["id"] != queued["id"]
    payload = store.get_job(job["id"])["payload"]
    assert payload["planning_requested"] is True
    assert payload["selected_strategy"]["selected_direction_id"] == chosen
    assert payload["frozen"]["project"]["brief"] == brief
    assert "submit_video" not in json.dumps(payload)
