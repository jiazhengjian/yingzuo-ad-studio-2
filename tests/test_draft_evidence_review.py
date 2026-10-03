"""Only initial new drafts may defer selected-fact coverage to normal review."""
from copy import deepcopy

import pytest

from app.ad_script import validate_script
from app.providers.gateway import ProviderError, reviewable_draft_enum_issues
from app.strategy import BriefFact, StrategyRecord
from ad_script_fixtures import with_action_timing
from test_ad_script import script, strategy
from test_draft_enum_review import enum_gateway
from test_providers import settings, source, strategy_stage, install_transport


MISSING_FACT = "fact_product_claim"


def require_another_fact(strategy):
    value = strategy.model_dump()
    value["brief"]["facts"].append({"id": MISSING_FACT, "text": "包装为纸盒",
        "source_kind": "merchant", "source_id": "brief"})
    selected = next(d for d in value["directions"] if d["id"] == value["selected_direction_id"])
    selected["evidence_ids"].append(MISSING_FACT)
    return StrategyRecord.model_validate(value)


def prepare_missing_draft(state, enum_field=None):
    state.merchant_brief = "蓝瓶商品，包装为纸盒"
    expanded = require_another_fact(state.selected_strategy)
    state.selected_strategy.brief = expanded.brief
    state.selected_strategy.directions = expanded.directions
    state.enum_field = enum_field
    state.initial_mutation = lambda raw: raw["beats"][0]["evidence_ids"].remove(MISSING_FACT)


def include_fact_in_review(raw):
    beat = raw["beats"][0]
    beat["evidence_ids"].append(MISSING_FACT)
    beat["visual_role"] = "同一瓶子静置在办公桌上，保留用户说明的纸盒包装入画。"
    beat["message"] = "商品与纸盒包装一起形成桌面搭配。"


def test_explicit_nonfinal_coverage_deferral_keeps_script_unchanged(script, strategy):
    strategy = require_another_fact(strategy)
    before = deepcopy(script)
    checked = validate_script(script, strategy, 20, final=False, require_selected_evidence=False)
    assert checked.model_dump() == before and script == before
    with pytest.raises(ValueError, match="选中创意的商品依据"):
        validate_script(script, strategy, 20, final=False)


@pytest.mark.parametrize("require_coverage", [True, False])
def test_final_cannot_disable_selected_fact_coverage(script, strategy, require_coverage):
    strategy = require_another_fact(strategy)
    with pytest.raises(ValueError, match="选中创意的商品依据"):
        validate_script(script, strategy, 20, require_selected_evidence=require_coverage)


@pytest.mark.parametrize("invalid", ["unknown", "duplicate", "wrong_union", "empty_union", "extra", "version"])
def test_draft_deferral_preserves_other_fact_and_shape_guards(script, strategy, invalid):
    strategy = require_another_fact(strategy)
    if invalid == "unknown": script["beats"][1]["evidence_ids"] = script["evidence_ids"] = ["invented"]
    elif invalid == "duplicate": script["beats"][1]["evidence_ids"] *= 2
    elif invalid == "wrong_union": script["evidence_ids"].append(MISSING_FACT)
    elif invalid == "empty_union":
        script["evidence_ids"] = []
        for beat in script["beats"]: beat["evidence_ids"] = []
    elif invalid == "extra": script["beats"][0]["untrusted"] = "extra field"
    else: script["skill_version"] = "historical-version"
    with pytest.raises(ValueError):
        validate_script(script, strategy, 20, final=False, require_selected_evidence=False)


@pytest.mark.parametrize("enum_field", ["boundary_before", "time_transition"])
def test_enum_check_copy_uses_same_explicit_draft_coverage_policy(script, strategy, enum_field):
    strategy = require_another_fact(strategy)
    raw = with_action_timing(script)
    if enum_field == "boundary_before": raw["beats"][0][enum_field] = "ellipsis"
    else: raw["beats"][0]["timing"][enum_field] = "complete"
    before = deepcopy(raw)
    with pytest.raises(ValueError, match="选中创意的商品依据"):
        reviewable_draft_enum_issues(raw, strategy, 20, "continuity_v1")
    assert reviewable_draft_enum_issues(raw, strategy, 20, "continuity_v1", require_selected_evidence=False)
    assert raw == before


@pytest.mark.parametrize("enum_field", [None, "boundary_before", "time_transition"])
def test_existing_review_receives_missing_ids_and_can_complete_the_draft_in_four_calls(enum_gateway, enum_field):
    prepare_missing_draft(enum_gateway, enum_field)
    enum_gateway.review_mutation = include_fact_in_review
    result = enum_gateway.run()
    assert enum_gateway.calls == ["ad_script", "script_review", "storyboard", "alignment_review"]
    review_input = enum_gateway.contexts[1]
    assert review_input["draft_missing_selected_evidence_ids"] == [MISSING_FACT]
    assert MISSING_FACT not in review_input["draft_script"]["beats"][0]["evidence_ids"]
    assert MISSING_FACT not in enum_gateway.raw["beats"][0]["evidence_ids"]
    assert "不能只补ID" in review_input["draft_evidence_repair_contract"]
    assert result["strategy"] == enum_gateway.selected_strategy.model_dump()
    assert MISSING_FACT in result["ad_script"]["beats"][0]["evidence_ids"]
    assert "纸盒" in result["ad_script"]["beats"][0]["visual_role"]
    assert result["ad_script"] == result["script_review"]["script"]


@pytest.mark.parametrize("enum_field", [None, "boundary_before"])
@pytest.mark.parametrize("over_capacity", [False, True])
def test_review_that_still_omits_selected_evidence_stops_before_capacity_return(enum_gateway, enum_field, over_capacity):
    prepare_missing_draft(enum_gateway, enum_field)
    if over_capacity:
        enum_gateway.review_mutation = lambda raw: raw["beats"][0]["timing"].update(visual_seconds=25)
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["ad_script", "script_review"]


@pytest.mark.parametrize("enum_field", [None, "boundary_before"])
@pytest.mark.parametrize("invalid", ["unknown", "duplicate", "empty_union", "extra", "missing_timing"])
def test_missing_coverage_draft_with_other_invalid_content_never_reaches_review(enum_gateway, enum_field, invalid):
    prepare_missing_draft(enum_gateway, enum_field)
    def mutate(raw):
        beat = raw["beats"][0]
        beat["evidence_ids"].remove(MISSING_FACT)
        if invalid == "unknown": beat["evidence_ids"].append("invented")
        elif invalid == "duplicate": beat["evidence_ids"] *= 2
        elif invalid == "empty_union": beat["evidence_ids"] = []
        elif invalid == "extra": beat["unsupported"] = "extra field"
        else: beat.pop("timing")
    enum_gateway.initial_mutation = mutate
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["ad_script"]


def test_review_cannot_replace_missing_selected_id_with_an_unknown_fact(enum_gateway):
    prepare_missing_draft(enum_gateway)
    enum_gateway.review_mutation = lambda raw: raw["beats"][0]["evidence_ids"].append("invented")
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["ad_script", "script_review"]


def test_capacity_return_cannot_drop_fact_coverage_completed_in_first_review(enum_gateway):
    prepare_missing_draft(enum_gateway, "boundary_before")
    def mutate(raw):
        if enum_gateway.calls.count("script_review") == 1:
            include_fact_in_review(raw)
            raw["beats"][0]["timing"]["visual_seconds"] = 25
        else:
            raw["beats"][0]["timing"]["visual_seconds"] = 5
            raw["beats"][0]["evidence_ids"].remove(MISSING_FACT)
    enum_gateway.review_mutation = mutate
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["ad_script", "script_review", "script_review"]
    assert "draft_missing_selected_evidence_ids" not in enum_gateway.contexts[2]


def test_legacy_initial_draft_still_requires_selected_coverage(enum_gateway, strategy_stage):
    enum_gateway.new_flow = False
    enum_gateway.enum_field = None
    enum_gateway.merchant_brief = "蓝瓶商品，包装为纸盒"
    strategy_stage["brief"]["facts"].append(BriefFact(id=MISSING_FACT, text="包装为纸盒",
        source_kind="merchant", source_id="brief").model_dump())
    strategy_stage["directions"][0]["evidence_ids"].append(MISSING_FACT)
    def mutate(raw):
        # Beat and root summaries share a fixture list; replace both explicitly.
        raw["beats"][0]["evidence_ids"] = ["fact_blue"]
        raw["evidence_ids"] = ["fact_blue"]
    enum_gateway.initial_mutation = mutate
    with pytest.raises(ProviderError): enum_gateway.run()
    assert enum_gateway.calls == ["strategy", "ad_script"]
