"""Spoken-line checks for ad copy. No model, network, or provider imports.

The script call can offer two hook lines and two closing lines. This module
picks one with local signals. A later rewrite runs only when a line still
reads like a planner note, and only those lines are sent back.
"""
from __future__ import annotations

from copy import deepcopy
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ad_form_playbook import brand_voiceover_budget, resolve_playbook_form, speech_profile_for_strategy
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
    "brand_tvc": "产品展示：画面和产品声音先承担意思，句子仍要有感官。大约15秒整条只围绕一个主好处，旁白、节拍和画面都说这一件。大约30秒可以在主好处之后加一个配角好处，分清主次。45秒以上可以再展开，仍然不要并列清单，不要文案腔。不描述镜头，不编画面上没有的人，不用痛点问句开头。多出来的卖点留空。口播不受这条限制。",
    "scene_story": "剧情：人物的短对白，要有来有回。意图、反应、对照或包袱，每句大约4到10字。不念画面上的动作，也不把这一镜已经看见的事再说一遍。抱怨、态度或一句提醒可以，口播卖点不行。好处最多一句，是看见结果后的反应。指定去向时用定格上的短旁白，人物不转头推销。",
    "presenter_pitch": "人物讲解：对着镜头，第一句就是钩子。每一句卖点都要有主语，用这款、它，或我用它。卖点之间用而且、还、另外、所以或关键是接上。资料短时可以补具体场合。",
}
_TARGET_LENGTH = {
    "brand_tvc": (8, 14),
    "scene_story": (4, 12),
    "presenter_pitch": (8, 28),
}


class _RewriteLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beat_id: str = Field(min_length=1, max_length=80)
    narration: str = Field(min_length=0, max_length=80)


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
    if form == "brand_tvc" and _BRAND_COPY.search(text):
        score -= 6
    elif form == "brand_tvc" and re.search(r"顺|不吵|听得清|就干|好用|不毛躁|不烫", text):
        score += 4
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
_STORY_RESULT = re.compile(r"顺了|不毛躁|终于不|安静了|听得见了|听清了")
_SCENE_NARRATION = re.compile(r"室友|同事|旁边|她在|他在|正在|拿着|拿起|对着|还在睡|还没醒|她拿|他拿")
_SCENE_PITCH = re.compile(r"负离子|吹得快|吹完是顺|声音不吵|高速马达|三分钟")
# A visible sleeper, retold as "don't wake her / I don't dare switch it on".
_SLEEP_SHOWN = re.compile(r"睡|没醒|闭眼|闭着|侧躺")
_SLEEP_TOLD = re.compile(r"吵醒|怕吵|别吵|叫醒|弄醒|还在睡|还没醒|不敢开|不敢按")
_SCENE_FREEZE_CTA = re.compile(r"私信|到店|门店|产品详情|详情页")
_RESTATE_DROP = re.compile(r"[她他我你这那]")
_BRAND_OFFSCREEN = re.compile(r"旁边|还在睡|还没醒|室友|同事|家人|赶时间|上班|出门")
_BRAND_OPENING = re.compile(r"早上|赶时间|头发厚|头发多|多久")
# Poetic triples and spec-sheet lines. A short spoken benefit stays clean.
_BRAND_COPY = re.compile(
    r"是它的脸|是它的声音|先到了|这一线|肩线|边角这一|先听见|再看见|"
    r"高速马达|万转|亿负离子|负离子护发"
)
_BRAND_BLANKABLE = ("旁白太密", "旁白在堆卖点")
_EXTRA_CLAIMS = (
    ("weight", re.compile(r"很轻|机身轻|分量轻")),
    ("grip", re.compile(r"握感|好握")),
    ("color", re.compile(r"颜色|配色")),
)


def script_line_offenses(script, strategy, locked_lines=(), plan=None, duration_seconds=None):
    return _script_line_offenses(script, strategy, locked_lines, plan, duration_seconds)


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


def _visual_texts_for_beat(script, plan, beat, index):
    """Picture text for this beat: its visual role, plus the shot that covers it."""
    texts = []
    if isinstance(beat, dict):
        role = beat.get("visual_role")
        if isinstance(role, str) and role.strip():
            texts.append(role)
    shots = _iter_shots(plan) if isinstance(plan, dict) else []
    beat_id = beat.get("id") if isinstance(beat, dict) else None
    matched = []
    for shot in shots:
        if beat_id and beat_id in _shot_beat_ids(shot):
            matched.append(shot)
    if not matched and shots and len(shots) == len(_beats(script)) and 0 <= index < len(shots):
        matched = [shots[index]]
    for shot in matched:
        for field in ("description", "start_state", "end_state"):
            value = shot.get(field)
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


def _picture_minus_line(texts, line):
    """Drop a shot sentence that only quotes the line, so the picture can still count."""
    core = _spoken_core(line)
    pieces = []
    for text in texts:
        spoken = _spoken_core(text)
        if core and core in spoken:
            spoken = spoken.replace(core, "", 1)
        if spoken:
            pieces.append(spoken)
    return "".join(pieces)


def _bigram_restates(line, picture):
    if _STORY_RESULT.search(line or ""):
        return False
    core = _spoken_core(line)
    if len(core) < 6 or len(picture) < 6:
        return False
    grams = [core[i:i + 2] for i in range(len(core) - 1)]
    if len(grams) < 4:
        return False
    hit = sum(1 for gram in grams if gram in picture)
    return hit / len(grams) >= 0.75


def _restates_visible_situation(line, picture):
    """True when character dialogue mostly retells what this shot already shows.

    A muttered complaint or a tip the picture cannot carry stays. A freeze
    call-to-action is not character dialogue and is left alone.
    """
    if not picture or _CTA_LINE.search(line) or _SCENE_FREEZE_CTA.search(line):
        return False
    if _SLEEP_SHOWN.search(picture) and _SLEEP_TOLD.search(line):
        leftover = _RESTATE_DROP.sub("", _spoken_core(_SLEEP_TOLD.sub("", line)))
        core = _spoken_core(line)
        if len(core) >= 4 and len(leftover) <= max(2, len(core) // 3):
            return True
    return _bigram_restates(line, picture)


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


_SPEED = re.compile(r"快|几下|吹干|迅速")
_NOISE = re.compile(r"安静|不吵|声音小|听得清|听得见")
_IONS = re.compile(r"负离子")
_DURATION_Q = re.compile(r"多久|吹得久|费时|太慢|好慢")
_NOISE_Q = re.compile(r"吵|安静|声音")
_FILLER = re.compile(r"就能出门|就可以出门|吹完就能走|用完就好|这样就行|就能走了")
_LOGIC_NAMES = ("问句答非所问", "一句里塞了两个卖点", "卖点重复", "空话", "标签式旁白")


def _benefit_dims(text):
    found = set()
    if _SPEED.search(text):
        found.add("speed")
    if _NOISE.search(text):
        found.add("noise")
    if _IONS.search(text):
        found.add("ions")
    return found


def _claim_tags(text):
    found = _benefit_dims(text)
    for name, pattern in _EXTRA_CLAIMS:
        if pattern.search(text or ""):
            found.add(name)
    return found


def _ordered_tags(text):
    ranked = []
    for name, pattern in _CLAIM_PATTERNS:
        match = pattern.search(text or "")
        if match:
            ranked.append((match.start(), name))
    ranked.sort()
    found = []
    for _, name in ranked:
        if name not in found:
            found.append(name)
    return found


def brand_claim_capacity(duration_seconds):
    """How many distinct benefits a product showcase may state.

    A missing duration stays in the short band: one claim. Around 30 seconds
    the piece may add one supporting claim after the main one. At 45 seconds
    and longer a third claim can be developed. One sentence still cannot hold
    two claims. This cap is only for brand_tvc. 口播 is not limited here.
    """
    duration = duration_seconds if isinstance(duration_seconds, int) and duration_seconds > 0 else 15
    if duration < 25:
        return 1
    if duration < 45:
        return 2
    return 3


def _spoken_length(text):
    return len(re.sub(r"[，。！？、,.!?\s]", "", text or ""))


def _line_logic_offenses(spoken, form):
    """Pain, then the matching benefit, then the result. One point at a time."""
    rows = []
    indexes = {}
    seen = set()
    audible = [beat for beat in spoken if not _CTA_LINE.search(beat.get("narration") or "")]
    last_id = audible[-1].get("id") if audible else None

    def add(beat, name):
        beat_id = beat.get("id")
        if beat_id not in indexes:
            indexes[beat_id] = len(rows)
            rows.append({"beat_id": beat_id, "index": 0, "narration": beat.get("narration"), "offenses": []})
        if name not in rows[indexes[beat_id]]["offenses"]:
            rows[indexes[beat_id]]["offenses"].append(name)

    for previous, current in zip(spoken, spoken[1:]):
        current_text = current.get("narration") or ""
        if _CTA_LINE.search(current_text):
            continue
        question = previous.get("narration") or ""
        if not _is_question(question):
            continue
        answer = current_text
        if "？" in question:
            _, _, after = question.partition("？")
            if after.strip():
                continue
        allowed = set()
        if _DURATION_Q.search(question) or "慢" in question:
            allowed.add("speed")
        if _NOISE_Q.search(question):
            allowed.add("noise")
        if _IONS.search(question):
            allowed.add("ions")
        got = _benefit_dims(answer)
        if allowed and got and not got <= allowed:
            add(current, "问句答非所问")
    for beat in spoken:
        text = beat.get("narration") or ""
        if _CTA_LINE.search(text):
            continue
        body = text
        if "？" in text:
            _, _, after = text.partition("？")
            if after.strip():
                body = after
                question = text.split("？", 1)[0]
                allowed = set()
                if _DURATION_Q.search(question) or "慢" in question:
                    allowed.add("speed")
                if _NOISE_Q.search(question):
                    allowed.add("noise")
                if _IONS.search(question):
                    allowed.add("ions")
                got = _benefit_dims(body)
                if allowed and got and not got <= allowed:
                    add(beat, "问句答非所问")
        if len(_benefit_dims(body)) >= 2:
            add(beat, "一句里塞了两个卖点")
        if _FILLER.search(text):
            add(beat, "空话")
        if form == "brand_tvc":
            dims = _benefit_dims(text)
            stacked = len(dims) >= 2 and re.search(r"还|、|和", text)
            if stacked:
                add(beat, "标签式旁白")
        dims = _benefit_dims(text)
        repeated = {item for item in dims if item in seen and item != "ions"}
        if repeated and beat.get("id") != last_id:
            add(beat, "卖点重复")
        if "ions" in dims and "ions" in seen and beat.get("id") != last_id:
            if "还" in text or _spoken_length(text) < 8:
                add(beat, "卖点重复")
        seen |= dims
    return rows


def _script_line_offenses(script, strategy, locked_lines, plan=None, duration_seconds=None):
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
        content_lines = [beat for beat in spoken if not _CTA_LINE.search(beat["narration"])]
        if content_lines and (_is_question(content_lines[0]["narration"]) or _BRAND_OPENING.search(content_lines[0]["narration"])):
            add(content_lines[0], indexes.get(id(content_lines[0]), 0), "旁白开成了讲解")
        budget = brand_voiceover_budget(duration_seconds)
        if len(content_lines) >= budget["rewrite_at"]:
            for beat in content_lines[budget["preferred_max"]:]:
                add(beat, indexes.get(id(beat), 0), "旁白太密")
        introduced = []
        capacity = brand_claim_capacity(duration_seconds)
        for beat in content_lines:
            text = beat["narration"]
            if _BRAND_COPY.search(text):
                add(beat, indexes.get(id(beat), 0), "旁白像文案")
            tags = _ordered_tags(text)
            novel = [tag for tag in tags if tag not in introduced]
            # The first spoken line can still be 「一句里塞了两个卖点」.
            # A later line stacks only when it would pass the duration cap.
            if introduced and novel and len(introduced) + len(novel) > capacity:
                add(beat, indexes.get(id(beat), 0), "旁白在堆卖点")
            for tag in tags:
                if tag not in introduced:
                    introduced.append(tag)
        for beat in spoken:
            text = beat["narration"]
            if not _CTA_LINE.search(text) and _BRAND_OFFSCREEN.search(text):
                add(beat, indexes.get(id(beat), 0), "旁白补了画面外")
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
        for beat in spoken:
            text = beat["narration"]
            if _CTA_LINE.search(text) or _SCENE_FREEZE_CTA.search(text):
                continue
            picture = _picture_minus_line(
                _visual_texts_for_beat(script, plan, beat, indexes.get(id(beat), 0)), text)
            if _SCENE_NARRATION.search(text) or _restates_visible_situation(text, picture):
                add(beat, indexes.get(id(beat), 0), "在念画面")
            if _SCENE_PITCH.search(text):
                add(beat, indexes.get(id(beat), 0), "口播腔")
        mentioned = any(
            _STORY_PRODUCT.search(beat["narration"]) or _STORY_RESULT.search(beat["narration"])
            or _SCENE_PITCH.search(beat["narration"]) or _has_product_subject(beat["narration"], names)
            for beat in spoken)
        if not mentioned:
            add(spoken[-1], indexes.get(id(spoken[-1]), 0), "人物没带到商品")
    try:
        logic = _line_logic_offenses(spoken, form)
    except (TypeError, ValueError, KeyError, IndexError, re.error):
        logic = []
    for item in logic:
        beat = next((candidate for candidate in spoken if candidate.get("id") == item["beat_id"]), None)
        if beat is None:
            continue
        item["index"] = indexes.get(id(beat), 0)
        extra.append(item)
    return extra


def offending_lines(script: dict, *, tagline_enabled: bool | None = None, locked_lines=(),
                    strategy=None, plan=None, duration_seconds=None) -> list[dict]:
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
    for item in _script_line_offenses(script, strategy, locked_lines, plan, duration_seconds):
        if item["beat_id"] in seen:
            current = next(row for row in beats if row["beat_id"] == item["beat_id"])
            for name in item["offenses"]:
                if name not in current["offenses"]:
                    current["offenses"].append(name)
        else:
            beats.append(item)
            seen.add(item["beat_id"])
    return beats


_CLAIM_PATTERNS = (
    ("speed", _SPEED),
    ("noise", _NOISE),
    ("ions", _IONS),
    *_EXTRA_CLAIMS,
)
_CLAUSE_SPLIT = re.compile(r"[，,。！？!?、；;]+")
_STACK_JOIN = re.compile(r"还|和|与")
_BRAND_STACKS = ("旁白在堆卖点", "一句里塞了两个卖点", "标签式旁白")


def _brand_form(strategy):
    if strategy is None:
        return None
    try:
        return script_form(strategy)
    except (ValidationError, TypeError, ValueError, KeyError):
        return None


def _earliest_tag(text):
    found = None
    at = None
    for name, pattern in _CLAIM_PATTERNS:
        match = pattern.search(text or "")
        if match and (at is None or match.start() < at):
            at = match.start()
            found = name
    return found


def _split_claim_clause(clause, depth=0):
    """Break one clause so each piece carries at most the first benefit."""
    if depth > 4 or not clause:
        return [clause] if clause else []
    if len(_claim_tags(clause)) < 2:
        return [clause]
    parts = [part.strip() for part in _STACK_JOIN.split(clause) if part and part.strip()]
    if len(parts) >= 2 and all(part != clause for part in parts):
        if sum(1 for part in parts if _claim_tags(part)) >= 2:
            pieces = []
            for part in parts:
                pieces.extend(_split_claim_clause(part, depth + 1))
            return pieces or [clause]
    primary = _earliest_tag(clause)
    cut = None
    for name, pattern in _CLAIM_PATTERNS:
        if name == primary:
            continue
        match = pattern.search(clause)
        if match and (cut is None or match.start() < cut):
            cut = match.start()
    if not cut:
        return [clause]
    head = re.sub(r"[还和与及，,、\s]+$", "", clause[:cut]).strip()
    tail = clause[cut:].strip()
    if not head or head == clause:
        return [clause]
    pieces = [head]
    if tail and tail != clause:
        pieces.extend(_split_claim_clause(tail, depth + 1))
    return pieces


def _spoken_pieces(text):
    pieces = []
    for clause in _CLAUSE_SPLIT.split(text or ""):
        clause = clause.strip()
        if clause:
            pieces.extend(_split_claim_clause(clause))
    return pieces


def _terminal_mark(text):
    match = re.search(r"[。！？!?]$", (text or "").strip())
    return match.group(0) if match else "。"


def collapse_brand_claims(script, locked_lines=(), duration_seconds=None):
    """Keep one main benefit in a product-ad voiceover, plus duration room.

    About 15 seconds stays on that one claim. About 30 seconds may keep one
    later supporting claim. At 45 seconds and longer a third claim may stay.
    A second claim inside the same sentence is still removed. A call-to-action
    line and any user-locked line stay as written. The same benefit may repeat.
    A result with no new claim, such as 吹完是顺的, stays when it is not riding
    on a claim this line dropped. Callers use this for brand_tvc only.
    """
    revised = deepcopy(script) if isinstance(script, dict) else {"beats": []}
    capacity = brand_claim_capacity(duration_seconds)
    allowed = []
    changed = False
    for beat in _beats(revised):
        if not isinstance(beat, dict):
            continue
        text = beat.get("narration")
        if not isinstance(text, str) or not text.strip():
            continue
        if _locked(text, locked_lines) or _CTA_LINE.search(text):
            if _locked(text, locked_lines):
                for tag in _ordered_tags(text):
                    if tag not in allowed:
                        allowed.append(tag)
            continue
        line_tag = None
        kept = []
        dropped_foreign = False
        for piece in _spoken_pieces(text):
            tags = _claim_tags(piece)
            if not tags:
                kept.append(piece)
                continue
            tag = _earliest_tag(piece) or next(iter(tags))
            if line_tag is not None and tag != line_tag:
                dropped_foreign = True
                continue
            if tag in allowed or len(allowed) < capacity:
                if tag not in allowed:
                    allowed.append(tag)
                line_tag = tag
                if tags <= {tag}:
                    kept.append(piece)
                else:
                    dropped_foreign = True
                continue
            dropped_foreign = True
        if dropped_foreign and not any(_claim_tags(piece) for piece in kept):
            kept = []
        new = ("，".join(kept) + _terminal_mark(text)) if kept else ""
        if new != text:
            beat["narration"] = new
            changed = True
    return revised, changed


def brand_stack_remaining(script, strategy, locked_lines=(), plan=None, duration_seconds=None):
    """True when a product ad still states a second benefit the checker can see."""
    if _brand_form(strategy) != "brand_tvc":
        return False
    for item in script_line_offenses(script, strategy, locked_lines, plan, duration_seconds):
        if any(name in _BRAND_STACKS for name in item.get("offenses") or []):
            return True
    return False


_PIECE_HOLD = "产品留在画面里。"
_SILENT_HOLD = "这一拍不说话，画面停住。"
_VO_POINTER = re.compile(
    r"旁白.{0,12}(?:说|念)|画外音?.{0,8}(?:说|念)|说这句|配上旁白|跟着旁白|随着旁白"
)
_BEAT_TEXT_FIELDS = ("purpose", "message", "visual_role")
_SHOT_TEXT_FIELDS = ("description", "purpose")
_CONTINUITY_TEXT_FIELDS = (
    "start_state", "allowed_changes", "end_state", "camera_move", "entry_reason",
)


def _shot_beat_ids(shot):
    found = []
    if not isinstance(shot, dict):
        return found
    for part in shot.get("coverage") or []:
        if isinstance(part, dict) and isinstance(part.get("beat_id"), str):
            found.append(part["beat_id"])
    if isinstance(shot.get("beat_id"), str) and shot["beat_id"]:
        found.append(shot["beat_id"])
    for beat_id in shot.get("beat_ids") or []:
        if isinstance(beat_id, str) and beat_id:
            found.append(beat_id)
    return found


def _narration_by_id(script):
    found = {}
    for beat in _beats(script):
        if isinstance(beat, dict) and isinstance(beat.get("id"), str):
            text = beat.get("narration")
            found[beat["id"]] = text.strip() if isinstance(text, str) else ""
    return found


def _allowed_brand_tags(script, locked_lines, duration_seconds):
    """Claims the collapsed voiceover still speaks, in first-heard order."""
    capacity = brand_claim_capacity(duration_seconds)
    allowed = []
    for beat in _beats(script):
        if not isinstance(beat, dict):
            continue
        text = beat.get("narration")
        if not isinstance(text, str) or not text.strip() or _CTA_LINE.search(text):
            continue
        locked = _locked(text, locked_lines)
        for tag in _ordered_tags(text):
            if tag in allowed:
                continue
            if locked or len(allowed) < capacity:
                allowed.append(tag)
    return allowed


def _visual_sentences(text):
    parts = re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", text or "")
    return [part.strip() for part in parts if part and part.strip()]


_CAMERA_QUICK = re.compile(r"快速|快切|尽快|快推|迅速(?=推|拉|摇|移|切|转)")


def _visual_claim_tags(text):
    """Benefit tags in a picture line. 快速推近 is camera, not a second claim."""
    return _claim_tags(_CAMERA_QUICK.sub("", text or ""))


def _filter_claim_sentence(sentence, permitted):
    """Drop clauses that state a benefit this spot is not allowed to show."""
    pieces = re.findall(r"[^，,]+[，,]?", sentence or "")
    if not pieces:
        return ""
    kept = []
    dropped_foreign = False
    for piece in pieces:
        tags = _visual_claim_tags(piece)
        if tags and not tags <= permitted:
            dropped_foreign = True
            continue
        kept.append(piece)
    if dropped_foreign and not any(_visual_claim_tags(piece) & permitted for piece in kept):
        return ""
    body = "".join(kept).strip("，, ")
    if not body:
        return ""
    mark = sentence.strip()[-1] if sentence.strip()[-1:] in "。！？!?；;" else ""
    if mark and not body.endswith(mark):
        body += mark
    return body


def _permitted_tags(narrations, beat_ids, allowed):
    """Primary claim everywhere; a supporting claim only on the beat that says it."""
    permitted = set(allowed[:1])
    for beat_id in beat_ids:
        permitted.update(tag for tag in _ordered_tags(narrations.get(beat_id, "")) if tag in allowed)
    return permitted


def _rewrite_claim_text(text, permitted, *, blank_hold):
    if not isinstance(text, str) or not text.strip():
        return text
    sentences = []
    for sentence in _visual_sentences(text):
        if _VO_POINTER.search(sentence) and blank_hold:
            continue
        kept = _filter_claim_sentence(sentence, permitted)
        if kept:
            sentences.append(kept)
    if sentences:
        return "".join(sentences)
    if not _visual_claim_tags(text) and not (blank_hold and _VO_POINTER.search(text)):
        return text
    return _SILENT_HOLD if blank_hold else _PIECE_HOLD


def _align_brand_piece(container, locked_lines, duration_seconds):
    """Shots, beats and the motif follow the same claims the voiceover kept.

    Product showcase only. A 15 second piece cannot show a second benefit in
    the picture after the line was blanked. A longer piece may show its
    supporting claim on the beat that speaks it, not as a flat list, and the
    motif stays on the main claim.
    """
    if not isinstance(container, dict):
        return False
    script = container.get("ad_script") if isinstance(container.get("ad_script"), dict) else container
    if not isinstance(script, dict):
        return False
    narrations = _narration_by_id(script)
    allowed = _allowed_brand_tags(script, locked_lines, duration_seconds)
    if not allowed:
        texts = []
        for beat in _beats(script):
            if not isinstance(beat, dict):
                continue
            for field in _BEAT_TEXT_FIELDS:
                value = beat.get(field)
                if isinstance(value, str):
                    texts.append(value)
        for shot in _iter_shots(container):
            description = shot.get("description")
            if isinstance(description, str):
                texts.append(description)
        discovered = []
        capacity = brand_claim_capacity(duration_seconds)
        for text in texts:
            for tag in _ordered_tags(_CAMERA_QUICK.sub("", text)):
                if tag not in discovered:
                    discovered.append(tag)
                if len(discovered) >= capacity:
                    break
            if len(discovered) >= capacity:
                break
        allowed = discovered
    changed = False
    primary = set(allowed[:1])
    for field in ("core_message", "audience_takeaway", "video_flow"):
        value = script.get(field) if field != "video_flow" else container.get(field)
        host = script if field != "video_flow" else container
        if not isinstance(value, str) or not value.strip():
            continue
        updated = _rewrite_claim_text(value, primary, blank_hold=False)
        if updated != value:
            host[field] = updated
            changed = True
    for beat in _beats(script):
        if not isinstance(beat, dict):
            continue
        spoken = beat.get("narration") if isinstance(beat.get("narration"), str) else ""
        if _locked(spoken, locked_lines) or _CTA_LINE.search(spoken or ""):
            permitted = set(_ordered_tags(spoken)) | primary
        else:
            permitted = _permitted_tags(narrations, [beat.get("id")], allowed)
        for field in _BEAT_TEXT_FIELDS:
            value = beat.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            updated = _rewrite_claim_text(value, permitted, blank_hold=not spoken.strip())
            if updated != value:
                beat[field] = updated
                changed = True
    beats = [beat for beat in _beats(script) if isinstance(beat, dict)]
    shots = _iter_shots(container)
    for index, shot in enumerate(shots):
        covered = _shot_beat_ids(shot)
        if not covered and len(shots) == len(beats) and isinstance(beats[index].get("id"), str):
            covered = [beats[index]["id"]]
        spoken = "".join(narrations.get(beat_id, "") for beat_id in covered)
        blank_hold = bool(covered) and not spoken.strip()
        permitted = _permitted_tags(narrations, covered, allowed)
        for field in _SHOT_TEXT_FIELDS:
            value = shot.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            updated = _rewrite_claim_text(value, permitted, blank_hold=blank_hold)
            if updated != value:
                shot[field] = updated
                changed = True
        motif = shot.get("visual_motif")
        if isinstance(motif, str) and motif.strip():
            updated = _rewrite_claim_text(motif, primary, blank_hold=False)
            if _visual_claim_tags(updated) - primary or (primary and _visual_claim_tags(motif) - primary):
                updated = ""
            elif updated == _PIECE_HOLD or updated == _SILENT_HOLD:
                updated = ""
            if updated != motif:
                shot["visual_motif"] = updated
                changed = True
        continuity = shot.get("continuity")
        if isinstance(continuity, dict):
            for field in _CONTINUITY_TEXT_FIELDS:
                value = continuity.get(field)
                if not isinstance(value, str) or not value.strip():
                    continue
                updated = _rewrite_claim_text(value, permitted, blank_hold=False)
                if updated != value:
                    continuity[field] = updated
                    changed = True
    return changed


def _iter_shots(container):
    if not isinstance(container, dict):
        return []
    if isinstance(container.get("shots"), list):
        return [shot for shot in container["shots"] if isinstance(shot, dict)]
    found = []
    for segment in container.get("segments") or []:
        if isinstance(segment, dict):
            found.extend(shot for shot in segment.get("shots") or [] if isinstance(shot, dict))
    return found


def _sync_quoted_narration(director, before, after):
    """Drop a shot sentence that still quotes the voiceover we just rewrote.

    A blanked line also drops stage directions that only pointed at it, such as
    「旁白说这句话时画面基本停住」, so the video prompt does not invent a line.
    """
    pairs = []
    blanked = set()
    for left, right in zip(_beats(before), _beats(after)):
        old = left.get("narration") if isinstance(left, dict) else ""
        new = right.get("narration") if isinstance(right, dict) else ""
        old = old.strip() if isinstance(old, str) else ""
        new = new.strip() if isinstance(new, str) else ""
        if old and old != new:
            pairs.append((old, new))
        if old and not new and isinstance(left, dict) and isinstance(left.get("id"), str):
            blanked.add(left["id"])
    if not isinstance(director, dict):
        return
    beats = [beat for beat in _beats(after) if isinstance(beat, dict)]
    shots = _iter_shots(director)
    for index, shot in enumerate(shots):
        covered = _shot_beat_ids(shot)
        if not covered and blanked and len(shots) == len(beats):
            beat_id = beats[index].get("id")
            if isinstance(beat_id, str):
                covered = [beat_id]
        fields = []
        for field in ("description", "purpose"):
            if isinstance(shot.get(field), str):
                fields.append((shot, field))
        continuity = shot.get("continuity")
        if isinstance(continuity, dict):
            for field in _CONTINUITY_TEXT_FIELDS:
                if isinstance(continuity.get(field), str):
                    fields.append((continuity, field))
        for host, field in fields:
            updated = host[field]
            for old, new in pairs:
                if old and old in updated:
                    updated = updated.replace(old, new if new else "")
                    continue
                bare = old.rstrip("。！？!?")
                if bare and bare in updated:
                    updated = updated.replace(bare, new.rstrip("。！？!?") if new else "")
            if blanked and (not covered or blanked.intersection(covered)):
                kept = [sentence for sentence in _visual_sentences(updated) if not _VO_POINTER.search(sentence)]
                if len(kept) != len(_visual_sentences(updated)):
                    updated = "".join(kept)
            updated = re.sub(r"[，,]{2,}", "，", updated).strip("，, ")
            if updated != host[field]:
                if not updated.strip() and field == "description":
                    updated = _SILENT_HOLD
                elif not updated.strip() and field != "description":
                    updated = host[field]
                host[field] = updated


def _fold_brand_director(director, locked_lines, duration_seconds):
    """Return (director, changed). Narration, beats and shots share one claim set."""
    script = director.get("ad_script") if isinstance(director.get("ad_script"), dict) else {"beats": []}
    revised, narration_changed = collapse_brand_claims(script, locked_lines, duration_seconds)
    replacement = deepcopy(director)
    replacement["ad_script"] = revised
    aligned = _align_brand_piece(replacement, locked_lines, duration_seconds)
    before_shots = deepcopy(_iter_shots(replacement))
    _sync_quoted_narration(replacement, script, revised)
    quote_changed = _iter_shots(replacement) != before_shots
    if not narration_changed and not aligned and not quote_changed:
        return director, False
    return replacement, True


def brand_visual_claim_overflow(container, strategy, locked_lines=(), duration_seconds=None):
    """True when a product showcase still shows a benefit the voiceover does not keep."""
    if _brand_form(strategy) != "brand_tvc" or not isinstance(container, dict):
        return False
    script = container.get("ad_script") if isinstance(container.get("ad_script"), dict) else container
    allowed = _allowed_brand_tags(script, locked_lines, duration_seconds)
    if not allowed:
        return False
    primary = set(allowed[:1])
    narrations = _narration_by_id(script)
    for field in ("core_message", "audience_takeaway"):
        value = script.get(field) if isinstance(script, dict) else ""
        if isinstance(value, str) and _visual_claim_tags(value) - primary:
            return True
    flow = container.get("video_flow")
    if isinstance(flow, str) and _visual_claim_tags(flow) - primary:
        return True
    beats = [beat for beat in _beats(script) if isinstance(beat, dict)]
    for beat in beats:
        spoken = beat.get("narration") if isinstance(beat.get("narration"), str) else ""
        if _locked(spoken, locked_lines) or _CTA_LINE.search(spoken or ""):
            continue
        permitted = _permitted_tags(narrations, [beat.get("id")], allowed)
        for field in _BEAT_TEXT_FIELDS:
            value = beat.get(field)
            if isinstance(value, str) and _visual_claim_tags(value) - permitted:
                return True
    shots = _iter_shots(container)
    for index, shot in enumerate(shots):
        covered = _shot_beat_ids(shot)
        if not covered and len(shots) == len(beats) and isinstance(beats[index].get("id"), str):
            covered = [beats[index]["id"]]
        permitted = _permitted_tags(narrations, covered, allowed)
        for field in _SHOT_TEXT_FIELDS:
            value = shot.get(field)
            if isinstance(value, str) and _visual_claim_tags(value) - permitted:
                return True
        motif = shot.get("visual_motif")
        if isinstance(motif, str) and _visual_claim_tags(motif) - primary:
            return True
        continuity = shot.get("continuity")
        if isinstance(continuity, dict):
            for field in _CONTINUITY_TEXT_FIELDS:
                value = continuity.get(field)
                if isinstance(value, str) and _visual_claim_tags(value) - permitted:
                    return True
    return False


def apply_brand_claim_collapse(director, strategy, locked_lines=(), duration_seconds=None):
    """Return (director, changed). Only a product showcase is rewritten."""
    if not isinstance(director, dict) or _brand_form(strategy) != "brand_tvc":
        return director, False
    return _fold_brand_director(director, locked_lines, duration_seconds)


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
        offenses = item.get("offenses") or []
        if any(name in offenses for name in _BRAND_BLANKABLE):
            one["properties"]["narration"]["minLength"] = 0
            one["properties"]["narration"]["description"] = (
                "这句把时长说满了，或又报了另一个卖点。留空，表示这一拍不说话。"
                "若还在说同一个好处，写成一句口语，不要加长。")
        elif "在念画面" in offenses:
            one["properties"]["narration"]["minLength"] = 1
            one["properties"]["narration"]["description"] = (
                "这句在复述镜头里已经看见的事。改成反应、态度，或画面带不走的一句提醒。"
                "不要再讲画面上的人和动作，也不要改成口播卖点。定格上的去向旁白不要改。")
        else:
            one["properties"]["narration"]["minLength"] = 1
            one["properties"]["narration"]["description"] = f"只重写这一句，含标点最多{one['properties']['narration']['maxLength']}字"
        items.append(one)
    schema["properties"]["lines"].update(minItems=len(items), maxItems=len(items), items={"oneOf": items})
    return schema


def apply_line_rewrite(script: dict, raw, *, limits: dict, locked_lines=(), facts_text: str = "",
                       duration_seconds: int | None = None, strategy=None, offense_names=None) -> dict | None:
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
    named = offense_names or {}
    for line in batch.lines:
        beat = by_id.get(line.beat_id)
        if beat is None or len(line.narration) > limits[line.beat_id]:
            return None
        if not line.narration.strip():
            if not any(name in (named.get(line.beat_id) or []) for name in _BRAND_BLANKABLE):
                return None
            beat["narration"] = ""
            continue
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
    if strategy is not None:
        try:
            if script_form(strategy) == "brand_tvc":
                leftover = _script_line_offenses(revised, strategy, locked_lines, duration_seconds=duration_seconds)
                blocked = {"旁白像文案", "旁白在堆卖点"}
                if any(name in blocked for item in leftover for name in item["offenses"]):
                    return None
        except (ValidationError, TypeError, ValueError, KeyError):
            return None
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
这些句子现在像策划说明。按 line_style 改成能说出口的话：产品展示只留薄薄两三句；剧情是人物的短对白；人物讲解对着镜头，钩子在第一句，带一个具体好处。
开场句要单独听得懂。产品片开场可以没有旁白。人物讲解说出商品叫法或用当下动作指着它。
承接 big_idea 和 hook 的意思，不要照抄策划词。只使用该句和 facts 里已经有的商品信息，不新增功效、数字、体验或比较。
不写关注、呈现、兼顾、打造、采用某技术、高端质感、赋能、满足你对、卖点，也不写最、第一、根治、国家级、绝对、永久。
不写购买、下单、抢购。优惠、限时、活动、价格和数字只在资料里已经有时才能说。结尾去向只按 ending_policy：没有指定时不说点链接、私信或到店。
每句不超过该句 max_characters，标点也算。用户固定台词原样留在句子里。旁白太密的句子按 schema 留空，表示这一拍不说话。
相邻两句不要近义重复。剧情广告全片只用一种人称，不要旁白说「她走过」同时又用「我」或「吧」。不要只复述看得见的动作。人物讲解的开场如果是问句，后面要有一句回答，而且回答的是同一个问题：问吹得久就回答快，不要顺口把安静和负离子也塞进这一句。
一条卖点说完整再换下一条。快、安静、负离子不要在后面的句子里再报一遍，除非最后一句是有意收回来的总括。没有信息的空话删掉，例如洗完就能出门。只有人物讲解在资料只有几个词时，才补一个具体场合和体感，例如赶早上班、头发厚、旁边还有人在睡。产品片和剧情不要补画面上没有的人、地方或时间。不编数字、规格、价格或折扣。
产品展示大约15秒整条只围绕一个主好处，像跟人说话。大约30秒可以在这一个主好处之后再留一个配角好处，分清主次。45秒以上可以再展开一句，仍然不要并列清单。不要文案腔，不要「是它的脸」「先到了」「肩线这一圈」，也不要「十万转高速马达」「一亿负离子」这种参数清单。短的口语好处不要改长，也不要补主语。例如「吹完是顺的。」「这风不吵。」「几下就干。」不要描述镜头里的光和动作，不要用痛点问句或场景交代开场。旁白太密，或超过本片时长还能说的卖点，那些句子 narration 留空。至少留一拍只有产品的声音。不编价格。口播不受这条限制，卖点之间仍用而且接上。
人物讲解的每一句卖点都要有主语：这款、它，或「我用它」。不要用「风大」「带负离子」这种没有主语的开头。卖点之间用而且、还、另外、所以或关键是接上。
产品片不必每句点名。直接、短地说画面证明不了的好处。不要描述镜头里的光、银线或发丝飘起。
剧情对白是意图、反应、对照或包袱，每句大约4到10字。不要把这一镜已经看见的事再说一遍。画面是室友在睡、她没按开关时，不要说「不敢开，怕吵醒她。」改成画面带不走的反应或提醒，例如「声音也太大了。」或「风口转开一点。」不要念室友在睡、她拿吹风机，也不要改成「带负离子，吹完是顺的」这种口播。好处最多一句，写成人物看见结果后的反应。指定了去向时用定格上的短旁白，人物不要转头对着镜头推销，这句不要改。数字和功效只在 facts 里已经有时才能写。若某句带了 picture，那就是这一镜已经拍到的内容。
tagline_policy.enabled 为 false 时不加独立口号字卡。为 true 时只有收束句可以是不超过10字的口播，仍然不写购买、下单、抢购。
不输出其他字段，不解释。"""


def _rewrite_payload(script, offenses, strategy, duration, locked_lines, plan=None):
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
        row = {
            "beat_id": item["beat_id"],
            "role": roles.get(item["beat_id"], "line"),
            "narration": item["narration"],
            "offenses": item["offenses"],
            "max_characters": max(1, min(profile.max_beat_characters, remaining)),
        }
        picture = _picture_minus_line(
            _visual_texts_for_beat(script, plan, beat, item.get("index") or 0), item["narration"])
        if picture:
            row["picture"] = picture[:80]
        lines.append(row)
    facts = [{"id": fact.id, "text": fact.text} for fact in strategy.brief.facts if fact.id in fact_ids]
    payload = {
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
    }
    if form == "brand_tvc":
        payload["voiceover_budget"] = brand_voiceover_budget(duration)
    return payload, {item["beat_id"]: item["max_characters"] for item in lines}, "".join(fact["text"] for fact in facts)


async def _optional_model_call(stage_call, system, payload):
    """Polish is optional. Any failure keeps the original and never stops the job."""
    try:
        return await stage_call(system, payload, [], 1200)
    except Exception:
        return None


def _narration_text(script):
    return [beat.get("narration") if isinstance(beat, dict) else "" for beat in _beats(script)]


async def rewrite_script_if_needed(stage_call, script, strategy, duration_seconds, locked_lines=()):
    """Return (called, script). Clean scripts do not call the model.

    A product ad that stacks benefits is collapsed locally even when the model
    call fails, so the original multi-claim lines are not kept.
    """
    data = script.model_dump() if hasattr(script, "model_dump") else deepcopy(script)
    brand = _brand_form(strategy) == "brand_tvc"
    collapsed, changed = collapse_brand_claims(data, locked_lines, duration_seconds) if brand else (data, False)
    if brand and _align_brand_piece(collapsed, locked_lines, duration_seconds):
        changed = True
    working = collapsed if changed else data
    offenses = offending_lines(working, locked_lines=locked_lines, strategy=strategy,
                               duration_seconds=duration_seconds)
    if not offenses and not changed:
        return False, script
    if offenses:
        payload, limits, facts_text = _rewrite_payload(
            working, offenses, strategy, duration_seconds, locked_lines)
        raw = await _optional_model_call(stage_call, LINE_REWRITE_SYSTEM, payload)
        revised = None
        if raw is not None:
            revised = apply_line_rewrite(working, raw, limits=limits, locked_lines=locked_lines,
                                         facts_text=facts_text, duration_seconds=duration_seconds,
                                         strategy=strategy,
                                         offense_names={item["beat_id"]: item["offenses"] for item in offenses})
        chosen = revised if revised is not None else working
    else:
        revised = None
        chosen = working
    if brand:
        chosen, again = collapse_brand_claims(chosen, locked_lines, duration_seconds)
        if _align_brand_piece(chosen, locked_lines, duration_seconds) or again:
            changed = True
    if revised is None and not changed:
        return True, script
    return True, chosen


async def rewrite_director_if_needed(stage_call, director: dict, strategy, duration_seconds, locked_lines=(),
                                    plan=None):
    """Return (called, replacement). No call when every spoken line is clean.

    Product-ad stacking is collapsed before the model is trusted. A failed or
    rejected rewrite still returns that one-benefit script.
    """
    script = director.get("ad_script") or {}
    brand = _brand_form(strategy) == "brand_tvc"
    folded = director
    changed = False
    if brand:
        folded, changed = _fold_brand_director(director, locked_lines, duration_seconds)
    working = (folded.get("ad_script") or {}) if brand else script
    shot_source = plan if isinstance(plan, dict) and _iter_shots(plan) else director
    offenses = offending_lines(working, locked_lines=locked_lines, strategy=strategy,
                               plan=shot_source, duration_seconds=duration_seconds)
    if not offenses and not changed:
        return False, None
    revised = None
    if offenses:
        payload, limits, facts_text = _rewrite_payload(
            working, offenses, strategy, duration_seconds, locked_lines, shot_source)
        raw = await _optional_model_call(stage_call, LINE_REWRITE_SYSTEM, payload)
        if raw is not None:
            revised = apply_line_rewrite(working, raw, limits=limits, locked_lines=locked_lines,
                                         facts_text=facts_text, duration_seconds=duration_seconds,
                                         strategy=strategy,
                                         offense_names={item["beat_id"]: item["offenses"] for item in offenses})
    chosen = revised if revised is not None else working
    if brand:
        folded = deepcopy(folded)
        folded["ad_script"] = chosen
        folded, again = _fold_brand_director(folded, locked_lines, duration_seconds)
        chosen = folded.get("ad_script") or chosen
        changed = changed or again
        if revised is None and not changed and _narration_text(chosen) == _narration_text(script):
            shots_same = _iter_shots(folded) == _iter_shots(director)
            if shots_same:
                return True, None
        return True, folded
    if revised is None and not changed and _narration_text(chosen) == _narration_text(script):
        return True, None
    replacement = deepcopy(director)
    replacement["ad_script"] = chosen
    _sync_quoted_narration(replacement, script, chosen)
    return True, replacement


_FOCUS_OFFENSES = ("卖点没有主语", "卖点之间缺少承接", "只描述画面", "台词贴着画面", "人物没带到商品", "开场问题没有下文", "旁白像文案", "旁白在堆卖点")
_FOCUS_NOTES = {
    "卖点没有主语": "这句卖点没有说清是哪件商品。要说这款、它，或我用它。",
    "卖点之间缺少承接": "这句和上一句接不上。可以用而且、所以把好处连起来。",
    "只描述画面": "这句在说镜头里看见的动作。台词改说这个画面证明的好处。",
    "台词贴着画面": "这句和画面说的是同一件事。台词改说这个画面证明的好处。",
    "人物没带到商品": "整条剧情还没让人听见商品或它的好处。",
    "开场问题没有下文": "开头问了，后面还没有一句回答。",
    "旁白像文案": "这句像文案或参数清单。改成一句口语，只说一个好处，不要加长，也不要编价格。",
    "旁白在堆卖点": "这句又报了另一个卖点。留空，或收成还在说同一个好处。",
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
    form = None
    if strategy is not None:
        try:
            form = script_form(strategy)
        except (ValidationError, TypeError, ValueError, KeyError):
            form = None
    suggestions = []
    for index, beat in enumerate(_beats(script)):
        names = flags.get(beat.get("id")) or []
        narration = beat.get("narration") if isinstance(beat.get("narration"), str) else ""
        if not names or not narration.strip():
            continue
        from .planner_recovery import clip_server_text
        pieces = []
        for name in names:
            if name not in _FOCUS_NOTES:
                continue
            if form == "scene_story" and name == "只描述画面":
                pieces.append("这句在念画面上的动作。改成这个人的反应或包袱，不要改成口播卖点。")
            elif form == "scene_story" and name == "人物没带到商品":
                pieces.append("好处要落在动作的结果上，用人物的一句反应带出来，不要改成口播。")
            else:
                pieces.append(_FOCUS_NOTES[name])
        note = "台词是否对焦商品、前后连贯。" + "".join(pieces)
        suggestions.append({
            "kind": "line_focus",
            "target": f"/ad_script/beats/{index}/narration",
            "quote": narration[:240],
            "note": clip_server_text(note),
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
