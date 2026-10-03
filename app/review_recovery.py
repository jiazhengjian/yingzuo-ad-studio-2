"""Bounded review-format feedback and rejection preservation, without I/O.

A format correction reviews the same immutable plan. It may complete malformed
records, but cannot erase an already explicit rejection of that plan. This is
not a replacement for the caller's complete PlanReview validation.
"""
from __future__ import annotations

from pydantic import ValidationError

from .ad_script import CHECK_CRITERIA
from .av_review import review_speech_requirements
from .plan_review import lower_failed_plan_verdicts
from .schemas import Plan


def review_validation_feedback(exc: Exception) -> list[dict]:
    """Return bounded field diagnostics, excluding offending inputs/context."""
    if isinstance(exc, ValidationError):
        return [{
            "loc": [part if type(part) is int else str(part)[:128]
                    for part in error["loc"][:12]],
            "type": error["type"][:80],
            "message": error["msg"][:500],
        } for error in exc.errors(include_url=False, include_context=False,
                                  include_input=False)[:10]]
    return [{"loc": [], "type": "review_contract", "message": str(exc)[:500]}]


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _identity(row, fields):
    key = tuple(row.get(field) for field in fields)
    return key if all(isinstance(part, str) for part in key) else None


def _matching_row(rows, fields, identity):
    matches = [row for row in rows if _identity(row, fields) == identity]
    # Never pair by array position or choose one duplicate as authoritative.
    # The full validator also rejects duplicate/foreign/missing identities.
    return matches[0] if len(matches) == 1 else {}


def _preserve_false(previous, corrected, fields, label):
    for field in fields:
        if previous.get(field) is False and corrected.get(field) is not False:
            raise ValueError(f"审核记录修正不能删除或提升已有失败结论：{label}.{field}")


def _preserve_negative_enum(previous, corrected, field, negative_values, label):
    value = previous.get(field)
    if value in negative_values and corrected.get(field) != value:
        raise ValueError(f"审核记录修正不能改写已有失败依据：{label}.{field}")


def preserve_review_rejections(previous, corrected, plan: Plan | dict):
    """Return a lowered copy of ``corrected`` if prior rejections survive.

    Only explicit booleans and the existing conservative lowering rules carry
    rejections forward. Missing fields are not failures. Item-level opinions
    bind to real plan identities, never guessed IDs or matching array indices.
    Unknown/duplicate records remain for full validation; no records, passing
    checks or notes are synthesized here. All three inputs remain untouched.
    """
    before = _mapping(lower_failed_plan_verdicts(previous, plan))
    result = lower_failed_plan_verdicts(corrected, plan)
    after = _mapping(result)
    _preserve_false(before, after, ("ready",), "review")
    _preserve_false(_mapping(before.get("execution_check")),
                    _mapping(after.get("execution_check")),
                    ("action_capacity", "state_continuity"), "execution_check")

    before_alignment = _mapping(before.get("alignment"))
    after_alignment = _mapping(after.get("alignment"))
    _preserve_false(before_alignment, after_alignment,
                    ("ready", "ad_expression_passed"), "alignment")

    corrected_checks = _rows(after.get("checks"))
    for check in _rows(before.get("checks")):
        identity = _identity(check, ("criterion",))
        if identity is None or identity[0] not in CHECK_CRITERIA:
            continue
        updated = _matching_row(corrected_checks, ("criterion",), identity)
        _preserve_false(check, updated, ("passed",), f"checks.{identity[0]}")

    subject = plan.model_dump() if isinstance(plan, Plan) else plan
    known = {"shots": set(), "spans": set()}
    span_shots = {}
    for segment in _rows(_mapping(subject).get("segments")):
        segment_id = segment.get("id")
        if not isinstance(segment_id, str):
            continue
        for collection, source in (("shots", "shots"), ("spans", "speech_spans")):
            for item in _rows(segment.get(source)):
                if isinstance(item.get("id"), str):
                    known[collection].add((segment_id, item["id"]))
                    if collection == "spans":
                        span_shots[(segment_id, item["id"])] = {
                            sid for sid in item.get("shot_ids", []) if isinstance(sid, str)}

    visible_speaking = review_speech_requirements(plan)["requires_visible_speaking"]
    production = _mapping(_mapping(subject).get("production"))
    continuation = (production.get("speech_mode") == "character_dialogue"
                    and production.get("speech_delivery") == "on_and_off_screen")
    for collection, identifier in (("spans", "span_id"), ("shots", "shot_id")):
        fields = ("segment_id", identifier)
        corrected_rows = _rows(after_alignment.get(collection))
        for check in _rows(before_alignment.get(collection)):
            identity = _identity(check, fields)
            if identity is None or identity not in known[collection]:
                continue
            updated = _matching_row(corrected_rows, fields, identity)
            label = f"alignment.{collection}.{identity[0]}.{identity[1]}"
            _preserve_false(check, updated, ("passed",), label)
            if collection == "shots":
                _preserve_false(_mapping(check.get("pacing")), _mapping(updated.get("pacing")),
                    ("action_timing_passed", "cut_necessity_passed"), label + ".pacing")
                _preserve_negative_enum(check, updated, "temporal_omission",
                    ("unsupported", "unclear"), label)
                _preserve_negative_enum(_mapping(check.get("visual_execution")),
                    _mapping(updated.get("visual_execution")), "verdict",
                    ("unclear", "conflict"), label + ".visual_execution")
            elif visible_speaking:
                _preserve_negative_enum(check, updated, "speaker_visibility",
                    ("off_screen", "mixed", "unclear", "not_applicable"), label)
            elif continuation:
                _preserve_negative_enum(check, updated, "speaker_visibility",
                    ("unclear", "not_applicable"), label)
                for shot in _rows(check.get("shot_visibility")):
                    shot_identity = _identity(shot, ("shot_id",))
                    if shot_identity is None or shot_identity[0] not in span_shots.get(identity, set()):
                        continue
                    updated_shot = _matching_row(_rows(updated.get("shot_visibility")),
                                                 ("shot_id",), shot_identity)
                    _preserve_negative_enum(shot, updated_shot, "visibility", ("unclear",),
                                            label + ".shot_visibility." + shot_identity[0])
    return result
