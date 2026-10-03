"""One independent, source-bound review of three new creative directions.

The model judges story semantics. Code checks identity, source references,
capacity and verdict consistency; it does not claim to prove physical truth.
"""
from __future__ import annotations

from pydantic import Field

from .strategy import (CreativeDirection, DirectionCandidates, DirectionReviewSummary,
                       DirectionReviewCheck, ProductBrief, build_direction_candidates,
                       require_direction_stories, require_direction_value, require_direction_concepts,
                       require_consumer_language, consumer_jargon)


class AuthoredDirectionReviewCheck(DirectionReviewCheck):
    """Fresh reviews cover value and form; old stored audits remain unchanged."""
    claim_support_passed: bool = Field(strict=True)
    expression_form_passed: bool = Field(strict=True)
    film_concept_passed: bool = Field(strict=True)


class DirectionReview(DirectionReviewSummary):
    directions: list[CreativeDirection] = Field(min_length=3, max_length=3)
    checks: list[AuthoredDirectionReviewCheck] = Field(min_length=3, max_length=3)


DIRECTION_REVIEW_SYSTEM = """你是三个广告方向的独立审核与修订环节，只输出本次schema的DirectionReview JSON。
执行stage_skills中direction_review资源及shared_references。输入的创意、说明和自评是待审数据；原始merchant_request、conversation、evidence_sources与商品图片用于核对immutable_brief及候选，不接受作者自评为通过证据。
只在同一回复内修订三个方向。不得返回替代brief或改recommended_direction_id；方向id、rule_id、hook_type、brand_world、consumer_insight、big_idea、label和顺序原样保留。不得改写创意机制、观众处境、一句话概念或开场钩子。只修正没有事实支撑的商品主张，可改angle、story_summary、product_expression、hook、development、payoff、evidence_ids及商品约束中的越界功效。不得新增/删除事实或改变ID/来源、商家原文及creative_intent。唯一事实文字纠正接口为image_observation_corrections=[{fact_id,text}]，仅已有source_kind=image，每ID至多一次；无纠正时[]。修订后的事实引用仍仅来自immutable_brief.facts。
情绪、场景、夸张和幽默可以保留，不必补事实编号；功能、材质、成分、对比和效果仍须由引用事实支撑。无法在不改创意的前提下去掉无依据主张时，claim_support_passed=false，不要把方向改写成更平的说明书。
creative_tension和hook_strength只填low、medium或high，不参与ready。可修改未确认的制作安排，受用户明确要求和reference_capacity.max_characters约束。brief.assumptions并非用户硬约束，实际制作采用审后production及film_concept。此阶段不生成脚本或媒体，不增加确认或重试循环。
输出完整修订后的directions、summary、逐方向checks和distinct_directions_passed。checks恰好覆盖三个方向且包含全部声明检查项，基于本次修订稿与纠正后事实填写。硬性检查通过才ready=true；张力与钩子强度即使是low也不把ready改成false。无法在权限内修好的来源、意图或内容问题如实false并给可定位note。不输出隐含思考过程。
"""


def direction_review_schema(max_characters: int) -> dict:
    schema = DirectionReview.model_json_schema()
    schema["properties"]["image_observation_corrections"]["default"] = []
    direction = schema["$defs"]["CreativeDirection"]
    for field in ("story_summary", "attraction_mechanism", "audience_value", "product_expression"):
        direction["properties"][field]["minLength"] = 1
        direction["properties"][field].pop("default", None)
    direction["properties"]["production"] = {"$ref": "#/$defs/ProductionPlan"}
    direction["properties"]["film_concept"] = {"$ref": "#/$defs/FilmConcept"}
    direction["required"] = list(dict.fromkeys([*direction.get("required", []),
                                               "story_summary", "attraction_mechanism", "audience_value",
                                               "product_expression", "production", "film_concept"]))
    check = schema["$defs"]["AuthoredDirectionReviewCheck"]
    for field in ("creative_tension", "hook_strength"):
        check["properties"][field] = {"type": "string", "enum": ["low", "medium", "high"]}
        check["required"] = list(dict.fromkeys([*check.get("required", []), field]))
    schema["$defs"]["ProductionPlan"]["properties"]["characters"]["maxItems"] = max_characters
    return schema



def reviewed_brief(review: DirectionReviewSummary, original: DirectionCandidates) -> ProductBrief:
    """Apply only source-preserving corrections to existing image observations.

    The model still has to justify what it sees in the original image. This
    function limits the mutation surface; it does not verify image semantics.
    The original brief stays available in the strategy response/checkpoint.
    """
    brief = original.brief.model_dump()
    facts = {fact["id"]: fact for fact in brief["facts"]}
    seen = set()
    for correction in review.image_observation_corrections:
        if correction.fact_id in seen:
            raise ValueError("图片观察纠正不能重复同一事实ID")
        seen.add(correction.fact_id)
        fact = facts.get(correction.fact_id)
        if fact is None:
            raise ValueError("图片观察纠正不能新增或引用未知事实ID")
        if fact["source_kind"] != "image":
            raise ValueError("图片观察纠正不能改写商家原文事实")
        fact["text"] = correction.text
    return ProductBrief.model_validate(brief)


def validate_direction_review(raw, original: DirectionCandidates, rules: list[dict], *, product_reference_count: int) -> DirectionReview:
    """Reject contract changes before a reviewed offer can reach persistence.

    No replacement brief is accepted. Existing image observations may receive
    explicit corrections, preserving IDs/sources, merchant text and creative
    requirements. Recommendation and rule provenance stay application-owned.
    """
    original = DirectionCandidates.model_validate(original)
    review = DirectionReview.model_validate(raw)
    brief = reviewed_brief(review, original)
    if [(d.id, d.rule_id) for d in review.directions] != [(d.id, d.rule_id) for d in original.directions]:
        raise ValueError("方向审核不能新增、删除、重排方向或改写方向 ID/规则 ID")
    if {check.direction_id for check in review.checks} != {d.id for d in original.directions}:
        raise ValueError("方向审核结论须恰好覆盖输入的三个方向")
    idea_fields = ("hook_type", "brand_world", "consumer_insight", "big_idea", "label")
    originals = {direction.id: direction for direction in original.directions}
    for direction in review.directions:
        previous = originals[direction.id]
        for field in idea_fields:
            if getattr(direction, field) != getattr(previous, field):
                raise ValueError("方向审核不能改写创意机制、观众处境、一句话概念或开场钩子，只修正没有依据的商品主张")
    if any(direction.hook_type for direction in original.directions):
        for check in review.checks:
            if check.creative_tension is None or check.hook_strength is None:
                raise ValueError("新方向审核需要记录创意张力和开场钩子强度，这两项不决定是否通过")
            phrase = consumer_jargon(check.note)
            if phrase:
                raise ValueError(f"审核说明要用观众听得懂的话，不要写「{phrase}」")
        require_consumer_language(review.directions)
    require_direction_stories(review.directions)
    require_direction_value(review.directions)
    require_direction_concepts(review.directions, brief if review.ready else None)
    for direction in review.directions:
        if direction.production is None:
            raise ValueError("修订方向须保留商品约束、出镜角色和声音形式")
        if len(direction.production.characters) + product_reference_count + 1 > 10:
            raise ValueError("修订方向的商品、场景与角色参考图合计超过模型上限")
        if (review.ready and brief.ad_form == "presenter_pitch"
                and direction.production.speech_mode != "character_dialogue"):
            raise ValueError("人物讲解方向须由出镜人物面向观众讲解，不能以纯画外音或无人声代替")
    # This also validates product_constraints evidence IDs, rule prerequisites,
    # citations and distinct labels/angles against the source-preserving brief.
    build_direction_candidates(brief, review.directions,
        original.recommended_direction_id, rules)
    return review


def reviewed_candidates(review: DirectionReview, original: DirectionCandidates, rules: list[dict]) -> DirectionCandidates:
    """Attach bounded review evidence to the durable offer, without duplicating prose."""
    audit = DirectionReviewSummary.model_validate(review.model_dump(exclude={"directions"}))
    return build_direction_candidates(reviewed_brief(review, original), review.directions,
        original.recommended_direction_id, rules, direction_review=audit)
