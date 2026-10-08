"""Small, source-bound creative rules; no model, network, or app-state imports.

The checks prove source references and conservative rule prerequisites, not that
a merchant claim is true or that an image observation is semantically correct.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from .production import ProductionPlan, validate_production_sources
from .ad_form import AdForm

logger = logging.getLogger(__name__)
performance_logger = logging.getLogger("uvicorn.error.planner")


STRATEGY_VERSION = "1.3.0"
Category = Literal["food", "appliance", "general"]
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
HistoricalDirectionText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1500)]
HookType = Literal[
    "macro_detail", "silhouette", "signature_sound", "scale_shift",
    "awkward_moment", "situational_conflict", "arresting_line",
    "direct_question", "counterintuitive", "result_first",
]
BrandWorld = Literal["functional", "identity", "sensory"]
PLAYBOOK_HOOKS = {
    "brand_tvc": frozenset({"macro_detail", "silhouette", "signature_sound", "scale_shift"}),
    "scene_story": frozenset({"awkward_moment", "situational_conflict", "arresting_line"}),
    "presenter_pitch": frozenset({"direct_question", "counterintuitive", "result_first"}),
}
HOOK_IDS = (
    "macro_detail", "silhouette", "signature_sound", "scale_shift",
    "awkward_moment", "situational_conflict", "arresting_line",
    "direct_question", "counterintuitive", "result_first",
)
# First allowed neighbor wins. Each hook lists one step toward the other playbooks
# so a disallowed opening can move into the current ad form without a model call.
_HOOK_NEIGHBORS = {
    "macro_detail": ("direct_question", "result_first", "awkward_moment", "silhouette"),
    "silhouette": ("macro_detail", "result_first", "arresting_line", "scale_shift"),
    "signature_sound": ("arresting_line", "counterintuitive", "situational_conflict", "scale_shift"),
    "scale_shift": ("counterintuitive", "macro_detail", "situational_conflict", "silhouette"),
    "awkward_moment": ("direct_question", "situational_conflict", "macro_detail", "result_first"),
    "situational_conflict": ("counterintuitive", "awkward_moment", "scale_shift", "direct_question"),
    "arresting_line": ("result_first", "direct_question", "silhouette", "awkward_moment"),
    "direct_question": ("awkward_moment", "counterintuitive", "macro_detail", "result_first"),
    "counterintuitive": ("situational_conflict", "direct_question", "scale_shift", "result_first"),
    "result_first": ("arresting_line", "direct_question", "macro_detail", "counterintuitive"),
}
EMOTIONAL_RULES = frozenset({
    "exaggeration", "contrast_humor", "identity_attitude", "ritual_sensory", "situational_conflict",
})
# Consumer-facing direction prose is what later lines tend to copy. These are
# planner phrases seen in real drafts, not a ban on ordinary words like 看 or 用.
JARGON_PATTERNS = (
    r"满足你对", r"兼顾", r"纳入", r"高端质感", r"打造", r"呈现", r"体现",
    r"主打", r"受众", r"记忆点", r"观看期待", r"卖点", r"策划", r"关注",
)
# Same-length plain words. Longer phrases go first so a short word cannot
# eat part of a longer one. Internal notes and assumptions are not rewritten.
JARGON_REPLACEMENTS = (
    ("满足你对", "你想要的"),
    ("观看期待", "想看下去"),
    ("高端质感", "看着讲究"),
    ("记忆点", "记住的"),
    ("兼顾", "顾到"),
    ("纳入", "放入"),
    ("打造", "做出"),
    ("呈现", "看见"),
    ("体现", "显出"),
    ("主打", "重点"),
    ("受众", "顾客"),
    ("卖点", "特点"),
    ("策划", "想法"),
    ("关注", "在意"),
)
CONSUMER_FIELDS = (
    "label", "angle", "audience_value", "product_expression", "story_summary",
    "attraction_mechanism", "hook", "development", "payoff", "consumer_insight", "big_idea",
    "reason",
)
# Merchant-facing card lines. Storage still accepts older, longer text.
CARD_LIMITS = {
    "product_expression": 60,
    "consumer_insight": 40,
    "hook": 40,
    "payoff": 40,
    "reason": 40,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class BriefFact(_StrictModel):
    id: Identifier
    text: LongText = Field(description="merchant 时逐字摘录 evidence_sources[source_id] 的连续原文，不能添加‘用户要求：’‘用户说明：’等前缀；image 时只描述原图可见信息")
    source_kind: Literal["merchant", "image"]
    source_id: Identifier


class ImageObservationCorrection(_StrictModel):
    """A review may correct an existing image observation, never its source."""
    fact_id: Identifier
    text: LongText = Field(description="纠正后对原图可辨认外观的描述；仅修正既有image事实的观察文本，不新增材质、机理、参数或功效推断")


ProductFilmStructure = Literal["product_visual", "world_intercut", "lifestyle"]
FilmStructure = Literal["product_visual", "world_intercut", "lifestyle", "scene_action", "presenter_explanation"]
VisualSkeleton = ProductFilmStructure
_VISUAL_SKELETONS = {"product_visual", "world_intercut", "lifestyle"}
# Auto may include the lifestyle path: life problem, product world, real result.
# 不出镜 is a carrier lock, not a reason to delete that path from the set.
_BRAND_AUTO_SKELETONS = {"product_visual", "world_intercut", "lifestyle"}
_SIDE_PROOF_SPEC = re.compile(r"\d|[０-９]|价格|价钱|折扣|规格|参数")


class CreativeIntent(_StrictModel):
    """Separate the merchant's creative requirements from product-use context."""
    use_context: HistoricalDirectionText = Field(description="商品在哪里、由谁使用；不自动要求广告在这里拍")
    setting_requirement: HistoricalDirectionText = Field(description="仅记录用户明确要求的成片地点；未指定则空，不继承旧AI稿的场所")
    reference_intent: HistoricalDirectionText = Field(description="用户明确要求的视觉表现或实际取得的参考特点；未取得的参考不得声称已看过，未指定则空")
    structure_preference: Literal["auto", "product_visual", "world_intercut", "lifestyle"] = Field(
        description="仅用户明确要求或参考明确限定结构时选对应值，否则auto；受众/使用环境不构成结构限制")


PITCH_LINE = 40


class FilmConcept(_StrictModel):
    """Structure plus the middle beat. Memory and sound are filled in the script."""
    structure: FilmStructure = Field(description="影片如何组织：产品视觉呈现/产品与场景交替/生活方式/剧情行动/人物讲解；不是三个候选必须各选一种")
    visual_skeleton: VisualSkeleton | None = Field(default=None, exclude_if=lambda value: value is None,
        description="导演层怎么讲画面：product_visual拆解、world_intercut穿梭、lifestyle生活。商家不选。新的三个方向必填")
    visual_motif: Annotated[str, StringConstraints(strip_whitespace=True, max_length=16)] = Field(
        default="", exclude_if=lambda value: not value,
        description="贯穿母题，只写一个元素，不超过16字")
    side_proof: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] = Field(
        default="", exclude_if=lambda value: not value,
        description="侧面证明，不超过40字。大白话，用场景、过程或概念层比喻让人看见好处。不写规格、数字或照片说明")
    key_visual: LongText = Field(description="中段，不超过40字。大白话写这一拍：剧情是转折，讲解是演示，产品片是产品高光。不是运镜清单，也不是摄影散文")
    memory_anchor: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写；脚本从核心画面和收尾推导观众留下的印象")
    sound_role: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写；脚本按 production 的声音形式和收尾来写声音")


class ProductBrief(_StrictModel):
    ad_form: AdForm | None = Field(default=None, exclude_if=lambda value: value is None,
        description="服务端继承用户的广告倾向，不是商品事实或制作能力承诺，模型不得更改")
    product_name: ShortText
    category: Category
    audience: str = Field(default="", max_length=500)
    scenario: str = Field(default="", max_length=500)
    creative_intent: CreativeIntent | None = Field(default=None, exclude_if=lambda value: value is None,
        description="本轮用途环境、成片地点要求与参考意图；新提案必须明确，历史缺失不推断")
    facts: list[BriefFact] = Field(default_factory=list, max_length=12)
    unknowns: list[ShortText] = Field(default_factory=list, max_length=30)
    assumptions: list[ShortText] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_fact_ids(self):
        _require_unique([fact.id for fact in self.facts], "商品事实 ID")
        return self


class CreativeDirection(_StrictModel):
    film_concept: FilmConcept | None = Field(default=None, exclude_if=lambda value: value is None,
        description="影片结构与中段动作；新提案必需，历史缺失不补写")
    production: ProductionPlan | None = Field(default=None, exclude_if=lambda value: value is None,
        description="本方向的商品形态与动作约束、出镜角色、声音形式；新创作必须明确，历史缺失不推断")
    id: Identifier
    rule_id: Identifier
    label: ShortText = Field(description="商家扫一眼就能选的短标题，2到8个字，像影棚特写、早上赶出门、工艺感。写这条和另外两条的差别，不写画面散文")
    audience: ShortText
    scenario: ShortText
    angle: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写主打说明；卖点由核心创意、中段和商品事实交给脚本展开")
    audience_value: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写；脚本从已选方向和简报说明受众为何在意")
    product_expression: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="核心创意，不超过60字。用大白话写出这条和另外两条差在哪。只绑已有事实，不写未确认功效，不写摄影散文")
    # Historical plans/gates predate these fields. Only newly authored offers
    # must provide them; reading or selecting a saved direction never invents
    # a synopsis from its old technical notes.
    story_summary: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写创意展开；脚本和分镜从核心创意、0–3秒、中段和结尾展开")
    attraction_mechanism: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写观看理由；脚本从0–3秒、结尾和为什么吸引人组织观看过程")
    consumer_insight: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="洞察，不超过40字。大白话写观众为什么在意。不写他们正在做什么，不写摄影散文。这是创意假设，不是调研")
    big_idea: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写。脚本优先用核心创意 product_expression")
    hook_type: HookType | None = Field(default=None, exclude_if=lambda value: value is None,
        description="具名开场钩子，必须属于当前广告类型 playbook 允许的那一组")
    brand_world: BrandWorld | None = Field(default=None, exclude_if=lambda value: value is None,
        description="概念层的品牌世界路径：functional 功能环境、identity 身份态度、sensory 感官仪式。不是商品事实")
    hook: LongText = Field(description="0到3秒，不超过40字。大白话写这一下和另外两条有什么不同。不是钩子编号的翻译，也不是镜头散文")
    development: HistoricalDirectionText = Field(default="", exclude_if=lambda value: not value,
        description="历史字段。新方向不写这段展开；中段写在 key_visual")
    payoff: LongText = Field(description="结尾，不超过40字。大白话写这一下落在哪。不写摄影散文")
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=12)
    reason: LongText = Field(description="为什么吸引人，不超过40字。大白话写为什么会停下来看。不承诺转化、销量或成片效果，不写摄影散文")
    production_notes: list[ShortText] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_evidence(self):
        _require_unique(self.evidence_ids, "方向证据 ID")
        return self


class ConceptSketch(_StrictModel):
    """Six id-only sketches so the program can keep three different directions.

    The merchant-facing sentence lives on the direction, not on every sketch.
    evidence_ids only name which facts the later pitch may use.
    """
    id: Identifier
    rule_id: Identifier
    hook_type: HookType
    brand_world: BrandWorld
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def unique_evidence(self):
        _require_unique(self.evidence_ids, "短概念证据 ID")
        return self


class AppliedRule(_StrictModel):
    id: Identifier
    version: Identifier
    sources: list[ShortText] = Field(min_length=1, max_length=10)


def require_direction_stories(directions: list[CreativeDirection]) -> None:
    """Check new-offer completeness, not the semantic quality of its creative."""
    for direction in directions:
        if not direction.story_summary or not direction.attraction_mechanism:
            raise ValueError(f"方向 {direction.id} 必须提供故事概要和吸引观众的方式")


def require_direction_value(directions: list[CreativeDirection]) -> None:
    """Require the authored value chain only on new offers or its revisions.

    Presence is a wire contract. Whether the claim is supported and the
    execution conveys it is the independent review's semantic responsibility.
    Saved pre-stage-two offers deliberately remain readable and selectable.
    """
    for direction in directions:
        if not direction.audience_value or not direction.product_expression:
            raise ValueError(f"方向 {direction.id} 必须说明受众为何在意及商品价值如何体现")


def direction_visuals_authored(directions: list[CreativeDirection]) -> bool:
    """True when every direction already carries a director-layer skeleton."""
    return bool(directions) and all(
        direction.film_concept is not None and direction.film_concept.visual_skeleton
        for direction in directions)


def require_direction_concepts(directions: list[CreativeDirection], brief: ProductBrief | None = None,
                               *, require_visuals: bool = False) -> None:
    """Check authored structure, not whether the prose realizes its intent.

    Call only for newly authored offers/reviews or retention of an existing
    concept. Legacy records remain readable and selectable without migration.
    Semantic agreement with original user sources belongs to the review.
    require_visuals is for a fresh three-direction offer, or a review of one
    that already wrote the skeleton. Older concepts stay readable without it.
    """
    if brief is not None and brief.creative_intent is None:
        raise ValueError("新方向必须明确用途场景、成片地点要求和参考意图")
    for direction in directions:
        concept = direction.film_concept
        if concept is None:
            raise ValueError(f"方向 {direction.id} 必须提供影片结构与核心画面")
        if brief is None:
            continue
        preference = brief.creative_intent.structure_preference
        if preference != "auto" and concept.structure != preference:
            raise ValueError(f"方向 {direction.id} 的影片结构须符合明确的参考意图")
        structures = {
            "brand_tvc": {"product_visual", "world_intercut", "lifestyle"},
            "scene_story": {"scene_action", "world_intercut", "lifestyle"},
            "presenter_pitch": {"presenter_explanation"},
        }.get(brief.ad_form)
        if structures is not None and concept.structure not in structures:
            raise ValueError(f"方向 {direction.id} 的影片结构与主要广告表达方式不一致")
    if require_visuals and brief is not None and len(directions) == 3:
        _require_visual_contract(directions, brief)


_STORY_FAMILIES = (
    re.compile(r"吵醒|叫醒|弄醒|还在睡|别吵|室友|出声"),
    re.compile(r"送礼|礼物|拆穿"),
    re.compile(r"赶出门|迟到|闹钟"),
    re.compile(r"扣子|卡住|按不开"),
)


def _story_text(direction: CreativeDirection) -> str:
    return f"{direction.label}{direction.hook}"


def _story_bigrams(text: str) -> set[str]:
    core = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    return {core[index:index + 2] for index in range(len(core) - 1)}


def _story_cores_collide(directions: list[CreativeDirection]) -> bool:
    """True when two scene directions are the same situation in different words."""
    texts = [_story_text(direction) for direction in directions]
    for family in _STORY_FAMILIES:
        if sum(bool(family.search(text)) for text in texts) >= 2:
            return True
    grams = [_story_bigrams(text) for text in texts]
    for left in range(len(grams)):
        for right in range(left + 1, len(grams)):
            if not grams[left] or not grams[right]:
                continue
            shared = len(grams[left] & grams[right]) / len(grams[left] | grams[right])
            if shared >= 0.45:
                return True
    return False


def _require_visual_contract(directions: list[CreativeDirection], brief: ProductBrief) -> None:
    """Director-layer skeleton, one motif, and one side proof. Merchants do not pick these."""
    concepts = []
    for direction in directions:
        concept = direction.film_concept
        motif = (concept.visual_motif or "").strip()
        proof = (concept.side_proof or "").strip()
        if concept.visual_skeleton not in _VISUAL_SKELETONS or not motif or not proof:
            raise ValueError(f"方向 {direction.id} 必须写怎么讲画面、贯穿母题和侧面证明")
        if _SIDE_PROOF_SPEC.search(proof):
            raise ValueError(f"方向 {direction.id} 的侧面证明不写规格、价格或数字")
        concepts.append(concept)
    form = brief.ad_form
    preference = brief.creative_intent.structure_preference
    if form == "presenter_pitch":
        if any(concept.visual_skeleton != "lifestyle" for concept in concepts):
            raise ValueError("人物讲解用生活画面讲，不用拆解或穿梭")
    elif form == "brand_tvc":
        if any(concept.visual_skeleton != concept.structure for concept in concepts):
            raise ValueError("产品片的画面骨架要和这条的结构一致")
        if preference == "auto":
            if any(concept.structure not in _BRAND_AUTO_SKELETONS for concept in concepts):
                raise ValueError("产品片没有锁定结构时，用拆解、穿梭或生活来讲")
            if len({concept.structure for concept in concepts}) < 2:
                raise ValueError("产品片的三个方向不能用同一种画面结构")
    elif form == "scene_story":
        if any(concept.visual_skeleton == "product_visual" for concept in concepts):
            raise ValueError("剧情不用拆解来讲画面")
        if sum(concept.visual_skeleton == "world_intercut" for concept in concepts) > 1:
            raise ValueError("剧情三个方向里最多一个用轻穿梭")
    signatures = [
        (concept.visual_skeleton, (concept.visual_motif or "").strip(), (concept.side_proof or "").strip())
        for concept in concepts
    ]
    if len(set(signatures)) < len(signatures):
        raise ValueError("三个方向要在画面结构、贯穿母题或侧面证明上彼此不同")
    if form == "scene_story" and _story_cores_collide(directions):
        raise ValueError("剧情三个方向的故事核要分开")


class StrategyRecord(_StrictModel):
    version: Identifier = STRATEGY_VERSION
    selection_mode: Literal["legacy", "merchant_choice"] = Field(default="legacy", exclude_if=lambda value: value == "legacy")
    brief: ProductBrief
    directions: list[CreativeDirection] = Field(min_length=1, max_length=3)
    selected_direction_id: Identifier
    rules_used: list[AppliedRule] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def references_are_consistent(self):
        _validate_directions(self.brief, self.directions, self.selected_direction_id,
                             distinct_rules=self.selection_mode == "legacy")
        _require_unique([rule.id for rule in self.rules_used], "应用规则 ID")
        expected = {direction.rule_id for direction in self.directions}
        if {rule.id for rule in self.rules_used} != expected:
            raise ValueError("rules_used 必须且只能记录方向实际使用的规则")
        return self


class DirectionReviewCheck(_StrictModel):
    direction_id: Identifier
    physical_plausibility_passed: bool = Field(strict=True)
    product_role_passed: bool = Field(strict=True,
        description="本候选是否独立回应原始要求中必须体现的内容，并让商品价值在实际画面或声音中成立；可选卖点不必全讲，不能靠其他候选补齐遗漏")
    attraction_passed: bool = Field(strict=True)
    claim_support_passed: bool | None = Field(default=None, strict=True, exclude_if=lambda value: value is None,
        description="主打特点、受众价值及具体表现是否由引用事实支持，未将未知效果、假设或演绎当证明")
    expression_form_passed: bool | None = Field(default=None, strict=True, exclude_if=lambda value: value is None,
        description="实际创意与制作选择是否兑现用户选定主要表达方式，而非只在标签上改类型")
    film_concept_passed: bool | None = Field(default=None, strict=True, exclude_if=lambda value: value is None,
        description="对照原始要求核对用途/片场/参考意图区分、影片结构与实际核心画面、卖点和记忆点相符；不能仅看标签")
    creative_tension: Literal["low", "medium", "high"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="创意张力：观众处境是否让人想看下去。只记录，不决定 ready")
    hook_strength: Literal["low", "medium", "high"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="开场钩子是否具体、属于本类型，并且后面有兑现。只记录，不决定 ready")
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)] = Field(
        description="针对修订后实际内容的简短可核对结论：引用事实、产品价值如何体现、主要表达方式及必要修正；不输出思考过程")


class DirectionReviewSummary(_StrictModel):
    """Observable conclusions only; independent review is not a quality guarantee."""
    ready: bool = Field(strict=True)
    distinct_directions_passed: bool = Field(strict=True)
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=800)]
    checks: list[DirectionReviewCheck] = Field(min_length=3, max_length=3)
    image_observation_corrections: list[ImageObservationCorrection] = Field(
        default_factory=list, max_length=12, exclude_if=lambda value: not value,
        description="本次依据原图纠正的已有image观察；不需要则空数组，不改merchant原文、事实ID、来源或其他brief要求")

    @model_validator(mode="after")
    def consistent_verdict(self):
        _require_unique([check.direction_id for check in self.checks], "方向审核 ID")
        passed = self.distinct_directions_passed and all(
            check.physical_plausibility_passed and check.product_role_passed and check.attraction_passed
            and check.claim_support_passed is not False and check.expression_form_passed is not False
            and check.film_concept_passed is not False
            for check in self.checks)
        if self.ready != passed:
            raise ValueError("方向审核 ready 必须与全部逐项检查和方向差异检查一致")
        return self


class DirectionCandidates(_StrictModel):
    """An unselected, source-checked offer. No scripts or media are created here."""
    version: Identifier = STRATEGY_VERSION
    brief: ProductBrief
    directions: list[CreativeDirection] = Field(min_length=3, max_length=3)
    recommended_direction_id: Identifier | None = None
    rules_used: list[AppliedRule] = Field(min_length=1, max_length=3)
    direction_review: DirectionReviewSummary | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def candidate_shape(self):
        _validate_directions(self.brief, self.directions, None, distinct_rules=False)
        if self.recommended_direction_id is not None and self.recommended_direction_id not in {d.id for d in self.directions}:
            raise ValueError("推荐方向必须来自本组候选，推荐不代表选择")
        _require_unique([d.label for d in self.directions], "方向名称")
        _require_unique([r.id for r in self.rules_used], "应用规则 ID")
        if {r.id for r in self.rules_used} != {d.rule_id for d in self.directions}:
            raise ValueError("候选规则记录与实际方向不一致")
        if self.direction_review is not None:
            if not self.direction_review.ready:
                raise ValueError("未通过审核的方向不能作为新候选交付")
            if {check.direction_id for check in self.direction_review.checks} != {d.id for d in self.directions}:
                raise ValueError("候选审核结论须恰好覆盖三个方向")
        return self

    def select(self, direction_id: str) -> StrategyRecord:
        if direction_id not in {d.id for d in self.directions}:
            raise ValueError("所选方向不在这组候选中")
        return StrategyRecord(brief=self.brief, directions=self.directions,
            selected_direction_id=direction_id, rules_used=self.rules_used,
            selection_mode="merchant_choice")


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label}不能重复")


@lru_cache(maxsize=1)
def _catalog() -> dict:
    # This is application-owned data, never model output or a user-supplied path.
    catalog = json.loads(Path(__file__).with_name("strategy_rules.json").read_text(encoding="utf-8"))
    if catalog["version"] != STRATEGY_VERSION:
        raise ValueError("策略规则库版本不匹配")
    _require_unique([rule["id"] for rule in catalog["rules"]], "规则库 ID")
    for rule in catalog["rules"]:
        rule["production_constraints"] = list(catalog["production_constraints"])
        for pattern in rule["prerequisites"]["merchant_patterns_all"]:
            re.compile(pattern)
    for pattern in catalog["uncertainty_patterns"]:
        re.compile(pattern)
    for pattern in catalog["citation_qualifier_patterns"]:
        re.compile(pattern)
    return catalog


def _rules_by_id() -> dict[str, dict]:
    return {rule["id"]: rule for rule in _catalog()["rules"]}


def _merchant_support(facts: list[BriefFact], prerequisites: dict) -> bool:
    """A conservative textual gate, not a semantic verifier of an ad claim."""
    patterns = prerequisites["merchant_patterns_all"]
    require_merchant = prerequisites.get("require_merchant", False)
    if not patterns and not require_merchant:
        return True
    uncertainty = _catalog()["uncertainty_patterns"]
    if require_merchant:
        # A named feature need not contain an operation verb or a number, but
        # image observations and explicitly qualified assertions are not enough.
        # Preserve the existing operation/comparison gates unchanged.
        uncertainty = uncertainty + _catalog()["citation_qualifier_patterns"]
    # All conditions must occur in one cited assertion, not fragments spliced
    # across unrelated statements. Unknowns and assumptions are never searched.
    return any(
        fact.source_kind == "merchant"
        and not any(re.search(pattern, fact.text, re.IGNORECASE) for pattern in uncertainty)
        and all(re.search(pattern, fact.text, re.IGNORECASE) for pattern in patterns)
        for fact in facts
    )


def select_rules(brief: ProductBrief | None) -> list[dict]:
    """Return the full discovery catalog, or 3–4 category-relevant rules.

    Retrieval does not authorize a claim. Some category rules have prerequisites
    which build_strategy checks against that direction's cited facts.
    The gateway and historical-offer rebuild use discovery, retaining every
    rule. Bounded category suggestions prefer supported specific methods over
    inapplicable conditional methods, then over broad visual/context fallbacks.
    """
    catalog = _catalog()
    if brief is None:
        return deepcopy(catalog["rules"])
    brief = ProductBrief.model_validate(brief)
    by_id = _rules_by_id()
    ids = list(catalog["category_selection"][brief.category])
    feature = by_id["feature_value"]
    if _merchant_support(brief.facts, feature["prerequisites"]):
        ids.append("feature_value")
    comparison = by_id["evidence_comparison"]
    if _merchant_support(brief.facts, comparison["prerequisites"]):
        ids.append("evidence_comparison")
    # Unusable specific methods lose priority first. If several methods have
    # real support, keep them instead of spending the same four slots on both
    # general-purpose fallback methods. Do not weaken their prerequisites.
    removable = [rule_id for rule_id in reversed(ids)
                 if not _merchant_support(brief.facts, by_id[rule_id]["prerequisites"])]
    removable += ["everyday_context", "appearance_reveal", "sensory_detail"]
    for rule_id in removable:
        if len(ids) <= 4:
            break
        if rule_id in ids:
            ids.remove(rule_id)
    return deepcopy([by_id[rule_id] for rule_id in ids])


class BriefEvidenceError(ValueError):
    """Complete citation feedback for a bounded repair, not a public log record.

    ``issues`` may contain the caller's authoritative source text. Only the
    fixed validation messages belong in str(error); identifiers and excerpts
    must not be interpolated into a general error/log message.
    """

    def __init__(self, issues: list[dict]):
        self.issues = deepcopy(issues)
        super().__init__("；".join(dict.fromkeys(issue["message"] for issue in issues)))


def validate_brief(
    brief: ProductBrief,
    evidence_sources: dict[str, str],
    asset_ids: set[str],
) -> ProductBrief:
    """Check exact merchant citations and original-image membership.

    The caller must supply only actual user messages in evidence_sources and
    only this run's original (non-generated) product image IDs in asset_ids.
    Image membership does not establish that the observation is correct.
    """
    brief = ProductBrief.model_validate(brief)
    merchant_sources = {key: value for key, value in evidence_sources.items()
                        if isinstance(value, str)}
    issues = []
    for index, fact in enumerate(brief.facts):
        if fact.source_kind == "merchant":
            source = merchant_sources.get(fact.source_id)
            matching_ids = [key for key, value in merchant_sources.items() if fact.text in value]
            if not isinstance(source, str):
                issues.append({"loc": ["brief", "facts", index, "source_id"],
                    "type": "merchant_source_missing", "message": "事实引用了不存在的商家来源",
                    "allowed_source_ids": list(merchant_sources), "matching_source_ids": matching_ids})
                # A correct quote in another source is a repair hint only. It
                # never authorizes guessing or changing the cited source.
                if not matching_ids:
                    issues.append({"loc": ["brief", "facts", index, "text"],
                        "type": "merchant_quote_mismatch",
                        "message": "事实必须是商家来源原文子串，不能改写或补充",
                        "allowed_source_ids": list(merchant_sources), "matching_source_ids": []})
                continue
            if fact.text not in source:
                issues.append({"loc": ["brief", "facts", index, "text"],
                    "type": "merchant_quote_mismatch",
                    "message": "事实必须是商家来源原文子串，不能改写或补充",
                    "source_text": source, "matching_source_ids": matching_ids})
            elif not _has_unstripped_context(fact.text, source):
                issues.append({"loc": ["brief", "facts", index, "text"],
                    "type": "merchant_qualifier_missing",
                    "message": "事实摘录省略了原句的否定或不确定限定，不能反转原意",
                    "source_text": source})
        elif fact.source_id not in asset_ids:
            issues.append({"loc": ["brief", "facts", index, "source_id"],
                "type": "image_source_missing", "message": "事实引用了不存在的商品原图",
                "allowed_asset_ids": sorted(asset_ids)})
    if issues:
        raise BriefEvidenceError(issues)
    return brief


def _has_unstripped_context(excerpt: str, source: str) -> bool:
    """Reject dropping explicit qualifiers from an excerpt's containing sentence.

    Inspect every occurrence: a repeated phrase can also have a separately
    affirmative source sentence. This is deliberately a conservative lexical
    guard; it does not resolve pronouns, contradictions, sarcasm or implication.
    """
    boundaries = list(re.finditer(r"[。！？!?；;\n\r]|(?<!\d)\.(?!\d)", source))
    patterns = _catalog()["citation_qualifier_patterns"]
    for occurrence in re.finditer(re.escape(excerpt), source):
        start = max((match.end() for match in boundaries if match.end() <= occurrence.start()), default=0)
        end = min((match.end() for match in boundaries if match.end() >= occurrence.end()), default=len(source))
        sentence = source[start:end]
        qualifiers = [match.group(0) for pattern in patterns
                      for match in re.finditer(pattern, sentence, re.IGNORECASE)]
        if all(qualifier.casefold() in excerpt.casefold() for qualifier in qualifiers):
            return True
    return False


def _normal_form(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()


def _validate_directions(
    brief: ProductBrief,
    directions: list[CreativeDirection],
    selected_id: str | None,
    *, distinct_rules: bool = True,
) -> None:
    _require_unique([direction.id for direction in directions], "创意方向 ID")
    if distinct_rules:
        _require_unique([direction.rule_id for direction in directions], "创意机制 rule_id")
    if selected_id is not None and selected_id not in {direction.id for direction in directions}:
        raise ValueError("所选创意方向不存在")
    # A merchant may compare three executions of the same supported value.
    # Requiring three different claims encourages synonyms or invented benefits.
    # Preserve the older rule-based contract, but judge new concept diversity
    # through execution and the independent semantic review.
    if distinct_rules:
        _require_unique([_normal_form(direction.angle) for direction in directions], "创意角度")
    _require_unique(
        [_normal_form(direction.development + direction.payoff) for direction in directions],
        "展开与收束内容",
    )
    facts = {fact.id: fact for fact in brief.facts}
    for direction in directions:
        validate_production_sources(direction.production, brief.facts)
        if any(fact_id not in facts for fact_id in direction.evidence_ids):
            raise ValueError(f"方向 {direction.id} 引用了不存在的商品事实")


def _validate_rule_prerequisites(brief: ProductBrief, directions: list[CreativeDirection]) -> None:
    # Current rules apply to new planning only. A stored StrategyRecord remains
    # readable after catalog upgrades, with its own version and provenance.
    facts = {fact.id: fact for fact in brief.facts}
    rules = _rules_by_id()
    for direction in directions:
        rule = rules.get(direction.rule_id)
        if rule is None:
            raise ValueError(f"未知策略规则：{direction.rule_id}")
        if brief.category not in rule["categories"]:
            raise ValueError(f"规则 {direction.rule_id} 不适用于该商品品类")
        cited = [facts[fact_id] for fact_id in direction.evidence_ids]
        if len(cited) < rule["prerequisites"]["min_facts"]:
            raise ValueError(f"规则 {direction.rule_id} 缺少商品事实依据")
        if not _merchant_support(cited, rule["prerequisites"]):
            raise ValueError(f"规则 {direction.rule_id} 缺少前提：{rule['prerequisites']['description']}")


def build_strategy(
    brief: ProductBrief,
    directions: list[CreativeDirection],
    selected_id: str,
    rules: list[dict],
    *, merchant_choice: bool = False,
) -> StrategyRecord:
    """Freeze a validated brief, distinct concepts, and exact rule provenance.

    validate_brief must first be called with this run's authoritative sources;
    this function deliberately has no access to user messages or project state.
    """
    brief = ProductBrief.model_validate(brief)
    directions = [CreativeDirection.model_validate(value) for value in directions]
    canonical = _rules_by_id()
    allowed: dict[str, dict] = {}
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("id") not in canonical:
            raise ValueError("传入了未知策略规则")
        rule_id = rule["id"]
        if rule_id in allowed:
            raise ValueError("传入规则不能重复")
        if rule != canonical[rule_id]:
            raise ValueError("传入规则与本地规则库不一致，不能修改来源或前提")
        allowed[rule_id] = rule
    if any(direction.rule_id not in allowed for direction in directions):
        raise ValueError("创意方向引用了本轮未检索的规则")
    _validate_directions(brief, directions, selected_id, distinct_rules=not merchant_choice)
    _validate_rule_prerequisites(brief, directions)
    used_ids = list(dict.fromkeys(direction.rule_id for direction in directions))
    return StrategyRecord(
        brief=brief.model_copy(deep=True),
        directions=[direction.model_copy(deep=True) for direction in directions],
        selected_direction_id=selected_id,
        selection_mode="merchant_choice" if merchant_choice else "legacy",
        rules_used=[AppliedRule(id=rule_id, version=allowed[rule_id]["version"],
                                sources=list(allowed[rule_id]["sources"])) for rule_id in used_ids],
    )


def allowed_hooks(ad_form: str | None, structure: str | None = None) -> frozenset[str]:
    """Playbook openings win. Auto follows the direction's own structure."""
    if ad_form in PLAYBOOK_HOOKS:
        return PLAYBOOK_HOOKS[ad_form]
    if structure == "scene_action":
        return PLAYBOOK_HOOKS["scene_story"]
    if structure == "presenter_explanation":
        return PLAYBOOK_HOOKS["presenter_pitch"]
    if structure == "lifestyle":
        return PLAYBOOK_HOOKS["brand_tvc"] | PLAYBOOK_HOOKS["scene_story"]
    if structure in {"product_visual", "world_intercut"}:
        return PLAYBOOK_HOOKS["brand_tvc"]
    return frozenset().union(*PLAYBOOK_HOOKS.values())


def playbook_hook_ids(ad_form: str | None) -> list[str]:
    """Stable hook ids the current ad form may emit. Unknown forms keep the full set."""
    if ad_form in PLAYBOOK_HOOKS:
        allowed = PLAYBOOK_HOOKS[ad_form]
        return [hook for hook in HOOK_IDS if hook in allowed]
    return list(HOOK_IDS)


def nearest_allowed_hook(hook: str, allowed: frozenset[str]) -> str | None:
    """Map a disallowed opening onto the current ad form, or leave it unusable."""
    if hook in allowed:
        return hook
    for candidate in _HOOK_NEIGHBORS.get(hook, ()):
        if candidate in allowed:
            return candidate
    return None


def consumer_jargon(text: str) -> str | None:
    """Return the first planner phrase in consumer-facing copy, if any."""
    for pattern in JARGON_PATTERNS:
        found = re.search(pattern, text)
        if found:
            return found.group(0)
    return None


def plain_consumer_wording(text: str) -> str:
    """Replace planner phrases with plain words. Drop a sentence that still hits.

    Replacements stay the same length so a 30- or 40-character pitch line does
    not grow past the card limit. Notes, summaries and assumptions are not
    passed here.
    """
    updated = text
    for _ in range(len(JARGON_REPLACEMENTS) + 1):
        previous = updated
        for source, replacement in JARGON_REPLACEMENTS:
            updated = re.sub(source, replacement, updated)
        if updated == previous or consumer_jargon(updated) is None:
            break
    if consumer_jargon(updated):
        pieces = re.split(r"(?<=[。！？!?；;])", updated)
        updated = "".join(piece for piece in pieces if piece and consumer_jargon(piece) is None).strip()
    while consumer_jargon(updated):
        updated = updated.replace(consumer_jargon(updated), "", 1).strip()
    return updated


def sanitize_direction_copy(direction: CreativeDirection) -> CreativeDirection:
    """Rewrite planner phrases on the lines a merchant or a later script can see."""
    updates = {}
    for field in CONSUMER_FIELDS:
        text = getattr(direction, field) or ""
        if not text:
            continue
        plain = plain_consumer_wording(text)
        if plain and plain != text and consumer_jargon(plain) is None:
            updates[field] = plain
    concept = direction.film_concept
    if concept is not None:
        concept_updates = {}
        for field in ("key_visual", "visual_motif", "side_proof", "memory_anchor", "sound_role"):
            text = getattr(concept, field) or ""
            if not text:
                continue
            plain = plain_consumer_wording(text)
            if plain and plain != text and consumer_jargon(plain) is None:
                concept_updates[field] = plain
        if concept_updates:
            updates["film_concept"] = concept.model_copy(update=concept_updates)
    if not updates:
        return direction
    return direction.model_copy(update=updates)


def clip_pitch(text: str, limit: int) -> str:
    """Shorten one card line at a sentence boundary. Empty stays empty."""
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    window = cleaned[:limit]
    for mark in ("。", "！", "？", "；", "，"):
        at = window.rfind(mark)
        if at >= max(8, limit // 3):
            return window[:at + 1].strip()
    return window.strip()


def clamp_direction_card(direction: CreativeDirection) -> CreativeDirection:
    """Fit merchant-facing lines to the card. A long saved line is shortened, not rejected."""
    updates = {}
    for field, limit in CARD_LIMITS.items():
        value = getattr(direction, field) or ""
        clipped = clip_pitch(value, limit)
        if clipped and clipped != value:
            updates[field] = clipped
    concept = direction.film_concept
    if concept is not None and concept.key_visual:
        clipped = clip_pitch(concept.key_visual, PITCH_LINE)
        if clipped and clipped != concept.key_visual:
            updates["film_concept"] = concept.model_copy(update={"key_visual": clipped})
    if not updates:
        return direction
    return direction.model_copy(update=updates)


def prepare_direction_card(direction: CreativeDirection) -> CreativeDirection:
    """Replace planner words, then fit the card. The same object comes back when nothing changed."""
    return clamp_direction_card(sanitize_direction_copy(direction))


def require_consumer_language(directions: list[CreativeDirection]) -> None:
    """Reject planner phrasing in the copy later lines tend to repeat."""
    for direction in directions:
        for field, text in _consumer_texts(direction):
            phrase = consumer_jargon(text)
            if phrase:
                raise ValueError(f"方向 {direction.id} 的{field}要用观众听得懂的话，不要写「{phrase}」")


def _consumer_texts(direction: CreativeDirection) -> list[tuple[str, str]]:
    texts = [(field, getattr(direction, field) or "") for field in CONSUMER_FIELDS]
    concept = direction.film_concept
    if concept is not None:
        texts.extend((field, getattr(concept, field) or "") for field in (
            "key_visual", "visual_motif", "side_proof", "memory_anchor", "sound_role"))
    return [(field, text) for field, text in texts if text]


def require_pitch_lines(directions: list[CreativeDirection]) -> None:
    """Card lines that are still too long were not clamped. Short historical lines stay."""
    for direction in directions:
        if len(direction.hook) > PITCH_LINE or len(direction.payoff) > PITCH_LINE:
            raise ValueError(f"方向 {direction.id} 的开头和收尾各不超过{PITCH_LINE}字")
        if len(direction.reason) > PITCH_LINE:
            raise ValueError(f"方向 {direction.id} 的为什么吸引人不超过{PITCH_LINE}字")
        if len(direction.consumer_insight) > PITCH_LINE:
            raise ValueError(f"方向 {direction.id} 的洞察不超过{PITCH_LINE}字")
        if len(direction.product_expression) > 60:
            raise ValueError(f"方向 {direction.id} 的核心创意不超过60字")
        concept = direction.film_concept
        if concept is not None and len(concept.key_visual) > PITCH_LINE:
            raise ValueError(f"方向 {direction.id} 的中段不超过{PITCH_LINE}字")


def require_ad_concepts(directions: list[CreativeDirection], brief: ProductBrief | None = None) -> None:
    """New offers need a playbook hook. Missing card lines do not fail the job.

    Historical directions omit these fields and stay readable. Length is
    clamped before this runs. This does not judge whether the idea is good.
    """
    for direction in directions:
        if direction.hook_type is None or direction.brand_world is None:
            raise ValueError(f"方向 {direction.id} 必须写开场钩子和品牌世界路径")
        structure = direction.film_concept.structure if direction.film_concept is not None else None
        form = brief.ad_form if brief is not None else None
        if direction.hook_type not in allowed_hooks(form, structure):
            raise ValueError(f"方向 {direction.id} 的开场钩子不属于当前广告类型")
    require_consumer_language(directions)
    require_pitch_lines(directions)


def _validate_sketch(brief: ProductBrief, sketch: ConceptSketch) -> None:
    issue = _sketch_issue(brief, sketch)
    if issue is not None:
        raise ValueError(_sketch_issue_message(sketch, issue))


def _sketch_issue(brief: ProductBrief, sketch: ConceptSketch) -> str | None:
    """Return a short reason code. Callers log the code, not the creative copy."""
    facts = {fact.id: fact for fact in brief.facts}
    rules = _rules_by_id()
    rule = rules.get(sketch.rule_id)
    if rule is None:
        return "unknown_rule"
    if brief.category not in rule["categories"]:
        return "category"
    if any(fact_id not in facts for fact_id in sketch.evidence_ids):
        return "missing_fact"
    cited = [facts[fact_id] for fact_id in sketch.evidence_ids]
    if len(cited) < rule["prerequisites"]["min_facts"] or not _merchant_support(cited, rule["prerequisites"]):
        return "prerequisite"
    if sketch.hook_type not in allowed_hooks(brief.ad_form):
        return "hook"
    return None


def _sketch_issue_message(sketch: ConceptSketch, issue: str) -> str:
    if issue == "unknown_rule":
        return f"未知策略规则：{sketch.rule_id}"
    if issue == "category":
        return f"规则 {sketch.rule_id} 不适用于该商品品类"
    if issue == "missing_fact":
        return f"短概念 {sketch.id} 引用了不存在的商品事实"
    if issue == "prerequisite":
        rule = _rules_by_id().get(sketch.rule_id) or {}
        description = (rule.get("prerequisites") or {}).get("description", "")
        return f"规则 {sketch.rule_id} 缺少前提：{description}"
    if issue == "hook":
        return f"短概念 {sketch.id} 的开场钩子不属于当前广告类型"
    return f"短概念 {sketch.id} 的措辞要用观众听得懂的话"


def _metric_id(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else "id"


def _record_strategy_salvage() -> None:
    """One content-free metric when a local salvage changed the offer."""
    from .planner_runtime import safe_planner_metrics
    performance_logger.info("Planner request metrics: %s", json.dumps(
        safe_planner_metrics("strategy", None, recovery="strategy_salvage"), ensure_ascii=True))


def _sketch_score(sketch: ConceptSketch, chosen: list[ConceptSketch]) -> tuple:
    """Prefer a new mechanism, hook and brand world. Ties keep earlier sketches."""
    rules = {item.rule_id for item in chosen}
    hooks = {item.hook_type for item in chosen}
    worlds = {item.brand_world for item in chosen}
    families = {("emotional" if item.rule_id in EMOTIONAL_RULES else "informational") for item in chosen}
    family = "emotional" if sketch.rule_id in EMOTIONAL_RULES else "informational"
    score = 0
    if sketch.rule_id not in rules:
        score += 5
    if sketch.hook_type not in hooks:
        score += 3
    if sketch.brand_world not in worlds:
        score += 2
    if family not in families:
        score += 4
    return (score, 1 if family == "emotional" else 0)


def select_concept_ids(sketches: list[ConceptSketch]) -> list[str]:
    """Pick three sketches that differ in mechanism, hook and brand world.

    Strength here is that diversity score, not a model self-rating and not a
    predicted conversion. The call order is only a tie-break.
    """
    remaining = list(sketches)
    chosen: list[ConceptSketch] = []
    while remaining and len(chosen) < 3:
        best = max(range(len(remaining)), key=lambda index: (_sketch_score(remaining[index], chosen), -index))
        chosen.append(remaining.pop(best))
    return [item.id for item in chosen]


def validate_direction_offer(brief: ProductBrief, directions: list[CreativeDirection], pool: list[ConceptSketch]):
    """Keep a usable trio from a six-sketch pool.

    One invalid sketch, a non-greedy but still distinct expansion, or a
    direction that paraphrases its sketch is not fatal. The returned
    directions carry the sketch's canonical fields, with a disallowed
    opening moved to the nearest hook for this ad form. The call fails
    only when fewer than three directions remain usable.
    """
    brief = ProductBrief.model_validate(brief)
    directions = [CreativeDirection.model_validate(value) for value in directions]
    pool = [ConceptSketch.model_validate(value) for value in pool]
    if len(pool) != 6:
        raise ValueError("新方向需要六个短概念，再展开其中三个")
    _require_unique([item.id for item in pool], "短概念 ID")
    valid: list[ConceptSketch] = []
    dropped_hooks = False
    salvaged = False
    try:
        for index, sketch in enumerate(pool):
            issue = _sketch_issue(brief, sketch)
            if issue is None:
                valid.append(sketch)
                continue
            salvaged = True
            dropped_hooks = dropped_hooks or issue == "hook"
            diagnostic = {"index": index, "reason": issue}
            if issue == "hook" and sketch.hook_type in HOOK_IDS:
                diagnostic["hook_type"] = sketch.hook_type
            logger.warning("Dropped invalid concept sketch: %s", json.dumps(diagnostic, ensure_ascii=True))
        if len(valid) < 3:
            detail = "开场钩子不属于当前广告类型" if dropped_hooks else "短概念未通过规则、前提或措辞检查"
            raise ValueError(f"有效短概念不足三个，{detail}")
        program_pick = select_concept_ids(valid)
        logger.info("Strategy concept pick: %s", json.dumps({
            "program_pick": [_metric_id(item) for item in program_pick],
            "expanded": [_metric_id(direction.id) for direction in directions],
        }, ensure_ascii=True))
        if [direction.id for direction in directions] != program_pick:
            salvaged = True
        by_id = {item.id: item for item in valid}
        usable: list[CreativeDirection] = []
        for index, direction in enumerate(directions):
            sketch = by_id.get(direction.id)
            if sketch is None:
                salvaged = True
                logger.warning("Dropped direction outside the valid sketches: %s", json.dumps(
                    {"index": index}, ensure_ascii=True))
                continue
            canonical = (sketch.rule_id, sketch.hook_type, sketch.brand_world, tuple(sketch.evidence_ids))
            if (direction.rule_id, direction.hook_type, direction.brand_world,
                    tuple(direction.evidence_ids)) != canonical:
                salvaged = True
            updated = prepare_direction_card(direction.model_copy(update={
                "rule_id": sketch.rule_id,
                "hook_type": sketch.hook_type,
                "brand_world": sketch.brand_world,
                "evidence_ids": list(sketch.evidence_ids),
            }))
            if (updated.product_expression, updated.consumer_insight, updated.hook, updated.payoff,
                    updated.reason, updated.film_concept.key_visual if updated.film_concept else "") != (
                    direction.product_expression, direction.consumer_insight, direction.hook, direction.payoff,
                    direction.reason, direction.film_concept.key_visual if direction.film_concept else ""):
                salvaged = True
            structure = updated.film_concept.structure if updated.film_concept is not None else None
            allowed = allowed_hooks(brief.ad_form, structure)
            if updated.hook_type not in allowed:
                coerced = nearest_allowed_hook(updated.hook_type, allowed)
                salvaged = True
                if coerced is None:
                    logger.warning("Dropped direction with disallowed hook: %s", json.dumps(
                        {"index": index, "hook_type": updated.hook_type if updated.hook_type in HOOK_IDS else "hook"},
                        ensure_ascii=True))
                    continue
                logger.warning("Coerced direction hook: %s", json.dumps({
                    "index": index,
                    "from": updated.hook_type if updated.hook_type in HOOK_IDS else "hook",
                    "to": coerced,
                }, ensure_ascii=True))
                updated = updated.model_copy(update={"hook_type": coerced})
            usable.append(updated)
        if len(usable) < 3:
            raise ValueError("可用方向不足三个")
        require_ad_concepts(usable, brief)
        if len({direction.rule_id for direction in usable}) < 2:
            raise ValueError("三个方向至少要使用两种创意机制")
        if len({direction.hook_type for direction in usable}) < 2:
            raise ValueError("三个方向至少要使用两种开场钩子")
        if len({direction.brand_world for direction in usable}) < 2:
            raise ValueError("三个方向至少要使用两种品牌世界路径")
        return usable, valid
    finally:
        if salvaged:
            _record_strategy_salvage()


_INVISIBLE_BENEFIT = re.compile(r"降噪|静音|隔音|无声|安静|配方|成分|气味|香味|香气")
_CHAR_ID = re.compile(r"^char_[a-z0-9_]{1,40}$")
_BRAND_SKELETONS = ("product_visual", "world_intercut", "lifestyle")


def _clip_ingress(value, limit):
    if not isinstance(value, str) or len(value) <= limit:
        return value
    clipped = clip_pitch(value, limit)
    return clipped or value[:limit]


def _dedupe_ids(values):
    if not isinstance(values, list):
        return values
    seen = []
    for item in values:
        if item not in seen:
            seen.append(item)
    return seen


def _fresh_character_id(value, taken):
    raw = value if isinstance(value, str) else ""
    slug = re.sub(r"[^a-z0-9_]", "", raw.lower()).strip("_")
    if slug.startswith("char_"):
        slug = slug[5:]
    elif slug.startswith("char"):
        slug = slug[4:].strip("_")
    base = "char_" + (slug or "person")
    if len(base) > 45:
        base = base[:45].rstrip("_") or "char_person"
    if not _CHAR_ID.fullmatch(base):
        base = "char_person"
    candidate = base
    number = 2
    while candidate in taken or not _CHAR_ID.fullmatch(candidate):
        suffix = str(number)
        trimmed = base[:45 - len(suffix)].rstrip("_")
        candidate = f"{trimmed}{suffix}"
        number += 1
        if number > 30:
            break
    return candidate


def _fit_cast(production, adjusted, product_name="", merchant_text=""):
    if not isinstance(production, dict):
        return
    people = production.get("characters")
    if not isinstance(people, list):
        return
    taken = {person["id"] for person in people
             if isinstance(person, dict) and isinstance(person.get("id"), str) and _CHAR_ID.fullmatch(person["id"])}
    mapping = {}
    for person in people:
        if not isinstance(person, dict):
            continue
        old = person.get("id")
        if not (isinstance(old, str) and _CHAR_ID.fullmatch(old)):
            new = _fresh_character_id(old, taken)
            taken.add(new)
            if isinstance(old, str):
                mapping[old] = new
            person["id"] = new
            adjusted.append("character_id")
        if isinstance(person.get("name"), str) and len(person["name"]) > 60:
            person["name"] = person["name"][:60]
            adjusted.append("name_length")
        from .design import record_character_gender, strip_product_from_cast
        description = person.get("description")
        if not isinstance(description, str) or not description.strip():
            person["description"] = "成年角色"
            adjusted.append("character_description")
        else:
            cleaned = strip_product_from_cast(description, product_name)
            if len(cleaned) > 800:
                cleaned = _clip_ingress(cleaned, 800)
            if cleaned != description:
                person["description"] = cleaned or "成年角色"
                adjusted.append("character_description")
            elif len(description) > 800:
                person["description"] = _clip_ingress(description, 800)
                adjusted.append("character_description")
        if record_character_gender(person, merchant_text):
            adjusted.append("character_gender")
    speaker = production.get("speaker_id")
    if isinstance(speaker, str) and speaker in mapping:
        production["speaker_id"] = mapping[speaker]
    for constraint in production.get("product_constraints") or []:
        if not isinstance(constraint, dict):
            continue
        if isinstance(constraint.get("evidence_ids"), list):
            deduped = _dedupe_ids(constraint["evidence_ids"])
            if deduped != constraint["evidence_ids"]:
                constraint["evidence_ids"] = deduped
                adjusted.append("evidence_ids")
        text = constraint.get("text")
        if isinstance(text, str) and len(text) > 500:
            constraint["text"] = _clip_ingress(text, 500)
            adjusted.append("constraint_length")
    voice = production.get("voice_description")
    if isinstance(voice, str) and len(voice) > 600:
        production["voice_description"] = _clip_ingress(voice, 600)
        adjusted.append("voice_length")


def _align_brand_picture(payload, adjusted):
    """Copy the telling method onto the structure so a first draft is not rejected.

    The equality is a wire check. A product film often names product_visual as
    the structure and puts the actual skeleton in visual_skeleton. Locked
    preferences stay on the locked skeleton.
    """
    brief = payload.get("brief")
    if not isinstance(brief, dict) or brief.get("ad_form") != "brand_tvc":
        return
    intent = brief.get("creative_intent") if isinstance(brief.get("creative_intent"), dict) else {}
    preference = intent.get("structure_preference") or "auto"
    for direction in payload.get("directions") or []:
        if not isinstance(direction, dict):
            continue
        concept = direction.get("film_concept")
        if not isinstance(concept, dict):
            continue
        if preference in _BRAND_SKELETONS:
            if concept.get("structure") != preference or concept.get("visual_skeleton") != preference:
                concept["structure"] = preference
                concept["visual_skeleton"] = preference
                adjusted.append("skeleton")
            continue
        skeleton = concept.get("visual_skeleton")
        if skeleton in _BRAND_SKELETONS and concept.get("structure") != skeleton:
            concept["structure"] = skeleton
            adjusted.append("skeleton")


def _coerce_unknown_hook(value, ad_form):
    if value in HOOK_IDS:
        return value
    allowed = allowed_hooks(ad_form if isinstance(ad_form, str) else None)
    coerced = nearest_allowed_hook(value if isinstance(value, str) else "", allowed)
    if coerced:
        return coerced
    ids = playbook_hook_ids(ad_form if isinstance(ad_form, str) else None)
    return ids[0] if ids else "macro_detail"


def prepare_strategy_ingress(payload):
    """Make a first directions reply fit the wire schema without another model call.

    Card lines are already shortened inside salvage. Motif and side proof are
    capped earlier, by pydantic, so a 17-character motif used to reject the
    whole offer and pay for strategy_response_repair. The same happens when
    the recommendation names a sketch that was not expanded, a character id
    is not char_*, or a product film's structure and skeleton disagree.
    Creative rules still run afterwards. Merchant quotes are not rewritten.
    """
    if not isinstance(payload, dict) or payload.get("kind") != "directions":
        return payload
    result = deepcopy(payload)
    adjusted = []
    message = result.get("message")
    if isinstance(message, str) and len(message) > 1000:
        result["message"] = _clip_ingress(message, 1000)
        adjusted.append("message_length")
    brief = result.get("brief") if isinstance(result.get("brief"), dict) else {}
    ad_form = brief.get("ad_form")
    _align_brand_picture(result, adjusted)
    for direction in result.get("directions") or []:
        if not isinstance(direction, dict):
            continue
        if isinstance(direction.get("evidence_ids"), list):
            deduped = _dedupe_ids(direction["evidence_ids"])
            if deduped != direction["evidence_ids"]:
                direction["evidence_ids"] = deduped
                adjusted.append("evidence_ids")
        if not isinstance(direction.get("audience"), str) or not direction["audience"].strip():
            direction["audience"] = brief.get("audience") if isinstance(brief.get("audience"), str) and brief["audience"].strip() else "顾客"
            adjusted.append("audience")
        if not isinstance(direction.get("scenario"), str) or not direction["scenario"].strip():
            direction["scenario"] = brief.get("scenario") if isinstance(brief.get("scenario"), str) and brief["scenario"].strip() else "使用时"
            adjusted.append("scenario")
        notes = direction.get("production_notes")
        if not isinstance(notes, list) or not any(isinstance(item, str) and item.strip() for item in notes):
            direction["production_notes"] = ["不编未确认的功效"]
            adjusted.append("production_notes")
        else:
            direction["production_notes"] = [_clip_ingress(item, 500) if isinstance(item, str) else item for item in notes]
        hook = direction.get("hook_type")
        if hook not in HOOK_IDS:
            direction["hook_type"] = _coerce_unknown_hook(hook, ad_form)
            adjusted.append("hook")
        concept = direction.get("film_concept")
        if isinstance(concept, dict):
            motif = concept.get("visual_motif")
            if isinstance(motif, str) and len(motif) > 16:
                concept["visual_motif"] = _clip_ingress(motif, 16)
                adjusted.append("motif_length")
            proof = concept.get("side_proof")
            if isinstance(proof, str) and len(proof) > 40:
                concept["side_proof"] = _clip_ingress(proof, 40)
                adjusted.append("side_proof_length")
        product_name = brief.get("product_name") if isinstance(brief.get("product_name"), str) else ""
        from .design import merchant_gender_corpus
        _fit_cast(direction.get("production"), adjusted, product_name, merchant_gender_corpus(brief))
        from .production import align_story_voice
        concept = direction.get("film_concept") if isinstance(direction.get("film_concept"), dict) else {}
        _production, did_voice = align_story_voice(
            ad_form, direction.get("production"), concept.get("structure") if isinstance(concept, dict) else None)
        if did_voice:
            adjusted.append("speech_mode")
    for sketch in result.get("concept_pool") or []:
        if not isinstance(sketch, dict):
            continue
        if isinstance(sketch.get("evidence_ids"), list):
            deduped = _dedupe_ids(sketch["evidence_ids"])
            if deduped != sketch["evidence_ids"]:
                sketch["evidence_ids"] = deduped
                adjusted.append("evidence_ids")
        hook = sketch.get("hook_type")
        if hook not in HOOK_IDS:
            sketch["hook_type"] = _coerce_unknown_hook(hook, ad_form)
            adjusted.append("hook")
    ids = {direction.get("id") for direction in result.get("directions") or [] if isinstance(direction, dict)}
    recommended = result.get("recommended_direction_id")
    if recommended is not None and recommended not in ids:
        result["recommended_direction_id"] = None
        adjusted.append("recommended_id")
    if adjusted:
        logger.info("Strategy ingress adjusted: %s", json.dumps(
            {"adjusted": sorted(set(adjusted))}, ensure_ascii=True))
    return result


def _benefit_texts(brief: ProductBrief) -> str:
    parts = [brief.product_name or ""]
    if brief.creative_intent is not None:
        parts.extend([brief.creative_intent.use_context, brief.creative_intent.setting_requirement,
                      brief.creative_intent.reference_intent])
    parts.extend(fact.text for fact in brief.facts)
    parts.extend(brief.unknowns)
    parts.extend(brief.assumptions)
    return "\n".join(part for part in parts if isinstance(part, str))


def _life_result_direction(directions: list[CreativeDirection]) -> CreativeDirection | None:
    """A real person, then a short product world. Intercut first, then lifestyle."""
    def people(direction):
        production = direction.production
        return bool(production is not None and production.characters)

    def skeleton(direction):
        concept = direction.film_concept
        if concept is None:
            return ""
        return concept.visual_skeleton or concept.structure

    intercut = [direction for direction in directions if skeleton(direction) == "world_intercut"]
    with_person = [direction for direction in intercut if people(direction)]
    if with_person:
        return with_person[0]
    if intercut:
        return intercut[0]
    living = [direction for direction in directions if skeleton(direction) == "lifestyle" and people(direction)]
    return living[0] if living else None


def recommend_invisible_benefit(brief: ProductBrief, directions: list[CreativeDirection], recommended_id):
    """Point the recommendation at a life result when the benefit cannot be seen.

    A studio spin stays in the three. It is not removed and it is not a block.
    不出镜 and a visible product leave the model's recommendation alone.
    """
    if brief.ad_form != "brand_tvc" or not _INVISIBLE_BENEFIT.search(_benefit_texts(brief)):
        return recommended_id
    from .picture_rules import off_camera_locked
    if off_camera_locked(_benefit_texts(brief)):
        return recommended_id
    better = _life_result_direction(directions)
    if better is None:
        return recommended_id
    current = next((direction for direction in directions if direction.id == recommended_id), None)
    if current is not None and current.film_concept is not None and (
            current.film_concept.visual_skeleton or current.film_concept.structure) in ("world_intercut", "lifestyle"):
        return recommended_id
    return better.id


def build_direction_candidates(brief, directions, recommended_id, rules, *, direction_review=None) -> DirectionCandidates:
    # Reuse the same fact, category, prerequisite and provenance checks. The
    # temporary selection is never returned/persisted as a merchant decision.
    # Also used when restoring historical unanswered offers after a reply;
    # new-offer story requirements belong to StrategyDecision at model ingress.
    if len(directions) != 3:
        raise ValueError("请提供恰好三个不同的创意方向")
    directions = [CreativeDirection.model_validate(value) for value in directions]
    from .production import align_story_voice
    form = brief.ad_form if not isinstance(brief, dict) else brief.get("ad_form")
    aligned = []
    for direction in directions:
        structure = direction.film_concept.structure if direction.film_concept is not None else None
        if direction.production is not None:
            production, did = align_story_voice(form, direction.production, structure)
            if did:
                direction = direction.model_copy(update={"production": production})
        aligned.append(direction)
    directions = aligned
    checked = build_strategy(brief, directions, directions[0].id, rules, merchant_choice=True)
    recommended_id = recommend_invisible_benefit(checked.brief, checked.directions, recommended_id)
    return DirectionCandidates(brief=checked.brief, directions=checked.directions,
        recommended_direction_id=recommended_id, rules_used=checked.rules_used, direction_review=direction_review)


def brief_from_legacy(facts: dict, category: Category = "general") -> ProductBrief:
    """Read historical name/claims without upgrading unreferenced claims to facts.

    Re-establish old assertions from current original messages/images before
    using them as direction evidence. This helper never changes stored plans.
    """
    name = facts.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "待确认商品"
    unknowns = [f"历史说法待核实：{claim.strip()}" for claim in facts.get("claims", [])
                if isinstance(claim, str) and claim.strip()]
    return ProductBrief(product_name=name, category=category, facts=[],
                        unknowns=list(dict.fromkeys(unknowns)))
