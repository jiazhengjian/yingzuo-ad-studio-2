"""Protocol metadata is server-owned; production content remains strict."""
import asyncio
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from app.production import (ProductionPlan, derive_authored_production_metadata,
                            validate_production_sources)
from app.providers.gateway import Gateway, ProviderError
from app.strategy import StrategyRecord, build_strategy, select_rules
from test_conversation_api import env, post_turn
from test_direction_selection import candidates_for, approved_direction_review, concept_pool_for, isolated_skill_loader


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Metadata tests must never call a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.mark.parametrize("emitted_version", ["1.1.0", None, "omitted"])
def test_gateway_owns_version_for_authored_strategy_and_review(env, monkeypatch, isolated_skill_loader, emitted_version):
    _, app, pid, asset = env
    app.state.settings.direction_selection_enabled = True
    candidates = candidates_for(env)
    calls, outputs = [], []

    async def planned(self, system, context, originals, max_tokens=7000):
        stage = context["stage"]
        calls.append(stage)
        production_schema = context["schema"]["$defs"]["ProductionPlan"]
        assert "version" not in production_schema["properties"]
        assert "version" not in production_schema.get("required", [])
        assert production_schema["additionalProperties"] is False
        if stage == "strategy":
            raw = {"kind": "directions", "message": "三个候选", "brief": candidates.brief.model_dump(),
                   "directions": [d.model_dump() for d in candidates.directions],
                   "concept_pool": concept_pool_for(candidates.directions)}
        else:
            assert stage == "direction_review"
            assert all(d["production"]["version"] == "1.0.0" for d in context["directions"])
            raw = approved_direction_review(context["directions"])
        for direction in raw["directions"]:
            if emitted_version == "omitted":
                direction["production"].pop("version")
            else:
                direction["production"]["version"] = emitted_version
        outputs.append((raw, deepcopy(raw)))
        return raw

    monkeypatch.setattr(Gateway, "_planner_json", planned)
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid),
        [app.state.store.get_asset(asset["id"])], [], [], None))
    assert calls == ["strategy", "direction_review"]  # No model retry for server metadata.
    assert all(raw == before for raw, before in outputs)  # Checkpoints retain exact model output.
    assert result["direction_candidates"]["directions"] == [d.model_dump() for d in candidates.directions]
    job = post_turn(env, content="策划广告").json()["job"]
    assert app.state.store.claim_job(job["id"])
    app.state.store.finish_decision(job["id"], result)
    saved = app.state.store.get_job(job["id"])["payload"]["direction_candidates"]
    assert all(d["production"]["version"] == "1.0.0" for d in saved["directions"])


def test_saved_selected_strategy_keeps_strict_protocol_validation(env, monkeypatch, isolated_skill_loader):
    _, app, pid, asset = env
    candidates = candidates_for(env)
    strategy = build_strategy(candidates.brief, candidates.directions, candidates.directions[0].id,
                              select_rules(None), merchant_choice=True).model_dump()
    strategy["directions"][0]["production"]["version"] = "1.1.0"
    with pytest.raises(ValidationError):
        StrategyRecord.model_validate(strategy)
    with pytest.raises(ValidationError):
        ProductionPlan.model_validate(strategy["directions"][0]["production"])

    async def denied(*args, **kwargs):
        pytest.fail("A saved invalid contract must fail before model authoring")
    monkeypatch.setattr(Gateway, "_planner_json", denied)
    with pytest.raises(ProviderError):
        asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid),
            [app.state.store.get_asset(asset["id"])], [], [], None, selected_strategy=strategy))


@pytest.mark.parametrize("problem", ["speaker", "evidence", "extra_field", "missing_constraints"])
def test_metadata_derivation_does_not_repair_invalid_content(env, problem):
    production = candidates_for(env).directions[0].production.model_dump()
    production["version"] = "1.1.0"
    if problem == "speaker":
        production.update(speech_mode="character_dialogue", speaker_id="char_invented")
    elif problem == "evidence":
        production["product_constraints"][0]["evidence_ids"] = ["invented_fact"]
    elif problem == "extra_field":
        production["inferred_fact"] = "invented"
    else:
        production.pop("product_constraints")
    derived = derive_authored_production_metadata({"directions": [{"production": production}]})
    with pytest.raises(ValueError):
        validate_production_sources(derived["directions"][0]["production"], candidates_for(env).brief.facts)


@pytest.mark.parametrize("production", [None, [], "invalid"])
def test_metadata_derivation_does_not_invent_missing_or_malformed_content(production):
    raw = {"directions": [{"production": production}, {"id": "missing_production"}],
           "other": {"production": {"version": "1.1.0"}}}
    assert derive_authored_production_metadata(raw) == raw
