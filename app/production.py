"""Approved product, cast and speech choices; no provider or inferred facts."""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Image and video prompts share this sentence. An explicit other look replaces it.
DEFAULT_CAST_LINE = "默认出镜为中国人，East Asian Chinese appearance，日常大陆长相。"
# Bump when the default sheet layout or cast line changes, so an unconfirmed
# older sheet can be told apart from one generated with this template.
CHARACTER_SHEET_TEMPLATE = "chinese-horizontal-v1"
OVERRIDE_CAST_LINE = "外貌按已确认资料中的明确要求绘制。"
_OTHER_CAST = re.compile(
    r"(?:不要|别用|不要画成|别画成).{0,4}(?:中国人|东亚面孔|华人)"
    r"|欧美(?:人|面孔|模特|女性|男性|长相|脸)"
    r"|西方(?:人|面孔|模特|女性|男性|长相|脸)"
    r"|外国人|外国模特|外国面孔"
    r"|白人|黑人|非裔|拉丁裔"
    r"|(?:日本|韩国|东南亚|南亚|印度)(?:人|面孔|模特|女性|男性)"
    r"|Caucasian|Western face|European face|African American|\bKorean\b|\bJapanese\b",
    re.IGNORECASE,
)


def merchant_requests_other_cast(*texts) -> bool:
    """True when the merchant, or the description written from that request, names another look."""
    return any(isinstance(text, str) and _OTHER_CAST.search(text) for text in texts)


def cast_appearance_clause(*texts) -> str:
    """The look sent with one character. Explicit requests replace the Chinese default."""
    if merchant_requests_other_cast(*texts):
        return OVERRIDE_CAST_LINE
    return DEFAULT_CAST_LINE


def saved_character_sheet_is_stale(metadata) -> bool:
    """True unless this saved sheet is the current horizontal Chinese-default template."""
    data = metadata if isinstance(metadata, dict) else {}
    return not (data.get("sheet_layout") == "horizontal_16x9"
                and data.get("template_version") == CHARACTER_SHEET_TEMPLATE)


def current_character_sheet_name(key: str, request) -> str:
    """New-template sheets use their own file so an older 2×2 image is left in place."""
    if (isinstance(request, dict) and request.get("sheet_layout") == "horizontal_16x9"
            and request.get("template_version") == CHARACTER_SHEET_TEMPLATE):
        return f"{key}.{CHARACTER_SHEET_TEMPLATE}.png"
    return f"{key}.png"


def apply_cast_instructions(instructions: str, *texts: str) -> str:
    """Keep the sheet layout, and use one appearance line."""
    clause = cast_appearance_clause(*texts)
    if DEFAULT_CAST_LINE in instructions:
        return instructions.replace(DEFAULT_CAST_LINE, clause)
    return instructions.rstrip() + "\n" + clause


class ProductConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500,
        description="由原图或用户资料支持的商品形态/数量/尺度关系及物理状态规则；不把谁操作、动作次序或旧稿剧情当商品事实，用户指定动作在故事中保留；未知规格不作事实")
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class CharacterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^char_[a-z0-9_]{1,40}$")
    name: str = Field(min_length=1, max_length=60, description="故事中的角色称呼")
    description: str = Field(min_length=1, max_length=800,
        description="创作设定的年龄段、发型、服装和体态。商家没有明确其他外貌时写成中国人、东亚面孔、日常大陆长相；商家明确要求的外貌按该要求写，不要改回中国人。供同一角色参考图与视频复用")
    appearance: Literal["face_visible", "partial_only"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="本片出镜安排：face_visible至少有脸部可见画面；partial_only只露手、头发或身体局部，不露脸。局部也是人物出镜，不把同一人拆成多个角色；历史缺省不推断")


class ProductionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["1.0.0"] = "1.0.0"
    product_constraints: list[ProductConstraint] = Field(min_length=1, max_length=12)
    characters: list[CharacterSpec] = Field(default_factory=list, max_length=8,
        description="所有有身份的出镜角色，包括只露手的主要角色；无人物时为空。每角色新增一张组合四视图")
    speech_mode: Literal["voiceover", "character_dialogue", "none"]
    speech_delivery: Literal["visible_only", "on_and_off_screen"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="人物声音安排：visible_only说话时始终可见开口；on_and_off_screen允许同一已确认角色在商品等镜头画外继续说，仍须有可见开口片段。缺省沿用visible_only；画外旁白或无人声不用此字段")
    speaker_id: str = Field(default="", max_length=45,
        description="人物对白时选择本方案一个角色ID；画外音/无人声为空，不临时新增说话人")
    voice_description: str = Field(default="", max_length=600,
        description="人声的语言、声线与表演要求，不写具体台词和动作时点；台词由本轮内容稿统一编排并保留用户固定原句；不声称声纹锁定，无人声为空")

    @model_validator(mode="after")
    def consistent_choices(self):
        ids = [person.id for person in self.characters]
        if len(ids) != len(set(ids)):
            raise ValueError("角色 ID 不能重复")
        if self.speech_mode == "character_dialogue":
            if self.speaker_id not in ids:
                raise ValueError("人物对白必须指定本方案中的说话角色")
            if next(person for person in self.characters if person.id == self.speaker_id).appearance == "partial_only":
                raise ValueError("人物对白说话角色须有可见开口画面，不能仅安排局部出镜")
        elif self.speaker_id:
            raise ValueError("画外音或无人声不能指定画内说话角色")
        if self.speech_delivery == "on_and_off_screen" and self.speech_mode != "character_dialogue":
            raise ValueError("同一人物画外延续只适用于已确认说话角色的人物对白")
        if self.speech_mode == "none":
            if self.voice_description.strip():
                raise ValueError("无人声方案不能同时要求人声")
        elif not self.voice_description.strip():
            raise ValueError("有声方案必须写清人声表演要求")
        return self


def production_authoring_schema(schema):
    """Omit application-owned protocol metadata from model authoring requests."""
    schema = deepcopy(schema)
    production = schema.get("$defs", {}).get("ProductionPlan")
    if production is not None:
        production["properties"].pop("version", None)
        if "required" in production:
            production["required"] = [key for key in production["required"] if key != "version"]
    character = schema.get("$defs", {}).get("CharacterSpec")
    if character is not None:
        appearance = character["properties"]["appearance"]
        appearance.update(next(option for option in appearance.pop("anyOf") if option.get("type") != "null"))
        appearance.pop("default", None)
        character["required"] = list(dict.fromkeys([*character.get("required", []), "appearance"]))
    return schema


def derive_authored_production_metadata(raw):
    """Set protocol metadata only on newly authored direction productions.

    Gateway calls this at the model-output boundary, never on saved or approved
    plans. All content still passes the normal strict schema/source validators.
    Do not recursively rewrite arbitrary or malformed production-shaped data.
    """
    result = deepcopy(raw)
    if isinstance(result, dict) and isinstance(result.get("directions"), list):
        for direction in result["directions"]:
            if isinstance(direction, dict) and isinstance(direction.get("production"), dict):
                direction["production"]["version"] = ProductionPlan.model_fields["version"].default
    return result


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def validate_production_sources(production, facts):
    if production is None:
        return
    production = ProductionPlan.model_validate(production)
    ids = {_get(fact, "id") for fact in facts}
    for constraint in production.product_constraints:
        if not constraint.text.strip() or not set(constraint.evidence_ids).issubset(ids):
            raise ValueError("商品形态和动作约束必须引用本轮已有事实")


def validate_production_plan(plan):
    strategy = _get(plan, "strategy")
    selected = next((d for d in _get(strategy, "directions", [])
                     if _get(d, "id") == _get(strategy, "selected_direction_id")), None)
    approved = _get(selected, "production")
    production = _get(plan, "production")
    if production is None:
        if approved is not None:
            raise ValueError("方案不能省略已选方向的人物、声音或商品约束")
        return
    production = ProductionPlan.model_validate(production)
    if production.speech_mode == "none":
        texts = [_get(plan, "script", "")]
        for segment in _get(plan, "segments", []):
            texts.append(_get(segment, "narration", ""))
            texts.extend(_get(shot, "narration", "") for shot in _get(segment, "shots", []))
        ad_script = _get(plan, "ad_script")
        texts.extend(_get(beat, "narration", "") for beat in _get(ad_script, "beats", []))
        if any(text and text.strip() for text in texts):
            raise ValueError("无人声方案不能保留任何台词")
    brief = _get(strategy, "brief")
    validate_production_sources(production, _get(brief, "facts", []))
    if approved is not None and ProductionPlan.model_validate(approved) != production:
        raise ValueError("分镜不能改写已选方向的人物、声音或商品约束")
    ids = {person.id for person in production.characters}
    for segment in _get(plan, "segments", []):
        by_id = {_get(shot, "id"): shot for shot in _get(segment, "shots", [])}
        for shot in by_id.values():
            if not set(_get(shot, "character_ids", [])).issubset(ids):
                raise ValueError("镜头只能引用已确认的出镜角色")
        for span in _get(segment, "speech_spans", []) or []:
            if production.speech_mode == "none":
                raise ValueError("无人声方案不能安排台词")
            expected = production.speaker_id if production.speech_mode == "character_dialogue" else ""
            if _get(span, "speaker_id", "") != expected:
                raise ValueError("台词说话人必须与已确认的声音形式一致")
            if expected and production.speech_delivery != "on_and_off_screen" and (not _get(span, "shot_ids", []) or any(
                    sid not in by_id or expected not in _get(by_id[sid], "character_ids", [])
                    for sid in _get(span, "shot_ids", []))):
                raise ValueError("人物对白的说话窗口须包含已确认的说话角色；跨到无人画面请调整语音窗口")
            if expected and production.speech_delivery == "on_and_off_screen" and (
                    not _get(span, "shot_ids", []) or any(sid not in by_id for sid in _get(span, "shot_ids", []))):
                raise ValueError("人物声音延续只能引用本段存在的镜头")


def required_designs(plan):
    """One environment, one contact sheet per character, plus a still per changed state."""
    from .story_logic import plan_state_references
    production = _get(plan, "production")
    characters = _get(production, "characters", [])
    return [{"key": "scene", "kind": "scene", "name": "场景参考图"}, *[
        {"key": "character_" + _get(person, "id"), "kind": "character",
         "name": _get(person, "name") + " · 四视图", "character": person,
         "character_id": _get(person, "id")} for person in characters],
        *plan_state_references(plan)]


def validate_design_assets(plan, assets):
    """Check confirmed identities and ownership upstream; no semantic claims."""
    scenes = [asset for asset in assets if asset["kind"] == "scene" and asset["metadata"].get("generated")]
    characters = [asset for asset in assets if asset["kind"] == "character" and asset["metadata"].get("generated")]
    designs = required_designs(plan)
    expected = [item["character_id"] for item in designs if item["kind"] == "character"]
    actual = [asset["metadata"].get("character_id") for asset in characters]
    if len(scenes) != 1 or any(not isinstance(cid, str) for cid in actual) or sorted(actual) != sorted(expected):
        raise ValueError("请先准备并确认本方案的场景图及每位角色的四视图")
    if any(asset["metadata"].get("view_layout") != "four_view" for asset in characters):
        raise ValueError("角色参考必须是本方案已确认的四视图")
    ordered = scenes + [next(asset for asset in characters if asset["metadata"]["character_id"] == cid) for cid in expected]
    needed = [item for item in designs if item["kind"] == "start_frame"]
    if not needed:
        return ordered
    states = [asset for asset in assets if asset["kind"] == "start_frame" and asset["metadata"].get("state_reference")]
    by_id = {asset["metadata"].get("state_id"): asset for asset in states}
    if any(item["state_id"] not in by_id for item in needed):
        raise ValueError("请先准备并确认状态变化的起始与结果参考图")
    return ordered + [by_id[item["state_id"]] for item in needed]
