"""Private failure replay evidence is bounded and never changes public errors."""
import asyncio
import json
import logging

import httpx
import pytest

from app.providers import gateway as gateway_module
from app.providers.gateway import Gateway, ProviderError, save_failed_planner_output
from test_conversation_api import env
from test_direction_selection import candidates_for, concept_pool_for, forbid_unmocked_network


def test_truncated_response_reports_stage_without_logging_private_content(env, monkeypatch, caplog):
    requests = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {
            "content": "PRIVATE_PARTIAL_PLAN", "reasoning_content": "PRIVATE_REASONING"}}],
            "usage": {"prompt_tokens": 11915, "completion_tokens": 24000,
                      "total_tokens": 35915, "secret": "PRIVATE_USAGE",
                      "completion_tokens_details": {"reasoning_tokens": 21900, "secret": "PRIVATE_DETAIL"}}})

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(ProviderError, match="长度上限"):
            asyncio.run(Gateway(env[1].state.settings)._planner_json(
                "json", {"stage": "direction_review"}, []))
    assert len(requests) == 1
    assert "direction_review" in caplog.text and "24000" in caplog.text
    assert "PRIVATE" not in caplog.text
    record = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Planner incomplete response: "))
    logged = json.loads(record.split(": ", 1)[1])
    assert logged["max_tokens"] == 24000
    assert logged["content_characters"] == len("PRIVATE_PARTIAL_PLAN")
    assert logged["usage"]["reasoning_tokens"] == 21900


def test_plan_review_length_retries_once_before_the_card(env, monkeypatch):
    requests = []
    original_client = httpx.AsyncClient

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {
                "content": "PRIVATE_PARTIAL_REVIEW"}}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "{\"ready\":true}"}}]})

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    result = asyncio.run(Gateway(env[1].state.settings)._planner_json(
        "json", {"stage": "plan_review"}, []))
    assert result == {"ready": True}
    assert len(requests) == 2
    assert "写超了" in json.dumps(requests[1], ensure_ascii=False)
    files = list((env[1].state.settings.data_root / "diagnostics").glob("*.json"))
    assert len(files) == 1
    assert "PRIVATE_PARTIAL_REVIEW" in files[0].read_text()


def test_invalid_json_keeps_bounded_final_output_only_for_private_replay(env, monkeypatch, caplog):
    original_client = httpx.AsyncClient
    def respond(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"broken": "PRIVATE_OUTPUT",', "reasoning_content": "NEVER_KEEP_REASONING"}}]})
    monkeypatch.setattr(gateway_module.httpx, "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(ProviderError, match="结构校验"):
            asyncio.run(Gateway(env[1].state.settings)._planner_json("json", {"stage": "strategy"}, []))
    assert "json_line" in caplog.text and "PRIVATE_OUTPUT" not in caplog.text
    files = list((env[1].state.settings.data_root / "diagnostics").glob("*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert "PRIVATE_OUTPUT" in files[0].read_text() and "NEVER_KEEP_REASONING" not in files[0].read_text()


@pytest.mark.parametrize("failure", ["schema", "guard"])
def test_failed_planner_saves_only_last_response_and_safe_guard_locations(env, monkeypatch, caplog, failure):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    response = {"kind": "directions", "message": "选择方向", "brief": candidate.brief.model_dump(),
        "directions": [d.model_dump() for d in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions)}
    if failure == "schema":
        response["extra"] = "REPLAYABLE_SCHEMA_FAILURE"
    else:
        response["brief"]["facts"][0]["source_id"] = "REPLAYABLE_BAD_SOURCE_ID"
    async def planned(self, system, context, originals, max_tokens=7000):
        return response
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(ProviderError) as error:
            asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
                [{"role": "user", "content": "PRIVATE_REQUEST_NOT_FOR_DIAGNOSTIC"}], [], None))
    files = list((app.state.settings.data_root / "diagnostics").glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved == {"stage": "strategy", "model": app.state.settings.planner_model, "raw_output": response}
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert files[0].stat().st_size <= 200 * 1024
    assert "PRIVATE_REQUEST_NOT_FOR_DIAGNOSTIC" not in files[0].read_text()
    assert "REPLAYABLE" not in str(error.value) and "diagnostics" not in str(error.value)
    assert "REPLAYABLE" not in caplog.text
    if failure == "guard":
        record = next(record.getMessage() for record in caplog.records
            if record.getMessage().startswith("Planner validation guard failed: "))
        frames = json.loads(record.split(": ", 1)[1])
        assert any(frame["file"] == "strategy.py" and frame["function"] == "validate_brief" for frame in frames)
        assert all(set(frame) == {"file", "line", "function"} for frame in frames)


def test_successful_planner_does_not_save_raw_output(env, monkeypatch):
    _, app, pid, asset = env
    async def planned(self, system, context, originals, max_tokens=7000):
        return {"kind": "reply", "message": "正常回复"}
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset], [], [], None))
    assert result["kind"] == "reply"
    assert not (app.state.settings.data_root / "diagnostics").exists()


def test_diagnostic_redacts_media_credentials_and_omits_oversize_output(env):
    settings = env[1].state.settings
    raw = {"api_key": "PRIVATE_SECRET", "headers": {"x-key": "PRIVATE_HEADER"},
        "request": {"messages": "PRIVATE_REQUEST"}, "image_base64": "PRIVATE_BINARY",
        "text": f"prefix {settings.deepseek_api_key} suffix", "unexpected_image": "data:image/png;base64,QUJDRA==",
        "story_summary": "可复现的普通生成内容"}
    save_failed_planner_output(settings, {"stage": "strategy", "raw_output": raw})
    directory = settings.data_root / "diagnostics"
    files = list(directory.glob("*.json"))
    assert len(files) == 1
    content = files[0].read_text()
    for secret in ("PRIVATE_SECRET", "PRIVATE_HEADER", "PRIVATE_REQUEST", "PRIVATE_BINARY", settings.deepseek_api_key, "QUJDRA=="):
        assert secret not in content
    assert "可复现的普通生成内容" in content
    save_failed_planner_output(settings, {"stage": "strategy", "raw_output": {"text": "a" * (200 * 1024)}})
    assert list(directory.glob("*.json")) == files


def test_diagnostic_write_failure_preserves_original_provider_error(env, monkeypatch):
    _, app, pid, asset = env
    async def planned(self, system, context, originals, max_tokens=7000):
        return {"kind": "reply", "message": "回复", "extra": "NEVER_PUBLIC"}
    def cannot_write(*args, **kwargs):
        raise PermissionError("DIAGNOSTIC_WRITE_FAILED_PRIVATE")
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    monkeypatch.setattr(gateway_module.os, "open", cannot_write)
    with pytest.raises(ProviderError, match="结构校验") as error:
        asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset], [], [], None))
    assert "DIAGNOSTIC_WRITE_FAILED_PRIVATE" not in str(error.value)
    assert not list((app.state.settings.data_root / "diagnostics").glob("*.json"))


def test_failed_storyboard_replay_keeps_bounded_validation_candidate(env):
    settings = env[1].state.settings
    save_failed_planner_output(settings, {"stage": "storyboard", "raw_output": {"segments": []},
        "validation_candidate": {"script": "已审定稿", "api_key": "PRIVATE_SECRET",
            "ad_script": {"beats": []}}})
    path = next((settings.data_root / "diagnostics").glob("*.json"))
    saved = json.loads(path.read_text())
    assert saved["raw_output"] == {"segments": []}
    assert saved["validation_candidate"]["script"] == "已审定稿"
    assert "PRIVATE_SECRET" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
