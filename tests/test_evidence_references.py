"""Wire aliases reduce copying errors without changing source or quote trust."""
import asyncio
from copy import deepcopy
import json

import httpx

from app.config import Settings
from app.evidence_references import strategy_evidence_request, restore_merchant_references
from app.providers.gateway import Gateway


def test_short_references_round_trip_only_registered_merchant_facts():
    source_id = "message_0123456789abcdef0123456789abcdef"
    context = {"stage": "strategy", "stage_skills": {"skills": []},
        "evidence_sources": {source_id: "包括产品本省采用等离子技术，护发", "merchant_1": "不是恒温"},
        "previous_response": {"brief": {"facts": [
            {"id": "f1", "source_kind": "merchant", "source_id": source_id, "text": "包括产品本省采用等离子技术，护发"},
            {"id": "image", "source_kind": "image", "source_id": source_id, "text": "银灰色"}]}}}
    original = deepcopy(context)
    wire, aliases = strategy_evidence_request(context)
    assert not set(aliases) & set(context["evidence_sources"])
    assert all(len(alias) < 16 for alias in aliases)
    assert sorted(wire["evidence_sources"].values()) == sorted(context["evidence_sources"].values())
    assert wire["previous_response"]["brief"]["facts"][0]["source_id"] in aliases
    assert wire["previous_response"]["brief"]["facts"][1] == original["previous_response"]["brief"]["facts"][1]
    assert restore_merchant_references(wire["previous_response"], aliases) == original["previous_response"]
    assert context == original


def test_wrong_alias_and_quote_are_not_guessed_or_rewritten():
    result = {"brief": {"facts": [{"id": "f1", "source_kind": "merchant", "source_id": "merchant1",
        "text": "包括我产品本省采用等离子技术，护发"}]}}
    before = deepcopy(result)
    assert restore_merchant_references(result, {"merchant_1": "real_source"}) == before
    assert result == before
    for context in ({"stage": "direction_review", "evidence_sources": {"s": "原文"}},
                    {"stage": "strategy", "evidence_sources": {"s": "原文"}}):
        assert strategy_evidence_request(context) == (context, {})


def test_actual_gateway_sends_short_registry_and_returns_canonical_ids(tmp_path, monkeypatch):
    source_id = "message_0123456789abcdef0123456789abcdef"
    quote = "没有确认恒温，已说明护发用途"
    context = {"stage": "strategy", "stage_skills": {"skills": []},
        "evidence_sources": {source_id: quote},
        "validation_feedback": [{"loc": ["brief", "facts", 0, "source_id"],
            "allowed_source_ids": [source_id], "matching_source_ids": [source_id]}]}
    original = deepcopy(context)
    requests = []

    def respond(request):
        requests.append(request)
        wire = json.loads(json.loads(request.content)["messages"][1]["content"][0]["text"])
        alias, = wire["evidence_sources"]
        assert alias != source_id and wire["evidence_sources"][alias] == quote
        assert wire["validation_feedback"][0]["allowed_source_ids"] == [alias]
        assert wire["validation_feedback"][0]["matching_source_ids"] == [alias]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"brief": {"facts": [{"id": "f1", "source_kind": "merchant",
                "source_id": alias, "text": quote}]}}, ensure_ascii=False)}}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    result = asyncio.run(Gateway(Settings(data_root=tmp_path, planner_provider="tudou",
        planner_model="gpt-6-sol", tudou_api_key="TEST_ONLY"))._planner_json("Return JSON", context, []))
    assert result["brief"]["facts"] == [{"id": "f1", "source_kind": "merchant", "source_id": source_id, "text": quote}]
    assert len(requests) == 1 and context == original
