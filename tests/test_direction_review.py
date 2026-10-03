"""Bounded independent direction review with mocked text-only transport."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from app import runtime_skills
from app.direction_review import (DirectionReview, direction_review_schema,
                                  reviewed_candidates, validate_direction_review)
from app.pipeline import Pipeline
from app.providers.gateway import Gateway
from app.strategy import DirectionCandidates, select_rules
from test_conversation_api import env, post_turn
from test_direction_selection import (approved_direction_review, candidates_for,
                                      isolated_skill_loader, offer)
from test_providers import chat_output, request_context, assert_skill_wire_matches_source


@pytest.fixture(autouse=True)
def forbid_unmocked_network(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Direction review tests must never call a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


def test_review_can_correct_action_state_without_rewriting_facts_or_identity(env):
    original = candidates_for(env).model_dump()
    original["brief"]["product_name"] = "蛋糕切块"
    original["brief"]["category"] = "food"
    original["brief"]["facts"][0]["text"] = "原图是一块蛋糕切块"
    original["directions"][0]["story_summary"] = "两人各取一小角后盘子就空了。"
    original["directions"][0]["payoff"] = "一块刚好两人份，两口吃完，空盘收束。"
    candidates = DirectionCandidates.model_validate(original)
    before = candidates.model_dump()
    raw = approved_direction_review([d.model_dump() for d in candidates.directions])
    raw["directions"][0].update(story_summary="两位同事各取一小角，盘上仍留下带两个缺口的蛋糕。两人相视一笑，休息片刻。",
        payoff="镜头停在留有两个缺口的蛋糕与两位同事的短暂休息，不断言商品份量。")
    raw["checks"][0]["note"] = "已将空盘改为剩余蛋糕，去掉未经确认的两口吃完和两人份断言。"
    checked = validate_direction_review(raw, candidates, select_rules(None), product_reference_count=1)
    result = reviewed_candidates(checked, candidates, select_rules(None))
    assert candidates.model_dump() == before
    assert result.brief.model_dump() == before["brief"]
    assert [(d.id, d.rule_id) for d in result.directions] == [(d.id, d.rule_id) for d in candidates.directions]
    assert result.rules_used == candidates.rules_used
    assert result.recommended_direction_id == candidates.recommended_direction_id
    assert result.directions[0].story_summary == raw["directions"][0]["story_summary"]
    assert result.direction_review.checks[0].note == raw["checks"][0]["note"]
    assert "directions" not in result.direction_review.model_dump()


@pytest.mark.parametrize("mutation", ["reorder", "id", "rule", "evidence", "constraint_evidence",
    "missing_story", "missing_attraction", "missing_production", "check_id", "over_capacity"])
def test_review_rejects_changed_contract_or_invalid_sources(env, mutation):
    candidates = candidates_for(env)
    raw = approved_direction_review([d.model_dump() for d in candidates.directions])
    if mutation == "reorder":
        raw["directions"].reverse()
    elif mutation == "id":
        raw["directions"][0]["id"] = "invented"
    elif mutation == "rule":
        raw["directions"][0]["rule_id"] = "everyday_context"
    elif mutation == "evidence":
        raw["directions"][0]["evidence_ids"] = ["invented_fact"]
    elif mutation == "constraint_evidence":
        raw["directions"][0]["production"]["product_constraints"][0]["evidence_ids"] = ["invented_fact"]
    elif mutation in ("missing_story", "missing_attraction"):
        raw["directions"][0]["story_summary" if mutation == "missing_story" else "attraction_mechanism"] = "  "
    elif mutation == "missing_production":
        raw["directions"][0].pop("production")
    elif mutation == "check_id":
        raw["checks"][0]["direction_id"] = "invented"
    elif mutation == "over_capacity":
        raw["directions"][0]["production"]["characters"] = [
            {"id": f"char_{i}", "name": f"角色{i}", "description": "创作设定：办公室成年同事"} for i in range(8)]
    with pytest.raises((ValueError, ValidationError)):
        validate_direction_review(raw, candidates, select_rules(None), product_reference_count=3)


@pytest.mark.parametrize("mutation", ["brief", "extra_check", "missing_check", "duplicate_check",
    "ready_mismatch", "distinct_mismatch", "boolean_string", "no_conclusion"])
def test_review_requires_closed_schema_complete_verdict_and_observable_notes(env, mutation):
    raw = approved_direction_review([d.model_dump() for d in candidates_for(env).directions])
    if mutation == "brief":
        raw["brief"] = {"facts": [{"text": "刚好两人份"}]}
    elif mutation == "extra_check":
        raw["checks"][0]["analysis"] = "hidden reasoning is not a review conclusion"
    elif mutation == "missing_check":
        raw["checks"].pop()
    elif mutation == "duplicate_check":
        raw["checks"][0] = deepcopy(raw["checks"][1])
    elif mutation == "ready_mismatch":
        raw["checks"][0]["physical_plausibility_passed"] = False
    elif mutation == "distinct_mismatch":
        raw["distinct_directions_passed"] = False
    elif mutation == "boolean_string":
        raw["ready"] = "true"
    elif mutation == "no_conclusion":
        raw["checks"][0]["note"] = " \n "
    with pytest.raises(ValidationError):
        DirectionReview.model_validate(raw)


def test_negative_review_is_valid_but_cannot_be_published_as_candidates(env):
    candidates = candidates_for(env)
    raw = approved_direction_review([d.model_dump() for d in candidates.directions])
    raw["ready"] = False
    raw["checks"][0]["physical_plausibility_passed"] = False
    raw["checks"][0]["note"] = "动作结束状态仍与实际取走量矛盾，不能通过。"
    review = validate_direction_review(raw, candidates, select_rules(None), product_reference_count=1)
    assert not review.ready
    with pytest.raises(ValueError, match="未通过审核"):
        reviewed_candidates(review, candidates, select_rules(None))


def test_historical_candidate_dump_does_not_add_review_and_new_schema_closes_fields(env):
    candidate = candidates_for(env)
    assert "direction_review" not in candidate.model_dump()
    assert DirectionCandidates.model_validate(candidate.model_dump()).model_dump() == candidate.model_dump()
    schema = direction_review_schema(6)
    assert schema["additionalProperties"] is False
    assert "brief" not in schema["properties"]
    assert {"story_summary", "attraction_mechanism", "production"} <= set(schema["$defs"]["CreativeDirection"]["required"])
    assert schema["$defs"]["ProductionPlan"]["properties"]["characters"]["maxItems"] == 6


def test_review_transport_uses_same_model_high_budget_original_image_and_persists_audit(env, monkeypatch):
    _, app, pid, asset = env
    asset = app.state.store.get_asset(asset["id"])
    app.state.settings.direction_selection_enabled = True
    candidate = candidates_for(env)
    # The current request must work independently of the retired instruction
    # package; schema-version metadata remains compatible elsewhere.
    def legacy_loader_forbidden():
        raise AssertionError("Current stage flow must not read legacy instructions")
    monkeypatch.setattr("app.providers.gateway.load_ad_expression_skill", legacy_loader_forbidden)
    calls, progresses, traces = [], [], []
    service_contexts, expected_traces = {}, []
    load = runtime_skills.stage_skill_context

    def capture(stage, mode=None):
        loaded = load(stage, mode)
        service_contexts[stage] = deepcopy(loaded)
        return loaded

    monkeypatch.setattr(runtime_skills, "stage_skill_context", capture)
    real_client = httpx.AsyncClient
    def handle(request):
        payload, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        assert "ad_expression_skill" not in context
        shared = context["stage_skills"]["shared_references"]
        assert {item["id"] for item in shared} == {"product-evidence", "ad-form-contract"}
        assert len(shared) == 2
        if stage == "strategy":
            assert context["orchestrator_skills"]["shared_references"] == []
        loaded_stages = ["orchestrator", stage] if stage == "strategy" else [stage]
        for loaded_stage in loaded_stages:
            loaded = service_contexts[loaded_stage]
            wire = context["orchestrator_skills" if loaded_stage == "orchestrator" else "stage_skills"]
            assert_skill_wire_matches_source(wire, loaded)
            expected_traces.extend([*runtime_skills.skill_trace(loaded), *runtime_skills.shared_reference_trace(loaded)])
        assert payload["model"] == app.state.settings.planner_model
        if stage == "strategy":
            return chat_output({"kind": "directions", "message": "初稿", "brief": candidate.brief.model_dump(),
                "directions": [d.model_dump() for d in candidate.directions]})
        assert stage == "direction_review"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        assert payload["max_tokens"] == 65536
        assert context["immutable_brief"] == candidate.brief.model_dump()
        assert context["conversation"] == [{"role": "user", "content": "办公室广告，不说份量"}]
        assert context["reference_capacity"] == {"max_total_images": 10, "product_reference_count": 1,
            "scene_reference_count": 1, "max_characters": 8, "character_reference_images_each": 1}
        assert len(payload["messages"][1]["content"]) == 2
        assert payload["messages"][1]["content"][1]["type"] == "image_url"
        raw = approved_direction_review(context["directions"])
        raw["directions"][0]["label"] = "审后保留的观看方向"
        raw["summary"] = "保留事实与原场景，三个实际展开不同；未添加商品份量或功能承诺。"
        return chat_output(raw)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: real_client(*args, transport=httpx.MockTransport(handle), **kwargs))
    result = asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
        [{"role": "user", "content": "办公室广告，不说份量"}], [], None,
        on_progress=progresses.append, on_skill_trace=traces.append))
    assert calls == ["strategy", "direction_review"]
    assert progresses == ["reviewing_directions"]
    assert result["direction_candidates"]["directions"][0]["label"] == "审后保留的观看方向"
    assert result["direction_candidates"]["direction_review"]["ready"]
    assert any(trace["stage"] == "direction_review" for trace in traces)
    assert traces == expected_traces
    assert all("content" not in trace for trace in traces)
    shared_traces = [trace for trace in traces if trace.get("execution_kind") == "shared_reference"]
    assert {(trace["stage"], trace["id"]) for trace in shared_traces} == {
        (stage, identifier) for stage in ("strategy", "direction_review")
        for identifier in ("product-evidence", "ad-form-contract")}
    assert len(shared_traces) == 4
    assert "plan" not in result or result["plan"] is None
    # Persistence revalidates rule provenance without discarding the audit.
    job = post_turn(env, client_turn_id="store-reviewed", content="策划广告").json()["job"]
    assert app.state.store.claim_job(job["id"])
    app.state.store.finish_decision(job["id"], result)
    saved = app.state.store.get_job(job["id"])["payload"]["direction_candidates"]
    assert saved == result["direction_candidates"]
    assert app.state.store.pending_gate(pid)["options"][0]["label"] == "审后保留的观看方向"


def test_failed_review_stops_once_preserves_existing_assets_and_does_not_offer_unreviewed_prose(env, monkeypatch, isolated_skill_loader):
    _, app, pid, asset = env
    app.state.settings.direction_selection_enabled = True
    gate, source = offer(env)
    before_source = deepcopy(app.state.store.get_job(source["id"])["payload"]["direction_candidates"])
    candidate = candidates_for(env)
    calls = []
    async def planned(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        if context["stage"] == "strategy":
            return {"kind": "directions", "message": "未审初稿", "brief": candidate.brief.model_dump(),
                "directions": [d.model_dump() for d in candidate.directions]}
        assert context["stage"] == "direction_review"
        raw = approved_direction_review(context["directions"])
        raw["ready"] = False
        raw["checks"][0]["physical_plausibility_passed"] = False
        raw["checks"][0]["note"] = "动作后的商品数量无法自洽，本次未完成修订。"
        return raw
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    job = post_turn(env, client_turn_id="rejected-review", content="重新给我三个创意方向").json()["job"]
    result = asyncio.run(Pipeline(app.state.store, Gateway(app.state.settings), app.state.settings).execute(job["id"]))
    assert result["status"] == "failed"
    assert "创意方向尚未通过内容检查" in result["error"]
    assert calls == ["strategy", "direction_review"]
    detail = app.state.store.project_detail(pid)
    assert [a["id"] for a in detail["assets"]] == [asset["id"]]
    assert detail["project"]["plan"] is None
    assert len(detail["gates"]) == 1 and detail["gates"][0]["options"] == gate["options"]
    assert app.state.store.get_job(source["id"])["payload"]["direction_candidates"] == before_source
    assert "direction_candidates" not in app.state.store.get_job(job["id"])["payload"]
    diagnostics = list((app.state.settings.data_root / "diagnostics").glob("*.json"))
    assert len(diagnostics) == 1
    assert json.loads(diagnostics[0].read_text())["stage"] == "direction_review"


def test_cancellation_callback_prevents_second_request(env, monkeypatch, isolated_skill_loader):
    from app.db import StaleRevision
    _, app, pid, asset = env
    app.state.settings.direction_selection_enabled = True
    candidate = candidates_for(env)
    calls = []
    async def planned(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        return {"kind": "directions", "message": "初稿", "brief": candidate.brief.model_dump(),
            "directions": [d.model_dump() for d in candidate.directions]}
    def canceled(phase):
        assert phase == "reviewing_directions"
        raise StaleRevision()
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    with pytest.raises(StaleRevision):
        asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset], [], [], None,
                                                     on_progress=canceled))
    assert calls == ["strategy"]
