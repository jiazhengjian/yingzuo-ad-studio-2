"""Optional editorial advice anchored to the immutable reviewed plan.

This metadata cannot supply, replace or promote any review verdict. Pointers
only address an explicit set of authored text fields; quotes remain verbatim.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


REVIEW_POLICY_VERSION = 1
MAX_REVIEW_SUGGESTIONS = 6
_INDEX = r"(?:0|[1-9][0-9]*)"
_TARGET = (rf"^(?:/ad_script/beats/{_INDEX}/(?:narration|visual_role|timing/action_plan)"
           rf"|/segments/{_INDEX}/shots/{_INDEX}/(?:description|narration|continuity/camera_move)"
           rf"|/segments/{_INDEX}/speech_spans/{_INDEX}/text)$")


class ReviewSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    kind: Literal["ordinary_wording", "expression_quality", "optional_pacing", "shot_variety", "type_fit"] = Field(
        description="仅普通措辞、表达质感、可选节奏、镜头丰富度或类型贴合建议；type_fit 不阻断，不能代替事实、动作容量、说话者等硬性检查")
    target: Annotated[str, StringConstraints(strict=True, max_length=160, pattern=_TARGET)] = Field(
        description="当前final_plan中允许的实际文本字段JSON Pointer；数组使用从0开始的索引，不能指向事实、审核结论或数值时长")
    quote: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=240)] = Field(
        description="逐字引用target字段中连续且非空的一段原文；不改字、不拼接、不引用自己的建议")
    note: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=300)] = Field(
        description="非阻断的可选改进建议，不表示问题已修复，不填写通过或失败结论")

    @field_validator("quote")
    @classmethod
    def meaningful_quote(cls, value):
        if not value.strip():
            raise ValueError("审核建议须引用非空的实际原文")
        return value


def review_suggestion_targets(plan: BaseModel | dict) -> dict[str, str]:
    """Return eligible text exactly as stored, without resolving arbitrary paths."""
    data = plan.model_dump(mode="json") if isinstance(plan, BaseModel) else plan
    result = {}

    def add(path, value):
        if isinstance(value, str) and value.strip():
            result[path] = value

    # The model may omit the concatenated script summary. Anchor advice to
    # actual beats and speech spans, which survive request compaction.
    script = data.get("ad_script") or {}
    for index, beat in enumerate(script.get("beats", [])):
        prefix = f"/ad_script/beats/{index}"
        for field in ("narration", "visual_role"):
            add(f"{prefix}/{field}", beat.get(field))
        add(f"{prefix}/timing/action_plan", (beat.get("timing") or {}).get("action_plan"))
    for segment_index, segment in enumerate(data.get("segments", [])):
        prefix = f"/segments/{segment_index}"
        for shot_index, shot in enumerate(segment.get("shots", [])):
            shot_path = f"{prefix}/shots/{shot_index}"
            for field in ("description", "narration"):
                add(f"{shot_path}/{field}", shot.get(field))
            add(f"{shot_path}/continuity/camera_move", (shot.get("continuity") or {}).get("camera_move"))
        for span_index, span in enumerate(segment.get("speech_spans") or []):
            add(f"{prefix}/speech_spans/{span_index}/text", span.get("text"))
    return result


def validate_review_suggestions(suggestions: list[ReviewSuggestion], plan: BaseModel | dict) -> None:
    """Check localization only, never interpret advice as a repair or verdict."""
    targets = review_suggestion_targets(plan)
    seen = set()
    for suggestion in suggestions:
        if suggestion.target not in targets:
            raise ValueError("审核建议须定位当前方案中允许的实际非空文本字段")
        if suggestion.quote not in targets[suggestion.target]:
            raise ValueError("审核建议引文须逐字出现在指定的当前方案字段中")
        identity = (suggestion.kind, suggestion.target, suggestion.quote)
        if identity in seen:
            raise ValueError("同类别、位置和引文的审核建议不能重复")
        seen.add(identity)
