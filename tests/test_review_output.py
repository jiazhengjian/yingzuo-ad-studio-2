"""Omit positive prose without omitting any decision or laundering a failure."""
from copy import deepcopy

import pytest

from app.ad_script import ScriptCheck, ScriptExecutionCheck
from app.plan_review import (lower_failed_plan_verdicts, plan_review_fingerprint,
                             plan_review_schema, validate_plan_review)
from app.review_output import compact_review_schema
from app.review_recovery import preserve_review_rejections
from app.schemas import Plan
from test_plan_review import plan, review
from test_omission_evidence import cut_plan, support
from test_providers import settings, source


def omit_success_notes(raw):
    result = deepcopy(raw)
    for check in result["checks"]:
        if check["passed"]:
            check.pop("note", None)
    execution = result["execution_check"]
    if execution["action_capacity"] and execution["state_continuity"]:
        execution.pop("note", None)
    for span in result["alignment"]["spans"]:
        if span["passed"]:
            span.pop("note", None)
    for shot in result["alignment"]["shots"]:
        if shot["passed"]:
            shot.pop("note", None)
        pacing = shot["pacing"]
        if pacing["action_timing_passed"] and pacing["cut_necessity_passed"]:
            pacing.pop("note", None)
    return result


@pytest.mark.parametrize("model,values", [
    (ScriptCheck, {"criterion": "factual_support", "passed": True}),
    (ScriptExecutionCheck, {"action_capacity": True, "state_continuity": True}),
])
def test_positive_script_decisions_need_no_invented_explanation(model, values):
    parsed = model.model_validate(values)
    assert parsed.note is None
    assert parsed.model_dump() == values
    original = {**values, "note": "原审核确实提供的可追溯说明。"}
    assert model.model_validate(original).model_dump() == original


@pytest.mark.parametrize("model,values", [
    (ScriptCheck, {"criterion": "factual_support", "passed": False}),
    (ScriptExecutionCheck, {"action_capacity": False, "state_continuity": True}),
    (ScriptExecutionCheck, {"action_capacity": True, "state_continuity": False}),
    (ScriptExecutionCheck, {"action_capacity": False, "state_continuity": False}),
])
@pytest.mark.parametrize("note", ["missing", None, "", "  "])
def test_negative_script_decisions_cannot_omit_or_blank_the_reason(model, values, note):
    data = deepcopy(values)
    if note != "missing":
        data["note"] = note
    with pytest.raises(ValueError):
        model.model_validate(data)


def test_schema_compaction_keeps_all_semantics_and_evidence_and_is_nonmutating():
    original = plan_review_schema()
    before = deepcopy(original)
    compact = compact_review_schema(original)
    assert original == before
    assert compact_review_schema(compact) == compact
    assert compact["properties"] == original["properties"]
    assert compact["required"] == original["required"]
    registered = {"ScriptCheck", "ScriptExecutionCheck", "AVSpanCheck", "AVShotCheck", "AVShotPacing"}
    for name, definition in original["$defs"].items():
        if name not in registered:
            assert compact["$defs"][name] == definition
            continue
        transformed = compact["$defs"][name]
        # Decisions, identity fields, evidence references and every old
        # conditional (including cut evidence) must survive unchanged.
        assert {k: v for k, v in transformed["properties"].items() if k != "note"} == {
            k: v for k, v in definition["properties"].items() if k != "note"}
        assert set(transformed["required"]) == set(definition["required"]) - {"note"}
        assert transformed["allOf"][:-1] == definition.get("allOf", [])
        assert transformed["allOf"][-1]["else"] == {"required": ["note"]}
        note = transformed["properties"]["note"]
        assert note["type"] == "string" and note["minLength"] == 1
        assert "anyOf" not in note and "default" not in note
    # Summaries and traceable evidence are deliberately outside this change.
    assert "summary" in compact["$defs"]["AVReview"]["required"]
    assert "note" in compact["$defs"]["AVSpeakerShotCheck"]["required"]
    assert "quote" in compact["$defs"]["AVVisualExecution"]["required"]


def test_schema_only_changes_registered_review_definitions():
    foreign = {"properties": {"note": {"type": "string"}}, "$defs": {
        "ProductFact": {"properties": {"passed": {"type": "boolean"},
            "note": {"type": "string", "minLength": 1}}, "required": ["passed", "note"]}}}
    assert compact_review_schema(foreign) == foreign
    malformed = plan_review_schema()
    malformed["$defs"]["ScriptCheck"]["properties"]["passed"] = {"type": "string"}
    with pytest.raises(ValueError, match="Unsupported"):
        compact_review_schema(malformed)


def test_success_without_notes_binds_same_subject_and_preserves_real_evidence(plan, review):
    raw = omit_success_notes(review)
    before = deepcopy(raw), plan.model_dump()
    checked = validate_plan_review(raw, plan)
    assert checked.ready
    assert checked.reviewed_plan_sha256 == plan_review_fingerprint(plan)
    canonical = checked.model_dump()
    assert {k: v for k, v in canonical.items() if not k.startswith("reviewed_")} == raw
    assert all("note" not in item for item in canonical["checks"])
    assert canonical["alignment"]["shots"][0]["visual_execution"] == review["alignment"]["shots"][0]["visual_execution"]
    assert Plan.model_validate({**plan.model_dump(), "plan_review": canonical}).plan_review == canonical
    assert (raw, plan.model_dump()) == before
    # Old authored explanations are never replaced, even on passing items.
    old = validate_plan_review(review, plan).model_dump()
    assert {k: v for k, v in old.items() if not k.startswith("reviewed_")} == review


@pytest.mark.parametrize("defect", ["missing_check", "duplicate_check", "missing_shot", "foreign_shot",
    "missing_span", "missing_visibility", "missing_number", "missing_ready", "missing_summary"])
def test_short_output_cannot_omit_coverage_or_decisions(plan, review, defect):
    raw = omit_success_notes(review)
    if defect == "missing_check":
        raw["checks"].pop()
    elif defect == "duplicate_check":
        raw["checks"][1] = deepcopy(raw["checks"][0])
    elif defect == "missing_shot":
        raw["alignment"]["shots"].pop()
    elif defect == "foreign_shot":
        raw["alignment"]["shots"][0]["shot_id"] = "unreviewed_shot"
    elif defect == "missing_span":
        raw["alignment"]["spans"].pop()
    elif defect == "missing_visibility":
        raw["alignment"]["spans"][0].pop("speaker_visibility")
    elif defect == "missing_number":
        raw["alignment"]["shots"][0]["pacing"].pop("action_seconds_required")
    elif defect == "missing_ready":
        raw.pop("ready")
    else:
        raw["alignment"].pop("summary")
    before = deepcopy(raw)
    with pytest.raises(ValueError):
        validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan)
    assert raw == before


@pytest.mark.parametrize("failure", ["content", "execution", "span", "shot", "timing", "cut",
                                     "omission", "visual"])
def test_explicit_or_lowered_failure_requires_a_real_reason(plan, review, failure):
    raw = omit_success_notes(review)
    shot = raw["alignment"]["shots"][0]
    if failure == "content":
        raw["checks"][0]["passed"] = False
    elif failure == "execution":
        raw["execution_check"]["state_continuity"] = False
    elif failure == "span":
        raw["alignment"]["spans"][0]["passed"] = False
    elif failure == "shot":
        shot["passed"] = False
    elif failure == "timing":
        shot["pacing"]["action_seconds_required"] = 11
    elif failure == "cut":
        shot["pacing"]["cut_necessity_passed"] = False
    elif failure == "omission":
        shot["temporal_omission"] = "unsupported"
    else:
        shot["visual_execution"]["verdict"] = "unclear"
    before = deepcopy(raw)
    lowered = lower_failed_plan_verdicts(raw, plan)
    assert lowered["ready"] is False
    with pytest.raises(ValueError, match="原因|说明|note"):
        validate_plan_review(lowered, plan)
    assert raw == before
    # A format repair may supply the actual missing explanation; it cannot
    # erase an existing rejection to avoid supplying one.
    attempted_upgrade = omit_success_notes(review)
    with pytest.raises(ValueError, match="已有失败"):
        preserve_review_rejections(raw, attempted_upgrade, plan)


def test_authored_failure_notes_remain_exact_and_do_not_release_a_plan(plan, review):
    raw = omit_success_notes(review)
    raw["alignment"]["shots"][0]["pacing"].update(action_seconds_required=11,
        note="本镜取物和挂起需十一秒，超过本镜十秒时窗。")
    raw["alignment"]["shots"][0]["note"] = "挂起动作不能在本镜现有时长内完成。"
    lowered = lower_failed_plan_verdicts(raw, plan)
    checked = validate_plan_review(lowered, plan)
    assert checked.ready is False
    assert checked.alignment.shots[0].note == raw["alignment"]["shots"][0]["note"]
    assert checked.alignment.shots[0].pacing.note == raw["alignment"]["shots"][0]["pacing"]["note"]
    assert checked.alignment.shots[0].pacing.action_seconds_required == 11


@pytest.mark.parametrize("defect", [None, "missing", "wrong_quote"])
def test_passing_temporal_evidence_is_required_even_without_positive_notes(cut_plan, defect):
    raw = omit_success_notes(support(cut_plan))
    shot = raw["alignment"]["shots"][1]
    if defect == "missing":
        shot.pop("temporal_omission_evidence")
    elif defect == "wrong_quote":
        shot["temporal_omission_evidence"]["before_description_quote"] = "并不存在的原文"
    before = deepcopy(raw), deepcopy(cut_plan)
    if defect:
        with pytest.raises(ValueError):
            validate_plan_review(raw, cut_plan)
    else:
        checked = validate_plan_review(raw, cut_plan)
        assert checked.ready
        assert checked.alignment.shots[1].temporal_omission_evidence.model_dump() == shot["temporal_omission_evidence"]
    assert (raw, cut_plan) == before


def test_short_review_cannot_be_reused_after_actual_subject_change(plan, review):
    checked = validate_plan_review(omit_success_notes(review), plan)
    changed = plan.model_dump()
    changed["facts"]["claims"].append("新增待审主张")
    with pytest.raises(ValueError, match="当前最终方案"):
        validate_plan_review(checked, changed)
