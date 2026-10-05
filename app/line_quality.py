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
_HARD_PURCHASE = re.compile(r"购买|下单|抢购")
_PROMO_WORDS = ("优惠", "限时", "活动")
_SOFT_CTA = re.compile(r"点下面|下方链接|链接放|去看看|小黄车")
_OPENING_DEICTIC = re.compile(r"^(?:就这|这么|这|那|它|转)")
_CLAIM_WORDS = ("根治", "治愈", "美白", "减肥", "抗衰", "医用", "治疗")
_LINE_STYLES = {
    "brand_tvc": "产品展示：短句直接说卖点、好处或感受，感官、自信，不必每句点名。不要描述镜头里的光、银线和发丝。数字只在资料里已经有时才写。",
    "scene_story": "剧情：人物当场说的短对白，要有来有回。跟着当时的处境，不套口播主语。全片一种人称，不复述看得见的动作。好处至少自然出现一次。",
    "presenter_pitch": "人物讲解：对着镜头，第一句就是钩子。每一句卖点都要有主语，用这款、它，或我用它。卖点之间用而且、还、另外、所以或关键是接上。",
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


_CATEGORY_NOUNS = (
    "吹风机", "头发", "瓶", "包", "袋子", "蛋糕", "杯子", "耳机", "手机", "鞋", "面霜", "口红", "风筒",
)


def product_terms(strategy) -> list[str] | None:
    """Product name and category words already in the brief. None when unknown."""
    if strategy is None:
        return None
    try:
        record = _strategy(strategy)
    except (ValidationError, TypeError, ValueError, KeyError):
        return None
    name = (record.brief.product_name or "").strip()
    blob = name + "".join(fact.text for fact in record.brief.facts)
    terms = []
    if len(name) >= 2:
        terms.append(name)
        for size in range(2, min(6, len(name)) + 1):
            for index in range(0, len(name) - size + 1):
                terms.append(name[index:index + size])
    for word in _CATEGORY_NOUNS:
        if word in blob:
            terms.append(word)
    return list(dict.fromkeys(term for term in terms if term))


def _opening_unclear(text: str, product_terms_list: list[str] | None) -> bool:
    if product_terms_list is None:
        return False
    stripped = text.lstrip(" \t“\"'「『")
    if not _OPENING_DEICTIC.match(stripped):
        return False
    return not any(term and term in text for term in product_terms_list)


def line_offenses(text: str, *, tagline_enabled: bool | None = None, role: str | None = None,
                  product_terms_list: list[str] | None = None, facts_text: str = "") -> list[str]:
    """Names of planner, spec-sheet, absolute, or purchase problems in one line.

    A spoken closer is allowed only when the tagline switch is on, and even
    then it cannot ask the viewer to buy. ``tagline_enabled`` stays in the
    signature so callers can pass the policy they already resolved; it does
    not relax this check. A closing line may point at the link. Promo words
    are allowed only when the same words are already in a confirmed fact.
    """
    _ = tagline_enabled
    if not isinstance(text, str) or not text.strip():
        return []
    found = [name for name, pattern in _JARGON if pattern.search(text)]
    found.extend(name for name, pattern in _ABSOLUTES if pattern.search(text))
    if _HARD_PURCHASE.search(text):
        found.append("购买引导")
    else:
        promo = [word for word in _PROMO_WORDS if word in text]
        if promo and not (isinstance(facts_text, str) and all(word in facts_text for word in promo)):
            found.append("购买引导")
    if role == "hook" and _opening_unclear(text, product_terms_list):
        found.append("开场指代不清")
    return found


def type_line_score(text: str, form: str | None, role: str, hook_echo: str = "") -> int:
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
        if role == "closing" and (_SOFT_CTA.search(text) or (hook_echo and hook_echo in text)):
            score += 3
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


def _spoken_roles(script) -> dict:
    spoken = [beat for beat in _beats(script)
              if isinstance(beat, dict) and isinstance(beat.get("narration"), str) and beat["narration"].strip()]
    roles = {}
    if spoken:
        roles[spoken[0].get("id")] = "hook"
        if spoken[-1].get("id") != spoken[0].get("id"):
            roles[spoken[-1].get("id")] = "closing"
    return roles


def _spoken_core(text):
    return re.sub(r"[^\u4e00-\u9fff]", "", text or "")


def _near_duplicate(left, right):
    first, second = _spoken_core(left), _spoken_core(right)
    if len(first) < 2 or len(second) < 2:
        return False
    short, long = (first, second) if len(first) <= len(second) else (second, first)
    return short in long and len(long) - len(short) <= 8


def _is_question(text):
    stripped = text.strip()
    return "？" in stripped or "?" in stripped or stripped.rstrip("。！! ") .endswith("吗")


_THIRD_PERSON = re.compile(r"[她他](?:走过|走|坐|拿|看|站|放|开|说|来|回|切|夹)")
_FIRST_PERSON = re.compile(r"我|咱们|我们|吧")
_ACTION_ONLY = re.compile(
    r"^(?:[她他])?(?:用)?(?:叉子|勺子|刀|手)?(?:切下|切开|拿起|放下|走过|走到|打开|盖上|倒入|舀起|夹起).{0,12}$"
)
_CONNECTIVE = re.compile(r"而且|还|另外|所以|关键是")
_SUBJECT = re.compile(r"这款|它|我用")
_BARE_ATTRIBUTE = re.compile(r"^(?:风[大小很太]|声音[大小很太]|带|负离子)")
_PRESENTER_CLAIM = re.compile(r"风[大小]|声音|负离子|吹开|吹干|听得清|听得见|马达|不毛躁|几下就")
_CTA_LINE = re.compile(r"链接|点下面|小黄车|去看看|下方")
_SHOT_DESCRIPTION = re.compile(
    r"亮起|光扫|扫过|飘起|飘起来|镜头|推近|特写|银线|一道线|闪过|映出|放在|台面|桌上|摆在|银色|风很大"
)
_STORY_PRODUCT = re.compile(
    r"它|这款|这个|这块|这只|喜欢|想要|好吃|好喝|好用|尝尝|给你|按这里|按这边|扣上|轮廓|(?<!吹)开了"
)


def script_line_offenses(script, strategy, locked_lines=(), plan=None):
    return _script_line_offenses(script, strategy, locked_lines, plan)


def _lead(text):
    return re.sub(r"^[\s，,。！!？?、]+", "", (text or "").strip())


def _has_product_subject(text, names):
    if _SUBJECT.search(text or ""):
        return True
    return any(isinstance(name, str) and len(name) >= 2 and name in (text or "") for name in names or ())


def _shot_blurbs(plan, beat_id):
    if not isinstance(plan, dict) or not beat_id:
        return []
    found = []
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for shot in segment.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            covered = list(shot.get("beat_ids") or [])
            if shot.get("beat_id"):
                covered.append(shot["beat_id"])
            if beat_id in covered:
                description = shot.get("description")
                if isinstance(description, str) and description.strip():
                    found.append(description)
    return found


def _shares_a_long_stretch(line, description):
    spoken = _spoken_core(line)
    picture = _spoken_core(description)
    if len(spoken) < 6 or len(picture) < 6:
        return False
    best = 0
    for start in range(len(spoken)):
        for other in range(len(picture)):
            size = 0
            while (start + size < len(spoken) and other + size < len(picture)
                   and spoken[start + size] == picture[other + size]):
                size += 1
            if size > best:
                best = size
    return best >= 6 and best / len(spoken) >= 0.6


def _script_line_offenses(script, strategy, locked_lines, plan=None):
    """Cross-line problems one rewrite should see. Locked lines stay untouched."""
    form = None
    if strategy is not None:
        try:
            form = script_form(strategy)
        except (ValidationError, TypeError, ValueError, KeyError):
            form = None
    spoken = [beat for beat in _beats(script)
              if isinstance(beat, dict) and isinstance(beat.get("narration"), str) and beat["narration"].strip()
              and not _locked(beat["narration"], locked_lines)]
    extra = []

    def add(beat, index, name):
        extra.append({"beat_id": beat.get("id"), "index": index, "narration": beat.get("narration"),
                      "offenses": [name]})

    indexes = {id(beat): index for index, beat in enumerate(_beats(script))}
    names = product_terms(strategy) or []
    seen_cores = []
    for previous, current in zip(spoken, spoken[1:]):
        if _near_duplicate(previous.get("narration"), current.get("narration")):
            add(current, indexes.get(id(current), 0), "相邻台词重复")
    for beat in spoken:
        core = _spoken_core(beat.get("narration"))
        if core and core in seen_cores:
            add(beat, indexes.get(id(beat), 0), "台词重复")
        elif core:
            seen_cores.append(core)
    if form == "scene_story" and spoken:
        third = [beat for beat in spoken if _THIRD_PERSON.search(_spoken_core(beat["narration"]))]
        first = [beat for beat in spoken if _FIRST_PERSON.search(beat["narration"])]
        if third and first:
            for beat in third + first:
                add(beat, indexes.get(id(beat), 0), "人称不统一")
    for beat in spoken:
        if _ACTION_ONLY.match(_spoken_core(beat["narration"])):
            add(beat, indexes.get(id(beat), 0), "只描述画面")
    if spoken and _is_question(spoken[0]["narration"]):
        later = spoken[1:]
        if later and not any(not _is_question(beat["narration"]) for beat in later):
            add(spoken[0], indexes.get(id(spoken[0]), 0), "开场问题没有下文")
        elif not later:
            add(spoken[0], indexes.get(id(spoken[0]), 0), "开场问题没有下文")
    if form == "presenter_pitch":
        claims = []
        for beat in spoken:
            text = beat["narration"]
            if _is_question(text) or _CTA_LINE.search(text):
                continue
            if _BARE_ATTRIBUTE.match(_lead(text)) or _PRESENTER_CLAIM.search(text):
                claims.append(beat)
        for beat in claims:
            text = beat["narration"]
            if _BARE_ATTRIBUTE.match(_lead(text)) and not _has_product_subject(text, names):
                add(beat, indexes.get(id(beat), 0), "卖点没有主语")
        for previous, current in zip(claims, claims[1:]):
            text = current["narration"]
            if _CONNECTIVE.search(text) or _has_product_subject(text, names):
                continue
            if _is_question(previous["narration"]):
                continue
            add(current, indexes.get(id(current), 0), "卖点之间缺少承接")
    if form == "brand_tvc":
        for beat in spoken:
            text = beat["narration"]
            core = _spoken_core(text)
            bare_name = any(core and core == _spoken_core(name) for name in names)
            if _SHOT_DESCRIPTION.search(text) or bare_name:
                add(beat, indexes.get(id(beat), 0), "只描述画面")
            if any(_shares_a_long_stretch(text, description) for description in _shot_blurbs(plan, beat.get("id"))):
                add(beat, indexes.get(id(beat), 0), "台词贴着画面")
    elif plan is not None:
        for beat in spoken:
            text = beat["narration"]
            if any(_shares_a_long_stretch(text, description) for description in _shot_blurbs(plan, beat.get("id"))):
                add(beat, indexes.get(id(beat), 0), "台词贴着画面")
    if form == "scene_story" and spoken:
        mentioned = any(
            _STORY_PRODUCT.search(beat["narration"]) or _has_product_subject(beat["narration"], names)
            for beat in spoken)
        if not mentioned:
            add(spoken[-1], indexes.get(id(spoken[-1]), 0), "人物没带到商品")
    return extra


def offending_lines(script: dict, *, tagline_enabled: bool | None = None, locked_lines=(),
                    strategy=None, plan=None) -> list[dict]:
    roles = _spoken_roles(script)
    terms = product_terms(strategy)
    facts_text = ""
    if strategy is not None and terms is not None:
        try:
            record = _strategy(strategy)
            facts_text = (record.brief.product_name or "") + "".join(fact.text for fact in record.brief.facts)
        except (ValidationError, TypeError, ValueError, KeyError):
            facts_text = ""
    beats = []
    for index, beat in enumerate(_beats(script)):
        narration = beat.get("narration") if isinstance(beat, dict) else None
        if not isinstance(narration, str) or not narration.strip() or _locked(narration, locked_lines):
            continue
        role = roles.get(beat.get("id"), "line")
        offenses = line_offenses(narration, tagline_enabled=tagline_enabled, role=role,
                                 product_terms_list=terms, facts_text=facts_text)
        if offenses:
            beats.append({"beat_id": beat.get("id"), "index": index, "narration": narration,
                          "offenses": offenses})
    seen = {item["beat_id"] for item in beats}
    for item in _script_line_offenses(script, strategy, locked_lines, plan):
        if item["beat_id"] in seen:
            current = next(row for row in beats if row["beat_id"] == item["beat_id"])
            for name in item["offenses"]:
                if name not in current["offenses"]:
                    current["offenses"].append(name)
        else:
            beats.append(item)
            seen.add(item["beat_id"])
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
        role = _spoken_roles(script).get(line.beat_id, "line")
        terms = product_terms(strategy)
        if line_offenses(line.narration, role=role, product_terms_list=terms, facts_text=facts_text):
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


LINE_REWRITE_SYSTEM = """你只重写列出的中文广告台词，只输出 JSON 对象，对象里只有 schema 的 lines。
这些句子现在像策划说明。按 line_style 改成能说出口的话：产品展示短、有感官；剧情是人物的来回；人物讲解对着镜头，钩子在第一句，带一个具体好处。
开场句要单独听得懂：说出商品叫法或用当下动作指着它。
承接 big_idea 和 hook 的意思，不要照抄策划词。只使用该句和 facts 里已经有的商品信息，不新增功效、数字、体验或比较。
不写关注、呈现、兼顾、打造、采用某技术、高端质感、赋能、满足你对、卖点，也不写最、第一、根治、国家级、绝对、永久。
不写购买、下单、抢购。优惠、限时、活动、价格和数字只在资料里已经有时才能说。结尾去向只按 ending_policy：没有指定时不说点链接、私信或到店。
每句不超过该句 max_characters，标点也算。用户固定台词原样留在句子里。
相邻两句不要近义重复。剧情广告全片只用一种人称，不要旁白说「她走过」同时又用「我」或「吧」。不要只复述看得见的动作。开场如果是问句，后面要有一句回答。
人物讲解的每一句卖点都要有主语：这款、它，或「我用它」。不要用「风大」「带负离子」这种没有主语的开头。卖点之间用而且、还、另外、所以或关键是接上。
产品片不必每句点名。直接、短地说卖点、好处或感受，例如「十万转高速马达，三分钟吹干」。不要描述镜头里的光、银线或发丝飘起。
剧情跟着人物当时的处境说，不套口播主语。全片至少有一句让人听见商品或它的好处。数字和功效只在 facts 里已经有时才能写。
tagline_policy.enabled 为 false 时不加独立口号字卡。为 true 时只有收束句可以是不超过10字的口播，仍然不写购买、下单、抢购。
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


async def _optional_model_call(stage_call, system, payload):
    """Polish is optional. Any failure keeps the original and never stops the job."""
    try:
        return await stage_call(system, payload, [], 1200)
    except Exception:
        return None


async def rewrite_script_if_needed(stage_call, script, strategy, duration_seconds, locked_lines=()):
    """Return (called, script). Clean scripts do not call the model."""
    data = script.model_dump() if hasattr(script, "model_dump") else deepcopy(script)
    offenses = offending_lines(data, locked_lines=locked_lines, strategy=strategy)
    if not offenses:
        return False, script
    payload, limits, facts_text = _rewrite_payload(data, offenses, strategy, duration_seconds, locked_lines)
    raw = await _optional_model_call(stage_call, LINE_REWRITE_SYSTEM, payload)
    if raw is None:
        return True, script
    revised = apply_line_rewrite(data, raw, limits=limits, locked_lines=locked_lines, facts_text=facts_text,
                                 duration_seconds=duration_seconds, strategy=strategy)
    return True, revised if revised is not None else script


async def rewrite_director_if_needed(stage_call, director: dict, strategy, duration_seconds, locked_lines=(),
                                    plan=None):
    """Return (called, replacement). No call when every spoken line is clean."""
    offenses = offending_lines(director.get("ad_script") or {}, locked_lines=locked_lines, strategy=strategy,
                               plan=plan)
    if not offenses:
        return False, None
    payload, limits, facts_text = _rewrite_payload(
        director["ad_script"], offenses, strategy, duration_seconds, locked_lines)
    raw = await _optional_model_call(stage_call, LINE_REWRITE_SYSTEM, payload)
    if raw is None:
        return True, None
    revised = apply_line_rewrite(director["ad_script"], raw, limits=limits, locked_lines=locked_lines,
                                 facts_text=facts_text, duration_seconds=duration_seconds, strategy=strategy)
    if revised is None:
        return True, None
    replacement = deepcopy(director)
    replacement["ad_script"] = revised
    return True, replacement


_FOCUS_OFFENSES = ("卖点没有主语", "卖点之间缺少承接", "只描述画面", "台词贴着画面", "人物没带到商品", "开场问题没有下文")
_FOCUS_NOTES = {
    "卖点没有主语": "这句卖点没有说清是哪件商品。要说这款、它，或我用它。",
    "卖点之间缺少承接": "这句和上一句接不上。可以用而且、所以把好处连起来。",
    "只描述画面": "这句在说镜头里看见的动作。台词改说这个画面证明的好处。",
    "台词贴着画面": "这句和画面说的是同一件事。台词改说这个画面证明的好处。",
    "人物没带到商品": "整条剧情还没让人听见商品或它的好处。",
    "开场问题没有下文": "开头问了，后面还没有一句回答。",
}


def grouped_line_flags(script, strategy, locked_lines=(), plan=None):
    """Beat id to the line-focus checks that still apply. Empty means that line is clear."""
    grouped = {}
    for item in script_line_offenses(script, strategy, locked_lines, plan):
        names = [name for name in item["offenses"] if name in _FOCUS_OFFENSES]
        if not names:
            continue
        bucket = grouped.setdefault(item["beat_id"], [])
        for name in names:
            if name not in bucket:
                bucket.append(name)
    return grouped


def line_focus_suggestions(plan, strategy, locked_lines=()):
    """Non-blocking notes for the criterion 台词是否对焦商品、前后连贯."""
    script = (plan or {}).get("ad_script") or {}
    flags = grouped_line_flags(script, strategy, locked_lines, plan)
    suggestions = []
    for index, beat in enumerate(_beats(script)):
        names = flags.get(beat.get("id")) or []
        narration = beat.get("narration") if isinstance(beat.get("narration"), str) else ""
        if not names or not narration.strip():
            continue
        note = "台词是否对焦商品、前后连贯。" + "".join(_FOCUS_NOTES[name] for name in names if name in _FOCUS_NOTES)
        suggestions.append({
            "kind": "line_focus",
            "target": f"/ad_script/beats/{index}/narration",
            "quote": narration[:240],
            "note": note[:300],
        })
    return suggestions


def with_line_focus_suggestions(review, plan, strategy=None, locked_lines=()):
    """Attach line-focus notes to a ready review. They never change the verdict."""
    extras = line_focus_suggestions(plan, strategy, locked_lines)
    ready = review.ready if hasattr(review, "ready") else (review.get("ready") if isinstance(review, dict) else False)
    if not extras or not ready:
        return review
    from .plan_review import validate_plan_review
    from .review_policy import MAX_REVIEW_SUGGESTIONS, review_suggestion_targets
    data = review.model_dump() if hasattr(review, "model_dump") else deepcopy(review)
    targets = review_suggestion_targets(plan)
    accepted = [item for item in extras if item["quote"] and item["quote"] in targets.get(item["target"], "")]
    if not accepted:
        return review
    merged, seen = [], set()
    for item in list(data.get("suggestions") or []) + accepted:
        identity = (item.get("kind"), item.get("target"), item.get("quote"))
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(item)
        if len(merged) >= MAX_REVIEW_SUGGESTIONS:
            break
    data["suggestions"] = merged
    try:
        return validate_plan_review(data, plan, require_speaker_visibility=True,
                                    require_temporal_omission=True, require_temporal_omission_evidence=True,
                                    require_visual_execution=True, require_review_policy=True)
    except (ValueError, KeyError, TypeError):
        return review
