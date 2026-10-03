"""Finite local SOL waiting policy; all transport and deadlines are offline."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from app.planner_transport import planner_request_timing
from app.planning_session import PlanningSession, PlanningSessionError
from app.providers.gateway import Gateway, PlannerRequestError
from test_planner_transport import settings, transport, completion, no_external_connections


@pytest.mark.parametrize("provider,model,expected,deadline", [
    ("tudou", "gpt-6-sol", {"connect": 15, "read": 600, "write": 60, "pool": 15}, 660),
    ("deepseek", "deepseek-flash", {"connect": 300, "read": 300, "write": 300, "pool": 300}, None),
    ("deepseek", "gpt-6-sol", {"connect": 300, "read": 300, "write": 300, "pool": 300}, None),
    ("tudou", "other-model", {"connect": 300, "read": 300, "write": 300, "pool": 300}, None),
])
def test_only_tudou_sol_changes_local_waiting(settings, provider, model, expected, deadline):
    timeout, actual_deadline = planner_request_timing(replace(settings, planner_provider=provider, planner_model=model))
    assert timeout.as_dict() == expected
    assert actual_deadline == deadline
    legacy_timeout, legacy_deadline = planner_request_timing(SimpleNamespace(planner_model="deepseek-flash"))
    assert legacy_timeout.as_dict() == {"connect": 300, "read": 300, "write": 300, "pool": 300}
    assert legacy_deadline is None


@pytest.mark.parametrize("stage,effort", [("storyboard", "high"), ("plan_review", "medium"),
                                          ("plan_refinement", "medium")])
def test_actual_request_uses_local_limits_with_selected_stage_profile_and_token_budget(settings, transport, stage, effort):
    received = []
    def respond(request):
        received.append(request)
        assert request.extensions["timeout"] == {"connect": 15, "read": 600, "write": 60, "pool": 15}
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-6-sol" and payload["reasoning_effort"] == effort
        assert payload["max_completion_tokens"] == 32768
        assert payload["stream"] is False
        assert "timeout" not in payload and "deadline" not in payload
        return completion({"ready": True})
    transport(respond)
    result = asyncio.run(Gateway(settings)._planner_json("Review JSON", {
        "stage": stage, "stage_skills": {"skills": []}}, []))
    assert result == {"ready": True} and len(received) == 1
    assert settings.provider_timeout_seconds == 1800


def test_total_deadline_cancels_hanging_request_and_uses_safe_transient_error(settings, transport, monkeypatch, caplog):
    cancelled = []
    async def hang(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    transport(hang)
    client_factory, clients = httpx.AsyncClient, []
    def tracked_client(**kwargs):
        client = client_factory(**kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(httpx, "AsyncClient", tracked_client)
    timeout, _ = planner_request_timing(settings)
    monkeypatch.setattr("app.providers.gateway.planner_request_timing", lambda *args, **kwargs: (timeout, .01))
    with caplog.at_level("INFO"), pytest.raises(PlannerRequestError) as caught:
        asyncio.run(Gateway(settings)._planner_json("DO_NOT_LOG_PROMPT", {"stage": "plan_review"}, []))
    assert cancelled == [True]
    assert len(clients) == 1 and clients[0].is_closed
    assert caught.value.planning_retry_kind == "transient"
    assert caught.value.retry_action == "retry_planning"
    assert "DO_NOT_LOG_PROMPT" not in caplog.text
    assert settings.tudou_api_key not in caplog.text + str(caught.value)


def test_read_timeout_hides_transport_url_body_and_credentials(settings, transport, caplog):
    supplier_body = "TEST_ONLY_RAW_SUPPLIER_BODY"
    def fail(request):
        raise httpx.ReadTimeout(supplier_body + " " + settings.tudou_api_key, request=request)
    transport(fail)
    with caplog.at_level("INFO"), pytest.raises(PlannerRequestError) as caught:
        asyncio.run(Gateway(settings)._planner_json("DO_NOT_LOG_PROMPT", {"stage": "plan_review"}, []))
    exposed = caplog.text + str(caught.value)
    assert supplier_body not in exposed and settings.tudou_api_key not in exposed
    assert "api.ai-tudou.net" not in exposed and "DO_NOT_LOG_PROMPT" not in exposed


@pytest.mark.parametrize("kind", ["read", "total"])
def test_timeouts_use_existing_two_transient_retries_and_persist_attempts(settings, transport, monkeypatch, kind):
    requests = []
    async def fail(request):
        requests.append(request)
        if kind == "read":
            raise httpx.ReadTimeout("offline read timeout", request=request)
        await asyncio.Event().wait()
    transport(fail)
    timeout, _ = planner_request_timing(settings)
    monkeypatch.setattr("app.providers.gateway.planner_request_timing", lambda *args, **kwargs: (timeout, .01))
    saved = []
    session = PlanningSession(None, saved.append, {"synthetic_scope": True}, retry_delay=0)
    gateway = Gateway(settings)
    with pytest.raises(PlanningSessionError, match="暂时未能完成"):
        asyncio.run(session.call("plan_review", {"synthetic_input": True},
            lambda: gateway._planner_json("Review", {"stage": "plan_review"}, [])))
    assert len(requests) == 3
    assert session.state["transient_retries"] == 2
    assert session.state["request_count"] == 3
    assert session.state["entries"][0]["attempts"] == 3
    assert saved[-1]["entries"][0]["state"] == "terminal"


def test_external_cancellation_is_not_converted_into_transport_retry(settings, transport, monkeypatch):
    async def cancel(request):
        raise asyncio.CancelledError()
    transport(cancel)
    client_factory, clients = httpx.AsyncClient, []
    def tracked_client(**kwargs):
        client = client_factory(**kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(httpx, "AsyncClient", tracked_client)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Gateway(settings)._planner_json("Review", {"stage": "plan_review"}, []))
    assert len(clients) == 1 and clients[0].is_closed
