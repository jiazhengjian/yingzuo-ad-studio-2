"""Ad-concept contract: insight, one-line idea, hook and distinct selection.

These checks are offline. They do not score whether a real model wrote an
attractive advertisement.
"""
import logging

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


def sketch(identifier, rule="appearance_reveal", hook="macro_detail", world="functional", idea=None):
    return ConceptSketch(id=identifier, rule_id=rule, hook_type=hook, brand_world=world,
        evidence_ids=["appearance"])


def test_fresh_direction_schema_stays_smaller_than_the_essay_contract():
    import json
    def packed(ad_form=None):
        return json.dumps(strategy_decision_schema(new_directions=True, ad_form=ad_form),
                           ensure_ascii=False, separators=(",", ":"))
    # With a saved ad form the essay contract was 11425 characters on 2026-10-04.
    typed = packed("brand_tvc")
    assert len(typed) <= 9842
    assert len(packed()) < 10200
    direction = strategy_decision_schema(new_directions=True, ad_form="brand_tvc")["$defs"]["CreativeDirection"]["properties"]
    film = strategy_decision_schema(new_directions=True, ad_form="brand_tvc")["$defs"]["FilmConcept"]["properties"]
    assert "key_visual" in film and "memory_anchor" not in film
    for field in ("story_summary", "attraction_mechanism", "audience_value", "product_expression", "consumer_insight"):
        assert field not in direction


def test_fresh_schema_requires_the_pitch_and_six_id_sketches():
    fresh = strategy_decision_schema(new_directions=True)
    direction_schema = fresh["$defs"]["CreativeDirection"]
    for field in ("big_idea", "hook_type", "brand_world"):
        assert field in direction_schema["required"]
    assert "consumer_insight" not in direction_schema["properties"]
    assert direction_schema["properties"]["big_idea"]["maxLength"] == 30
    assert "consumer_insight" not in fresh["$defs"]["ConceptSketch"]["properties"]
    assert "big_idea" not in fresh["$defs"]["ConceptSketch"]["properties"]
    assert direction_schema["properties"]["hook_type"]["enum"][0] == "macro_detail"
    presenter = strategy_decision_schema(new_directions=True, ad_form="presenter_pitch")
    presenter_hooks = ["direct_question", "counterintuitive", "result_first"]
    assert presenter["$defs"]["CreativeDirection"]["properties"]["hook_type"]["enum"] == presenter_hooks
    assert presenter["$defs"]["ConceptSketch"]["properties"]["hook_type"]["enum"] == presenter_hooks
    assert "macro_detail" not in presenter["$defs"]["ConceptSketch"]["properties"]["hook_type"]["enum"]
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


def test_repeated_one_liner_is_rejected_on_the_directions(env):
    offer = candidates_for(env)
    repeated = offer.directions[0].big_idea
    directions = [direction.model_copy(update={"big_idea": repeated}) for direction in offer.directions]
    with pytest.raises(ValueError, match="一句话概念"):
        validate_direction_offer(offer.brief, directions, concept_pool_for(directions))


def test_emotional_rules_stay_in_the_full_catalog_and_still_need_a_real_fact():
    catalog = {rule["id"] for rule in select_rules(None)}
    assert {"exaggeration", "contrast_humor", "identity_attitude", "ritual_sensory", "situational_conflict"} <= catalog
    narrowed = {rule["id"] for rule in select_rules(brief())}
    assert {"exaggeration", "contrast_humor", "identity_attitude", "ritual_sensory", "situational_conflict"}.isdisjoint(narrowed)
    record = build_strategy(brief(), [direction("exaggeration")], "d1", select_rules(None))
    assert record.rules_used[0].id == "exaggeration"
    with pytest.raises(ValueError, match="不存在的商品事实"):
        build_strategy(brief(), [direction("exaggeration", evidence_ids=["missing"])], "d1", select_rules(None))


def test_planner_phrases_rewrite_to_plain_words_of_the_same_length():
    from app.strategy import JARGON_REPLACEMENTS, plain_consumer_wording
    for source, replacement in JARGON_REPLACEMENTS:
        assert len(source) == len(replacement)
        assert consumer_jargon(plain_consumer_wording(f"先{source}一下。")) is None
    assert plain_consumer_wording("受众意义对应年轻女生生日送礼") == "顾客意义对应年轻女生生日送礼"


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


def _presenter_offer(env):
    offer = candidates_for(env)
    brief = offer.brief.model_copy(update={"ad_form": "presenter_pitch"})
    hooks = ("direct_question", "counterintuitive", "result_first")
    directions = [direction.model_copy(update={
        "hook_type": hooks[index],
        "film_concept": direction.film_concept.model_copy(update={"structure": "presenter_explanation"}),
    }) for index, direction in enumerate(offer.directions)]
    return brief, directions


def _watch_salvage(caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error.planner")


def test_two_invalid_hook_sketches_do_not_fail_a_usable_offer(env, caplog):
    brief, directions = _presenter_offer(env)
    pool = concept_pool_for(directions)
    pool[3]["hook_type"] = "macro_detail"
    pool[4]["hook_type"] = "arresting_line"
    with caplog.at_level(logging.WARNING, logger="app.strategy"):
        _watch_salvage(caplog)
        corrected, kept = validate_direction_offer(brief, directions, pool)
    assert [item.id for item in corrected] == [item.id for item in directions]
    assert [item.hook_type for item in corrected] == ["direct_question", "counterintuitive", "result_first"]
    assert {item.id for item in kept} == {"direction_0", "direction_1", "direction_2", "spare_2"}
    assert "macro_detail" in caplog.text and "arresting_line" in caplog.text
    assert "瓶身轮廓" not in caplog.text
    assert "strategy_salvage" in caplog.text


def test_distinct_non_top_trio_is_accepted_and_program_pick_is_only_a_metric(env, caplog):
    offer = candidates_for(env)
    pool = concept_pool_for(offer.directions)[:3]
    pool.extend([
        {"id": "alt_0", "rule_id": "appearance_reveal", "hook_type": "macro_detail", "brand_world": "functional",
            "evidence_ids": ["appearance"]},
        {"id": "alt_1", "rule_id": "everyday_context", "hook_type": "scale_shift", "brand_world": "identity",
            "evidence_ids": ["appearance"]},
        {"id": "alt_2", "rule_id": "appearance_reveal", "hook_type": "silhouette", "brand_world": "sensory",
            "evidence_ids": ["appearance"]},
    ])
    program = select_concept_ids([ConceptSketch.model_validate(item) for item in pool])
    assert program == ["direction_0", "direction_1", "direction_2"]
    expanded = [direction.model_copy(update={"id": f"alt_{index}"}) for index, direction in enumerate(offer.directions)]
    with caplog.at_level(logging.INFO, logger="app.strategy"):
        _watch_salvage(caplog)
        corrected, _kept = validate_direction_offer(offer.brief, expanded, pool)
    assert [item.id for item in corrected] == ["alt_0", "alt_1", "alt_2"]
    assert "direction_0" in caplog.text and "alt_0" in caplog.text
    assert "strategy_salvage" in caplog.text
    outside = [direction.model_copy(update={"id": "missing"}) for direction in offer.directions]
    with pytest.raises(ValueError, match="可用方向不足三个"):
        validate_direction_offer(offer.brief, outside, pool)


def test_one_liner_stays_on_the_direction_while_hook_ids_follow_the_sketch(env, caplog):
    offer = candidates_for(env)
    pool = concept_pool_for(offer.directions)
    ideas = ("先看见一条轮廓", "桌角终于有了它", "光线换了瓶子还在")
    directions = [direction.model_copy(update={"hook_type": "silhouette", "big_idea": ideas[index]})
                  for index, direction in enumerate(offer.directions)]
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        corrected, kept = validate_direction_offer(offer.brief, directions, pool)
    assert "strategy_salvage" in caplog.text
    sketches = {item.id: item for item in kept}
    assert corrected[0].hook_type == sketches[corrected[0].id].hook_type
    assert corrected[0].hook_type != "silhouette"
    assert corrected[0].big_idea == ideas[0]
    decision = StrategyDecision.model_validate({
        "kind": "directions", "message": "三个方向", "brief": offer.brief.model_dump(),
        "directions": [item.model_dump() for item in directions], "concept_pool": pool,
        "selected_direction_id": None,
    })
    assert decision.directions[0].big_idea == ideas[0]
    assert decision.directions[0].hook_type == offer.directions[0].hook_type


def test_disallowed_direction_hook_is_coerced_to_the_structure_playbook(env, caplog):
    offer = candidates_for(env)
    brief = offer.brief.model_copy(update={"ad_form": None})
    directions = [direction.model_copy(update={
        "film_concept": direction.film_concept.model_copy(update={"structure": "scene_action"}),
    }) for direction in offer.directions]
    with caplog.at_level(logging.WARNING, logger="app.strategy"):
        _watch_salvage(caplog)
        corrected, _kept = validate_direction_offer(brief, directions, concept_pool_for(directions))
    assert [item.hook_type for item in corrected] == ["awkward_moment", "situational_conflict", "arresting_line"]
    assert "strategy_salvage" in caplog.text
    assert "Coerced direction hook" in caplog.text
    assert "macro_detail" in caplog.text and "awkward_moment" in caplog.text


def test_clean_offer_matching_the_program_pick_is_not_a_salvage(env, caplog):
    offer = candidates_for(env)
    with caplog.at_level(logging.INFO, logger="uvicorn.error.planner"):
        corrected, _kept = validate_direction_offer(offer.brief, offer.directions, concept_pool_for(offer.directions))
    assert [item.id for item in corrected] == [item.id for item in offer.directions]
    assert "strategy_salvage" not in caplog.text


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


def test_review_note_can_keep_planner_words(env):
    offer = candidates_for(env)
    raw = approved_direction_review([item.model_dump() for item in offer.directions])
    raw["checks"][0]["note"] = "这一条兼顾了卖点呈现。"
    raw["summary"] = "受众会留下记忆点，审核说明可以这么写。"
    review = validate_direction_review(raw, offer, select_rules(None), product_reference_count=1)
    assert review.ready
    assert review.checks[0].note == "这一条兼顾了卖点呈现。"
    assert "受众" in review.summary


def test_plan_replies_do_not_carry_a_new_concept_pool():
    with pytest.raises(ValidationError, match="不重新竞选短概念"):
        StrategyDecision.model_validate({"kind": "plan", "message": "沿用", "concept_pool": [sketch("only").model_dump()],
            "brief": ProductBrief(product_name="瓶子", category="general", facts=[
                BriefFact(id="appearance", text="瓶身轮廓", source_kind="image", source_id="img")]).model_dump(),
            "directions": [direction().model_dump()], "selected_direction_id": "d1"})
