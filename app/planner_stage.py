"""Shared planner-stage payload used by the live gateway and offline replan."""
from __future__ import annotations


def enrich_planner_stage(data, *, legacy_storyboard=False):
    """Attach the playbook, ending rule, skills and speech budget the live planner sees.

    Mutates ``data`` and returns the skill bundles so the caller can record traces.
    """
    from app.ad_form_playbook import (ending_policy, resolve_playbook_form, speech_profile,
                                       structure_from_stage_data, tagline_policy)
    from app.runtime_skills import stage_skill_context

    stage = data["stage"]
    skill_stage = "storyboard" if stage == "storyboard_repair" else stage
    if stage == "ad_script" and data.get("draft_format") == "content_only":
        skill_stage = "content_draft"
    if stage == "ad_script" and data.get("draft_format") == "speech_only":
        skill_stage = "speech_draft"
    if data.get("task_kind") == "line_jargon_rewrite":
        skill_stage = "line_rewrite"
    if data.get("task_kind") == "shot_craft_rewrite":
        skill_stage = "shot_craft_rewrite"
    if data.get("task_kind") == "background_continuity_rewrite":
        skill_stage = "storyboard"
    if skill_stage == "storyboard" and legacy_storyboard:
        skill_stage = "legacy_storyboard"
    # Retired resource branches keep their protocol stages. Callers still run;
    # they load the skill that now owns the method.
    skill_stage = {
        "content_draft": "ad_script",
        "speech_draft": "ad_script",
        "legacy_storyboard": "storyboard",
        "script_review": "plan_review",
        "alignment_review": "plan_review",
    }.get(skill_stage, skill_stage)
    playbook_form = (resolve_playbook_form(data.get("ad_form"), structure_from_stage_data(data))
                     if "ad_form" in data else None)
    data["tagline_policy"] = tagline_policy()
    if playbook_form == "scene_story":
        # 「根据商品推荐」后来落成剧情时，先前记下的去向不再交给文案。
        data["cta_target"] = "none"
    data["ending_policy"] = ending_policy(playbook_form, data.get("cta_target"))
    bundle = (stage_skill_context(skill_stage, playbook_form=playbook_form) if playbook_form
              else stage_skill_context(skill_stage))
    data["stage_skills"] = bundle
    if playbook_form in ("brand_tvc", "scene_story", "presenter_pitch") and isinstance(data.get("speech_budget"), dict):
        profile = speech_profile(playbook_form)
        data["speech_budget"]["characters_per_second"] = profile.characters_per_second
        data["speech_budget"]["max_beat_characters"] = profile.max_beat_characters
        if isinstance(data.get("duration_seconds"), int):
            data["speech_budget"]["max_total_characters"] = data["duration_seconds"] * profile.characters_per_tenth // 10
    bundles = [bundle]
    if stage in ("ad_script", "script_review") and isinstance(data.get("creative_strategy"), dict):
        data["creative_strategy"].pop("selected_direction", None)
    data["output_style"] = "字段简洁完整；共享人物外观、商品形态和场所不逐段复述。动作状态只写理解接续所必需的变化；审核只给可定位结论，不重述整份方案。"
    for key in ("runtime_skill", "ad_expression_skill"):
        if isinstance(data.get(key), dict):
            data[key] = {k: v for k, v in data[key].items() if k != "content"}
    return bundles
