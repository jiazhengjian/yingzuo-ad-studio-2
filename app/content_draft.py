"""An experimental content-only first draft, never a deliverable timed plan."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .strategy import StrategyRecord


CONTENT_DRAFT_VERSION = "1"
_BeatId = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80)]
_FactId = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=160)]


class ContentBeat(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    id: _BeatId
    # A draft exceeding the requested 29 characters can still be coordinated
    # by the director, as in the existing progressive route. This bounded
    # internal draft is not a valid final AdScript or a relaxed output request.
    narration: str = Field(strict=True, max_length=500,
        description="实际说出的完整广告话语，含标点空白最多29字符；无话语时明确填空字符串，不写镜头描述或创作说明")
    visual_role: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)] = Field(
        description="这一拍实际需要看见的商品画面、动作或变化；与相邻内容相承接，不写目的解释、审核自评、镜头参数或秒数")
    evidence_ids: list[_FactId] = Field(max_length=12,
        description="仅列本拍实际采用的已有商品事实ID，可为空；不新增事实，不重复引用")

    @model_validator(mode="after")
    def valid_content(self):
        if self.narration and not self.narration.strip():
            raise ValueError("静音内容拍须明确使用空台词，不能只含空白")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("内容初稿每拍的商品事实ID不能重复")
        return self


class ContentDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    beats: list[ContentBeat] = Field(min_length=1, max_length=12,
        description="按先后顺序写实际内容，不预定镜头数量、时长和动作执行估计；后续分镜须据此编排并独立审核")

    @model_validator(mode="after")
    def unique_beats(self):
        if len({beat.id for beat in self.beats}) != len(self.beats):
            raise ValueError("内容初稿的节拍ID不能重复")
        return self


def validate_content_draft(raw: ContentDraft | dict, strategy: StrategyRecord | dict) -> ContentDraft:
    """Validate only authored draft data, without inventing timings or metadata."""
    result = ContentDraft.model_validate(raw)
    strategy = StrategyRecord.model_validate(strategy)
    known = {fact.id for fact in strategy.brief.facts}
    if any(fact_id not in known for beat in result.beats for fact_id in beat.evidence_ids):
        raise ValueError("内容初稿只能引用本轮已有商品事实")
    return result


def content_draft_schema(strategy: StrategyRecord | dict) -> dict:
    schema = ContentDraft.model_json_schema()
    strategy = StrategyRecord.model_validate(strategy)
    schema["$defs"]["ContentBeat"]["properties"]["narration"]["maxLength"] = 29
    schema["$defs"]["ContentBeat"]["properties"]["evidence_ids"]["items"] = {
        "type": "string", "enum": [fact.id for fact in strategy.brief.facts]}
    return schema


CONTENT_DRAFT_SYSTEM = """你写中文商品广告的内容初稿，只返回本次schema的ContentDraft JSON，执行stage_skills中的content_draft方法。
依据brief中的已确认事实、selected_direction及用户原话，逐拍写实际台词与需要看见的画面。两者共同兑现选中方向和商品特点，不把画面描述念成旁白，不以创作说明代替内容。商品事实、广告倾向、角色、声音形式与用户固定原句均不可改变。
只写beats中的id、narration、visual_role、evidence_ids；无台词的拍明确narration=""。evidence_ids仅引用已有事实，台词每拍含标点最多29字符，保留完整句意。既定无人声方向所有台词为空。
本步不分配镜头、秒数或动作估时，不写purpose、message、创作理由、审核结论、版本信息或成片保证。分镜阶段会把内容落成可执行镜头并统一编排，最终方案仍须完整独立审核。
"""
