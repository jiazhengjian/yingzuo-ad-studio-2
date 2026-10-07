"""Compile a scene request from the confirmed plan, without choosing a new setting."""
from __future__ import annotations

import json
import re
from .picture_rules import off_camera_locked
from .production import CHARACTER_SHEET_TEMPLATE, apply_cast_instructions, cast_appearance_clause
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
    lighting = visual_direction.get("lighting") if isinstance(visual_direction.get("lighting"), str) else ""
    if lighting.strip():
        instructions += f"\n光线方向按已确认方案执行：{lighting.strip()}。窗户、窗帘和光斑要落在这个方向，不要改到相反的一侧。"
    return {"version": "scene-context-v4" if isinstance(intent, dict) else "scene-context-v3", "scenario": scenario, "skill_trace": [trace],
            "prompt": instructions + "\n已确认的场景与视觉方案：\n" + json.dumps(context, ensure_ascii=False)}


def _product_terms(plan):
    terms = []
    if not isinstance(plan, dict):
        return terms
    facts = plan.get("facts") if isinstance(plan.get("facts"), dict) else {}
    name = facts.get("name") if isinstance(facts.get("name"), str) else ""
    if name.strip():
        terms.append(name.strip())
    brief = ((plan.get("strategy") or {}).get("brief") or {}) if isinstance(plan.get("strategy"), dict) else {}
    product = brief.get("product_name") if isinstance(brief, dict) and isinstance(brief.get("product_name"), str) else ""
    if product.strip() and product.strip() not in terms:
        terms.append(product.strip())
    return terms


_WEARABLE_NOUNS = ("头戴式耳机", "耳机", "耳塞", "耳麦", "手表", "眼镜", "墨镜")
_BARE_WEAR = re.compile(r"佩戴|戴着|戴上|头戴|(?<![摘取])戴")
_FEMALE_LOOK = re.compile(r"(?<![其])她(?!们)|女生|女性|女孩|女士|女人")
_MALE_LOOK = re.compile(r"(?<![其把让给向为被])他(?!们)|男生|男性|男孩|男士|男人")


def _expanded_product_terms(terms):
    """The product name plus the wearable noun inside it, so 降噪耳机 still matches 耳机."""
    expanded = []
    for term in terms or []:
        if not isinstance(term, str) or not term.strip():
            continue
        text = term.strip()
        if text not in expanded:
            expanded.append(text)
        for noun in _WEARABLE_NOUNS:
            if noun in text and noun not in expanded:
                expanded.append(noun)
    return expanded


def _clause_draws_product(clause, terms):
    """Identity sheets leave the product off the body. Clothes stay.

    Bare 「戴银色头戴式耳机」counts. A hat or coat that is not the product stays.
    """
    if not clause:
        return False
    expanded = _expanded_product_terms(terms)
    if _HANDHELD_CLAUSE.search(clause):
        if not expanded or any(term in clause for term in expanded):
            return True
        return False
    if not _BARE_WEAR.search(clause) and not _HOLD_OR_WEAR.search(clause):
        return False
    if not _BARE_WEAR.search(clause):
        return False
    if "全程" in clause:
        return True
    return any(term in clause for term in expanded)


def _strip_product_clauses(description, terms):
    description = (description or "").strip()
    if not description:
        return description
    kept = []
    for clause in re.split(r"[，。；]", description):
        clause = clause.strip()
        if not clause or _clause_draws_product(clause, terms):
            continue
        kept.append(clause)
    return "，".join(kept)


def character_sheet_description(description, plan):
    """Prompt copy with the product taken off the body. Clothes stay."""
    return _strip_product_clauses(description, _product_terms(plan))


def _shot_blob_for(plan, person):
    from .story_logic import iter_plan_shots
    own = person.get("id") if isinstance(person, dict) else None
    name = person.get("name") if isinstance(person, dict) and isinstance(person.get("name"), str) else ""
    parts = []
    for shot in iter_plan_shots(plan):
        ids = shot.get("character_ids") if isinstance(shot.get("character_ids"), list) else []
        blob = shot.get("description") if isinstance(shot.get("description"), str) else ""
        if own and ids and own not in ids and (not name or name not in blob):
            continue
        parts.append(blob)
        continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
        for key in ("start_state", "end_state", "allowed_changes"):
            value = continuity.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def character_gender(plan, person):
    """female or male, from the shots that show this person. The cast line is only a fallback."""
    shots = _shot_blob_for(plan, person if isinstance(person, dict) else {})
    description = person.get("description") if isinstance(person, dict) and isinstance(person.get("description"), str) else ""
    female_shots = len(_FEMALE_LOOK.findall(shots))
    male_shots = len(_MALE_LOOK.findall(shots))
    if female_shots or male_shots:
        if female_shots >= male_shots and female_shots:
            return "female"
        if male_shots:
            return "male"
    if _FEMALE_LOOK.search(description):
        return "female"
    if _MALE_LOOK.search(description):
        return "male"
    return ""


def apply_character_gender(description, gender):
    """Lock the stored line to the gender the shots already use."""
    description = (description or "").strip()
    if gender == "female":
        description = re.sub(r"男性|男生|男人|男孩|男士", "女性", description)
        description = re.sub(r"(?<![其把让给向为被])他(?!们)", "她", description)
        if description and not _FEMALE_LOOK.search(description):
            description = "女性，" + description
        elif not description:
            description = "女性"
    elif gender == "male":
        description = re.sub(r"女性|女生|女人|女孩|女士", "男性", description)
        description = re.sub(r"(?<![其])她(?!们)", "他", description)
        if description and not _MALE_LOOK.search(description):
            description = "男性，" + description
        elif not description:
            description = "男性"
    return description


def cast_look_lock(plan, person):
    """One sentence for the image prompt. Shot text wins over an unstated gender."""
    gender = character_gender(plan, person)
    if gender == "female":
        return "这个人是女性，东亚女性面孔，按镜头里的「她」来画，不要画成男性。"
    if gender == "male":
        return "这个人是男性，东亚男性面孔，按镜头里的「他」来画，不要画成女性。"
    return ""


def rewrite_character_description(person, plan):
    """Drop the product from a stored cast line.

    Gender stays in the image prompt via cast_look_lock. The stored line is
    rewritten only when it names the opposite gender from the shots, or when
    it puts the product on the body. Clothes that are not the product stay.
    """
    if not isinstance(person, dict):
        return False
    description = person.get("description") if isinstance(person.get("description"), str) else ""
    cleaned = _strip_product_clauses(description, _product_terms(plan))
    gender = character_gender(plan, person)
    if gender == "female" and _MALE_LOOK.search(cleaned) and not _FEMALE_LOOK.search(cleaned):
        cleaned = apply_character_gender(cleaned, "female")
    elif gender == "male" and _FEMALE_LOOK.search(cleaned) and not _MALE_LOOK.search(cleaned):
        cleaned = apply_character_gender(cleaned, "male")
    if not cleaned:
        cleaned = description.strip()
    if cleaned == description:
        return False
    person["description"] = cleaned[:800]
    return True


def settle_cast_appearance(plan):
    """Rewrite every stored character on this plan. Idempotent."""
    if not isinstance(plan, dict):
        return False
    changed = False
    production = plan.get("production") if isinstance(plan.get("production"), dict) else {}
    for person in production.get("characters") or []:
        if rewrite_character_description(person, plan):
            changed = True
    strategy = plan.get("strategy") if isinstance(plan.get("strategy"), dict) else {}
    for direction in strategy.get("directions") or []:
        if not isinstance(direction, dict):
            continue
        nested = direction.get("production") if isinstance(direction.get("production"), dict) else {}
        for person in nested.get("characters") or []:
            if rewrite_character_description(person, plan):
                changed = True
    return changed


def strip_product_from_cast(description, *terms):
    """Ingress helper. Product name compounds are enough; shots are not required."""
    return _strip_product_clauses(description, [term for term in terms if isinstance(term, str)])


def character_sheet_product_notes(plan):
    """Soft note when a sheet would wear the product the whole film but a shot holds it."""
    if not isinstance(plan, dict):
        return []
    from .story_logic import iter_plan_shots
    people = [person for person in ((plan.get("production") or {}).get("characters") or []) if isinstance(person, dict)]
    shots = list(iter_plan_shots(plan))
    for person in people:
        description = person.get("description") if isinstance(person.get("description"), str) else ""
        match = _THROUGHOUT_WEAR.search(description)
        if not match:
            continue
        worn = match.group(1)
        tokens = [run for run in re.findall(r"[\u4e00-\u9fff]{2,}", worn)]
        extras = [run[-2:] for run in tokens if len(run) > 2]
        tokens.extend(item for item in extras if item not in tokens)
        own = person.get("id")
        name = person.get("name") if isinstance(person.get("name"), str) else ""
        for shot in shots:
            ids = shot.get("character_ids") if isinstance(shot.get("character_ids"), list) else []
            blob = shot.get("description") if isinstance(shot.get("description"), str) else ""
            if own and ids and own not in ids and (not name or name not in blob):
                continue
            if not tokens or not _HANDHELD_CLAUSE.search(blob):
                continue
            if not any(token in blob for token in tokens):
                continue
            return [_PRODUCT_SHEET_NOTE]
    return []


def character_design_request(project, plan, character):
    """Compile one horizontal identity sheet for one approved, identifiable character."""
    approved = (plan.get("production") or {}).get("characters", [])
    if not isinstance(character, dict) or character not in approved:
        raise ValueError("角色四视图必须引用本次已确认方案中的角色")
    instructions, trace = compiler_template("ads-video-asset-prompts", "references/character-template.txt")
    rewrite_character_description(character, plan)
    instructions = apply_cast_instructions(instructions, character.get("description", ""))
    from .story_logic import cast_distinction_line
    distinction = cast_distinction_line(plan, character)
    if distinction:
        instructions += "\n" + distinction
    look = cast_look_lock(plan, character)
    if look:
        instructions += "\n" + look
    instructions += "\n" + _SHEET_NO_PRODUCT
    context = {"name": character["name"], "description": character_sheet_description(character.get("description", ""), plan)}
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
    """Characters who could own a person-state sheet. Empty when the film is locked off-camera."""
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


_PRODUCT_STATE_SUBJECTS = frozenset({"盖子", "瓶盖", "杯", "门", "商品"})


_BUN = re.compile(r"丸子头|丸子|盘发|发髻")
_HAIR_SHOULDER = re.compile(r"落回肩|落到肩|落在肩|肩侧|及肩|齐肩|披肩|中长发")
_HAIR_SHORT = re.compile(r"短发|寸头|齐耳")
_HAIR_PONY = re.compile(r"马尾")


def _hair_source_text(plan, person):
    from .story_logic import iter_plan_shots
    parts = [person.get("description") or ""] if isinstance(person, dict) else []
    own = person.get("id") if isinstance(person, dict) else None
    for shot in iter_plan_shots(plan):
        ids = shot.get("character_ids") if isinstance(shot.get("character_ids"), list) else []
        if own and ids and own not in ids:
            continue
        parts.append(shot.get("description") or "")
        continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
        parts.extend(continuity.get(key) or "" for key in ("start_state", "end_state") if isinstance(continuity.get(key), str))
    return " ".join(part for part in parts if isinstance(part, str))


def _hairstyle_line(text):
    if _HAIR_SHOULDER.search(text or ""):
        return "头发自然披到肩膀，不要丸子头，不要盘发。"
    if _HAIR_PONY.search(text or ""):
        return "头发扎成马尾，不要改成披肩或丸子头。"
    if _HAIR_SHORT.search(text or ""):
        return "头发是短发，不要画成长发或丸子头。"
    return ""


def state_sheet_person(project, plan, instruction):
    """The one person this state sheet copies. None for an object, or when nobody is on camera."""
    instruction = instruction or {}
    if (instruction.get("subject") or "") in _PRODUCT_STATE_SUBJECTS:
        return None
    people = state_context_characters(project, plan, instruction)
    if not people:
        return None
    wanted = instruction.get("character_id")
    if isinstance(wanted, str) and wanted:
        chosen = next((person for person in people if person.get("id") == wanted), None)
        if chosen:
            return chosen
    production = plan.get("production") if isinstance(plan, dict) and isinstance(plan.get("production"), dict) else {}
    speaker_id = production.get("speaker_id")
    shot = _plan_shot(plan, instruction.get("shot_id")) or {}
    ids = shot.get("character_ids") if isinstance(shot, dict) else None
    if isinstance(speaker_id, str) and speaker_id and isinstance(ids, list) and speaker_id in ids:
        chosen = next((person for person in people if person.get("id") == speaker_id), None)
        if chosen:
            return chosen
    return people[0]


def _state_phase_line(phase):
    if phase == "start":
        return "这张停在过程开始时的样子，不要画成已经做完。"
    return "这张是切到结果之后的终点，要画成这一变化真正做完的样子，不要画成做到一半。"


_EMPTY_HANDS = "双手空手，不要商品，不要道具，背景只用白或浅灰。"
_SHEET_NO_PRODUCT = "双手空手，不要商品，不要道具。商品只按每镜的商品参考出现，不要画在身上或手里。"
_HOLD_OR_WEAR = re.compile(r"手里|手上|手中|拿着|握着|举着|捧着|佩戴|戴着|戴上|头戴|全程")
_HANDHELD_CLAUSE = re.compile(r"手里|手上|手中|拿着|握着|举着|捧着")
_THROUGHOUT_WEAR = re.compile(r"全程[^，。；]{0,8}(?:佩戴|戴着|戴上|头戴)([^，。；]{1,24})")
_PRODUCT_SHEET_NOTE = "角色设定图不画商品。全程佩戴和镜头里拿在手里对不上，已记下，不因此停下。"
_APPEARANCE_LEAD = "外观原文照抄，脸、五官、肤色、身材、头发长度和发色、衣服、鞋子都按这句："
_LATER_WEAR = re.compile(r"(?:出门前|稍后|之后|离开前|出门时|走之前).{0,16}(?:披上|穿上|套上)")
_WORN_NOW = re.compile(r"披上|穿上|套上|穿着|披着")
_HANDHELD = re.compile(r"手里|手上拿|拿着")
_SAME_OUTFIT = "衣服、颜色和鞋子与起始图保持一致，不要换颜色，不要改成另一双鞋，也不要凭空加鞋。只改声明的这一处状态。"
_SHEET_CLOTHES_NOTE = "状态图只画这一镜身上的衣服，稍后才披的没有提前画上。"


def _neutral_description(description, phrases):
    """Drop clauses that repeat a declared state phrase. The face and clothes stay."""
    description = (description or "").strip()
    if not description or not phrases:
        return description
    kept = []
    for clause in re.split(r"[，。；、]", description):
        clause = clause.strip()
        if not clause:
            continue
        if any(isinstance(phrase, str) and phrase and phrase in clause for phrase in phrases):
            continue
        kept.append(clause)
    return "，".join(kept)


def _shots_through(plan, shot_id):
    from .story_logic import iter_plan_shots
    found = []
    for shot in iter_plan_shots(plan):
        found.append(shot)
        if shot.get("id") == shot_id:
            return found, True
    return found, False


def _worn_by(shots):
    from .story_logic import _GARMENTS
    worn = set()
    for shot in shots:
        blob = shot.get("description") if isinstance(shot.get("description"), str) else ""
        for garment in _GARMENTS:
            if garment not in blob:
                continue
            for clause in re.split(r"[，。；]", blob):
                if garment in clause and _WORN_NOW.search(clause) and not _LATER_WEAR.search(clause):
                    worn.add(garment)
    return worn


def _sheet_appearance(description, plan, shot_id):
    """Clothes and empty hands for this sheet's own shot. Later layers stay off."""
    description = (description or "").strip()
    if not description or not isinstance(shot_id, str) or not shot_id:
        return description
    shots, found = _shots_through(plan, shot_id)
    if not found:
        shots = []
    from .story_logic import _GARMENTS
    worn = _worn_by(shots)
    kept = []
    for clause in re.split(r"[，。；]", description):
        clause = clause.strip()
        if not clause or _HANDHELD.search(clause):
            continue
        garments = [item for item in _GARMENTS if item in clause]
        if _LATER_WEAR.search(clause) and garments and any(item not in worn for item in garments):
            continue
        kept.append(clause)
    return "，".join(kept)


def state_sheet_clothing_notes(plan):
    """Soft note when a state sheet drops a garment that is put on later."""
    if not isinstance(plan, dict):
        return []
    people = {person.get("id"): person for person in ((plan.get("production") or {}).get("characters") or [])
              if isinstance(person, dict)}
    for change in plan.get("state_changes") or []:
        if not isinstance(change, dict):
            continue
        person = people.get(change.get("owner"))
        raw = person.get("description") if isinstance(person, dict) else ""
        if not isinstance(raw, str) or not raw:
            continue
        for shot_id in (change.get("start_shot"), change.get("result_shot")):
            filtered = _sheet_appearance(raw, plan, shot_id)
            for clause in re.split(r"[，。；]", raw):
                clause = clause.strip()
                if clause and _LATER_WEAR.search(clause) and clause not in filtered:
                    return [_SHEET_CLOTHES_NOTE]
    return []


def _appearance_line(description):
    """The same face, body, outfit and shoes sentence for the start sheet and the result edit."""
    description = (description or "").strip()
    return _APPEARANCE_LEAD + cast_appearance_clause(description) + description


def state_design_request(project, plan, instruction):
    """One state, drawn like a reference sheet, not a frame from the film.

    The start sheet is that person's identity: four views, a clean background,
    and no other image. The result sheet is conditioned only on that start
    sheet. An object, or an off-camera film, stays the product on a clean
    background. The scene image stays with the video, where it still owns the room.
    """
    instruction = instruction or {}
    plan = plan if isinstance(plan, dict) else {}
    phase = instruction.get("phase")
    label = instruction.get("label") or "这一状态"
    subject = instruction.get("subject") or "主体"
    hidden = off_camera_locked(_state_brief_text(project, plan), (plan.get("facts") or {}).get("name") or "")
    phase_name = "起始" if phase == "start" else "结果"
    person = state_sheet_person(project, plan, instruction)
    state_text = instruction.get("state_text") if isinstance(instruction.get("state_text"), str) and instruction.get("state_text").strip() else label
    common = {
        "version": "state-sheet-v1",
        "state_id": instruction.get("state_id"),
        "phase": phase,
        "shot_id": instruction.get("shot_id"),
        "state_label": label,
        "aspect_ratio": "16:9",
    }
    if isinstance(instruction.get("state_change_id"), str) and instruction.get("state_change_id"):
        common["state_change_id"] = instruction["state_change_id"]
    if person is None:
        prompt = (
            f"生成一张商品状态参考，只表现「{subject}」的{phase_name}状态：{label}。状态原文：{state_text}。"
            "干净的白或浅灰无缝背景，同一件商品，只有这一处状态不同。"
            "不要房间，不要人物，不要场景道具，不要把商品放进故事画面，不要前后对比拼图，不要可读文字。"
            "商品外形、颜色和标识以商品原图为准，不重画商标。"
            "参考图只有商品原图，不要场景图，不要角色图。"
            + _state_phase_line(phase)
        )
        if hidden:
            prompt += "本片不出镜：不要人脸，不要把独立发束或局部画成真人头。"
        return {**common, "reference_kinds": ["product"], "prompt": prompt}
    rewrite_character_description(person, plan)
    description = _sheet_appearance(_neutral_description(
        _BUN.sub("", person.get("description") or "").strip("，,、 \n"),
        instruction.get("appearance_phrases") or []), plan, instruction.get("shot_id"))
    description = _strip_product_clauses(description, _product_terms(plan))
    hair = _hairstyle_line(_hair_source_text(plan, person))
    instructions, trace = compiler_template("ads-video-asset-prompts", "references/character-template.txt")
    instructions = apply_cast_instructions(instructions, description)
    look = cast_look_lock(plan, person)
    if look:
        instructions += "\n" + look
    appearance = _appearance_line(description)
    if phase == "start":
        instructions += (
            f"\n这张是同一人的起始状态四视图，也是这个人的身份参考。只表现这一处起始状态：{label}。状态原文：{state_text}。"
            + (hair + "\n" if hair else "\n")
            + appearance + "。"
            + _EMPTY_HANDS
            + "不要房间，不要其他人物，不要场景里的道具，不要商品，不要场景图。"
            "不要另画一张普通角色四视图。版式不进入成片。"
            + _state_phase_line(phase)
        )
        kinds = []
        identity_edit = False
    else:
        instructions += (
            f"\n这是对起始状态四视图的改图，不是重画一个人。唯一主参考就是这张起始图，只把声明的状态改成：{label}。状态原文：{state_text}。"
            + (hair + "\n" if hair else "\n")
            + appearance + "。"
            + "脸、五官、肤色、身材、头发长度和发色、衣服、鞋子都锁在起始图上，只有这一处状态可以不同。"
            + _SAME_OUTFIT
            + _EMPTY_HANDS
            + "不要房间，不要其他人物，不要场景里的道具，不要商品。"
            "参考图只有这一位的起始状态四视图，不要场景图，不要商品原图，不要另一张普通角色四视图。"
            + _state_phase_line(phase)
        )
        kinds = ["state_start"]
        identity_edit = True
    context = {"name": person.get("name") or "角色", "description": description,
               "state": label, "phase": phase_name}
    return {**common, "character_id": person.get("id"), "sheet_layout": "horizontal_16x9",
            "template_version": CHARACTER_SHEET_TEMPLATE, "reference_kinds": kinds,
            "identity_edit": identity_edit,
            "skill_trace": [trace], "prompt": instructions + "\n已确认角色与这一处状态：\n"
            + json.dumps(context, ensure_ascii=False)}
