"""Server-owned script summaries must not hide invalid beat-level references."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.ad_script import (AdScript, ScriptReview, derive_script_evidence,
                           script_stage_schema, validate_script)
from app.strategy import StrategyRecord
from test_ad_script import strategy, script, review_for


def expanded_strategy(strategy):
    raw = strategy.model_dump()
    raw["brief"]["facts"].extend([
        {"id": "office", "text": "办公室下午茶", "source_kind": "merchant", "source_id": "brief"},
        {"id": "box", "text": "纸盒包装", "source_kind": "image", "source_id": "photo"},
    ])
    return StrategyRecord.model_validate(raw)


def test_new_wire_schemas_omit_metadata_but_canonical_and_historical_schemas_require_it():
    canonical = AdScript.model_json_schema()
    reviewer = ScriptReview.model_json_schema()
    for schema in (script_stage_schema(), script_stage_schema(review=True)["$defs"]["AdScript"]):
        for field in ("evidence_ids", "skill_version"):
            assert field not in schema["properties"]
            assert field not in schema["required"]
        assert schema["additionalProperties"] is False
    assert "evidence_ids" in script_stage_schema()["$defs"]["AdBeat"]["properties"]
    for field in ("evidence_ids", "skill_version"):
        assert field in canonical["required"]
        assert field in reviewer["$defs"]["AdScript"]["required"]
    assert AdScript.model_json_schema() == canonical
    assert ScriptReview.model_json_schema() == reviewer


@pytest.mark.parametrize("top_level", [None, ["layers", "office"], ["untrusted_stale_summary"]])
def test_summary_is_ordered_union_without_rewriting_beats_or_fact_sources(strategy, script, top_level):
    strategy = expanded_strategy(strategy)
    script["beats"][0]["evidence_ids"] = ["office", "layers"]
    script["beats"][1]["evidence_ids"] = ["layers", "box"]
    if top_level is None:
        script.pop("evidence_ids")
    else:
        script["evidence_ids"] = top_level
    before_script, before_strategy = deepcopy(script), strategy.model_dump()
    derived = derive_script_evidence(script, strategy)
    assert derived["evidence_ids"] == ["office", "layers", "box"]
    assert derived["beats"] == before_script["beats"]
    assert {k: v for k, v in derived.items() if k != "evidence_ids"} == {
        k: v for k, v in before_script.items() if k != "evidence_ids"}
    checked = validate_script(derived, strategy, 20)
    assert checked.evidence_ids == ["office", "layers", "box"]
    assert script == before_script and strategy.model_dump() == before_strategy


def test_review_no_longer_retains_unused_summary_fact(strategy, script):
    strategy = expanded_strategy(strategy)
    raw = review_for(script)
    raw["script"]["evidence_ids"] = ["layers", "office"]
    canonical = {**raw, "script": derive_script_evidence(raw["script"], strategy)}
    checked = ScriptReview.model_validate(canonical)
    assert validate_script(checked.script, strategy, 20).evidence_ids == ["layers"]
    assert raw["script"]["evidence_ids"] == ["layers", "office"]


@pytest.mark.parametrize("problem", ["unknown_beat_fact", "duplicate_beat_fact", "extra_beat_field"])
def test_derivation_rejects_invalid_original_beat_data_before_aggregation(strategy, script, problem):
    script["evidence_ids"] = ["layers"]  # A valid summary cannot hide a bad beat.
    if problem == "unknown_beat_fact":
        script["beats"][0]["evidence_ids"] = ["invented"]
    elif problem == "duplicate_beat_fact":
        script["beats"][1]["evidence_ids"] = ["layers", "layers"]
    else:
        script["beats"][0]["extra_fact"] = "not in the schema"
    before = deepcopy(script)
    with pytest.raises((ValueError, ValidationError)):
        derive_script_evidence(script, strategy)
    assert script == before


def test_missing_selected_fact_still_fails_after_valid_union_is_derived(strategy, script):
    strategy = expanded_strategy(strategy)
    script["beats"][1]["evidence_ids"] = ["office"]
    derived = derive_script_evidence(script, strategy)
    assert derived["evidence_ids"] == ["office"]
    with pytest.raises(ValueError, match="选中创意的商品依据"):
        validate_script(derived, strategy, 20, final=False)


def test_empty_union_is_not_filled_from_selected_direction(strategy, script):
    for beat in script["beats"]:
        beat["evidence_ids"] = []
    derived = derive_script_evidence(script, strategy)
    assert derived["evidence_ids"] == []
    with pytest.raises(ValidationError):
        validate_script(derived, strategy, 20, final=False)


def test_legacy_validation_keeps_strict_summary_contract(strategy, script):
    strategy = expanded_strategy(strategy)
    script["evidence_ids"] = ["layers", "office"]
    with pytest.raises(ValueError, match="各节拍实际引用"):
        validate_script(script, strategy, 20, require_current_version=False)
    script.pop("evidence_ids")
    with pytest.raises(ValidationError):
        validate_script(script, strategy, 20, require_current_version=False)
