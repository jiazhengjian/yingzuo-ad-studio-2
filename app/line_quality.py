"""Spoken-line checks for ad copy. No model, network, or provider imports.

The script call can offer two hook lines and two closing lines. This module
picks one with local signals. A later rewrite runs only when a line still
reads like a planner note, and only those lines are sent back.
"""
from __future__ import annotations

from copy import deepcopy
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ad_form_playbook import resolve_playbook_form, speech_profile_for_strategy
from .beat_timing import script_capacity_budget
from .strategy import StrategyRecord


# Planner phrases that showed up in real voiceover. Ordinary words such as
# 看 and 用 are not on this list.
_JARGON = (
    ("关注", re.compile("关注")),
    ("呈现", re.compile("呈现")),
    ("兼顾", re.compile("兼顾")),
    ("打造", re.compile("打造")),
    ("采用技术", re.compile(r"采用.{0,12}技术")),
    ("高端质感", re.compile("高端质感")),
    ("赋能", re.compile("赋能")),
    ("满足你对", re.compile("满足你对")),
    ("纳入", re.compile("纳入")),
    ("体现", re.compile("体现")),
    ("卖点", re.compile("卖点")),
    ("主打", re.compile("主打")),
    ("高阶审美", re.compile("高阶审美")),
    ("产品质感", re.compile("产品质感")),
    ("正面侧面", re.compile("正面侧面")),
    ("设计细节", re.compile("设计细节")),
    ("颜色罗列", re.compile(
        r"(?:银|灰|紫|红|蓝|白|黑|金|绿|棕|粉){1,2}色?(?:与|和|、)"
        r"(?:银|灰|紫|红|蓝|白|黑|金|绿|棕|粉)")),
)
# Advertising-law absolutes. 最后 / 第一次 / 第一口 stay usable.
_ABSOLUTES = (
    ("根治", re.compile("根治")),
    ("国家级", re.compile("国家级")),
    ("世界级", re.compile("世界级")),
    ("绝对", re.compile("绝对")),
    ("永久", re.compile("永久")),
    ("万能", re.compile("万能")),
    ("治愈", re.compile("治愈")),
    ("最好", re.compile(r"最(?:好|强|佳|新|低|高|快|安全|有效|便宜|先进|专业)")),
    ("第一", re.compile(r"第一(?:品牌|名|位)|全网第一|销量第一")),
)
_PURCHASE = re.compile(r"购买|下单|优惠|限时|抢购")
_CLAIM_WORDS = ("根治", "治愈", "美白", "减肥", "抗衰", "医用", "治疗")
_LINE_STYLES = {
    "brand_tvc": "产品展示：少说，每句大约8到14字，感官、自信，指给观众看，不报参数。",
    "scene_story": "剧情：人物当场说的短对白，每轮大约4到12字，要有来有回，不像旁白总结。",
    "presenter_pitch": "人物讲解：对着镜头，第一句就是钩子，用你或设问，带一个具体好处。",
}
_TARGET_LENGTH = {
    "brand_tvc": (8, 14),
    "scene_story": (4, 12),
    "presenter_pitch": (8, 28),
}


class _RewriteLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beat_id: str = Field(min_length=1, max_length=80)
    narration: str = Field(min_length=1, max_length=80)


class _RewriteBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[_RewriteLine] = Field(min_length=1, max_length=12)


def line_style(form: str | None) -> str:
    return _LINE_STYLES.get(form or "", "说人话，不念策划说明，不报参数清单。")


def line_offenses(text: str, *, tagline_enabled: bool | None = None) -> list[str]:
    """Names of planner, spec-sheet, absolute, or purchase problems in one line.

    A spoken closer is allowed only when the tagline switch is on, and even
    then it cannot ask the viewer to buy. ``tagline_enabled`` stays in the
    signature so callers can pass the policy they already resolved; it does
    not relax this check.
    """
    _ = tagline_enabled
    if not isinstance(text, str) or not text.strip():
        return []
    found = [name for name, pattern in _JARGON if pattern.search(text)]
    found.extend(name for name, pattern in _ABSOLUTES if pattern.search(text))
    if _PURCHASE.search(text):
        found.append("购买引导")
    return found


def type_line_score(text: str, form: str | None, role: str) -> int:
    """Higher means the line sounds more like this ad type. Not a pass/fail."""
    if not isinstance(text, str) or not text.strip():
        return -100
    size = len(text)
    score = 0
    low, high = _TARGET_LENGTH.get(form or "", (0, 0))
    if low and low <= size <= high:
        score += 6
    elif form == "brand_tvc" and size > 24:
        score -= 4
    elif form == "scene_story" and size > 18:
        score -= 4
    if form == "brand_tvc" and re.search(r"[光声边肩雾盖圈]", text):
        score += 3
    elif form == "scene_story" and re.search(r"[你我嘿？！]", text):
        score += 4
    elif form == "presenter_pitch":
        if re.search(r"[你？]", text):
            score += 6
        if re.search(r"[看摸按放开折]", text):
            score += 3
        if role == "hook" and "？" in text:
            score += 2
    if re.search(r"[。？！]$", text):
        score += 2
    if re.search(r"[，,、：:；;]$", text):
        score -= 8
    score -= 20 * len(line_offenses(text))
    return score


def _strategy(value) -> StrategyRecord:
    return value if isinstance(value, StrategyRecord) else StrategyRecord.model_validate(value)


def _selected(strategy: StrategyRecord):
    return next(item for item in strategy.directions if item.id == strategy.selected_direction_id)


def script_form(strategy) -> str | None:
    strategy = _strategy(strategy)
    selected = _selected(strategy)
    structure = selected.film_concept.structure if selected.film_concept is not None else None
    form = resolve_playbook_form(strategy.brief.ad_form, structure)
    return form if form in _LINE_STYLES else None


def _locked(narration: str, locked_lines) -> bool:
    return any(isinstance(line, str) and line and line in narration for line in locked_lines or ())


def _overlap(line: str, idea: str) -> int:
    if not idea:
        return 0
    spoken = re.sub(r"[^\u4e00-\u9fff]", "", line)
    concept = re.sub(r"[^\u4e00-\u9fff]", "", idea)
    for size in range(min(8, len(concept)), 1, -1):
        for index in range(len(concept) - size + 1):
            if concept[index:index + size] in spoken:
                return size
    return 0


def _score_line(text, *, form, role, big_idea, beat_cap) -> int | None:
    if not isinstance(text, str) or not text.strip() or len(text) > beat_cap:
        return None
    if re.search(r"[，,、：:；;]\s*[”’」』\"']*\s*$", text):
        return None
    overlap = _overlap(text, big_idea)
    return type_line_score(text, form, role) + (2 * overlap if overlap >= 2 else 0)


def key_line_options_schema(max_characters: int) -> dict:
    """Optional hook and closing alternates. The stored beat keeps one line."""
    line = {"type": "string", "minLength": 1, "maxLength": max_characters}
    choice = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "beat_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "options": {"type": "array", "minItems": 2, "maxItems": 2, "items": line},
        },
        "required": ["beat_id", "options"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "description": "可选。开场有声句和收束有声句各再给两句可说的候选，程序只留一句，不替代 beats 里已经写下的句子。",
        "properties": {"hook": choice, "closing": choice},
    }


def speech_option_schema(max_characters: int) -> dict:
    return {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "items": {"type": "string", "minLength": 1, "maxLength": max_characters},
        "description": "可选。这一句另外两句可说的候选，程序只留一句。",
    }


def _beats(script: dict) -> list[dict]:
    beats = script.get("beats")
    return beats if isinstance(beats, list) else []


def _within_total(script, beat_id, narration, duration, tenth) -> bool:
    if type(duration) is not int:
        return True
    total = 0
    for beat in _beats(script):
        text = narration if beat.get("id") == beat_id else beat.get("narration") or ""
        total += len(text) if isinstance(text, str) else 0
    return total <= duration * tenth // 10


def prepare_script_lines(script: dict, strategy, duration_seconds: int | None = None,
                         locked_lines=()) -> dict:
    """Keep one hook line and one closing line when the same call offered two."""
    if not isinstance(script, dict):
        raise ValueError("脚本须包含完整内容节拍列表")
    raw = deepcopy(script)
    options = raw.pop("key_line_options", None)
    if not isinstance(options, dict):
        return raw
    strategy = _strategy(strategy)
    profile = speech_profile_for_strategy(strategy)
    form = script_form(strategy)
    selected = _selected(strategy)
    big_idea = selected.product_expression or selected.big_idea or ""
    roles = []
    spoken = [beat for beat in _beats(raw) if isinstance(beat.get("narration"), str) and beat["narration"].strip()]
    if spoken:
        roles.append(("hook", spoken[0]["id"]))
        if spoken[-1]["id"] != spoken[0]["id"]:
            roles.append(("closing", spoken[-1]["id"]))
    allowed = {role: beat_id for role, beat_id in roles}
    for role, choice in options.items():
        if role not in allowed or not isinstance(choice, dict):
            continue
        beat_id = choice.get("beat_id")
        if beat_id != allowed[role]:
            continue
        beat = next((item for item in _beats(raw) if item.get("id") == beat_id), None)
        if beat is None or _locked(beat.get("narration") or "", locked_lines):
            continue
        candidates = [beat.get("narration") or ""]
        extra = choice.get("options")
        if isinstance(extra, list):
            candidates.extend(extra[:2])
        best_text, best_score = beat.get("narration") or "", None
        for text in candidates:
            if not _within_total(raw, beat_id, text, duration_seconds, profile.characters_per_tenth):
                continue
            score = _score_line(text, form=form, role=role, big_idea=big_idea,
                                beat_cap=profile.max_beat_characters)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best_text, best_score = text, score
        if best_text and best_text != beat.get("narration"):
            beat["narration"] = best_text
    return raw


def prepare_speech_draft(raw: dict, strategy, duration_seconds: int, locked_lines=()) -> dict:
    """Pick a hook sentence and a closing sentence inside one voiceover draft."""
    if not isinstance(raw, dict):
        return raw
    draft = deepcopy(raw)
    narration = draft.get("narration")
    hook_options = draft.pop("hook_options", None)
    closing_options = draft.pop("closing_options", None)
    if not isinstance(narration, str):
        return draft
    profile = speech_profile_for_strategy(strategy)
    limit = duration_seconds * profile.characters_per_tenth // 10
    form = script_form(strategy)
    strategy = _strategy(strategy)
    selected = _selected(strategy)
    big_idea = selected.product_expression or selected.big_idea or ""
    parts = [part for part in re.split(r"(?<=[。？！])", narration) if part]
    if not parts:
        return draft

    def choose(index, options, role):
        current = parts[index]
        if _locked(current, locked_lines) or not isinstance(options, list):
            return current
        best, best_score = current, _score_line(current, form=form, role=role, big_idea=big_idea,
                                                 beat_cap=profile.max_beat_characters)
        for text in options[:2]:
            score = _score_line(text, form=form, role=role, big_idea=big_idea,
                                beat_cap=profile.max_beat_characters)
            if score is None:
                continue
            trial = "".join(text if slot == index else part for slot, part in enumerate(parts))
            if len(trial) > limit:
                continue
            if best_score is None or score > best_score:
                best, best_score = text, score
        return best

    if len(parts) == 1:
        options = []
        if isinstance(hook_options, list):
            options.extend(hook_options[:2])
        if isinstance(closing_options, list):
            options.extend(closing_options[:2])
        parts[0] = choose(0, options, "hook")
    else:
        parts[0] = choose(0, hook_options, "hook")
        parts[-1] = choose(len(parts) - 1, closing_options, "closing")
    draft["narration"] = "".join(parts)
    return draft


def offending_lines(script: dict, *, tagline_enabled: bool | None = None, locked_lines=()) -> list[dict]:
    beats = []
    for index, beat in enumerate(_beats(script)):
        narration = beat.get("narration") if isinstance(beat, dict) else None
        if not isinstance(narration, str) or not narration.strip() or _locked(narration, locked_lines):
            continue
        offenses = line_offenses(narration, tagline_enabled=tagline_enabled)
        if offenses:
            beats.append({"beat_id": beat.get("id"), "index": index, "narration": narration,
                          "offenses": offenses})
    return beats


def narration_budget_ok(script: dict, strategy, duration_seconds: int) -> bool:
    profile = speech_profile_for_strategy(strategy)
    narrations = [beat.get("narration") or "" for beat in _beats(script)]
    if any(not isinstance(text, str) or len(text) > profile.max_beat_characters for text in narrations):
        return False
    return sum(len(text) for text in narrations) <= duration_seconds * profile.characters_per_tenth // 10


def rewrite_schema(lines: list[dict], beat_cap: int) -> dict:
    schema = _RewriteBatch.model_json_schema()
    line = schema.pop("$defs")["_RewriteLine"]
    items = []
    for item in lines:
        one = deepcopy(line)
        one["properties"]["beat_id"]["const"] = item["beat_id"]
        one["properties"]["narration"]["maxLength"] = min(beat_cap, item["max_characters"])
        one["properties"]["narration"]["description"] = f"只重写这一句，含标点最多{one['properties']['narration']['maxLength']}字"
        items.append(one)
    schema["properties"]["lines"].update(minItems=len(items), maxItems=len(items), items={"oneOf": items})
    return schema


def apply_line_rewrite(script: dict, raw, *, limits: dict, locked_lines=(), facts_text: str = "",
                       duration_seconds: int | None = None, strategy=None) -> dict | None:
    """Accept a full set of cleaner lines, or nothing."""
    try:
        batch = _RewriteBatch.model_validate(raw)
    except (ValidationError, TypeError):
        return None
    expected = set(limits)
    got = [line.beat_id for line in batch.lines]
    if len(got) != len(set(got)) or set(got) != expected:
        return None
    revised = deepcopy(script)
    by_id = {beat.get("id"): beat for beat in _beats(revised)}
    for line in batch.lines:
        beat = by_id.get(line.beat_id)
        if beat is None or len(line.narration) > limits[line.beat_id]:
            return None
        if line_offenses(line.narration):
            return None
        source = (beat.get("narration") or "") + facts_text
        if any(word in line.narration and word not in source for word in _CLAIM_WORDS):
            return None
        if set(re.findall(r"\d+", line.narration)) - set(re.findall(r"\d+", source)):
            return None
        beat["narration"] = line.narration
    narration = "".join(beat.get("narration") or "" for beat in _beats(revised))
    if any(isinstance(item, str) and item and item not in narration for item in locked_lines or ()):
        return None
    if strategy is not None and type(duration_seconds) is int:
        if not narration_budget_ok(revised, strategy, duration_seconds):
            return None
        if any(isinstance(beat, dict) and beat.get("timing") for beat in _beats(revised)):
            profile = speech_profile_for_strategy(strategy)
            before = script_capacity_budget(script, duration_seconds, profile.characters_per_tenth)
            after = script_capacity_budget(revised, duration_seconds, profile.characters_per_tenth)
            if after["overrun_seconds"] > before["overrun_seconds"]:
                return None
    return revised


LINE_REWRITE_SYSTEM = """你只重写列出的中文广告台词，只输出 schema 的 lines。
这些句子现在像策划说明。按 line_style 改成能说出口的话：产品展示短、有感官；剧情是人物的来回；人物讲解对着镜头，钩子在第一句，带一个具体好处。
承接 big_idea 和 hook 的意思，不要照抄策划词。只使用该句和 facts 里已经有的商品信息，不新增功效、数字、体验或比较。
不写关注、呈现、兼顾、打造、采用某技术、高端质感、赋能、满足你对、卖点，也不写最、第一、根治、国家级、绝对、永久。
每句不超过该句 max_characters，标点也算。用户固定台词原样留在句子里。
tagline_policy.enabled 为 false 时不加口号和购买引导。为 true 时只有收束句可以是不超过10字的口播，仍然不写购买引导。
不输出其他字段，不解释。"""


def _rewrite_payload(script, offenses, strategy, duration, locked_lines):
    strategy = _strategy(strategy)
    profile = speech_profile_for_strategy(strategy)
    form = script_form(strategy)
    selected = _selected(strategy)
    spoken = [beat for beat in _beats(script) if isinstance(beat.get("narration"), str) and beat["narration"].strip()]
    roles = {}
    if spoken:
        roles[spoken[0].get("id")] = "hook"
        roles[spoken[-1].get("id")] = "closing" if spoken[-1].get("id") != spoken[0].get("id") else "hook"
    fact_ids = set()
    lines = []
    for item in offenses:
        beat = next(beat for beat in _beats(script) if beat.get("id") == item["beat_id"])
        fact_ids.update(beat.get("evidence_ids") or [])
        remaining = duration * profile.characters_per_tenth // 10 - sum(
            len(other.get("narration") or "") for other in _beats(script)
            if other.get("id") != item["beat_id"])
        lines.append({
            "beat_id": item["beat_id"],
            "role": roles.get(item["beat_id"], "line"),
            "narration": item["narration"],
            "offenses": item["offenses"],
            "max_characters": max(1, min(profile.max_beat_characters, remaining)),
        })
    facts = [{"id": fact.id, "text": fact.text} for fact in strategy.brief.facts if fact.id in fact_ids]
    return {
        "stage": "ad_script",
        "task_kind": "line_jargon_rewrite",
        "schema": rewrite_schema(lines, profile.max_beat_characters),
        "ad_form": form,
        "line_style": line_style(form),
        "duration_seconds": duration,
        "speech_budget": {
            "characters_per_second": profile.characters_per_second,
            "max_beat_characters": profile.max_beat_characters,
            "max_total_characters": duration * profile.characters_per_tenth // 10,
        },
        "big_idea": selected.product_expression or selected.big_idea or "",
        "hook": selected.hook,
        "facts": facts,
        "locked_lines": list(locked_lines or ()),
        "lines": lines,
    }, {item["beat_id"]: item["max_characters"] for item in lines}, "".join(fact["text"] for fact in facts)


async def rewrite_script_if_needed(stage_call, script, strategy, duration_seconds, locked_lines=()):
    """Return (called, script). Clean scripts do not call the model."""
    data = script.model_dump() if hasattr(script, "model_dump") else deepcopy(script)
    offenses = offending_lines(data, locked_lines=locked_lines)
    if not offenses:
        return False, script
    payload, limits, facts_text = _rewrite_payload(data, offenses, strategy, duration_seconds, locked_lines)
    raw = await stage_call(LINE_REWRITE_SYSTEM, payload, [], 1200)
    revised = apply_line_rewrite(data, raw, limits=limits, locked_lines=locked_lines, facts_text=facts_text,
                                 duration_seconds=duration_seconds, strategy=strategy)
    return True, revised if revised is not None else script


async def rewrite_director_if_needed(stage_call, director: dict, strategy, duration_seconds, locked_lines=()):
    """Return (called, replacement). No call when every spoken line is clean."""
    offenses = offending_lines(director.get("ad_script") or {}, locked_lines=locked_lines)
    if not offenses:
        return False, None
    payload, limits, facts_text = _rewrite_payload(
        director["ad_script"], offenses, strategy, duration_seconds, locked_lines)
    raw = await stage_call(LINE_REWRITE_SYSTEM, payload, [], 1200)
    revised = apply_line_rewrite(director["ad_script"], raw, limits=limits, locked_lines=locked_lines,
                                 facts_text=facts_text, duration_seconds=duration_seconds, strategy=strategy)
    if revised is None:
        return True, None
    replacement = deepcopy(director)
    replacement["ad_script"] = revised
    return True, replacement
