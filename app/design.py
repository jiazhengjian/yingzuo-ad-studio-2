"""Compile a scene request from the confirmed plan, without choosing a new setting."""
from __future__ import annotations

import json
from .picture_rules import off_camera_locked
from .production import CHARACTER_SHEET_TEMPLATE, apply_cast_instructions
from .runtime_skills import compiler_template


def scene_design_request(project, plan):
    instructions, trace = compiler_template("ads-video-asset-prompts", "references/scene-template.txt")
    strategy = plan.get("strategy") or {}
    brief = strategy.get("brief") or {}
    selected = next((direction for direction in strategy.get("directions", [])
                     if direction.get("id") == strategy.get("selected_direction_id")), {})
    visual_direction = plan.get("visual_direction") or {}
    intent = brief.get("creative_intent")
    # New briefs keep product-use context separately. The selected film's
    # location must survive here, rather than reverting to a customer's salon.
    scenario = str((selected.get("scenario") or visual_direction.get("scene") or intent.get("setting_requirement") or "")
                   if isinstance(intent, dict) else
                   (brief.get("scenario") or selected.get("scenario") or visual_direction.get("scene") or "")).strip()
    context = {"product_name": plan["facts"]["name"], "confirmed_scenario": scenario}
    # A scene image needs a location and its visual conditions, not the film's
    # actions, dialogue, shot timings or the planner's explanation of the idea.
    if visual_direction:
        context["visual_direction"] = {key: visual_direction[key]
            for key in ("scene", "lighting", "color_palette") if visual_direction.get(key)}
    elif selected.get("scenario") and selected["scenario"] != scenario:
        context["scene_details"] = selected["scenario"]
    # Historical plans predate the scene contract. Preserve their accepted
    # segment-level visual context without copying every shot or its dialogue.
    if not scenario:
        accepted = list(dict.fromkeys(str(segment.get("prompt", "")).strip()
                        for segment in plan.get("segments", []) if str(segment.get("prompt", "")).strip()))
        if accepted:
            context["legacy_visual_context"] = accepted
        else:
            context["legacy_original_brief"] = str(project.get("brief", ""))
    return {"version": "scene-context-v4" if isinstance(intent, dict) else "scene-context-v3", "scenario": scenario, "skill_trace": [trace],
            "prompt": instructions + "\n已确认的场景与视觉方案：\n" + json.dumps(context, ensure_ascii=False)}


def character_design_request(project, plan, character):
    """Compile one horizontal identity sheet for one approved, identifiable character."""
    approved = (plan.get("production") or {}).get("characters", [])
    if not isinstance(character, dict) or character not in approved:
        raise ValueError("角色四视图必须引用本次已确认方案中的角色")
    instructions, trace = compiler_template("ads-video-asset-prompts", "references/character-template.txt")
    instructions = apply_cast_instructions(instructions, character.get("description", ""))
    context = {"name": character["name"], "description": character["description"]}
    return {"version": "character-context-v2", "character_id": character["id"],
            "aspect_ratio": "16:9", "sheet_layout": "horizontal_16x9",
            "template_version": CHARACTER_SHEET_TEMPLATE,
            "skill_trace": [trace], "prompt": instructions + "\n已确认角色：\n"
            + json.dumps(context, ensure_ascii=False)}


def _plan_shot(plan, shot_id):
    from .story_logic import iter_plan_shots
    if not shot_id or not isinstance(plan, dict):
        return None
    for shot in iter_plan_shots(plan):
        if shot.get("id") == shot_id:
            return shot
    return None


def _state_brief_text(project, plan):
    brief = ((plan.get("strategy") or {}).get("brief") or {}) if isinstance(plan, dict) else {}
    return " ".join(part for part in (
        brief.get("product_name") or "",
        project.get("brief") if isinstance(project, dict) else "",
        " ".join(fact.get("text") or "" for fact in brief.get("facts") or [] if isinstance(fact, dict)),
    ) if isinstance(part, str))


def state_context_characters(project, plan, instruction):
    """Characters this still must keep. Empty when the film is locked off-camera."""
    if not isinstance(plan, dict):
        return []
    hidden = off_camera_locked(_state_brief_text(project, plan), (plan.get("facts") or {}).get("name") or "")
    if hidden:
        return []
    cast = [person for person in ((plan.get("production") or {}).get("characters") or []) if isinstance(person, dict)]
    shot = _plan_shot(plan, (instruction or {}).get("shot_id"))
    ids = shot.get("character_ids") if isinstance(shot, dict) else None
    if isinstance(ids, list) and ids:
        chosen = [person for person in cast if person.get("id") in ids]
        if chosen:
            return chosen[:3]
    return cast[:3]


def state_design_request(project, plan, instruction):
    """One still of one state, in the same scene and wardrobe as the story.

    Not a before/after collage and not a video frame API. The image call also
    receives the scene, the character sheets, and the product original.
    """
    instruction = instruction or {}
    plan = plan if isinstance(plan, dict) else {}
    phase = instruction.get("phase")
    label = instruction.get("label") or "这一状态"
    subject = instruction.get("subject") or "主体"
    hidden = off_camera_locked(_state_brief_text(project, plan), (plan.get("facts") or {}).get("name") or "")
    phase_name = "起始" if phase == "start" else "结果"
    shot = _plan_shot(plan, instruction.get("shot_id")) or {}
    description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    visual = plan.get("visual_direction") if isinstance(plan.get("visual_direction"), dict) else {}
    place = " ".join(part for part in (
        visual.get("scene") if isinstance(visual.get("scene"), str) else "",
        visual.get("lighting") if isinstance(visual.get("lighting"), str) else "",
    ) if part)
    people = state_context_characters(project, plan, instruction)
    cast_line = "；".join(
        f"{person.get('name') or '角色'}：{person.get('description') or ''}" for person in people)[:240]
    prompt = (
        f"生成一张写实静帧，只表现「{subject}」的{phase_name}状态：{label}。"
        "这是这条片子里的同一时刻：同一个人、同一套衣服、同一间房间、同一种光线。"
        "不要换成另一个人，不要换成另一套衣服，不要换成另一个房间，不要改成棚拍。"
        "不要渐变，不要前后对比拼图，不要分格，不要可读文字、字幕或口号。"
        "商品外形、颜色和标识以商品原图为准，不重画商品，不改商标。"
        "参考图顺序：先是商品原图，接着是场景图，然后是这一镜相关的角色参考。"
    )
    if description.strip():
        prompt += f"这一镜的画面：{description.strip()[:180]}。"
    start_state = continuity.get("start_state") if isinstance(continuity.get("start_state"), str) else ""
    end_state = continuity.get("end_state") if isinstance(continuity.get("end_state"), str) else ""
    if start_state or end_state:
        prompt += f"这一镜的连续性：起始 {start_state or '未写'}；结束 {end_state or '未写'}。"
    if place.strip():
        prompt += f"场景与光线：{place.strip()[:120]}。"
    if cast_line:
        prompt += f"角色与服装：{cast_line}。"
    if phase == "start":
        prompt += "这张停在过程开始时的样子，不要画成已经做完。"
    else:
        prompt += "这张是切到结果之后的终点，要画成这一变化真正做完的样子，不要画成做到一半。"
    if hidden:
        prompt += "本片不出镜：不要人脸，不要把独立发束或局部画成真人头。"
    else:
        prompt += "外貌、服装与已确认的角色参考一致，不换脸。"
    return {
        "version": "state-still-v2",
        "aspect_ratio": "9:16",
        "state_id": instruction.get("state_id"),
        "phase": phase,
        "shot_id": instruction.get("shot_id"),
        "state_label": label,
        "prompt": prompt,
    }
