"""Stage-two contracts; synthetic replies do not measure creative quality."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.ad_script import script_context
from app.db import Store
from app.direction_review import direction_review_schema, validate_direction_review, reviewed_candidates
from app.planner_runtime import planner_wire_context
from app.planning import StrategyDecision, strategy_decision_schema
from app.strategy import DirectionCandidates, DirectionReviewSummary, select_rules
from test_conversation_api import env
from test_direction_selection import candidates_for, approved_direction_review, concept_pool_for, offer, select


@pytest.mark.parametrize("field", ["audience_value", "product_expression"])
@pytest.mark.parametrize("value", [None, "", " \n\t "])
def test_new_offers_require_value_and_execution_without_fabricated_defaults(env, field, value):
    candidate = candidates_for(env)
    raw = {"kind": "directions", "message": "选择概念", "brief": candidate.brief.model_dump(),
           "directions": [d.model_dump() for d in candidate.directions],
           "concept_pool": concept_pool_for(candidate.directions)}
    if value is None:
        raw["directions"][0].pop(field)
    else:
        raw["directions"][0][field] = value
    with pytest.raises(ValidationError, match="受众为何在意"):
        StrategyDecision.model_validate(raw)


def test_schema_requires_value_for_fresh_and_authored_revisions_only():
    for field in ("audience_value", "product_expression"):
        for args in ({"new_directions": True}, {"retain_direction_value": True}):
            schema = strategy_decision_schema(**args)["$defs"]["CreativeDirection"]
            assert field in schema["required"] and schema["properties"][field]["minLength"] == 1
            assert "default" not in schema["properties"][field]
        assert field not in strategy_decision_schema(retain_direction_story=True)["$defs"]["CreativeDirection"]["required"]
        assert field in direction_review_schema(8)["$defs"]["CreativeDirection"]["required"]


def test_one_supported_value_can_have_three_executions_but_not_duplicate_development(env):
    raw = candidates_for(env).model_dump()
    for d in raw["directions"]:
        d["angle"] = "瓶身轮廓"
        d["evidence_ids"] = ["appearance"]
    candidate = DirectionCandidates.model_validate(raw)
    assert candidate.select("direction_1").directions[1].angle == "瓶身轮廓"
    raw["directions"][2].update({key: raw["directions"][0][key] for key in ("development", "payoff")})
    with pytest.raises(ValidationError, match="展开与收束"):
        DirectionCandidates.model_validate(raw)


@pytest.mark.parametrize("field", ["claim_support_passed", "expression_form_passed", "product_role_passed"])
def test_fresh_review_requires_content_checks_and_cannot_publish_negative_verdict(env, field):
    original = candidates_for(env)
    raw = approved_direction_review([d.model_dump() for d in original.directions])
    raw["checks"][0].pop(field)
    with pytest.raises(ValidationError):
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    raw["checks"][0][field] = False
    with pytest.raises(ValidationError, match="ready"):
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    raw["ready"] = False
    review = validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    with pytest.raises(ValueError, match="未通过审核"):
        reviewed_candidates(review, original, select_rules(None))


def test_pre_stage_two_offer_and_review_round_trip_without_new_claims(env):
    raw = candidates_for(env).model_dump()
    raw["brief"].pop("creative_intent")
    for d in raw["directions"]:
        d.pop("film_concept")
        d.pop("audience_value")
        d.pop("product_expression")
    audit = approved_direction_review(raw["directions"])
    audit.pop("directions")
    for check in audit["checks"]:
        check.pop("film_concept_passed")
        check.pop("claim_support_passed")
        check.pop("expression_form_passed")
    raw["direction_review"] = audit
    assert DirectionCandidates.model_validate(raw).model_dump() == raw
    assert DirectionReviewSummary.model_validate(audit).model_dump() == audit


def test_review_can_correct_presenter_concept_in_same_request_but_not_approve_voiceover_montage(env):
    raw = candidates_for(env).model_dump()
    raw["brief"]["ad_form"] = "presenter_pitch"
    for direction in raw["directions"]:
        direction["film_concept"]["structure"] = "presenter_explanation"
    original = DirectionCandidates.model_validate(raw)
    before = deepcopy(raw)
    reviewed = approved_direction_review(raw["directions"])
    with pytest.raises(ValueError, match="人物讲解方向"):
        validate_direction_review(reviewed, original, select_rules(None), product_reference_count=1)
    for d in reviewed["directions"]:
        d["production"].update(characters=[{"id": "char_host", "name": "讲解者",
            "description": "创作角色：成年桌面用品讲解者"}], speech_mode="character_dialogue", speaker_id="char_host")
        d["product_expression"] = "讲解者面向观众介绍瓶形，手持商品帮助辨认轮廓，不宣称亲测功能。"
        d["story_summary"] = "讲解者提出桌面布置的选择，展示瓶形并说明为什么留下它，收束到完整商品。"
    result = validate_direction_review(reviewed, original, select_rules(None), product_reference_count=1)
    assert result.ready and original.model_dump() == before


def test_selected_value_chain_survives_store_reopen_and_script_handoff(env):
    gate, _ = offer(env)
    response = select(env, gate)
    assert response.status_code == 200
    store = env[1].state.store
    reopened = Store(store.db_path, store.media_root)
    strategy = reopened.get_job(response.json()["job"]["id"])["payload"]["selected_strategy"]
    expected = next(d for d in strategy["directions"] if d["id"] == strategy["selected_direction_id"])
    context = script_context(strategy, 20)
    context["stage"] = "ad_script"
    wire = planner_wire_context(context)
    for key in ("angle", "audience_value", "product_expression", "evidence_ids"):
        assert wire["selected_direction"][key] == expected[key]
        assert next(o for o in gate["options"] if o["id"] == expected["id"])[key] == expected[key]
    assert store.get_project(env[2])["plan"] is None
    assert [a["kind"] for a in store.project_detail(env[2])["assets"]] == ["product"]
