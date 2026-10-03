"""Ad-concept contract: insight, one-line idea, hook and distinct selection.

These checks are offline. They do not score whether a real model wrote an
attractive advertisement.
"""
import pytest
from pydantic import ValidationError

from app.direction_review import validate_direction_review
from app.planning import StrategyDecision, strategy_decision_schema
from app.strategy import (
    ConceptSketch, ProductBrief, BriefFact, build_strategy, consumer_jargon,
    select_concept_ids, select_rules, validate_direction_offer,
)
from test_conversation_api import env
from test_direction_selection import approved_direction_review, candidates_for, concept_pool_for
from test_strategy import brief, direction


def sketch(identifier, rule="appearance_reveal", hook="macro_detail", world="functional",
           idea="先看见一条轮廓"):
    return ConceptSketch(id=identifier, rule_id=rule, hook_type=hook, brand_world=world,
        consumer_insight="还没看清整只瓶子，就想知道它长什么样。",
        big_idea=idea, evidence_ids=["appearance"])


def test_fresh_schema_requires_the_concept_and_six_sketches_historical_does_not():
    fresh = strategy_decision_schema(new_directions=True)
    direction_schema = fresh["$defs"]["CreativeDirection"]
    for field in ("consumer_insight", "big_idea", "hook_type", "brand_world"):
        assert field in direction_schema["required"]
    assert direction_schema["properties"]["big_idea"]["maxLength"] == 28
    assert direction_schema["properties"]["hook_type"]["enum"][0] == "macro_detail"
    assert fresh["properties"]["concept_pool"]["minItems"] == 6
    assert "concept_pool" in fresh["required"]
    historical = strategy_decision_schema()
    assert "consumer_insight" not in historical["$defs"]["CreativeDirection"]["required"]
    assert "minItems" not in historical["properties"]["concept_pool"]


def test_selector_prefers_a_new_mechanism_hook_world_and_keeps_earlier_ties():
    chosen = select_concept_ids([
        sketch("first"),
        sketch("second", "everyday_context", "scale_shift", "identity", "桌角终于有了它"),
        sketch("third", "sensory_detail", "silhouette", "sensory", "光线停在瓶身上"),
        sketch("later", "problem_solution", "signature_sound", "functional", "放下以后才安心"),
        sketch("dup"),
        sketch("dup2"),
    ])
    assert chosen == ["first", "second", "third"]
    emotional = select_concept_ids([
        sketch("plain"),
        sketch("copy"),
        sketch("context", "everyday_context", "scale_shift", "identity", "桌角终于有了它"),
        sketch("joke", "exaggeration", "silhouette", "sensory", "瓶子大得占满桌"),
        sketch("humor", "contrast_humor", "signature_sound", "functional", "放下时轻轻一声"),
        sketch("again"),
    ])
    assert emotional[0] == "joke"
    assert set(emotional) == {"joke", "plain", "context"}


def test_repeated_one_liner_loses_to_a_different_idea():
    chosen = select_concept_ids([
        sketch("first", idea="先看见一条轮廓"),
        sketch("echo", "everyday_context", "scale_shift", "identity", "先看见一条轮廓"),
        sketch("other", "sensory_detail", "silhouette", "sensory", "光线停在瓶身上"),
        sketch("spare", "problem_solution", "signature_sound", "functional", "放下以后才安心"),
        sketch("dup"),
        sketch("dup2"),
    ])
    assert "echo" not in chosen
    assert chosen == ["first", "other", "spare"]


def test_emotional_rules_stay_in_the_full_catalog_and_still_need_a_real_fact():
    catalog = {rule["id"] for rule in select_rules(None)}
    assert {"exaggeration", "contrast_humor", "identity_attitude", "ritual_sensory", "situational_conflict"} <= catalog
    narrowed = {rule["id"] for rule in select_rules(brief())}
    assert {"exaggeration", "contrast_humor", "identity_attitude", "ritual_sensory", "situational_conflict"}.isdisjoint(narrowed)
    record = build_strategy(brief(), [direction("exaggeration")], "d1", select_rules(None))
    assert record.rules_used[0].id == "exaggeration"
    with pytest.raises(ValueError, match="不存在的商品事实"):
        build_strategy(brief(), [direction("exaggeration", evidence_ids=["missing"])], "d1", select_rules(None))


def test_consumer_copy_rejects_planner_phrases_and_foreign_hooks(env):
    offer = candidates_for(env)
    assert consumer_jargon("采用等离子技术，关注秀发护理") == "关注"
    jargon = [direction.model_copy(update={"angle": "关注瓶身的轮廓关系"}) if direction.id == "direction_0" else direction
              for direction in offer.directions]
    with pytest.raises(ValueError, match="不要写「关注」"):
        validate_direction_offer(offer.brief, jargon, concept_pool_for(jargon))
    scene = offer.brief.model_copy(update={"ad_form": "scene_story"})
    with pytest.raises(ValueError, match="开场钩子"):
        validate_direction_offer(scene, offer.directions, concept_pool_for(offer.directions))


def test_expanded_directions_must_be_the_program_pick(env):
    offer = candidates_for(env)
    pool = concept_pool_for(offer.directions)
    validate_direction_offer(offer.brief, offer.directions, pool)
    swapped = [offer.directions[0], offer.directions[1], offer.directions[1].model_copy(update={
        "id": "spare_0", "label": "另一条", "angle": "先听见瓶子放下",
        "big_idea": "听见它放下的声音", "development": "另一段展开", "payoff": "另一段收束"})]
    with pytest.raises(ValueError, match="direction_0"):
        validate_direction_offer(offer.brief, swapped, pool)


def test_review_keeps_the_idea_records_tension_and_can_rewrite_a_claim(env):
    offer = candidates_for(env)
    raw = approved_direction_review([item.model_dump() for item in offer.directions])
    for check in raw["checks"]:
        check["creative_tension"] = "low"
        check["hook_strength"] = "low"
    raw["directions"][0]["story_summary"] = "窗光先勾出一条线，再看清整只瓶子，不说它能护理头发。"
    review = validate_direction_review(raw, offer, select_rules(None), product_reference_count=1)
    assert review.ready
    assert review.checks[0].creative_tension == "low"
    assert review.directions[0].big_idea == offer.directions[0].big_idea
    assert "护理" in review.directions[0].story_summary
    raw["directions"][0]["big_idea"] = "瓶子自己会说话呀"
    with pytest.raises(ValueError, match="不能改写创意"):
        validate_direction_review(raw, offer, select_rules(None), product_reference_count=1)


def test_review_note_cannot_smuggle_planner_phrasing(env):
    offer = candidates_for(env)
    raw = approved_direction_review([item.model_dump() for item in offer.directions])
    raw["checks"][0]["note"] = "这一条兼顾了卖点呈现。"
    with pytest.raises(ValueError, match="不要写「兼顾」"):
        validate_direction_review(raw, offer, select_rules(None), product_reference_count=1)


def test_plan_replies_do_not_carry_a_new_concept_pool():
    with pytest.raises(ValidationError, match="不重新竞选短概念"):
        StrategyDecision.model_validate({"kind": "plan", "message": "沿用", "concept_pool": [sketch("only").model_dump()],
            "brief": ProductBrief(product_name="瓶子", category="general", facts=[
                BriefFact(id="appearance", text="瓶身轮廓", source_kind="image", source_id="img")]).model_dump(),
            "directions": [direction().model_dump()], "selected_direction_id": "d1"})
