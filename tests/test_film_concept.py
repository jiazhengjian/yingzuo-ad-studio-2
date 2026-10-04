"""Film intent survives authoring and persistence; offline contracts, not a semantic eval.

All transport replies below are explicit fixtures. Passing these checks does not
establish that a real model respects a reference film or produces good concepts.
"""
import asyncio
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from app.ad_script import script_context
from app.db import Store
from app.direction_review import direction_review_schema, reviewed_candidates, validate_direction_review
from app.planner_runtime import planner_wire_context
from app.planning import StrategyDecision, strategy_decision_schema
from app.providers.gateway import Gateway, ProviderError
from app.strategy import DirectionCandidates, DirectionReviewSummary, select_rules
from test_conversation_api import env
from test_direction_selection import (
    approved_direction_review, candidates_for, concept_pool_for, historical_candidates,
    isolated_skill_loader, offer, select,
)


@pytest.fixture(autouse=True)
def no_unmocked_provider(monkeypatch):
    async def deny(*args, **kwargs):
        pytest.fail("Film-concept tests must not call a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)


def fresh(candidate):
    directions = [d.model_dump() for d in candidate.directions]
    return {"kind": "directions", "message": "比较三个广告概念", "brief": candidate.brief.model_dump(),
            "directions": directions, "concept_pool": concept_pool_for(directions)}


@pytest.mark.parametrize("location", ["brief", "direction"])
@pytest.mark.parametrize("value", ["omitted", None])
def test_new_directions_require_explicit_intent_and_concepts(env, location, value):
    raw = fresh(candidates_for(env))
    target, key = ((raw["brief"], "creative_intent") if location == "brief"
                   else (raw["directions"][1], "film_concept"))
    if value == "omitted":
        target.pop(key)
    else:
        target[key] = value
    with pytest.raises(ValidationError):
        StrategyDecision.model_validate(raw)


@pytest.mark.parametrize("value", ["", " \n\t ", None])
def test_film_concept_cannot_replace_the_key_visual_with_empty_text(env, value):
    raw = fresh(candidates_for(env))
    raw["directions"][0]["film_concept"]["key_visual"] = value
    with pytest.raises(ValidationError):
        StrategyDecision.model_validate(raw)


@pytest.mark.parametrize("field", ["memory_anchor", "sound_role"])
@pytest.mark.parametrize("value", ["", " \n\t ", None])
def test_new_film_concept_omits_memory_and_sound_without_inventing_them(env, field, value):
    raw = fresh(candidates_for(env))
    if value is None:
        raw["directions"][0]["film_concept"].pop(field)
    else:
        raw["directions"][0]["film_concept"][field] = value
    decision = StrategyDecision.model_validate(raw)
    assert not getattr(decision.directions[0].film_concept, field)


def test_use_context_and_film_location_remain_distinct_data(env):
    raw = fresh(candidates_for(env))
    intent = {"use_context": "给办公室顾客选择桌面用品", "setting_requirement": "全片纯影棚",
              "reference_intent": "围绕瓶形的产品概念展示", "structure_preference": "product_visual"}
    raw["brief"]["creative_intent"] = intent
    # One structural family can offer three visual ideas. Never force the three
    # available product structures into three slots just to make them different.
    for d in raw["directions"]:
        d["film_concept"]["structure"] = "product_visual"
    decision = StrategyDecision.model_validate(raw)
    assert decision.brief.creative_intent.model_dump() == intent
    assert {d.film_concept.structure for d in decision.directions} == {"product_visual"}


@pytest.mark.parametrize("bad_structure", ["world_intercut", "lifestyle"])
def test_explicit_product_reference_cannot_be_replaced_by_another_structure(env, bad_structure):
    candidate = candidates_for(env)
    raw = fresh(candidate)
    raw["brief"]["ad_form"] = "brand_tvc"
    raw["brief"]["creative_intent"]["structure_preference"] = "product_visual"
    for d in raw["directions"]:
        d["film_concept"]["structure"] = "product_visual"
    original_raw = deepcopy(raw)
    raw["directions"][1]["film_concept"]["structure"] = bad_structure
    with pytest.raises(ValidationError):
        StrategyDecision.model_validate(raw)
    original = candidate.model_copy(update={"brief": candidate.brief.model_validate(original_raw["brief"]),
        "directions": [type(candidate.directions[0]).model_validate(d) for d in original_raw["directions"]]})
    review = approved_direction_review(raw["directions"])
    with pytest.raises(ValueError):
        validate_direction_review(review, original, select_rules(None), product_reference_count=1)


@pytest.mark.parametrize("ad_form,bad_structure", [
    ("brand_tvc", "scene_action"), ("brand_tvc", "presenter_explanation"),
    ("scene_story", "product_visual"), ("scene_story", "presenter_explanation"),
    ("presenter_pitch", "product_visual"), ("presenter_pitch", "lifestyle"),
])
def test_declared_structure_must_match_selected_primary_form(env, ad_form, bad_structure):
    raw = fresh(candidates_for(env))
    raw["brief"]["ad_form"] = ad_form
    for direction in raw["directions"]:
        direction["film_concept"]["structure"] = bad_structure
    with pytest.raises(ValidationError):
        StrategyDecision.model_validate(raw)


def test_fresh_schema_and_revision_schema_do_not_leave_concept_nullable():
    for args in ({"new_directions": True}, {"retain_direction_concept": True}):
        schema = strategy_decision_schema(**args)
        direction = schema["$defs"]["CreativeDirection"]
        assert "film_concept" in direction["required"]
        assert direction["properties"]["film_concept"] == {"$ref": "#/$defs/FilmConcept"}
        brief = schema["$defs"]["ProductBrief"]
        assert "creative_intent" in brief["required"]
        assert brief["properties"]["creative_intent"] == {"$ref": "#/$defs/CreativeIntent"}
    historical = strategy_decision_schema()
    assert "film_concept" not in historical["$defs"]["CreativeDirection"]["required"]
    assert "creative_intent" not in historical["$defs"]["ProductBrief"]["required"]
    reviewer = direction_review_schema(8)
    assert "film_concept" in reviewer["$defs"]["CreativeDirection"]["required"]
    assert "film_concept_passed" in reviewer["$defs"]["AuthoredDirectionReviewCheck"]["required"]


@pytest.mark.parametrize("bad_value", ["omitted", None, "true", False])
def test_fresh_review_cannot_omit_or_overrule_failed_concept_check(env, bad_value):
    original = candidates_for(env)
    raw = approved_direction_review([d.model_dump() for d in original.directions])
    if bad_value == "omitted":
        raw["checks"][0].pop("film_concept_passed")
    else:
        raw["checks"][0]["film_concept_passed"] = bad_value
    with pytest.raises(ValidationError):
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    if bad_value is False:
        raw["ready"] = False
        negative = validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
        with pytest.raises(ValueError, match="未通过审核"):
            reviewed_candidates(negative, original, select_rules(None))



def test_review_cannot_publish_directions_after_removing_their_visual_concept(env):
    original = candidates_for(env)
    raw = approved_direction_review([d.model_dump() for d in original.directions])
    raw["directions"][0].pop("film_concept")
    with pytest.raises(ValueError):
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)

def test_historical_candidates_reviews_and_selection_do_not_invent_new_intent(env):
    raw = historical_candidates(env)
    audit = approved_direction_review(raw["directions"])
    audit.pop("directions")
    for check in audit["checks"]:
        check.pop("film_concept_passed")
    raw["direction_review"] = audit
    restored = DirectionCandidates.model_validate(raw)
    assert restored.model_dump() == raw
    assert DirectionReviewSummary.model_validate(audit).model_dump() == audit
    selected = restored.select("direction_1").model_dump()
    assert "creative_intent" not in selected["brief"]
    assert all("film_concept" not in d for d in selected["directions"])


def test_source_gate_selected_job_and_script_wire_preserve_film_concept(env):
    gate, source = offer(env)
    result = select(env, gate)
    assert result.status_code == 200
    original_store = env[1].state.store
    store = Store(original_store.db_path, original_store.media_root)
    source_candidates = store.get_job(source["id"])["payload"]["direction_candidates"]
    strategy = store.get_job(result.json()["job"]["id"])["payload"]["selected_strategy"]
    expected = next(d for d in source_candidates["directions"] if d["id"] == strategy["selected_direction_id"])
    assert strategy["brief"]["creative_intent"] == source_candidates["brief"]["creative_intent"]
    assert next(o for o in gate["options"] if o["id"] == expected["id"])["film_concept"] == expected["film_concept"]
    script = {**script_context(strategy, 20), "stage": "ad_script"}
    wire = planner_wire_context(script)
    assert wire["selected_direction"]["film_concept"] == expected["film_concept"]
    assert wire["creative_strategy"]["brief"]["creative_intent"] == strategy["brief"]["creative_intent"]
    # A normal revision projects away other choices but must keep this intent
    # in both the selected strategy and the answered gate provided to planning.
    revision = {"stage": "strategy", "stage_skills": {"skills": []},
        "existing_direction": expected, "direction_selection_required": False,
        "current_plan": {"strategy": strategy}, "gates": [{**gate, "status": "answered",
            "selected_option_id": expected["id"]}]}
    projected = planner_wire_context(revision)
    assert projected["gates"][0]["options"][0]["film_concept"] == expected["film_concept"]
    assert projected["current_plan"]["strategy"]["directions"][0]["film_concept"] == expected["film_concept"]
    assert projected["current_plan"]["strategy"]["brief"]["creative_intent"] == strategy["brief"]["creative_intent"]
    assert store.get_project(env[2])["plan"] is None
    assert [a["kind"] for a in store.project_detail(env[2])["assets"]] == ["product"]


@pytest.mark.parametrize("dropped", ["film_concept", "creative_intent"])
def test_normal_revision_cannot_silently_drop_authored_concept(env, monkeypatch, isolated_skill_loader, dropped):
    _, app, pid, asset = env
    candidate = candidates_for(env)
    selected = candidate.directions[1].model_dump()
    brief = candidate.brief.model_dump()
    (selected if dropped == "film_concept" else brief).pop(dropped)
    calls = []
    async def reply(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        assert context["stage"] == "strategy", "invalid revision reached script generation"
        assert "film_concept" in context["schema"]["$defs"]["CreativeDirection"]["required"]
        return {"kind": "plan", "message": "只缩短台词", "brief": brief,
            "directions": [selected], "selected_direction_id": selected["id"]}
    monkeypatch.setattr(Gateway, "_planner_json", reply)
    current = {"strategy": candidate.select("direction_1").model_dump()}
    before = deepcopy(current)
    with pytest.raises(ProviderError, match="策划回复未通过"):
        asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
            [{"role": "user", "content": "把台词改短一点"}], [], current))
    assert current == before and calls == ["strategy", "strategy"]


def accepted_product_plan():
    return {"facts": {"name": "吹风机"}, "strategy": {
        "brief": {"product_name": "吹风机", "audience": "专业造型师", "scenario": "理发店",
            "creative_intent": {"use_context": "供专业造型师在理发店使用", "setting_requirement": "",
                "reference_intent": "纯产品概念展示，产品本身组织画面", "structure_preference": "product_visual"},
            "facts": [{"id": "form", "text": "圆环形出风口", "source_kind": "image", "source_id": "test_photo"}]},
        "selected_direction_id": "product", "directions": [
            {"id": "product", "scenario": "深色影棚，产品独立悬置", "evidence_ids": ["form"],
                "film_concept": {"structure": "product_visual", "key_visual": "环形光与出风口轮廓相接",
                    "memory_anchor": "完整产品的圆环轮廓", "sound_role": "音乐与气流音效，不设旁白"}},
            {"id": "old", "scenario": "未选中的理发店故事", "evidence_ids": []} ]},
        "visual_direction": {"scene": "深色影棚，产品独立悬置", "lighting": "轮廓光"},
        "segments": [{"prompt": "已确认产品概念画面"}]}


def test_scene_and_copy_handoff_do_not_turn_product_use_context_back_into_setting():
    import json
    from app.design import scene_design_request
    from app.planning import advertising_copy_brief
    plan = accepted_product_plan()
    before = deepcopy(plan)
    request = scene_design_request({"brief": "给理发店用的吹风机做广告"}, plan)
    scene = json.loads(request["prompt"].split("已确认的场景与视觉方案：\n", 1)[1])
    assert request["version"] == "scene-context-v4"
    assert scene["confirmed_scenario"] == request["scenario"] == "深色影棚，产品独立悬置"
    assert "理发店" not in request["prompt"]
    copy = advertising_copy_brief(plan)
    assert copy["scenario"] == "深色影棚，产品独立悬置"
    assert copy["creative_intent"] == plan["strategy"]["brief"]["creative_intent"]
    assert copy["film_concept"] == plan["strategy"]["directions"][0]["film_concept"]
    assert copy["supported_facts"] == plan["strategy"]["brief"]["facts"]
    assert plan == before


def test_legacy_scene_and_copy_handoff_keep_the_previously_accepted_setting():
    from app.design import scene_design_request
    from app.planning import advertising_copy_brief
    plan = accepted_product_plan()
    plan["strategy"]["brief"].pop("creative_intent")
    for direction in plan["strategy"]["directions"]:
        direction.pop("film_concept", None)
    before = deepcopy(plan)
    request = scene_design_request({}, plan)
    assert request["version"] == "scene-context-v3"
    assert request["scenario"] == "理发店"
    copy = advertising_copy_brief(plan)
    assert copy["scenario"] == "理发店"
    assert "creative_intent" not in copy and "film_concept" not in copy
    assert plan == before
