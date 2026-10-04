"""Offline checks for optional planner speed controls.

No provider key is used. Defaults stay on the historical single-model path.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import logging

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.pipeline import Pipeline
from app.planner_transport import parse_fallback_on, parse_stage_routes, planner_request_timing, stage_route
from app.providers import gateway as gateway_module
from app.providers.gateway import Gateway, PlannerInterruptedResponse, PlannerRequestError
from app.speculative_plan import direction_selection_text, schedule_speculation
from test_conversation_api import env
from test_direction_selection import offer, select
from test_planner_transport import completion, isolated_environment, settings, transport
from test_progressive_planning import progressive_gateway
from test_providers import install_transport, source


@pytest.fixture(autouse=True)
def reset_unreliable_streams():
    gateway_module._UNRELIABLE_STREAM_PROVIDERS.clear()
    gateway_module._NONSTREAM_IDLE_CUT.clear()
    yield
    gateway_module._UNRELIABLE_STREAM_PROVIDERS.clear()
    gateway_module._NONSTREAM_IDLE_CUT.clear()


def event_stream(chunks, *, done=True):
    body = "".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def last_planner_metric(caplog):
    return json.loads(next(record.getMessage().split(": ", 1)[1] for record in reversed(caplog.records)
                           if record.getMessage().startswith("Planner request metrics:")))


def test_stage_route_parser_rejects_incomplete_or_unknown_entries():
    assert parse_stage_routes("  ") == {}
    parsed = parse_stage_routes("storyboard=deepseek:deepseek-flash:low, plan_review=tudou:gpt-6-sol")
    assert parsed["storyboard"] == {"provider": "deepseek", "model": "deepseek-flash", "effort": "low"}
    assert parsed["plan_review"]["effort"] is None
    with pytest.raises(ValueError):
        parse_stage_routes("unknown=deepseek:deepseek-flash")
    with pytest.raises(ValueError):
        parse_fallback_on("timeout,500")
    assert parse_fallback_on("504, timeout") == ("504", "timeout")


def test_unconfigured_speed_profile_keeps_historical_defaults(isolated_environment, monkeypatch):
    configured = Settings.from_env()
    assert configured.planner_stage_routes == {}
    assert configured.planner_fallback_on == ()
    assert configured.planner_stream == "off"
    assert configured.planner_request_deadline_seconds is None
    assert configured.planner_max_refinements == 2
    assert configured.planner_refinement_format == "sparse_or_full"
    assert configured.planner_direction_review_effort is None
    assert configured.planner_speculate_recommended is False


@pytest.mark.parametrize("key,value", [
    ("PLANNER_STREAM", "always"),
    ("PLANNER_MAX_REFINEMENTS", "3"),
    ("PLANNER_REQUEST_DEADLINE_SECONDS", "10"),
    ("PLANNER_FALLBACK_ON", "timeout"),
    ("PLANNER_REFINEMENT_FORMAT", "full"),
])
def test_invalid_speed_profile_fails_before_a_request(isolated_environment, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_deadline_lowers_sol_wait_without_becoming_the_default(settings):
    timeout, wall = planner_request_timing(settings)
    assert timeout.as_dict()["read"] == 600 and wall == 660
    limited = replace(settings, planner_request_deadline_seconds=270)
    timeout, wall = planner_request_timing(limited)
    assert timeout.as_dict() == {"connect": 15, "read": 270, "write": 60, "pool": 15}
    assert wall == 270
    other = planner_request_timing(replace(limited, planner_provider="deepseek", planner_model="deepseek-flash"))
    assert other[0].as_dict()["read"] == 270 and other[1] == 270


def test_stage_route_uses_the_other_account_and_explicit_effort(settings, transport):
    routed = replace(settings, planner_stage_routes={
        "storyboard": {"provider": "deepseek", "model": "deepseek-flash", "effort": "low"}})
    seen = []

    def respond(request):
        seen.append(request)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer " + settings.deepseek_api_key
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["reasoning_effort"] == "low"
        assert payload["stream"] is False
        return completion({"ready": True})

    transport(respond)
    result = asyncio.run(Gateway(routed)._planner_json("JSON", {"stage": "storyboard", "stage_skills": {"skills": []}}, []))
    assert result == {"ready": True} and len(seen) == 1
    assert stage_route(routed, "strategy").primary is True
    assert stage_route(routed, "storyboard").primary is False


def test_direction_review_effort_is_opt_in_for_sol_only(settings, transport):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content)["reasoning_effort"])
        return completion({"ready": True})

    transport(respond)
    context = {"stage": "direction_review", "stage_skills": {"skills": []}}
    asyncio.run(Gateway(settings)._planner_json("JSON", context, []))
    asyncio.run(Gateway(replace(settings, planner_direction_review_effort="medium"))._planner_json("JSON", context, []))
    deepseek = replace(settings, planner_provider="deepseek", planner_model="deepseek-flash",
                       planner_direction_review_effort="medium")
    asyncio.run(Gateway(deepseek)._planner_json("JSON", context, []))
    assert seen == ["high", "medium", "high"]


def test_stream_assembles_text_and_records_time_to_first_token(settings, transport, caplog):
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["stream"])
        if payload["stream"] is not True:
            return completion({"ok": True})
        chunks = [
            {"choices": [{"delta": {"content": "{\"ok\": "}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "true}"}, "finish_reason": "stop"}],
             "usage": {"completion_tokens": 4}},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    transport(respond)
    tokens = []
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        result = asyncio.run(Gateway(replace(settings, planner_stream="auto"))._planner_json(
            "JSON", {"stage": "plan_review"}, [], on_first_token=lambda: tokens.append("first")))
    assert result == {"ok": True} and seen == [True] and tokens == ["first"]
    metric = json.loads(next(record.getMessage().split(": ", 1)[1] for record in caplog.records
                             if record.getMessage().startswith("Planner request metrics:")))
    assert metric["stream"] is True and metric["transport"] == "primary"
    assert metric["first_token_ms"] >= 0
    assert settings.tudou_api_key not in caplog.text


def test_stream_rejection_retries_once_without_streaming(settings, transport, caplog):
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["stream"])
        if payload["stream"] is True:
            return httpx.Response(400, json={"error": {"message": "stream unsupported"}})
        return completion({"ok": True})

    transport(respond)
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        result = asyncio.run(Gateway(replace(settings, planner_stream="on"))._planner_json(
            "JSON", {"stage": "ad_script"}, []))
    assert result == {"ok": True} and seen == [True, False]
    metric = json.loads(next(record.getMessage().split(": ", 1)[1] for record in reversed(caplog.records)
                             if record.getMessage().startswith("Planner request metrics:")))
    assert metric["stream"] is False and metric["stream_fallback"] is True
    assert metric["recovery"] == "nonstream"


def test_truncated_sse_retries_non_streaming_and_is_not_a_schema_error(settings, transport, caplog):
    partial = '{"concept_pool":[{"id":"c1"}],"directions":[{"label":"PRIVATE_PARTIAL_DIRECTION"'
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["stream"])
        if payload["stream"] is True:
            return event_stream([
                {"choices": [{"delta": {"content": partial}, "finish_reason": None}]},
            ], done=False)
        return completion({"ok": True})

    transport(respond)
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        result = asyncio.run(Gateway(replace(settings, planner_stream="auto"))._planner_json(
            "JSON", {"stage": "strategy"}, []))
    assert result == {"ok": True} and seen == [True, False]
    assert "结构校验" not in caplog.text and "PRIVATE_PARTIAL_DIRECTION" not in caplog.text
    assert not (settings.data_root / "diagnostics").exists()
    metric = last_planner_metric(caplog)
    assert metric["stream"] is False and metric["stream_fallback"] is True
    assert metric["recovery"] == "nonstream"
    assert "Planner stream interrupted" in caplog.text


def test_stream_without_done_is_interrupted_even_when_the_json_would_parse(settings, transport):
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["stream"])
        if payload["stream"] is True:
            return event_stream([
                {"choices": [{"delta": {"content": "{\"ok\": "}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "true}"}, "finish_reason": "stop"}]},
            ], done=False)
        return completion({"delivered": "json"})

    transport(respond)
    result = asyncio.run(Gateway(replace(settings, planner_stream="on"))._planner_json(
        "JSON", {"stage": "strategy"}, []))
    assert result == {"delivered": "json"} and seen == [True, False]


def test_stream_length_retries_non_streaming_instead_of_failing_closed(settings, transport):
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["stream"])
        if payload["stream"] is True:
            return event_stream([
                {"choices": [{"delta": {"content": "{\"partial\":"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "length"}],
                 "usage": {"completion_tokens": 12}},
            ])
        return completion({"delivered": "json"})

    transport(respond)
    result = asyncio.run(Gateway(replace(settings, planner_stream="auto"))._planner_json(
        "JSON", {"stage": "strategy"}, []))
    assert result == {"delivered": "json"} and seen == [True, False]


def test_stream_connection_close_retries_non_streaming_once(settings, transport):
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["stream"])
        if payload["stream"] is True:
            raise httpx.RemoteProtocolError("offline close")
        return completion({"ok": True})

    transport(respond)
    assert asyncio.run(Gateway(replace(settings, planner_stream="auto"))._planner_json(
        "JSON", {"stage": "strategy"}, [])) == {"ok": True}
    assert seen == [True, False]


def test_interrupted_stream_uses_fallback_when_the_same_model_has_no_time_left(settings, transport, monkeypatch):
    monkeypatch.setattr(gateway_module, "_NONSTREAM_RETRY_FLOOR_SECONDS", 10**9)
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), payload["stream"]))
        if "ai-tudou.net" in str(request.url):
            return event_stream([
                {"choices": [{"delta": {"content": '{"cut":'}, "finish_reason": None}]},
            ], done=False)
        return completion({"ok": True})

    routed = replace(settings, planner_stream="auto", planner_fallback_provider="deepseek",
                     planner_fallback_model="deepseek-flash", planner_fallback_on=("timeout",))
    transport(respond)
    assert asyncio.run(Gateway(routed)._planner_json("JSON", {"stage": "strategy"}, [])) == {"ok": True}
    assert seen == [
        ("https://api.ai-tudou.net/v1/chat/completions", True),
        ("https://api.deepseek.com/chat/completions", True),
    ]


def test_interrupted_stream_is_retryable_and_keeps_the_full_partial_body(settings, transport, monkeypatch, caplog):
    monkeypatch.setattr(gateway_module, "_NONSTREAM_RETRY_FLOOR_SECONDS", 10**9)
    partial = '{"direction":"PRIVATE_PARTIAL_DIRECTION' + ("x" * 5100)
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append(payload["stream"])
        return event_stream([
            {"choices": [{"delta": {"content": partial}, "finish_reason": None}]},
        ], done=False)

    transport(respond)
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(PlannerInterruptedResponse) as error:
            asyncio.run(Gateway(replace(settings, planner_stream="auto"))._planner_json(
                "JSON", {"stage": "strategy"}, []))
    assert seen == [True]
    assert error.value.retryable is True and error.value.retry_action == "retry_planning"
    assert error.value.error_code == "planning_response_interrupted"
    assert "回复中途断开" in str(error.value) and "结构校验" not in str(error.value)
    assert "PRIVATE_PARTIAL_DIRECTION" not in caplog.text and "PRIVATE_PARTIAL_DIRECTION" not in str(error.value)
    saved = json.loads(next((settings.data_root / "diagnostics").glob("*.json")).read_text())
    assert saved["raw_output"]["unparsed_content"] == partial
    assert len(partial) > 5000


def test_unreliable_stream_is_remembered_for_that_provider_only(settings, transport, caplog):
    seen = []

    def respond(request):
        payload = json.loads(request.content)
        seen.append((str(request.url), payload["stream"]))
        if payload["stream"] is True and "ai-tudou.net" in str(request.url):
            return event_stream([
                {"choices": [{"delta": {"content": '{"cut":'}, "finish_reason": None}]},
            ], done=False)
        if payload["stream"] is True:
            return event_stream([
                {"choices": [{"delta": {"content": "{\"ok\": "}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": "true}"}, "finish_reason": "stop"}]},
            ])
        return completion({"ok": True})

    transport(respond)
    streaming = replace(settings, planner_stream="auto")
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        assert asyncio.run(Gateway(streaming)._planner_json("JSON", {"stage": "strategy"}, [])) == {"ok": True}
        assert asyncio.run(Gateway(streaming)._planner_json("JSON", {"stage": "storyboard"}, [])) == {"ok": True}
    deepseek = replace(settings, planner_provider="deepseek", planner_model="deepseek-flash", planner_stream="auto")
    assert asyncio.run(Gateway(deepseek)._planner_json("JSON", {"stage": "strategy"}, [])) == {"ok": True}
    assert [stream for _, stream in seen] == [True, False, False, True]
    assert seen[2][0] == "https://api.ai-tudou.net/v1/chat/completions"
    assert "api.deepseek.com" in seen[3][0]
    remembered = last_planner_metric(caplog)
    assert remembered["stream_fallback"] is True and remembered["recovery"] == "nonstream"


def test_timeout_falls_back_once_to_the_fast_model(settings, transport, caplog):
    calls = []

    async def respond(request):
        calls.append(str(request.url))
        if "ai-tudou.net" in str(request.url):
            raise httpx.ReadTimeout("offline", request=request)
        return completion({"ok": True})

    routed = replace(settings, planner_fallback_provider="deepseek", planner_fallback_model="deepseek-flash",
                     planner_fallback_on=("timeout",))
    transport(respond)
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        assert asyncio.run(Gateway(routed)._planner_json("JSON", {"stage": "storyboard"}, [])) == {"ok": True}
    assert calls == ["https://api.ai-tudou.net/v1/chat/completions", "https://api.deepseek.com/chat/completions"]
    assert last_planner_metric(caplog)["recovery"] == "fallback"


def test_504_on_the_fallback_does_not_try_a_third_model(settings, transport):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(504, json={"error": "gateway"})

    routed = replace(settings, planner_fallback_provider="deepseek", planner_fallback_model="deepseek-flash",
                     planner_fallback_on=("504",))
    transport(respond)
    with pytest.raises(PlannerRequestError):
        asyncio.run(Gateway(routed)._planner_json("JSON", {"stage": "storyboard"}, []))
    assert calls == ["https://api.ai-tudou.net/v1/chat/completions", "https://api.deepseek.com/chat/completions"]


def test_missing_fallback_credential_does_not_call_the_other_account(settings, transport, caplog):
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(504, json={"error": "gateway"})

    transport(respond)
    routed = replace(settings, deepseek_api_key="", planner_fallback_provider="deepseek",
                     planner_fallback_model="deepseek-flash", planner_fallback_on=("504",))
    with caplog.at_level(logging.WARNING), pytest.raises(PlannerRequestError):
        asyncio.run(Gateway(routed)._planner_json("SECRET_PROMPT", {"stage": "storyboard"}, []))
    assert calls == ["https://api.ai-tudou.net/v1/chat/completions"]
    assert "missing_credential" in caplog.text
    assert "SECRET_PROMPT" not in caplog.text and settings.tudou_api_key not in caplog.text


def test_same_model_fallback_is_not_a_second_attempt(settings, transport):
    calls = []

    def respond(request):
        calls.append(1)
        return httpx.Response(504, json={"error": "gateway"})

    transport(respond)
    routed = replace(settings, planner_fallback_provider="tudou", planner_fallback_model="gpt-6-sol",
                     planner_fallback_on=("504",))
    with pytest.raises(PlannerRequestError):
        asyncio.run(Gateway(routed)._planner_json("JSON", {"stage": "storyboard"}, []))
    assert calls == [1]


def test_stage_route_and_fallback_policy_do_not_replay_the_primary_checkpoint(settings, transport):
    calls, checkpoints = [], []

    def respond(request):
        calls.append(str(request.url))
        return completion({"kind": "reply", "message": "请补充商品信息。"})

    transport(respond)
    project = {"id": "project_offline", "brief": "你好", "route": "product", "revision": 0,
               "context_version": 1, "duration_seconds": 20, "ad_form": "auto"}
    messages = [{"id": "user_offline", "role": "user", "content": "你好"}]
    primary = Gateway(settings)
    result = asyncio.run(primary.decide(project, [], messages, [], None,
                                        on_checkpoint=lambda value: checkpoints.append(deepcopy(value))))
    checkpoint = deepcopy(checkpoints[-1])
    routed = replace(settings, planner_stage_routes={
        "strategy": {"provider": "deepseek", "model": "deepseek-flash", "effort": "low"}})
    routed_result = asyncio.run(Gateway(routed).decide(project, [], messages, [], None, planning_checkpoint=checkpoint))
    fallback = replace(settings, planner_fallback_provider="deepseek", planner_fallback_model="deepseek-flash",
                       planner_fallback_on=("504",))
    fallback_result = asyncio.run(Gateway(fallback).decide(project, [], messages, [], None, planning_checkpoint=checkpoint))
    assert result["kind"] == routed_result["kind"] == fallback_result["kind"] == "reply"
    assert calls == [
        "https://api.ai-tudou.net/v1/chat/completions",
        "https://api.deepseek.com/chat/completions",
        "https://api.ai-tudou.net/v1/chat/completions",
    ]


def test_stage_result_cache_replays_text_stages_without_another_http_call(progressive_gateway, settings, source):
    settings.planner_provider = "deepseek"
    settings.planner_model = "deepseek-flash"
    state = progressive_gateway
    cache = {}

    async def once(use_cache):
        return await Gateway(settings).decide(
            {"brief": state.brief, "duration_seconds": state.duration}, [source], state.conversation, [],
            state.current_plan, selected_strategy=state.strategy.model_dump(),
            stage_cache_sink=lambda digest, stage, result: cache.__setitem__(
                digest, {"stage": stage, "result": deepcopy(result)}),
            **({"stage_result_cache": cache} if use_cache else {}))

    first = asyncio.run(once(False))
    produced = list(state.calls)
    assert produced and cache and first["plan"]["plan_review"]["ready"] is True
    second = asyncio.run(once(True))
    assert state.calls == produced
    assert second["plan"]["plan_review"]["ready"] is True


def test_speculative_text_plan_is_reused_only_for_the_recommended_direction(env):
    gate, source_job = offer(env)
    store = env[1].state.store
    project_id = env[2]
    settings = env[1].state.settings
    settings.planner_speculate_recommended = True
    recommended = "direction_0"

    class TextOnly:
        def __init__(self):
            self.submits = 0
            self.plans = 0

        async def decide(self, project, assets, messages, gates, current_plan, **kwargs):
            assert messages[-1]["content"] == direction_selection_text("轮廓之美")
            assert kwargs["on_checkpoint"] is not None
            kwargs["stage_cache_sink"]("cached-storyboard", "storyboard", {"cached": True})
            return {"kind": "plan"}

        async def _submit(self, *args, **kwargs):
            self.submits += 1
            raise AssertionError("speculation must not submit paid work")

        async def plan(self, *args, **kwargs):
            self.plans += 1
            raise AssertionError("speculation must not enter the legacy plan path")

    gateway = TextOnly()

    async def run():
        task = schedule_speculation(store, gateway, settings, source_job["id"])
        assert task is not None
        await task

    asyncio.run(run())
    spec = store.get_job(source_job["id"])["payload"]["speculative_plan"]
    assert spec["status"] == "ready" and spec["direction_id"] == recommended
    assert gateway.submits == 0 and gateway.plans == 0
    assert store.get_project(project_id)["plan"] is None

    other = select(env, gate, direction_id="direction_1", request_id="other-direction")
    assert other.status_code == 200
    assert "stage_result_cache" not in store.get_job(other.json()["job"]["id"])["payload"]
    assert store.get_job(source_job["id"])["payload"]["speculative_plan"]["abandoned"] is True
    late = store.merge_speculative_plan(source_job["id"], {
        "direction_id": recommended, "status": "ready",
        "entries": {"late": {"stage": "ad_script", "result": {"late": True}}}})
    assert "late" not in late["payload"]["speculative_plan"]["entries"]
    assert store.get_project(project_id)["plan"] is None


def test_matching_direction_copies_the_cache_and_streams_a_static_label(env):
    gate, source_job = offer(env)
    store = env[1].state.store
    store.merge_speculative_plan(source_job["id"], {
        "direction_id": "direction_0", "status": "ready",
        "entries": {"digest": {"stage": "storyboard", "result": {"cached": True}}}})
    chosen = select(env, gate, direction_id="direction_0", request_id="recommended")
    assert chosen.status_code == 200
    payload = store.get_job(chosen.json()["job"]["id"])["payload"]
    assert payload["stage_result_cache_direction_id"] == "direction_0"
    assert payload["stage_result_cache"]["digest"]["result"] == {"cached": True}
    assert store.get_project(env[2])["plan"] is None

    class Recording:
        supports_strategy_planning = True
        supports_direction_selection = True
        supports_planning_checkpoints = True

        def __init__(self):
            self.cache = None

        async def decide(self, project, assets, messages, gates, current_plan, **kwargs):
            self.cache = kwargs.get("stage_result_cache")
            kwargs["on_planner_stream"]("storyboard")
            kwargs["on_planner_stream"]("not-a-stage")
            return {"kind": "reply", "message": "沿用已有文字进度。"}

    gateway = Recording()
    asyncio.run(Pipeline(store, gateway, env[1].state.settings).execute(chosen.json()["job"]["id"]))
    assert gateway.cache["digest"]["stage"] == "storyboard"
    labels = [event["label"] for event in store.events_after(env[2])["events"]]
    assert labels.count("正在接收画面安排") == 1
    assert store.get_project(env[2])["plan"] is None


def test_abandoned_speculation_merge_does_not_overwrite_a_canceled_direction(env):
    gate, source_job = offer(env)
    store = env[1].state.store
    store.merge_speculative_plan(source_job["id"], {
        "direction_id": "direction_0", "status": "running", "entries": {}})
    store.request_cancel(env[2])
    merged = store.merge_speculative_plan(source_job["id"], {
        "direction_id": "direction_0", "status": "ready",
        "entries": {"digest": {"stage": "ad_script", "result": {"should": "drop"}}}})
    assert merged["status"] == "canceled"
    assert merged["payload"]["speculative_plan"]["abandoned"] is True
    assert merged["payload"]["speculative_plan"]["entries"] == {}
    assert store.pending_gate(env[2]) is None
