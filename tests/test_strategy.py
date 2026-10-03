"""Strategy tests import no app/config/DB and make no supplier requests."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.strategy import (
    AppliedRule, BriefFact, CreativeDirection, ProductBrief, StrategyRecord,
    brief_from_legacy, build_strategy, select_rules, validate_brief,
)


def brief(category="general", text="蓝瓶", source_kind="merchant"):
    return ProductBrief(product_name="商品", category=category, facts=[
        BriefFact(id="f1", text=text, source_kind=source_kind,
                  source_id="brief" if source_kind == "merchant" else "original-product"),
    ])


def direction(rule_id="appearance_reveal", id="d1", evidence_ids=None):
    return CreativeDirection(
        id=id, rule_id=rule_id, label="完整外观", audience="喜欢简洁外观的人",
        scenario="窗边桌面", angle=f"从轮廓认识商品-{id}", hook="先看瓶肩细节",
        development=f"逐步展示轮廓和完整形状-{id}", payoff="回到同一商品的完整外观",
        evidence_ids=evidence_ids or ["f1"], reason="有原图外观依据",
        production_notes=["复用商品原图", "不加字幕、独立尾字或真人口型"],
    )


def make_strategy(value=None, directions=None):
    value = value or brief()
    directions = directions or [direction()]
    return build_strategy(value, directions, directions[0].id, select_rules(value))


def test_all_public_models_forbid_uncontracted_performance_fields():
    models = [
        BriefFact(id="f1", text="蓝瓶", source_kind="merchant", source_id="brief"),
        brief(), direction(), AppliedRule(id="r", version="1", sources=["https://example.com/source"]),
        make_strategy(),
    ]
    for value in models:
        payload = value.model_dump()
        payload["predicted_conversion"] = 0.42
        with pytest.raises(ValidationError, match="Extra inputs"):
            type(value).model_validate(payload)


def test_discovery_returns_versioned_rules_with_pinned_sources_and_constraints():
    rules = select_rules(None)
    assert {rule["id"] for rule in rules} == {
        "sensory_detail", "everyday_context", "appearance_reveal",
        "operation_walkthrough", "problem_solution", "evidence_comparison", "feature_value",
    }
    for rule in rules:
        assert rule["version"] == ("1.2.0" if rule["id"] in {"everyday_context", "appearance_reveal"} else "1.1.0" if rule["id"] in {
            "sensory_detail", "feature_value"
        } else "1.0.0")
        assert all("/blob/" in source and len(source.split("/blob/")[1].split("/")[0]) == 40
                   for source in rule["sources"])
        assert rule["shot_functions"] and rule["must_not_claim"]
        assert "不强制字幕或独立结尾文字" in rule["production_constraints"]
        assert any("角色对白只交给已确认说话人" in text for text in rule["production_constraints"])
        assert any("每位一张组合四视图" in text for text in rule["production_constraints"])


@pytest.mark.parametrize("category", ["food", "appliance", "general"])
def test_category_retrieval_is_small_and_applicable(category):
    rules = select_rules(brief(category))
    assert 3 <= len(rules) <= 4
    assert all(category in rule["categories"] for rule in rules)
    assert "appearance_reveal" in {rule["id"] for rule in rules}
    assert "evidence_comparison" not in {rule["id"] for rule in rules}


def test_food_and_appliance_retrieve_different_mechanisms():
    food = {rule["id"] for rule in select_rules(brief("food"))}
    appliance = {rule["id"] for rule in select_rules(brief("appliance", text="按下按钮可以启动"))}
    assert "sensory_detail" in food
    assert "operation_walkthrough" in appliance and "problem_solution" in appliance
    assert "sensory_detail" not in appliance


@pytest.mark.parametrize("category,text", [
    ("appliance", "风速、温度控制、护发功能"),
    ("food", "配料含燕麦和葡萄干"),
    ("general", "封面为蓝色"),
])
@pytest.mark.parametrize("ad_form", ["brand_tvc", "scene_story", "presenter_pitch"])
def test_feature_value_accepts_confirmed_characteristics_without_numbers_or_operations(category, text, ad_form):
    value = brief(category, text=text)
    value.ad_form = ad_form
    validate_brief(value, {"brief": text}, set())
    rules = select_rules(value)
    assert "feature_value" in {rule["id"] for rule in rules}
    record = build_strategy(value, [direction("feature_value")], "d1", rules)
    assert record.rules_used[0].id == "feature_value"
    assert record.brief.ad_form == ad_form


@pytest.mark.parametrize("text", [
    "没有护发功能", "不含燕麦", "不能宣称可恒温", "不具备保温功能",
    "护发功能尚未确认", "可能有护发功能", "假设可以保温", "是否护发不清楚",
])
def test_feature_value_cannot_use_negative_or_uncertain_merchant_assertions(text):
    value = brief("appliance", text=text)
    assert "feature_value" not in {rule["id"] for rule in select_rules(value)}
    with pytest.raises(ValueError, match="缺少前提"):
        build_strategy(value, [direction("feature_value")], "d1", select_rules(None))


def test_feature_value_requires_merchant_support_in_the_cited_fact():
    value = brief("appliance", text="银色机身", source_kind="image")
    assert "feature_value" not in {rule["id"] for rule in select_rules(value)}
    with pytest.raises(ValueError, match="缺少前提"):
        build_strategy(value, [direction("feature_value")], "d1", select_rules(None))
    value.facts.append(BriefFact(id="feature", text="温度控制", source_kind="merchant", source_id="brief"))
    rules = select_rules(value)
    assert "feature_value" in {rule["id"] for rule in rules}
    with pytest.raises(ValueError, match="缺少前提"):
        build_strategy(value, [direction("feature_value")], "d1", rules)
    assert build_strategy(value, [direction("feature_value", evidence_ids=["feature"])], "d1", rules)


def test_named_features_do_not_weaken_operation_or_comparison_requirements():
    value = brief("appliance", text="风速、温度控制、护发功能")
    assert build_strategy(value, [direction("feature_value")], "d1", select_rules(None))
    for rule_id in ("operation_walkthrough", "evidence_comparison"):
        with pytest.raises(ValueError, match="缺少前提"):
            build_strategy(value, [direction(rule_id)], "d1", select_rules(None))


def test_bounded_retrieval_prioritizes_supported_methods_and_discovery_keeps_old_rules():
    value = brief("appliance", text="按下按钮可以启动，相比旧款重量从500克降至400克")
    rules = select_rules(value)
    assert {rule["id"] for rule in rules} == {
        "feature_value", "operation_walkthrough", "problem_solution", "evidence_comparison"}
    assert len(rules) == 4
    # Production retrieval and restoration pass select_rules(None), so making
    # room in category suggestions cannot retire a historical selected rule.
    for rule_id in ("appearance_reveal", "everyday_context"):
        record = build_strategy(value, [direction(rule_id)], "d1", select_rules(None))
        historical = StrategyRecord.model_validate(record.model_dump())
        rebuilt = build_strategy(historical.brief, historical.directions, "d1", select_rules(None))
        assert rebuilt.selected_direction_id == "d1"
        assert rebuilt.rules_used == record.rules_used


def test_rule_retrieval_cannot_mutate_future_results():
    rules = select_rules(None)
    rules[0]["sources"].clear()
    rules[0]["production_constraints"].clear()
    rules[0]["prerequisites"]["min_facts"] = 0
    fresh = select_rules(None)[0]
    assert fresh["sources"] and fresh["production_constraints"]
    assert fresh["prerequisites"]["min_facts"] == 1


def test_merchant_fact_requires_an_exact_substring_of_the_named_user_source():
    value = brief()
    assert validate_brief(value, {"brief": "我的商品是蓝瓶，请拍得明亮些。"}, set()).facts[0].text == "蓝瓶"
    with pytest.raises(ValueError, match="不存在的商家来源"):
        validate_brief(value, {"another-message": "蓝瓶"}, set())
    with pytest.raises(ValueError, match="原文子串"):
        validate_brief(brief(text="蓝瓶，保温八小时"), {"brief": "蓝瓶"}, set())
    with pytest.raises(ValueError, match="原文子串"):
        validate_brief(brief(text="蓝色的瓶子"), {"brief": "蓝瓶"}, set())


def test_fact_is_not_grounded_just_because_it_occurs_in_another_message():
    with pytest.raises(ValueError, match="原文子串"):
        validate_brief(brief(text="支持充电"), {"brief": "蓝瓶", "other": "支持充电"}, set())


@pytest.mark.parametrize("source", [
    "这款小家电不能宣称可以杀菌。没有经过这方面测试。",
    "请勿在广告中说可以杀菌。",
    "可以杀菌这个说法尚未验证。",
    "据说可以杀菌，我们仍然需要检测。",
    "假设可以杀菌，也不代表经过实测。",
])
def test_exact_substring_cannot_strip_negative_or_uncertain_sentence_context(source):
    # Applies to every direction, including appearance: false facts must not
    # enter the brief just because later rule checks are low risk.
    with pytest.raises(ValueError, match="省略了原句的否定或不确定限定"):
        validate_brief(brief("appliance", text="可以杀菌"), {"brief": source}, set())


def test_qualifiers_kept_in_full_excerpt_are_not_reversed_but_do_not_authorize_demo():
    text = "不能宣称可以杀菌"
    value = validate_brief(brief("appliance", text=text), {"brief": f"这款{text}。"}, set())
    with pytest.raises(ValueError, match="缺少前提"):
        build_strategy(value, [direction("problem_solution")], "d1", select_rules(None))


def test_repeated_phrase_can_use_a_separate_affirmative_occurrence():
    source = "旧款不能宣称可以杀菌。本款检测说明写明可以杀菌。"
    value = validate_brief(brief("appliance", text="可以杀菌"), {"brief": source}, set())
    assert value.facts[0].text == "可以杀菌"


def test_uncertainty_in_a_different_sentence_does_not_negate_an_exterior_observation():
    source = "性能尚未验证。瓶身是蓝瓶外观。"
    assert validate_brief(brief(), {"brief": source}, set()).facts[0].text == "蓝瓶"
    # An excerpt may itself include the sentence-ending punctuation.
    assert validate_brief(brief(text="蓝瓶。"), {"brief": "蓝瓶。性能尚未验证。"}, set())


def test_image_fact_checks_original_asset_membership_without_claiming_semantics():
    observed = brief(text="瓶身有蓝色区域", source_kind="image")
    result = validate_brief(observed, {}, {"original-product"})
    assert result.facts[0].text == "瓶身有蓝色区域"
    with pytest.raises(ValueError, match="不存在的商品原图"):
        validate_brief(observed, {}, {"generated-scene"})
    # Deliberately no semantic assertion: image inspection belongs upstream.
    invented_observation = brief(text="此观察不由字符串成员校验验证", source_kind="image")
    assert validate_brief(invented_observation, {}, {"original-product"})


def test_duplicate_fact_ids_and_references_are_rejected():
    payload = brief().model_dump()
    payload["facts"] *= 2
    with pytest.raises(ValueError, match="事实 ID不能重复"):
        ProductBrief.model_validate(payload)
    with pytest.raises(ValueError, match="证据 ID不能重复"):
        direction(evidence_ids=["f1", "f1"])


def test_unknowns_and_assumptions_never_satisfy_a_fact_reference():
    value = ProductBrief(product_name="杯子", category="general", facts=[],
                         unknowns=["是否保温八小时"], assumptions=["适合通勤"])
    with pytest.raises(ValueError, match="不存在的商品事实"):
        build_strategy(value, [direction(evidence_ids=["unknown-0"])], "d1", select_rules(value))
    payload = direction().model_dump()
    payload["evidence_ids"] = []
    with pytest.raises(ValidationError):
        CreativeDirection.model_validate(payload)


def test_old_unreferenced_claims_become_unknowns_not_new_facts():
    value = brief_from_legacy({"name": "蛋糕", "claims": ["零糖", "好吃", "零糖"]}, "food")
    assert value.product_name == "蛋糕" and value.facts == []
    assert value.unknowns == ["历史说法待核实：零糖", "历史说法待核实：好吃"]
    with pytest.raises(ValueError, match="不存在的商品事实"):
        make_strategy(value)
    # Re-establishing an assertion needs a real source, not historical claims.
    restored = brief("food", text="零糖")
    with pytest.raises(ValueError, match="不存在的商家来源"):
        validate_brief(restored, {}, {"original-product"})


def test_build_freezes_original_brief_and_exact_used_rule_provenance():
    value = validate_brief(brief(), {"brief": "商品是蓝瓶"}, set())
    directions = [direction(), direction("everyday_context", "d2")]
    rules = select_rules(value)
    record = build_strategy(value, directions, "d2", rules)
    assert record.selected_direction_id == "d2" and record.version == "1.2.0"
    assert [rule.id for rule in record.rules_used] == ["appearance_reveal", "everyday_context"]
    assert record.rules_used[0].sources == rules[0]["sources"]
    value.facts[0].text = "后来改过的商品描述"
    directions[0].production_notes.clear()
    rules[0]["sources"].clear()
    assert record.brief.facts[0].text == "蓝瓶"
    assert record.directions[0].production_notes and record.rules_used[0].sources


@pytest.mark.parametrize("mutation, error", [
    ("selected", "所选创意方向不存在"),
    ("fact", "不存在的商品事实"),
    ("rule", "本轮未检索"),
    ("duplicate_id", "方向 ID不能重复"),
    ("duplicate_rule", "rule_id不能重复"),
    ("same_angle", "创意角度不能重复"),
    ("same_body", "展开与收束内容不能重复"),
])
def test_invalid_or_only_relabelled_directions_are_rejected(mutation, error):
    data = [direction().model_dump(), direction("everyday_context", "d2").model_dump()]
    selected = "d1"
    if mutation == "selected":
        selected = "missing"
    elif mutation == "fact":
        data[0]["evidence_ids"] = ["source-id-is-not-fact-id"]
    elif mutation == "rule":
        data[0]["rule_id"] = "invented-rule"
    elif mutation == "duplicate_id":
        data[1]["id"] = data[0]["id"]
    elif mutation == "duplicate_rule":
        data[1]["rule_id"] = data[0]["rule_id"]
    elif mutation == "same_angle":
        data[1]["angle"] = data[0]["angle"] + "。"
    else:
        data[1]["development"] = data[0]["development"]
        data[1]["payoff"] = data[0]["payoff"]
        data[1]["production_notes"] = ["把推镜换成摇镜"]
    with pytest.raises(ValueError, match=error):
        build_strategy(brief(), data, selected, select_rules(brief()))


def test_modified_rule_prerequisite_cannot_bypass_catalog():
    rules = select_rules(None)
    comparison = next(rule for rule in rules if rule["id"] == "evidence_comparison")
    comparison["prerequisites"]["merchant_patterns_all"] = []
    with pytest.raises(ValueError, match="不能修改来源或前提"):
        build_strategy(brief(), [direction("evidence_comparison")], "d1", rules)


def test_unretrieved_and_inapplicable_rules_are_rejected():
    value = brief("appliance", text="按下开关即可启动")
    with pytest.raises(ValueError, match="不适用于该商品品类"):
        build_strategy(value, [direction("sensory_detail")], "d1", select_rules(None))
    with pytest.raises(ValueError, match="本轮未检索"):
        build_strategy(value, [direction("problem_solution")], "d1", select_rules(None)[:3])


@pytest.mark.parametrize("rule_id", ["operation_walkthrough", "problem_solution", "evidence_comparison"])
def test_image_only_or_generic_fact_does_not_authorize_function_or_comparison(rule_id):
    for value in [brief(), brief(source_kind="image")]:
        with pytest.raises(ValueError, match="缺少前提"):
            build_strategy(value, [direction(rule_id)], "d1", select_rules(None))


@pytest.mark.parametrize("rule_id, text", [
    ("operation_walkthrough", "按下开关即可启动"),
    ("problem_solution", "折叠后可以放进收纳袋"),
    ("evidence_comparison", "相比旧款重量从500克降至400克"),
])
def test_explicit_merchant_source_can_support_a_conditional_rule(rule_id, text):
    value = validate_brief(brief("appliance", text=text), {"brief": f"商家说明：{text}。"}, set())
    record = build_strategy(value, [direction(rule_id)], "d1", select_rules(None))
    assert record.rules_used[0].id == rule_id


@pytest.mark.parametrize("text", ["相比旧款可能重量更轻", "相比旧款重量是否更轻尚无数据", "蓝瓶比红瓶好看"])
def test_uncertain_or_dimensionless_comparison_is_not_accepted(text):
    value = brief(text=text)
    with pytest.raises(ValueError, match="缺少前提"):
        build_strategy(value, [direction("evidence_comparison")], "d1", select_rules(None))
    assert "evidence_comparison" not in {rule["id"] for rule in select_rules(value)}


def test_prerequisites_must_be_in_cited_fact_not_an_uncited_fact():
    value = brief("appliance")
    value.facts.append(BriefFact(id="operation", text="按下开关启动", source_kind="merchant", source_id="brief"))
    with pytest.raises(ValueError, match="缺少前提"):
        build_strategy(value, [direction("operation_walkthrough")], "d1", select_rules(None))
    assert build_strategy(value, [direction("operation_walkthrough", evidence_ids=["operation"])],
                          "d1", select_rules(None))


def test_old_plan_can_read_its_own_retired_rule_without_current_catalog_authority():
    payload = make_strategy().model_dump()
    payload["version"] = "0.9.0"
    payload["directions"][0]["rule_id"] = "retired-visual-rule"
    payload["rules_used"][0].update(id="retired-visual-rule", version="0.9.0", sources=["https://example.com/old-source"])
    historical = StrategyRecord.model_validate(payload)
    assert historical.version == "0.9.0"
    with pytest.raises(ValueError, match="本轮未检索"):
        build_strategy(historical.brief, historical.directions, historical.selected_direction_id,
                       select_rules(historical.brief))


def test_record_rejects_missing_or_unrelated_provenance():
    payload = make_strategy().model_dump()
    payload["rules_used"][0]["id"] = "unrelated-rule"
    with pytest.raises(ValueError, match="实际使用的规则"):
        StrategyRecord.model_validate(payload)


def test_fact_limit_and_direction_limit_are_enforced():
    payload = brief().model_dump()
    payload["facts"] = [{**payload["facts"][0], "id": f"f{i}"} for i in range(13)]
    with pytest.raises(ValidationError):
        ProductBrief.model_validate(payload)
    payload = make_strategy().model_dump()
    payload["directions"] *= 4
    with pytest.raises(ValidationError):
        StrategyRecord.model_validate(payload)


def test_catalog_sources_match_the_fixed_repository_versions():
    catalog = json.loads((Path(__file__).parents[1] / "app/strategy_rules.json").read_text())
    assert len(catalog["sources"]) == 3
    assert {source["license"] for source in catalog["sources"]} == {"MIT"}
    for rule in select_rules(None):
        for url in rule["sources"]:
            assert any(f"/{source['repository']}/blob/{source['commit']}/" in url
                       for source in catalog["sources"])
