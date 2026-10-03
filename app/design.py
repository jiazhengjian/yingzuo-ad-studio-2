"""Compile a scene request from the confirmed plan, without choosing a new setting."""
from __future__ import annotations

import json
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
    """Compile one four-view sheet for one approved, identifiable character."""
    approved = (plan.get("production") or {}).get("characters", [])
    if not isinstance(character, dict) or character not in approved:
        raise ValueError("角色四视图必须引用本次已确认方案中的角色")
    instructions, trace = compiler_template("ads-video-asset-prompts", "references/character-template.txt")
    context = {"name": character["name"], "description": character["description"]}
    return {"version": "character-context-v1", "character_id": character["id"],
            "skill_trace": [trace], "prompt": instructions + "\n已确认角色：\n"
            + json.dumps(context, ensure_ascii=False)}
