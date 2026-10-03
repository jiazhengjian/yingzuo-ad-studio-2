"""Narrow planner JSON boundary recovery, with no live provider or user data."""
import asyncio
from copy import deepcopy
import json
import logging
import traceback

import httpx
import pytest

from app.providers.gateway import Gateway, PlannerInvalidResponse, ProviderError, _decode_planner_json
from test_providers import install_transport, request_context, settings, source, strategy_stage


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("JSON boundary tests must not reach a real provider")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    for method in ("create_designs", "submit_video", "synthesize_voice"):
        monkeypatch.setattr(Gateway, method, denied)


@pytest.fixture
def isolated_settings(settings, tmp_path):
    settings.data_root = tmp_path / "isolated-data"
    settings.planner_retry_delay_seconds = 0
    return settings


def stopped_output(content, finish_reason="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason,
        "message": {"content": content, "reasoning_content": "PRIVATE_REASONING_NOT_FOR_LOGS"}}]})


@pytest.mark.parametrize("raw", [
    '{}', ' \t\r\n{"ready":false,"nested":{"items":[1,true,null]}}\n',
    '{"text":"字符串里的 } 不是边界","escaped":"\\\"}\\\\","unknown":{"keep":true}}',
    '[]', '[{"keep":true}]', 'null', 'false', '3', '"a string }"',
])
def test_normal_json_keeps_standard_parser_semantics_without_correction(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        assert _decode_planner_json(raw) == json.loads(raw)
    assert "boundary normalized" not in caplog.text


@pytest.mark.parametrize("prefix,suffix", [('', '}'), (' \t\r\n', '\r\n } \t\n')])
@pytest.mark.parametrize("value", [
    {}, {"ready": False, "unknown": {"keep": [None, True, 3]}},
    {"text": '中文、括号 } { 和转义引号 "、反斜杠 \\', "locked": "原句保持完整。"},
])
def test_only_single_redundant_brace_preserves_every_decoded_value(prefix, suffix, value, caplog):
    before = deepcopy(value)
    raw = prefix + json.dumps(value, ensure_ascii=False) + suffix
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        assert _decode_planner_json(raw) == value
    assert value == before
    records = [r.getMessage() for r in caplog.records if r.name == "app.providers.gateway"]
    assert len(records) == 1
    assert json.loads(records[0].split(": ", 1)[1]) == {
        "correction": "single_redundant_closing_brace", "content_characters": len(raw)}


@pytest.mark.parametrize("raw", [
    '{} {}', '{} []', '{} null', '{} true', '{} 3', '{} "second"',
    '{} second text', '{} } text', '{} } {}', '{} }}', '{} } }',
    '[]}', '[{"a":1}]}', 'null}', 'true}', '3}', '"value"}',
    '{"a":', '{"a":1', '{"nested":{"a":1}', '{"a":"unterminated}',
    '{"a":1,}}', '{"a":]}', '```json\n{}\n```}',
    '\ufeff{}}', '\u00a0{}}', '{}\u00a0}', '{} }\u00a0', '{}\v}', '{} }\f',
])
def test_boundary_recovery_never_discards_second_values_or_repairs_incomplete_json(raw, caplog):
    with pytest.raises(json.JSONDecodeError):
        _decode_planner_json(raw)
    assert "boundary normalized" not in caplog.text


def test_gateway_accepts_redundant_brace_without_storing_successful_output(
        isolated_settings, install_transport, caplog):
    value = {"ad_script": {"beats": [{"id": "b1", "narration": "PRIVATE_AUTHORED_LINE"}]},
        "shots": [{"id": "s1", "description": "PRIVATE_SHOT_DETAIL"}], "unknown": "kept"}
    raw = json.dumps(value) + '}'
    calls = []
    def respond(request):
        calls.append(request_context(request)[1])
        return stopped_output(raw)
    install_transport(respond)
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        actual = asyncio.run(Gateway(isolated_settings)._planner_json(
            "PRIVATE_SYSTEM", {"stage": "plan_refinement", "merchant_request": "PRIVATE_REQUEST"}, []))
    assert actual == value
    assert len(calls) == 1
    assert "PRIVATE" not in caplog.text
    assert not (isolated_settings.data_root / "diagnostics").exists()


@pytest.mark.parametrize("raw", ['[]', 'null', 'true', '3', '"PRIVATE_SCALAR"', '[]}',
    '{"PRIVATE_KEY":"PRIVATE_VALUE"} {"second":true}', '{"PRIVATE_KEY":"PRIVATE_VALUE"}}}'])
def test_gateway_still_rejects_non_objects_and_unsupported_boundaries_safely(
        isolated_settings, install_transport, caplog, raw):
    install_transport(lambda request: stopped_output(raw))
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(PlannerInvalidResponse) as error:
            asyncio.run(Gateway(isolated_settings)._planner_json("json", {"stage": "plan_refinement"}, []))
    public_error = "".join(traceback.format_exception(error.value))
    assert "PRIVATE" not in public_error and "PRIVATE" not in caplog.text
    assert "boundary normalized" not in caplog.text
    files = list((isolated_settings.data_root / "diagnostics").glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["raw_output"] == {"unparsed_content": raw}
    assert "PRIVATE_REASONING" not in files[0].read_text()
    assert files[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", None])
def test_even_complete_object_with_redundant_brace_requires_stopped_response(
        isolated_settings, install_transport, caplog, finish_reason):
    install_transport(lambda request: stopped_output('{"value":"PRIVATE_BODY"}}', finish_reason))
    with pytest.raises(PlannerInvalidResponse):
        asyncio.run(Gateway(isolated_settings)._planner_json("json", {"stage": "plan_refinement"}, []))
    assert "boundary normalized" not in caplog.text and "PRIVATE" not in caplog.text


@pytest.mark.parametrize("failure", ["schema", "foreign_source"])
def test_recovered_object_still_passes_through_strategy_schema_and_fact_guards(
        isolated_settings, install_transport, source, strategy_stage, caplog, failure):
    value = deepcopy(strategy_stage)
    if failure == "schema":
        value["unexpected"] = "PRIVATE_INVALID_FIELD"
    else:
        value["brief"]["facts"][0].update(source_kind="image", source_id="PRIVATE_FOREIGN_IMAGE")
    calls = []
    def respond(request):
        calls.append(request_context(request)[1])
        return stopped_output(json.dumps(value) + '}')
    install_transport(respond)
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(ProviderError) as error:
            asyncio.run(Gateway(isolated_settings).decide({"brief": "蓝瓶商品"}, [source], [], [], None))
    # This legacy fixture has no direction-selection correction loop; an invalid
    # recovered object must stop before any script, storyboard or media stage.
    assert [context["stage"] for context in calls] == ["strategy"]
    assert ("extra_forbidden" if failure == "schema" else "validate_brief") in caplog.text
    files = list((isolated_settings.data_root / "diagnostics").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["raw_output"] == value
    assert "PRIVATE" not in str(error.value) and "PRIVATE" not in caplog.text
