"""Format-only review repair may not promote existing content rejections."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.plan_review import lower_failed_plan_verdicts, validate_plan_review
from app.review_recovery import preserve_review_rejections, review_validation_feedback
from test_plan_review import plan, review


def locate(value, path):
    for part in path:
        value = value[part]
    return value


FAILURE_PATHS = [
    ("ready",), ("execution_check", "action_capacity"),
    ("execution_check", "state_continuity"), ("alignment", "ready"),
    ("alignment", "ad_expression_passed"), ("checks", 0, "passed"),
    ("alignment", "spans", 0, "passed"), ("alignment", "shots", 0, "passed"),
    ("alignment", "shots", 0, "pacing", "action_timing_passed"),
    ("alignment", "shots", 0, "pacing", "cut_necessity_passed"),
]


@pytest.mark.parametrize("path", FAILURE_PATHS)
@pytest.mark.parametrize("replacement", [True, None, "false", 0, "missing"])
def test_existing_explicit_failure_cannot_be_upgraded_or_dropped(plan, review, path, replacement):
    locate(review, path[:-1])[path[-1]] = False
    original = deepcopy(review)
    corrected = lower_failed_plan_verdicts(review, plan)
    target = locate(corrected, path[:-1])
    if replacement == "missing":
        target.pop(path[-1])
    else:
        target[path[-1]] = replacement
    # Keep parent failures so item-specific guards, rather than only ready,
    # prevent laundering an individual rejected opinion.
    corrected_before = deepcopy(corrected)
    with pytest.raises(ValueError, match="已有失败"):
        preserve_review_rejections(review, corrected, plan)
    assert review == original and corrected == corrected_before


@pytest.mark.parametrize("path", FAILURE_PATHS)
def test_format_correction_can_keep_every_existing_failure(plan, review, path):
    locate(review, path[:-1])[path[-1]] = False
    corrected = deepcopy(review)
    corrected["alignment"]["summary"] = "修正格式后保留实际未通过项目。"
    expected = lower_failed_plan_verdicts(corrected, plan)
    assert preserve_review_rejections(review, corrected, plan) == expected


def test_missing_fields_are_not_invented_rejections_and_inputs_are_unchanged(plan, review):
    previous = deepcopy(review)
    previous.pop("ready")
    previous["checks"].pop()
    previous["alignment"]["shots"][0].pop("temporal_omission")
    previous["alignment"]["spans"][0].pop("speaker_visibility")
    snapshots = deepcopy(previous), deepcopy(review), plan.model_dump()
    result = preserve_review_rejections(previous, review, plan)
    assert validate_plan_review(result, plan).ready
    assert result == review and result is not review
    assert (previous, review, plan.model_dump()) == snapshots


@pytest.mark.parametrize("value", ["false", 0, None])
def test_false_like_values_are_not_rejections(plan, review, value):
    previous = deepcopy(review)
    previous["execution_check"]["action_capacity"] = value
    assert preserve_review_rejections(previous, review, plan) == review


def test_numeric_overrun_rejection_survives_attempt_to_reduce_estimate(plan, review):
    previous = deepcopy(review)
    previous["alignment"]["shots"][0]["pacing"]["action_seconds_required"] = 11
    corrected = lower_failed_plan_verdicts(previous, plan)
    corrected["alignment"]["shots"][0]["pacing"].update(
        action_seconds_required=2, action_timing_passed=True)
    with pytest.raises(ValueError, match="pacing.action_timing_passed"):
        preserve_review_rejections(previous, corrected, plan)


@pytest.mark.parametrize("value", ["unsupported", "unclear"])
@pytest.mark.parametrize("replacement", ["no_omission", "supported_by_cut", None, "missing"])
def test_negative_omission_opinion_cannot_be_erased_even_if_passed_remains_false(
        plan, review, value, replacement):
    review["alignment"]["shots"][0]["temporal_omission"] = value
    corrected = lower_failed_plan_verdicts(review, plan)
    if replacement == "missing":
        corrected["alignment"]["shots"][0].pop("temporal_omission")
    else:
        corrected["alignment"]["shots"][0]["temporal_omission"] = replacement
    with pytest.raises(ValueError, match="已有失败依据"):
        preserve_review_rejections(review, corrected, plan)


@pytest.mark.parametrize("value", ["off_screen", "unclear", "not_applicable"])
def test_character_speech_cannot_gain_visibility_during_format_repair(plan, review, value):
    # Only the immutable sound mode is needed by the pure lowering/helper.
    # Complete authored production contracts are validated by the caller.
    subject = plan.model_dump()
    subject["production"] = {"speech_mode": "character_dialogue", "speaker_id": "speaker"}
    review["alignment"]["spans"][0]["speaker_visibility"] = value
    corrected = lower_failed_plan_verdicts(review, subject)
    corrected["alignment"]["spans"][0]["speaker_visibility"] = "visible_speaking"
    with pytest.raises(ValueError, match="已有失败依据"):
        preserve_review_rejections(review, corrected, subject)


def test_voiceover_does_not_make_offscreen_a_failure(plan, review):
    subject = plan.model_dump()
    subject["production"] = {"speech_mode": "voiceover"}
    previous = deepcopy(review)
    previous["alignment"]["spans"][0]["speaker_visibility"] = "off_screen"
    assert preserve_review_rejections(previous, review, subject) == review


@pytest.mark.parametrize("collection", ["spans", "shots"])
def test_identity_is_segment_and_local_id_not_array_order(plan, review, collection):
    review["alignment"][collection][0]["passed"] = False
    corrected = deepcopy(review)
    corrected["alignment"][collection].reverse()
    assert not validate_plan_review(preserve_review_rejections(review, corrected, plan), plan).ready
    # Both segments share the local ID: moving the failure to the other segment
    # while keeping the root false must still be rejected.
    corrected = lower_failed_plan_verdicts(corrected, plan)
    corrected["alignment"][collection][0]["passed"] = False
    corrected["alignment"][collection][1]["passed"] = True
    with pytest.raises(ValueError, match="已有失败"):
        preserve_review_rejections(review, corrected, plan)


def test_checks_pair_by_criterion_not_position(plan, review):
    review["checks"][0]["passed"] = False
    corrected = deepcopy(review)
    corrected["checks"].reverse()
    assert not validate_plan_review(preserve_review_rejections(review, corrected, plan), plan).ready


@pytest.mark.parametrize("mutation", ["duplicate_shot", "unknown_shot", "duplicate_check", "unknown_check"])
def test_invalid_identities_are_never_filled_or_silently_accepted(plan, review, mutation):
    corrected = deepcopy(review)
    if mutation == "duplicate_shot":
        corrected["alignment"]["shots"].append(deepcopy(corrected["alignment"]["shots"][0]))
    elif mutation == "unknown_shot":
        corrected["alignment"]["shots"][0]["shot_id"] = "unknown"
    elif mutation == "duplicate_check":
        corrected["checks"][1]["criterion"] = corrected["checks"][0]["criterion"]
    else:
        corrected["checks"][0]["criterion"] = "unknown"
    before = deepcopy(corrected)
    result = preserve_review_rejections(review, corrected, plan)
    assert result == before and corrected == before
    with pytest.raises(ValueError):
        validate_plan_review(result, plan)


def test_duplicate_rejection_cannot_be_paired_with_one_convenient_row(plan, review):
    review["alignment"]["shots"][0]["passed"] = False
    corrected = lower_failed_plan_verdicts(review, plan)
    duplicate = deepcopy(corrected["alignment"]["shots"][0])
    duplicate["passed"] = True
    corrected["alignment"]["shots"].append(duplicate)
    with pytest.raises(ValueError, match="已有失败"):
        preserve_review_rejections(review, corrected, plan)


def test_feedback_omits_inputs_and_bounds_each_dimension(plan, review):
    private_text = "PRIVATE_INVALID_VALUE_" * 100
    review["ready"] = private_text
    review["alignment"]["shots"][0]["pacing"]["action_timing_passed"] = private_text
    with pytest.raises(ValidationError) as caught:
        validate_plan_review(review, plan)
    feedback = review_validation_feedback(caught.value)
    assert len(feedback) == 2
    assert feedback[0]["loc"] == ["ready"]
    assert all(set(item) == {"loc", "type", "message"} for item in feedback)
    assert private_text not in str(feedback)
    assert all(len(item["message"]) <= 500 and len(item["type"]) <= 80 for item in feedback)
    assert review_validation_feedback(ValueError("x" * 1000)) == [
        {"loc": [], "type": "review_contract", "message": "x" * 500}]


def test_feedback_bounds_number_and_location_of_errors():
    errors = [{"type": "bool_type", "loc": ("x" * 300, *range(20)), "input": "private"}
              for _ in range(20)]
    exception = ValidationError.from_exception_data("review", errors)
    feedback = review_validation_feedback(exception)
    assert len(feedback) == 10
    assert all(len(item["loc"]) == 12 and len(item["loc"][0]) == 128 for item in feedback)
    assert "private" not in str(feedback)


@pytest.mark.parametrize("value", ["unclear", "conflict"])
@pytest.mark.parametrize("replacement", ["clear", None, "missing"])
def test_visual_execution_rejection_cannot_be_laundered(plan, review, value, replacement):
    review["alignment"]["shots"][0]["visual_execution"] = {"verdict": value}
    corrected = lower_failed_plan_verdicts(review, plan)
    target = corrected["alignment"]["shots"][0]
    if replacement == "missing":
        target.pop("visual_execution")
    else:
        target["visual_execution"]["verdict"] = replacement
    with pytest.raises(ValueError, match="已有失败依据"):
        preserve_review_rejections(review, corrected, plan)
