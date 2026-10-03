"""A narrowly misplaced review field is relocated without weakening verdicts."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.plan_review import (
    lower_failed_plan_verdicts, normalize_plan_review_wire,
    plan_review_fingerprint, validate_plan_review,
)
from app.schemas import Plan
from test_plan_review import plan, review


def misplaced(review, value="no_omission"):
    result = deepcopy(review)
    shot = result["alignment"]["shots"][1]
    shot.pop("temporal_omission")
    shot["pacing"]["temporal_omission"] = value
    return result


def test_correct_wire_is_unchanged_and_normalization_returns_an_independent_copy(review):
    before = deepcopy(review)
    normalized = normalize_plan_review_wire(review)
    assert normalized == review == before
    normalized["alignment"]["shots"][0]["pacing"]["note"] = "改变副本，不改变原始审核。"
    assert review == before


@pytest.mark.parametrize("value", ["no_omission", "supported_by_cut", "unsupported", "unclear"])
def test_relocation_is_local_and_failure_values_are_lowered_before_validation(plan, review, value):
    if value == "supported_by_cut":
        # Legacy bound support remains readable. Relocation never invents the
        # cut evidence now required for a fresh provider opinion.
        review = validate_plan_review(review, plan).model_dump()
    raw = misplaced(review, value)
    original, subject = deepcopy(raw), plan.model_dump()
    normalized = normalize_plan_review_wire(raw)
    expected = deepcopy(review)
    expected["alignment"]["shots"][1]["temporal_omission"] = value
    assert normalized == expected
    # Moving a field itself does not invent or change an opinion.
    assert normalized["ready"] is True
    assert normalized["alignment"]["shots"][1]["passed"] is True
    lowered = lower_failed_plan_verdicts(raw, plan)
    accepted = value not in ("unsupported", "unclear")
    assert lowered["alignment"]["shots"][1]["passed"] is accepted
    assert lowered["alignment"]["ready"] is accepted
    assert lowered["ready"] is accepted
    assert lowered["alignment"]["shots"][0] == review["alignment"]["shots"][0]
    bound = validate_plan_review(lowered, plan, require_temporal_omission=True)
    assert bound.ready is accepted
    assert bound.reviewed_plan_sha256 == plan_review_fingerprint(plan)
    assert raw == original and plan.model_dump() == subject


@pytest.mark.parametrize("value", ["unknown", "", None, True, 1, [], {"value": "no_omission"}])
def test_invalid_nested_value_is_preserved_for_strict_rejection(plan, review, value):
    raw = misplaced(review, value)
    assert normalize_plan_review_wire(raw) == raw
    with pytest.raises(ValidationError):
        validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan)


@pytest.mark.parametrize("canonical", ["no_omission", "supported_by_cut", None])
def test_existing_canonical_key_prevents_relocation_even_if_values_match(plan, review, canonical):
    raw = misplaced(review, "no_omission")
    raw["alignment"]["shots"][1]["temporal_omission"] = canonical
    assert normalize_plan_review_wire(raw) == raw
    assert raw["alignment"]["shots"][1]["pacing"]["temporal_omission"] == "no_omission"
    with pytest.raises(ValidationError):
        validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan)


@pytest.mark.parametrize("pacing", [None, [], "no_omission"])
def test_non_dictionary_pacing_cannot_supply_a_verdict(plan, review, pacing):
    raw = misplaced(review)
    raw["alignment"]["shots"][1]["pacing"] = pacing
    assert normalize_plan_review_wire(raw) == raw
    with pytest.raises(ValueError):
        validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan)


def test_missing_pacing_and_missing_verdict_are_not_filled_from_note(plan, review):
    raw = deepcopy(review)
    shot = raw["alignment"]["shots"][1]
    shot.pop("pacing")
    shot.pop("temporal_omission")
    shot["note"] = "supported_by_cut，作者声称省略已完成。"
    assert normalize_plan_review_wire(raw) == raw
    with pytest.raises(ValueError):
        validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan)


@pytest.mark.parametrize("location", ["pacing", "shot", "alignment", "top"])
def test_relocation_preserves_other_unknown_fields_for_rejection(plan, review, location):
    raw = misplaced(review)
    targets = {"pacing": raw["alignment"]["shots"][1]["pacing"],
        "shot": raw["alignment"]["shots"][1], "alignment": raw["alignment"], "top": raw}
    targets[location]["foreign_field"] = "must remain visible to validation"
    original = deepcopy(raw)
    normalized = normalize_plan_review_wire(raw)
    expected = deepcopy(raw)
    expected["alignment"]["shots"][1]["temporal_omission"] = expected["alignment"]["shots"][1]["pacing"].pop("temporal_omission")
    assert normalized == expected
    with pytest.raises(ValidationError):
        validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan)
    assert raw == original


def test_relocated_positive_value_does_not_upgrade_existing_failure(plan, review):
    review = validate_plan_review(review, plan).model_dump()
    raw = misplaced(review, "supported_by_cut")
    raw["alignment"]["shots"][1]["passed"] = False
    raw["alignment"]["ready"] = raw["ready"] = False
    lowered = lower_failed_plan_verdicts(raw, plan)
    assert lowered == normalize_plan_review_wire(raw)
    assert validate_plan_review(lowered, plan).ready is False


def test_relocated_review_binds_the_same_plan_and_cannot_follow_later_content_edits(plan, review):
    original_bound = validate_plan_review(review, plan)
    moved_bound = validate_plan_review(lower_failed_plan_verdicts(misplaced(review), plan), plan)
    assert moved_bound == original_bound
    changed = plan.model_dump()
    changed["segments"][0]["shots"][0]["description"] = "另一种商品操作。"
    with pytest.raises(ValueError, match="当前最终方案"):
        validate_plan_review(moved_bound, changed)


@pytest.mark.parametrize("bound", [False, True])
def test_direct_strict_validator_and_stored_plan_do_not_normalize_wire_errors(plan, review, bound):
    raw = validate_plan_review(review, plan).model_dump() if bound else review
    raw = misplaced(raw)
    with pytest.raises(ValidationError, match="temporal_omission"):
        validate_plan_review(raw, plan)
    if bound:
        with pytest.raises(ValidationError, match="temporal_omission"):
            Plan.model_validate({**plan.model_dump(), "plan_review": raw})


def test_historical_missing_verdict_remains_missing_without_fabricated_evidence(plan, review):
    old = validate_plan_review(review, plan).model_dump()
    old["alignment"]["shots"][1].pop("temporal_omission")
    assert normalize_plan_review_wire(old) == old
    restored = validate_plan_review(old, plan)
    assert restored.alignment.shots[1].temporal_omission is None
    with pytest.raises(ValueError, match="时间省略"):
        validate_plan_review(old, plan, require_temporal_omission=True)
