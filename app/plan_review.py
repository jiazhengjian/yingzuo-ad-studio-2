"""One independent review of the final plan, bound to its reviewed contents.

Contracts and conservative verdict aggregation only. No provider, settings,
state access, hidden reasoning or generated-media quality claims live here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .ad_script import CHECK_CRITERIA, ScriptCheck, ScriptExecutionCheck
from .av_review import (AVReview, all_speech_explicitly_off_screen, lower_failed_av_verdicts, negative_speaker_visibility,
                        review_speech_requirements, validate_av_review)
from .schemas import Plan
from .review_policy import (MAX_REVIEW_SUGGESTIONS, REVIEW_POLICY_VERSION, ReviewSuggestion,
                            prepare_review_citations, review_suggestion_targets, validate_review_suggestions)


_SegmentId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-zA-Z0-9_-]{1,48}$")]
_AssetId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=160)]
_ReviewedReferences = dict[_SegmentId, Annotated[list[_AssetId], Field(min_length=1, max_length=10)]]


class PlanReview(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    ready: bool = Field(strict=True, description="六项内容、动作执行及最终音画检查均不存在需要阻断的具体问题才为true；不代表艺术质量满分，可选表达建议独立记录")
    checks: list[ScriptCheck] = Field(min_length=6, max_length=6,
        description="直接审核最终Plan中的实际台词、动作和画面，六项标准各一次；通过指不存在需要阻断的具体问题，不要求艺术质量满分，非阻断建议放suggestions")
    execution_check: ScriptExecutionCheck = Field(
        description="审核最终方案的动作容量与状态承接，不改写脚本")
    alignment: AVReview = Field(description="逐项审核最终Plan的全部旁白时段和实际镜头，包括静音镜头与逐镜pacing")
    policy_version: Literal[1] | None = Field(default=None, exclude_if=lambda value: value is None,
        description="审核分级约定版本；1保留全部硬性检查，可选表达建议独立记录，不影响ready")
    suggestions: list[ReviewSuggestion] | None = Field(default=None, max_length=MAX_REVIEW_SUGGESTIONS,
        exclude_if=lambda value: value is None,
        description="有逐字原文定位的非阻断建议；没有建议时明确返回空数组，不据此触发返修或声明已修复")
    reviewed_plan_sha256: str | None = Field(default=None, strict=True, pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
        description="服务端绑定审核对象的内容哈希；模型输出schema不包含此字段")
    reviewed_reference_asset_ids: _ReviewedReferences | None = Field(default=None, min_length=1, max_length=6,
        exclude_if=lambda value: value is None,
        description="服务端保存审核时各段原图引用；后续追加场景及角色参考不代表已经过本次计划审核")

    @field_validator("policy_version", mode="before")
    @classmethod
    def strict_policy_version(cls, value):
        if value is not None and (type(value) is not int or value != REVIEW_POLICY_VERSION):
            raise ValueError("审核分级版本须为整数1，不能用布尔值或字符串代替")
        return value

    @model_validator(mode="after")
    def verdict_matches_checks(self):
        if (self.policy_version is None) != (self.suggestions is None):
            raise ValueError("审核分级版本与建议列表须同时保留；无建议时返回空数组")
        if (self.reviewed_plan_sha256 is None) != (self.reviewed_reference_asset_ids is None):
            raise ValueError("综合审核的内容哈希与原图引用绑定须同时保留")
        if {check.criterion for check in self.checks} != set(CHECK_CRITERIA):
            raise ValueError("综合审核必须逐项包含六个不重复的内容检查结论")
        passed = (all(check.passed for check in self.checks)
                  and self.execution_check.action_capacity
                  and self.execution_check.state_continuity
                  and self.alignment.ready)
        if self.ready != passed:
            raise ValueError("综合审核结论必须与六项内容、动作执行及最终音画检查一致")
        return self


def plan_review_schema(plan=None) -> dict:
    """Model wire contract: no rewritten script or server-owned binding."""
    schema = PlanReview.model_json_schema()
    for field in ("reviewed_plan_sha256", "reviewed_reference_asset_ids"):
        schema["properties"].pop(field)
    for field in ("policy_version", "suggestions"):
        definition = schema["properties"][field]
        definition.update(next(option for option in definition.pop("anyOf") if option.get("type") != "null"))
        definition.pop("default", None)
        schema.setdefault("required", []).append(field)
    schema["$defs"]["ScriptCheck"]["properties"]["passed"]["description"] = (
        "当前标准不存在需要阻断的具体问题才为true；不代表艺术质量满分。"
        "事实、固定信息或实际可执行性等具体失败仍须false，不能移到suggestions后改成true。")
    if plan is not None:
        schema["$defs"]["ReviewSuggestion"]["properties"]["target"]["enum"] = list(review_suggestion_targets(plan))
    alignment = schema["$defs"]["AVReview"]
    alignment["properties"]["shots"] = {"type": "array", "items": {"$ref": "#/$defs/AVShotCheck"},
        "maxItems": 108, "description": "恰好覆盖全部实际镜头，包括静音镜头"}
    alignment.setdefault("required", []).append("shots")
    shot = schema["$defs"]["AVShotCheck"]
    shot["properties"]["pacing"] = {"$ref": "#/$defs/AVShotPacing"}
    shot.setdefault("required", []).append("pacing")
    shot["properties"]["visual_execution"] = {"$ref": "#/$defs/AVVisualExecution"}
    shot.setdefault("required", []).append("visual_execution")
    shot["allOf"] = [{
        "if": {"properties": {"temporal_omission": {"const": "supported_by_cut"}},
               "required": ["temporal_omission"]},
        "then": {"required": ["temporal_omission_evidence"], "properties": {
            "temporal_omission_evidence": {"$ref": "#/$defs/AVTemporalOmissionEvidence"}}},
    }]
    for definition, field in (("AVSpanCheck", "speaker_visibility"), ("AVShotCheck", "temporal_omission")):
        check = schema["$defs"][definition]
        verdict = check["properties"][field]
        verdict.update(next(option for option in verdict.pop("anyOf") if option.get("type") != "null"))
        verdict.pop("default", None)
        check.setdefault("required", []).append(field)
    if plan is not None and review_speech_requirements(plan)["allows_off_screen"]:
        span = schema["$defs"]["AVSpanCheck"]
        visibility = span["properties"]["shot_visibility"]
        visibility.update(next(option for option in visibility.pop("anyOf") if option.get("type") != "null"))
        visibility.pop("default", None)
        span.setdefault("required", []).append("shot_visibility")
    return schema


def _validated_review_subject(plan: Plan | dict) -> Plan:
    # Removing only the attached new review breaks the Plan -> review -> Plan
    # cycle while retaining every existing plan/content/legacy contract.
    data = plan.model_dump(mode="json") if isinstance(plan, Plan) else deepcopy(plan)
    if isinstance(data, dict):
        data.pop("plan_review", None)
    return Plan.model_validate(data)


# Written by the server after a review, or while preparing media. They must not
# make a saved review look like the merchant changed the film.
_SERVER_SEGMENT_FIELDS = ("speech_characters_per_tenth", "reference_roles")


def _fingerprint_validated_plan(plan: Plan, reviewed_references: _ReviewedReferences | None = None, *,
                                include_server_fields: bool = False) -> str:
    # Review opinions and skill execution provenance do not change the subject.
    # Merchant-visible content stays covered: facts, script, directing,
    # production, original reference IDs, shots and speech timing. Speech rate
    # and reference-role maps are server stamps. Later media preparation may
    # append references; the picture gate still checks ownership and content.
    content = plan.model_dump(mode="json", exclude={
        "plan_review", "script_review", "alignment_review", "skill_trace"})
    if reviewed_references is not None and set(reviewed_references) != {segment.id for segment in plan.segments}:
        raise ValueError("综合审核的原图引用绑定须完整对应当前最终方案各段")
    for segment in content["segments"]:
        if not include_server_fields:
            for field in _SERVER_SEGMENT_FIELDS:
                segment.pop(field, None)
        if reviewed_references is None:
            continue
        original_ids = reviewed_references[segment["id"]]
        if segment["reference_asset_ids"][:len(original_ids)] != original_ids:
            raise ValueError("综合审核的原图引用须作为当前最终方案的完整有序前缀保留")
        segment["reference_asset_ids"] = list(original_ids)
    wire = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def plan_review_fingerprint(plan: Plan | dict) -> str:
    """Identity of reviewed planning content, excluding later media additions."""
    attached = plan.plan_review if isinstance(plan, Plan) else plan.get("plan_review") if isinstance(plan, dict) else None
    references = PlanReview.model_validate(attached).reviewed_reference_asset_ids if attached is not None else None
    return _fingerprint_validated_plan(_validated_review_subject(plan), references)


def validate_plan_review(raw: PlanReview | dict, plan: Plan | dict, *,
                         require_speaker_visibility: bool = False,
                         require_temporal_omission: bool = False,
                         require_temporal_omission_evidence: bool = False,
                         require_visual_execution: bool = False,
                         require_review_policy: bool = False) -> PlanReview:
    """Validate complete coverage and bind the exact final plan on the server.

    A rejected review is valid data. Callers must require ``ready`` before
    delivery. This never rewrites the subject or fills in missing verdicts.
    Existing bindings must match; only a fresh unbound review gains a hash and
    original-reference snapshot. Later references may only follow that exact
    original prefix; generated media itself has not been reviewed here.
    """
    raw = prepare_review_citations(raw, plan)
    review = PlanReview.model_validate(raw)
    subject = _validated_review_subject(plan)
    # Live callers explicitly require the new policy even if a provider sends
    # an old server binding. Historical adapters keep their original contract.
    if require_review_policy and review.policy_version is None:
        raise ValueError("本次综合审核必须明确审核分级版本及建议列表")
    if review.suggestions is not None:
        validate_review_suggestions(review.suggestions, subject)
    references = review.reviewed_reference_asset_ids
    if references is None:
        references = {segment.id: list(segment.reference_asset_ids) for segment in subject.segments}
    fingerprint = _fingerprint_validated_plan(subject, references)
    stored_hash = review.reviewed_plan_sha256
    if stored_hash is not None and stored_hash != fingerprint:
        # Reviews saved before server fields were left out of the fingerprint
        # still name this same film. Rebind to the canonical hash. A real
        # script, shot, timing or fact change matches neither hash.
        legacy = _fingerprint_validated_plan(subject, references, include_server_fields=True)
        if stored_hash != legacy:
            raise ValueError("综合审核记录必须对应当前最终方案；修改后须清除旧审核并重新审核")
    # New provider verdicts must complete the current review fields. Bound
    # records predating them remain readable without invented evidence.
    review.alignment = validate_av_review(review.alignment, subject, require_pacing=True,
        require_speaker_visibility=require_speaker_visibility or review.reviewed_plan_sha256 is None,
        require_temporal_omission=require_temporal_omission or review.reviewed_plan_sha256 is None,
        require_temporal_omission_evidence=require_temporal_omission_evidence or review.reviewed_plan_sha256 is None,
        require_visual_execution=require_visual_execution or review.reviewed_plan_sha256 is None,
        # Stored bound records stay readable. The live wire path explicitly
        # requires visual execution, so a supplied old hash cannot bypass this.
        require_action_windows=require_visual_execution or review.reviewed_plan_sha256 is None)
    return review.model_copy(update={"reviewed_plan_sha256": fingerprint,
                                     "reviewed_reference_asset_ids": deepcopy(references)})


def normalize_plan_review_wire(raw):
    """Relocate one known wire field; never invent verdicts or discard conflicts."""
    result = deepcopy(raw)
    if not isinstance(result, dict) or not isinstance(result.get("alignment"), dict):
        return result
    shots = result["alignment"].get("shots")
    for check in shots if isinstance(shots, list) else []:
        if not isinstance(check, dict) or "temporal_omission" in check:
            continue
        pacing = check.get("pacing")
        if (isinstance(pacing, dict) and pacing.get("temporal_omission") in
                ("no_omission", "supported_by_cut", "unsupported", "unclear")):
            check["temporal_omission"] = pacing.pop("temporal_omission")
    return result


def lower_failed_plan_verdicts(raw, plan):
    """Only lower explicit contradictions; never fill or promote a verdict.

    Numeric shot overruns use the existing conservative AV rule. Invalid and
    incomplete records remain invalid for validate_plan_review to reject.
    The provider response is retained unchanged by deep-copying first.
    """
    result = normalize_plan_review_wire(raw)
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("alignment"), dict):
        shots = result["alignment"].get("shots")
        for check in shots if isinstance(shots, list) else []:
            if (isinstance(check, dict) and check.get("passed") is True
                    and check.get("temporal_omission") in ("unsupported", "unclear")):
                check["passed"] = False
        negative = negative_speaker_visibility(plan)
        if negative:
            spans = result["alignment"].get("spans")
            for check in spans if isinstance(spans, list) else []:
                details = check.get("shot_visibility") if isinstance(check, dict) else None
                unclear_shot = (review_speech_requirements(plan)["allows_off_screen"]
                    and any(isinstance(item, dict) and item.get("visibility") == "unclear"
                        for item in details if isinstance(details, list))) if isinstance(details, list) else False
                if (isinstance(check, dict) and check.get("passed") is True
                        and (check.get("speaker_visibility") in negative or unclear_shot)):
                    check["passed"] = False
        if (result["alignment"].get("ad_expression_passed") is True
                and all_speech_explicitly_off_screen(result["alignment"], plan)):
            result["alignment"]["ad_expression_passed"] = False
        result["alignment"] = lower_failed_av_verdicts(result["alignment"], plan)
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    execution = result.get("execution_check") if isinstance(result.get("execution_check"), dict) else {}
    alignment = result.get("alignment") if isinstance(result.get("alignment"), dict) else {}
    failed = (any(isinstance(check, dict) and check.get("passed") is False for check in checks)
              or execution.get("action_capacity") is False
              or execution.get("state_continuity") is False
              or alignment.get("ready") is False)
    if result.get("ready") is True and failed:
        result["ready"] = False
    return result


# Runtime review role/contract lives in progressive_planning.PLAN_REVIEW_SYSTEM.
# Advertising review methods live in the registered plan-review reference.
