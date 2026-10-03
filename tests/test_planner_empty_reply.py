"""A stopped, empty text response gets one guarded current-stage retry only."""
import asyncio
from collections import Counter
from copy import deepcopy
import json
import logging

import httpx
import pytest

from app.pipeline import Pipeline
from app.providers.gateway import EmptyPlannerResponse, Gateway, ProviderError
from test_conversation_api import env, post_turn
from test_direction_selection import approved_direction_review, candidates_for, concept_pool_for, isolated_skill_loader
from test_providers import chat_output, request_context


@pytest.fixture(autouse=True)
def forbid_unmocked_network_and_media(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Empty planner retry must never call real transport or a media provider")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    for method in ("create_designs", "submit_video", "synthesize_voice"):
        monkeypatch.setattr(Gateway, method, denied)


def install_transport(monkeypatch, handler):
    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs:
        original_client(*args, transport=httpx.MockTransport(handler), **kwargs))


def empty_output(content="", finish_reason="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason,
        "message": {"content": content, "reasoning_content": "PRIVATE_REASONING_MUST_NOT_BE_LOGGED"}}]})


def strategy_output(candidate):
    return {"kind": "directions", "message": "待审核的三个方向", "brief": candidate.brief.model_dump(),
        "directions": [direction.model_dump() for direction in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions)}


def run_decision(env, **callbacks):
    _, app, pid, asset = env
    app.state.settings.direction_selection_enabled = True
    source = app.state.store.get_asset(asset["id"])
    return asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid),
        [source], [], [], None, **callbacks))


@pytest.mark.parametrize("content", [None, "", " \n\t "])
def test_only_stopped_empty_content_has_a_dedicated_safe_error(env, monkeypatch, caplog, content):
    calls = []
    def respond(request):
        calls.append(request)
        return empty_output(content)
    install_transport(monkeypatch, respond)
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(EmptyPlannerResponse) as error:
            asyncio.run(Gateway(env[1].state.settings)._planner_json("json", {"stage": "direction_review"}, []))
    assert len(calls) == 1  # Transport parser itself has no retry loop.
    assert "PRIVATE" not in str(error.value) and "PRIVATE" not in caplog.text
    for path in (env[1].state.settings.data_root / "diagnostics").glob("*.json"):
        assert "PRIVATE_REASONING" not in path.read_text()


def test_empty_review_retries_exact_same_stage_once_without_reloading_skills(env, monkeypatch, isolated_skill_loader):
    candidate = candidates_for(env)
    calls, review_bodies, progresses, traces = [], [], [], []
    def respond(request):
        assert request.url.host == "api.deepseek.com"
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        if stage == "strategy":
            return chat_output(strategy_output(candidate))
        assert stage == "direction_review"
        review_bodies.append(json.loads(request.content))
        if len(review_bodies) == 1:
            return empty_output()
        return chat_output(approved_direction_review(context["directions"]))
    install_transport(monkeypatch, respond)
    result = run_decision(env, on_progress=progresses.append, on_skill_trace=traces.append)
    assert result["kind"] == "directions"
    assert calls == ["strategy", "direction_review", "direction_review"]
    assert review_bodies[0] == review_bodies[1]
    assert progresses.count("retrying_planner") == 1
    assert Counter(trace["stage"] for trace in traces) == {
        "orchestrator": 1, "strategy": 1, "direction_review": 1}
    assert result.get("plan") is None
    assert [asset["kind"] for asset in env[1].state.store.project_detail(env[2])["assets"]] == ["product"]


@pytest.mark.parametrize("failure_pattern,expected_calls", [
    ("repeat_stage", ["strategy", "direction_review", "direction_review"]),
    ("separate_stages", ["strategy", "strategy", "direction_review"]),
])
def test_retry_allowance_is_one_for_the_entire_turn(env, monkeypatch, isolated_skill_loader,
                                                  failure_pattern, expected_calls):
    candidate = candidates_for(env)
    calls, progresses = [], []
    def respond(request):
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        if stage == "strategy":
            if failure_pattern == "separate_stages" and calls.count(stage) == 1:
                return empty_output()
            return chat_output(strategy_output(candidate))
        assert stage == "direction_review"
        return empty_output()
    install_transport(monkeypatch, respond)
    with pytest.raises(ProviderError):
        run_decision(env, on_progress=progresses.append)
    assert calls == expected_calls
    assert progresses.count("retrying_planner") == 1
    detail = env[1].state.store.project_detail(env[2])
    assert detail["project"]["plan"] is None and not detail["gates"]
    assert [asset["kind"] for asset in detail["assets"]] == ["product"]


@pytest.mark.parametrize("failure", ["malformed_json", "array_json", "object_schema", "length", "http", "missing_content"])
def test_nonempty_or_non_success_failures_do_not_trigger_retry(env, monkeypatch, isolated_skill_loader, failure):
    candidate = candidates_for(env)
    calls, progresses = [], []
    def respond(request):
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        if stage == "strategy":
            return chat_output(strategy_output(candidate))
        assert stage == "direction_review"
        if failure == "malformed_json":
            return empty_output('{"partial":')
        if failure == "array_json":
            return empty_output("[]")
        if failure == "object_schema":
            return chat_output({"ready": True})
        if failure == "length":
            return empty_output("", finish_reason="length")
        if failure == "http":
            return httpx.Response(503, text="PRIVATE_PROVIDER_ERROR")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {}}]})
    install_transport(monkeypatch, respond)
    with pytest.raises(ProviderError) as error:
        run_decision(env, on_progress=progresses.append)
    assert calls == ["strategy"] + ["direction_review"] * (3 if failure == "http" else 1)
    assert "retrying_planner" not in progresses
    assert progresses.count("retrying_planner_request") == (2 if failure == "http" else 0)
    assert "PRIVATE_PROVIDER_ERROR" not in str(error.value)


@pytest.mark.parametrize("change,expected_status", [("cancel", "canceled"), ("context", "needs_review"), ("revision", "needs_review")])
def test_cancel_or_changed_context_prevents_empty_response_resubmission(env, monkeypatch,
        isolated_skill_loader, change, expected_status):
    _, app, pid, asset = env
    app.state.settings.direction_selection_enabled = True
    store = app.state.store
    candidate = candidates_for(env)
    original_asset = deepcopy(store.get_asset(asset["id"]))
    calls = []
    def respond(request):
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        if stage == "strategy":
            return chat_output(strategy_output(candidate))
        assert stage == "direction_review"
        if change == "cancel":
            store.request_cancel(pid)
        elif change == "context":
            store.add_message(pid, "user", "需求已改变，旧策划不能继续")
        else:
            # Isolated fixture emulates a concurrent accepted revision without
            # manufacturing an unrelated plan or exercising another feature.
            with store._connection(True) as connection:
                connection.execute("UPDATE projects SET revision=revision+1 WHERE id=?", (pid,))
        return empty_output()
    install_transport(monkeypatch, respond)
    response = post_turn(env, client_turn_id="empty-reply-guard", content="请先给三个广告方向")
    assert response.status_code == 200
    job = response.json()["job"]
    result = asyncio.run(Pipeline(store, Gateway(app.state.settings), app.state.settings).execute(job["id"]))
    assert result["status"] == expected_status
    assert calls == ["strategy", "direction_review"]
    assert result["submission_count"] == 0
    assert store.get_asset(asset["id"]) == original_asset
    assert not store.project_detail(pid)["gates"]


def test_each_new_turn_has_its_own_one_retry_budget(env, monkeypatch, isolated_skill_loader):
    candidate = candidates_for(env)
    calls = []
    def respond(request):
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        if stage == "strategy":
            return chat_output(strategy_output(candidate))
        assert stage == "direction_review"
        if calls.count(stage) % 2:
            return empty_output()
        return chat_output(approved_direction_review(context["directions"]))
    install_transport(monkeypatch, respond)
    _, app, pid, asset = env
    app.state.settings.direction_selection_enabled = True
    gateway = Gateway(app.state.settings)
    source = app.state.store.get_asset(asset["id"])
    for _ in range(2):
        result = asyncio.run(gateway.decide(app.state.store.get_project(pid), [source], [], [], None))
        assert result["kind"] == "directions"
    assert calls == ["strategy", "direction_review", "direction_review"] * 2
