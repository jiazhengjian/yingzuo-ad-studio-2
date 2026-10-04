"""One independent, source-bound review of three new creative directions.

The model judges story semantics. Code checks identity, source references,
capacity and verdict consistency; it does not claim to prove physical truth.
A review that already says ready may omit fields the strategy step owns.
Those fields are copied back. A passed review does not fail the job.
Planner words in consumer-facing lines are rewritten in place. Check notes,
the review summary and brief assumptions are internal and are left as written.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
import re

from pydantic import Field, ValidationError

from .strategy import (CreativeDirection, DirectionCandidates, DirectionReviewSummary,
                       DirectionReviewCheck, ProductBrief, build_direction_candidates,
                       require_direction_stories, require_direction_value, require_direction_concepts,
                       require_pitch_lines, require_consumer_language, CONSUMER_FIELDS,
                       plain_consumer_wording, sanitize_direction_copy)


performance_logger = logging.getLogger("uvicorn.error.planner")
_IDEA_FIELDS = ("hook_type", "brand_world", "consumer_insight", "big_idea", "label")
_SAFE_TEXT_FIELDS = ("angle", "story_summary", "product_expression", "hook", "development", "payoff")
_REASON_NEEDLES = (
    ("不能新增、删除、重排", "direction_identity"),
    ("结论须恰好覆盖", "check_coverage"),
    ("不能改写创意机制", "idea_field"),
    ("创意张力和开场钩子", "tension_hook"),
    ("观众听得懂", "jargon"),
    ("故事概要", "story"),
    ("受众为何在意", "value"),
    ("影片结构", "structure"),
    ("商品约束", "production"),
    ("参考图合计", "capacity"),
    ("人物讲解", "speech_mode"),
    ("图片观察", "image_fact"),
    ("未通过审核", "not_ready"),
    ("ready 必须", "verdict"),
)


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
只在同一回复内修订三个方向。不得返回替代brief或改recommended_direction_id；方向id、rule_id、hook_type、brand_world、consumer_insight、big_idea、label和顺序原样保留。不得改写创意机制、一句话创意或开场钩子编号。只修正没有事实支撑的商品主张，可改hook、payoff、key_visual、evidence_ids及商品约束中的越界功效。历史方向里已有的audience_value、product_expression、story_summary、attraction_mechanism、angle、development、memory_anchor、sound_role若本schema没有这些键，不要补写；程序会从审前方向保留。不得新增/删除事实或改变ID/来源、商家原文及creative_intent。唯一事实文字纠正接口为image_observation_corrections=[{fact_id,text}]，仅已有source_kind=image，每ID至多一次；无纠正时[]。修订后的事实引用仍仅来自immutable_brief.facts。
情绪、场景、夸张和幽默可以保留，不必补事实编号；功能、材质、成分、对比和效果仍须由引用事实支撑。无法在不改创意的前提下去掉无依据主张时，claim_support_passed=false，不要把方向改写成更平的说明书。
creative_tension和hook_strength只填low、medium或high，不参与ready。可修改未确认的制作安排，受用户明确要求和reference_capacity.max_characters约束。brief.assumptions并非用户硬约束，实际制作采用审后production及film_concept。此阶段不生成脚本或媒体，不增加确认或重试循环。
输出完整修订后的directions、summary、逐方向checks和distinct_directions_passed。checks恰好覆盖三个方向且包含全部声明检查项，基于本次修订稿与纠正后事实填写。硬性检查通过才ready=true；张力与钩子强度即使是low也不把ready改成false。无法在权限内修好的来源、意图或内容问题如实false并给可定位note。不输出隐含思考过程。
"""


_REVIEW_OMIT = ("story_summary", "attraction_mechanism", "audience_value", "product_expression",
                "consumer_insight", "angle", "development")


def direction_review_schema(max_characters: int) -> dict:
    schema = DirectionReview.model_json_schema()
    schema["properties"]["image_observation_corrections"]["default"] = []
    direction = schema["$defs"]["CreativeDirection"]
    for field in _REVIEW_OMIT:
        direction["properties"].pop(field, None)
        direction["required"] = [name for name in direction.get("required", []) if name != field]
    for field in ("hook", "payoff"):
        direction["properties"][field]["maxLength"] = 40
    direction["properties"]["big_idea"]["maxLength"] = 30
    film = schema["$defs"]["FilmConcept"]
    for field in ("memory_anchor", "sound_role"):
        film["properties"].pop(field, None)
        film["required"] = [name for name in film.get("required", []) if name != field]
    film["properties"]["key_visual"]["maxLength"] = 40
    direction["properties"]["production"] = {"$ref": "#/$defs/ProductionPlan"}
    direction["properties"]["film_concept"] = {"$ref": "#/$defs/FilmConcept"}
    direction["required"] = list(dict.fromkeys([*direction.get("required", []), "production", "film_concept"]))
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
        # checks[].note, summary and brief.assumptions are internal. Only the
        # lines a merchant can read, or a later script can copy, are scanned.
        require_consumer_language(review.directions)
    if any(direction.story_summary or direction.attraction_mechanism for direction in original.directions):
        require_direction_stories(review.directions)
    if any(direction.audience_value or direction.product_expression for direction in original.directions):
        require_direction_value(review.directions)
    if all(len(direction.hook) <= 40 and len(direction.payoff) <= 40 for direction in original.directions):
        require_pitch_lines(review.directions)
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


def review_failure_reasons(exc) -> list[str]:
    """Ids and enums only. Never the model's prose or the merchant's words."""
    reasons = []
    if isinstance(exc, ValidationError):
        for item in exc.errors(include_url=False, include_context=False, include_input=False)[:8]:
            loc = [part for part in item.get("loc", ())
                   if isinstance(part, str) and re.fullmatch(r"[A-Za-z_]{1,40}", part)]
            kind = item.get("type")
            if not isinstance(kind, str) or not re.fullmatch(r"[a-z0-9_]{1,40}", kind):
                kind = "schema"
            token = "_".join([*loc[:2], kind]) if loc else kind
            if token not in reasons:
                reasons.append(token[:40])
        return reasons[:8] or ["schema"]
    text = str(exc)
    for needle, code in _REASON_NEEDLES:
        if needle in text and code not in reasons:
            reasons.append(code)
    return reasons[:8] or ["contract"]


def _record_direction_review_salvage(reasons):
    from .planner_runtime import safe_planner_metrics
    performance_logger.info("Planner request metrics: %s", json.dumps(
        safe_planner_metrics("direction_review", None, recovery="direction_review_salvage", reasons=reasons),
        ensure_ascii=True))


def _with_canonical_fields(raw, original: DirectionCandidates):
    """Put strategy-owned identity back onto a revised offer that omitted it."""
    patched = deepcopy(raw)
    directions = patched.get("directions") if isinstance(patched, dict) else None
    if not isinstance(directions, list):
        return patched
    if [item.get("id") if isinstance(item, dict) else None for item in directions] != [d.id for d in original.directions]:
        return patched
    by_id = {item["id"]: item for item in directions}
    for previous in original.directions:
        incoming = by_id[previous.id]
        incoming["rule_id"] = previous.rule_id
        for field in _IDEA_FIELDS:
            value = getattr(previous, field)
            if value is None or value == "":
                incoming.pop(field, None)
            else:
                incoming[field] = value
        for field in ("story_summary", "attraction_mechanism", "audience_value", "product_expression",
                      "angle", "development"):
            previous_value = getattr(previous, field) or ""
            incoming_value = incoming.get(field)
            if previous_value and (not isinstance(incoming_value, str) or not incoming_value.strip()):
                incoming[field] = previous_value
        concept = incoming.get("film_concept")
        previous_concept = previous.film_concept
        if isinstance(concept, dict) and previous_concept is not None:
            for field in ("memory_anchor", "sound_role"):
                previous_value = getattr(previous_concept, field) or ""
                incoming_value = concept.get(field)
                if previous_value and (not isinstance(incoming_value, str) or not incoming_value.strip()):
                    concept[field] = previous_value
    return patched


def _constraint_text(direction, incoming):
    production = direction.get("production")
    revised = incoming.get("production") if isinstance(incoming, dict) else None
    if not isinstance(production, dict) or not isinstance(revised, dict):
        return
    current = production.get("product_constraints")
    proposed = revised.get("product_constraints")
    if not isinstance(current, list) or not isinstance(proposed, list) or len(current) != len(proposed):
        return
    for left, right in zip(current, proposed):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return
        if left.get("evidence_ids") != right.get("evidence_ids"):
            return
        text = right.get("text")
        if isinstance(text, str) and text.strip():
            left["text"] = text


def _review_shell(original: DirectionCandidates, raw, *, overlay_text, overlay_evidence=True):
    summary = raw.get("summary") if isinstance(raw, dict) and isinstance(raw.get("summary"), str) else ""
    shell = {
        "ready": True,
        "distinct_directions_passed": True if not isinstance(raw, dict) else bool(raw.get("distinct_directions_passed", True)),
        "summary": summary.strip() or "审核已通过，沿用已校验的方向。",
        "checks": deepcopy(raw.get("checks")) if isinstance(raw, dict) else None,
        "image_observation_corrections": deepcopy(raw.get("image_observation_corrections") or []) if isinstance(raw, dict) else [],
        "directions": [direction.model_dump() for direction in original.directions],
    }
    if not overlay_text or not isinstance(raw, dict):
        return shell
    revised = {item.get("id"): item for item in raw.get("directions") or [] if isinstance(item, dict)}
    for direction in shell["directions"]:
        incoming = revised.get(direction["id"])
        if not isinstance(incoming, dict):
            continue
        for field in _SAFE_TEXT_FIELDS:
            value = incoming.get(field)
            if isinstance(value, str) and value.strip():
                direction[field] = value
        if not overlay_evidence:
            continue
        evidence = incoming.get("evidence_ids")
        if (isinstance(evidence, list) and evidence
                and all(isinstance(item, str) and item.strip() for item in evidence)):
            direction["evidence_ids"] = list(evidence)
        _constraint_text(direction, incoming)
    return shell


_FILM_CONSUMER_FIELDS = ("key_visual", "memory_anchor", "sound_role")


def _plain_field(value):
    if not isinstance(value, str) or not value.strip():
        return value, False
    plain = plain_consumer_wording(value)
    if plain and plain != value:
        return plain, True
    return value, False


def _sanitize_raw_directions(raw):
    """Rewrite planner phrases on direction lines. Leave notes and summary alone."""
    patched = deepcopy(raw)
    directions = patched.get("directions") if isinstance(patched, dict) else None
    if not isinstance(directions, list):
        return patched
    for item in directions:
        if not isinstance(item, dict):
            continue
        for field in CONSUMER_FIELDS:
            plain, changed = _plain_field(item.get(field))
            if changed:
                item[field] = plain
        concept = item.get("film_concept")
        if isinstance(concept, dict):
            for field in _FILM_CONSUMER_FIELDS:
                plain, changed = _plain_field(concept.get(field))
                if changed:
                    concept[field] = plain
    return patched


def _sanitized_candidates(original: DirectionCandidates) -> DirectionCandidates:
    directions = [sanitize_direction_copy(direction) for direction in original.directions]
    if all(updated is direction for updated, direction in zip(directions, original.directions)):
        return original
    return original.model_copy(update={"directions": directions})


def salvage_direction_review(raw, original: DirectionCandidates, rules: list[dict], *, product_reference_count: int):
    """Keep a ready review. Copy owned fields, then the pre-review directions.

    The strategy step already accepted the three directions. A later review
    that says ready must not discard that offer because it omitted hook,
    brand world, insight or the one-line idea. Text the review is allowed to
    change is kept when it still validates. Planner wording on those lines is
    replaced before the retry. Otherwise the pre-review directions stand,
    with the same replacement. A stub that only says ready, without the three
    checks, is still rejected.
    """
    rejected = None
    try:
        return validate_direction_review(raw, original, rules, product_reference_count=product_reference_count), []
    except (ValidationError, ValueError) as first:
        rejected = first
        reasons = review_failure_reasons(first)
        if not (isinstance(raw, dict) and raw.get("ready") is True):
            raise
    clean_original = _sanitized_candidates(original)
    clean_raw = _sanitize_raw_directions(raw)
    patched = _with_canonical_fields(clean_raw, clean_original)
    try:
        review = validate_direction_review(patched, clean_original, rules, product_reference_count=product_reference_count)
    except (ValidationError, ValueError) as exc:
        for code in review_failure_reasons(exc):
            if code not in reasons:
                reasons.append(code)
        review = None
    if review is None:
        for overlay, evidence in ((True, True), (True, False), (False, False)):
            candidate = _review_shell(clean_original, clean_raw, overlay_text=overlay, overlay_evidence=evidence)
            try:
                review = validate_direction_review(
                    candidate, clean_original, rules, product_reference_count=product_reference_count)
                break
            except (ValidationError, ValueError) as exc:
                for code in review_failure_reasons(exc):
                    if code not in reasons:
                        reasons.append(code)
                review = None
    if review is None:
        raise rejected
    _record_direction_review_salvage(reasons[:8])
    return review, reasons[:8]


def reviewed_candidates(review: DirectionReview, original: DirectionCandidates, rules: list[dict]) -> DirectionCandidates:
    """Attach bounded review evidence to the durable offer, without duplicating prose."""
    audit = DirectionReviewSummary.model_validate(review.model_dump(exclude={"directions"}))
    return build_direction_candidates(reviewed_brief(review, original), review.directions,
        original.recommended_direction_id, rules, direction_review=audit)
