"""Small visual-direction contracts shared by planning and media compilation.

These fields are instructions and traceable references, not observations of a
rendered film. They do not establish semantic truth, identity or voice fidelity.
No provider, secrets, app settings or production state are imported here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


_Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=800)]
_Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=160)]


class _DirectingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class VisualDirection(_DirectingModel):
    version: Literal["1.0.0"] = "1.0.0"
    product_anchor_ids: list[_Identifier] = Field(min_length=1, max_length=12,
        description="从本轮strategy.brief.facts引用需要全片保留的商品特征，不自行补写外观、功能或材质")
    scene: _Text = Field(description="实际场所及可识别线索，保留商家指定环境；创意布置不是商品事实")
    lighting: _Text = Field(description="统一的光源方向、明暗与光线性质；是计划要求，不表示成片已匹配")
    color_palette: _Text = Field(description="统一色彩关系，不能借色调改变商品本身颜色")
    camera_style: _Text = Field(description="全片可执行的景别、景深和运镜原则，避免只有高级感等空泛词")
    sound: _Text = Field(description="旁白、背景声及节奏要求，不是音色锁定或已验证音画同步的承诺")

    @model_validator(mode="after")
    def distinct_product_anchors(self):
        if len(self.product_anchor_ids) != len(set(self.product_anchor_ids)):
            raise ValueError("视觉约定的商品特征引用不能重复")
        return self


class ShotContinuity(_DirectingModel):
    start_state: _Text = Field(description="镜头开始时主体的物理状态、位置或动作阶段；不要把机位或景别写进主体状态")
    allowed_changes: _Text = Field(description="本镜头允许发生的明确动作和状态变化；静止展示则明确不改变主体状态")
    end_state: _Text = Field(description="镜头结束时主体的物理状态，供下一连续镜头逐字继承")
    camera_move: _Text = Field(description="实际机位、景别或摄影机运动，与主体物理状态分开描述")
    entry_transition: Literal["opening", "continuous", "cut", "scene_change"]
    entry_reason: _Text = Field(description="这次开始或切换如何支持信息、情境或必要收束；不能用换场景掩盖商品状态跳变")


def storyboard_continuity_schema():
    """New wire permits one derived field; stored continuity stays complete.

    A model need not repeat the previous shot's state for continuous entries.
    Non-continuous entries still own and must supply their actual start state.
    """
    schema = ShotContinuity.model_json_schema()
    schema["required"].remove("start_state")
    schema["properties"]["start_state"]["description"] = (
        "continuous镜头可省略，由程序继承上一镜end_state，跨生成段同样继承；"
        "显式填写时仍须逐字一致。opening、cut、scene_change须完整填写主体起始状态。")
    schema["allOf"] = [{
        "if": {"properties": {"entry_transition": {"const": "continuous"}},
               "required": ["entry_transition"]},
        "else": {"required": ["start_state"]},
    }]
    return schema


def derive_continuous_start_states(raw):
    """Fill only omitted new-wire references, without changing authored data.

    The caller opts in only at the new storyboard boundary, before strict Plan
    validation. Missing predecessors, malformed states and explicit values are
    left for the canonical validators to reject. No history is rewritten and
    no semantic equivalence between two authored states is inferred.
    """
    result = deepcopy(raw)
    if not isinstance(result, dict) or not isinstance(result.get("segments"), list):
        return result
    previous = None
    for segment in result["segments"]:
        if not isinstance(segment, dict) or not isinstance(segment.get("shots"), list):
            previous = None
            continue
        for shot in segment["shots"]:
            current = shot.get("continuity") if isinstance(shot, dict) else None
            if not isinstance(current, dict):
                previous = None
                continue
            if (current.get("entry_transition") == "continuous" and "start_state" not in current
                    and previous is not None and isinstance(previous.get("end_state"), str)):
                current["start_state"] = previous["end_state"]
            previous = current
    return result


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _direction_and_anchors(plan):
    raw_direction = _get(plan, "visual_direction")
    if raw_direction is None:
        return None, []
    direction = VisualDirection.model_validate(raw_direction)
    strategy = _get(plan, "strategy")
    if strategy is None:
        raise ValueError("视觉约定需要本轮商品事实作为依据")
    facts = _get(_get(strategy, "brief"), "facts", [])
    by_id = {_get(fact, "id"): fact for fact in facts}
    if not set(direction.product_anchor_ids).issubset(by_id):
        raise ValueError("视觉约定只能引用本轮已有商品事实")
    anchors = [{key: _get(by_id[fact_id], key) for key in ("id", "text", "source_kind", "source_id")}
               for fact_id in direction.product_anchor_ids]
    return direction, anchors


def validate_directing_plan(plan, *, required=False):
    """Check ownership and structural continuity; semantic review stays separate."""
    direction, _ = _direction_and_anchors(plan)
    segments = _get(plan, "segments", [])
    shots = [shot for segment in segments for shot in _get(segment, "shots", [])]
    any_continuity = any(_get(shot, "continuity") is not None for shot in shots)
    if direction is None:
        if required or any_continuity:
            raise ValueError("新分镜需要统一视觉约定与逐镜头连续性，不能只提供其中一项")
        return
    if not shots or any(_get(shot, "continuity") is None for shot in shots):
        raise ValueError("视觉约定必须落实到全部镜头的连续性说明，包括静音镜头")
    for segment in segments:
        ids = [_get(shot, "id", "") for shot in _get(segment, "shots", [])]
        if any(not shot_id.strip() for shot_id in ids) or len(ids) != len(set(ids)):
            raise ValueError("导演约定的镜头ID须非空且在片段内唯一")
    script = _get(plan, "ad_script")
    beats = {_get(beat, "id"): beat for beat in _get(script, "beats", [])}
    seen_beats, previous = set(), None
    for index, shot in enumerate(shots):
        continuity = ShotContinuity.model_validate(_get(shot, "continuity"))
        if index == 0:
            if continuity.entry_transition != "opening":
                raise ValueError("全片第一镜必须标为opening")
        elif continuity.entry_transition == "opening":
            raise ValueError("只有全片第一镜可以标为opening，生成段重启不等于广告重新开场")
        elif continuity.entry_transition == "continuous" and continuity.start_state != previous.end_state:
            raise ValueError("连续镜头的开始状态必须逐字继承上一镜结束状态，机位变化另行描述")
        beat_id = _get(shot, "beat_id", "")
        if continuity.entry_transition == "scene_change":
            if (beat_id not in beats or beat_id in seen_beats
                    or _get(beats[beat_id], "boundary_before", "complete") != "scene_change"):
                raise ValueError("场景转换只能发生在已审脚本明确scene_change的节拍首镜")
        seen_beats.update(_get(shot, "beat_ids", None) or [beat_id])
        previous = continuity


def direction_context(plan):
    """Resolve referenced facts once; callers reuse this in image/video inputs.

    The result includes only the owned direction and explicitly cited facts.
    It is not user-facing analysis and never imports historical free-text claims.
    """
    direction, anchors = _direction_and_anchors(plan)
    if direction is None:
        return None
    return {**direction.model_dump(), "product_anchors": anchors}
