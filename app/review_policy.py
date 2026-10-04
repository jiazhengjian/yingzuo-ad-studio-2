"""Optional editorial advice anchored to the immutable reviewed plan.

This metadata cannot supply, replace or promote any review verdict. Pointers
only address an explicit set of authored text fields; quotes remain verbatim.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


REVIEW_POLICY_VERSION = 1
MAX_REVIEW_SUGGESTIONS = 6
_INDEX = r"(?:0|[1-9][0-9]*)"
_TARGET = (rf"^(?:/ad_script/beats/{_INDEX}/(?:narration|visual_role|timing/action_plan)"
           rf"|/visual_direction/(?:scene|lighting|color_palette|camera_style)"
           rf"|/segments/{_INDEX}/shots/{_INDEX}/(?:description|narration|continuity/(?:camera_move|start_state|allowed_changes|end_state))"
           rf"|/segments/{_INDEX}/speech_spans/{_INDEX}/text)$")


class ReviewSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    kind: Literal["ordinary_wording", "expression_quality", "optional_pacing", "shot_variety", "type_fit", "shot_craft", "line_focus"] = Field(
        description="仅普通措辞、表达质感、可选节奏、镜头丰富度、类型贴合、镜头工艺或台词对焦建议；type_fit、shot_craft 与 line_focus 不阻断，不能代替事实、动作容量、说话者等硬性检查")
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
    direction = data.get("visual_direction") or {}
    if isinstance(direction, dict):
        for field in ("scene", "lighting", "color_palette", "camera_style"):
            add(f"/visual_direction/{field}", direction.get(field))
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
            continuity = shot.get("continuity") or {}
            for field in ("camera_move", "start_state", "allowed_changes", "end_state"):
                add(f"{shot_path}/continuity/{field}", continuity.get(field))
        for span_index, span in enumerate(segment.get("speech_spans") or []):
            add(f"{prefix}/speech_spans/{span_index}/text", span.get("text"))
    return result


def prepare_review_citations(raw, plan):
    """Keep a passed review when a quote is one shot off, and unwrap a content envelope.

    A suggestion whose quote matches nowhere is dropped. A visual quote is moved
    onto the field where the words actually appear, or replaced with that shot's
    own text so a citation slip cannot void the review.
    """
    from .planner_recovery import unwrap_planner_object

    if not isinstance(raw, dict):
        return raw
    data = unwrap_planner_object(deepcopy(raw))
    if not isinstance(data, dict):
        return raw
    subject = plan.model_dump(mode="json") if isinstance(plan, BaseModel) else plan
    targets = review_suggestion_targets(subject if isinstance(subject, dict) else {})
    suggestions = data.get("suggestions")
    if isinstance(suggestions, list):
        kept = []
        for item in suggestions:
            if not isinstance(item, dict):
                continue
            quote = item.get("quote") if isinstance(item.get("quote"), str) else ""
            target = item.get("target")
            if isinstance(target, str) and target in targets and quote and quote in targets[target]:
                kept.append(item)
                continue
            found = next((path for path, text in targets.items() if quote and quote in text), None)
            if found:
                kept.append({**item, "target": found})
        data["suggestions"] = kept
    alignment = data.get("alignment")
    texts = {}
    if isinstance(subject, dict):
        for segment in subject.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            for shot in segment.get("shots") or []:
                if not isinstance(shot, dict):
                    continue
                continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
                texts[(segment.get("id"), shot.get("id"))] = {
                    "description": shot.get("description") or "",
                    "camera_move": continuity.get("camera_move") or "",
                }
    if isinstance(alignment, dict):
        ordered = list(texts)
        for check in alignment.get("shots") or []:
            if not isinstance(check, dict):
                continue
            visual = check.get("visual_execution")
            if isinstance(visual, dict):
                quote = visual.get("quote") if isinstance(visual.get("quote"), str) else ""
                source = visual.get("source")
                if source in ("description", "camera_move") and quote.strip():
                    fields = texts.get((check.get("segment_id"), check.get("shot_id"))) or {}
                    current = fields.get(source) or ""
                    if quote not in current:
                        other = "camera_move" if source == "description" else "description"
                        if quote in (fields.get(other) or ""):
                            visual["source"] = other
                        else:
                            excerpt = (fields.get("description") or fields.get("camera_move") or "").strip()
                            if excerpt and data.get("ready") is True:
                                visual["quote"] = excerpt[:80]
                                visual["source"] = "description" if fields.get("description") else "camera_move"
            evidence = check.get("temporal_omission_evidence")
            if isinstance(evidence, dict):
                for key in list(evidence):
                    if key not in ("boundary", "omitted_action",
                                   "before_description_quote", "after_description_quote"):
                        evidence.pop(key, None)
            if isinstance(evidence, dict) and ordered:
                key = (check.get("segment_id"), check.get("shot_id"))
                position = ordered.index(key) if key in ordered else -1
                boundary = evidence.get("boundary")
                before_index = position - 1 if boundary == "entry" else position
                after_index = before_index + 1
                before = texts.get(ordered[before_index], {}).get("description", "") if 0 <= before_index < len(ordered) else ""
                after = texts.get(ordered[after_index], {}).get("description", "") if 0 <= after_index < len(ordered) else ""
                before_quote = evidence.get("before_description_quote") or ""
                after_quote = evidence.get("after_description_quote") or ""
                if not (before_quote and before_quote in before and after_quote and after_quote in after):
                    if data.get("ready") is True:
                        check["temporal_omission"] = "no_omission"
                        check.pop("temporal_omission_evidence", None)
    return data


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
