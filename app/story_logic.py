"""Story ending, punchlines, proof shots, state stills, gaze, and background cast.

These checks stay generic. A hair dryer is only one home-use example. They do
not invent a shot count, a line, or a first/last-frame video API. The video
model may still ignore a hard cut or a still.
"""
from __future__ import annotations

from copy import deepcopy
import os
import re


_EXIT = re.compile(r"出门|走出门|带出门|带出家|离开家|门外|走出屋|走出家门")
_HOLDING = re.compile(r"拿着|带着|拎着|握着|手里|手上|提着|抱着|举着")
_STAYS = re.compile(r"放回|留在|台上|支架|洗手台|洗手池|水槽|插座上|原处|座上")
_HOME_GOOD = re.compile(
    r"吹风机|电吹风|卷发棒|直发器|直发夹|电动牙刷|剃须刀|加湿器|净水器|"
    r"台灯|电饭煲|空气炸锅|吸尘器|电熨斗|挂烫机|咖啡机|家用电器|插着电|还插着|拖着线"
)
_PRESENTS = re.compile(
    r"举向镜头|对着镜头举|递给镜头|展示给镜头|对着镜头拿|举到镜头|"
    r"棚拍定格|影棚定格|logo居中|标志居中|商标居中"
)
_HERO = re.compile(r"英雄定格|英雄镜头")
_PUNCH_LINE = re.compile(r"没白|终于|还好有它|还好有这|幸亏|幸好")
_PRODUCT_SUBJECT = re.compile(r"它让|这个\S{0,12}让|这款\S{0,8}让")
_PURPOSE_POINT = re.compile(r"替观众|说实|点明|说出画面|点题|把画面里")
_CTA = re.compile(r"链接|点下面|小黄车|去看看|下方|私信|到店|门店|详情页")
_TECHNIQUE = re.compile(
    r"慢动作|慢镜|升格|微距|逆光|轮廓光|侧逆光|发丝边缘|移焦|焦点从|浅景深|"
    r"高速摄影|前景.{0,12}(?:睡|脸|后景)"
)
_CLAIM_VISUAL = {
    "speed": re.compile(r"快|干|吹|风口|气流|风"),
    # A rack focus onto the roommate or sleeper is the quiet proof, even without the word 安静.
    "noise": re.compile(r"安静|不吵|睡|轻声|声音|室友|侧躺|闭眼|装睡|没醒"),
    "smooth": re.compile(r"顺|梳|发丝|毛躁"),
    "ions": re.compile(r"离子|光点|微粒"),
}
_STATE_REF_ON = frozenset({"1", "true", "on", "yes"})
_STATE_REF_OFF = frozenset({"0", "false", "off", "no"})
_SLOGAN = re.compile(r"口号|标语")
_LOOK = re.compile(r"(?<![不别])(?:看向镜头|看着镜头|看镜头|面向镜头|直视镜头|对着镜头)")
_BG_NAME = re.compile(r"室友|同事|朋友|旁边的人|另一个人")
_BG_EQUIV = re.compile(r"床上的人|床上那个人|睡着的人|睡熟的人")
_BG_ABSENT = re.compile(r"没有人|床是空的|空无一人|人不在床|不在床上|已经起床|已经起来|下了床|离开了床")
_BG_SILENCE = re.compile(r"没有人(?=\s*(?:说话|开口|出声|说))")
_CLOCK_GAP = re.compile(r"[\s\u3000，。！？、；：,.!?;:\"'“”‘’「」『』…—\-（）()《》【】\[\]]+")
_ASLEEP = re.compile(r"睡着|在睡|睡熟|酣睡|没醒|未醒")
_AWAKE = re.compile(r"(?<!没)醒了|醒来|已经醒|坐起来")
_LISTENER_WORDS = ("室友", "同事", "朋友", "旁边的人", "另一个人")
# 「另一个人」「不是一个人」 are not "she is alone". 「对面…一个人」 is handled apart.
_ALONE_PERSON = re.compile(r"(?<![另不])(?<!不是)一个人")
_ALONE_NOW = re.compile(r"独自|身旁没有|旁边没有人|没有别人")
_OPPOSITE_ONE = re.compile(r"对面.{0,8}一个人")
# A named companion opposite her. Bare 坐在对面, 对坐, and 对面…人 do not name anyone.
_OPPOSITE_COMPANY = re.compile(
    r"(?:朋友|室友|同事).{0,12}(?:坐在对面|坐在对过|对坐|对面)"
    r"|(?:坐在对面|坐在对过|对坐).{0,12}(?:朋友|室友|同事)"
)
_CLOSE_UP = re.compile(r"特写|微距|大特写|只拍")
_HANDS_OR_CAKE = re.compile(r"手|蛋糕")
# 镜头推近 is the camera, not a person pushing something. 往柜子里推 still counts.
# 缓慢推近 is one camera unit: the 推 is not a body action just because 镜头 is not the previous character.
_CAMERA_MOVE_UNIT = re.compile(r"推(?:近|远|进|向|上|下)")
_BODY_IN_FRAME = re.compile(r"脸|全身|走|站着|坐着|回头|(?<!镜头)推|柜子|脚步")
_INNER_NOTE = "这一场没有醒着的人听，台词改成她自己的内心独白，不对口型。"
_SHOT_INNER_NOTE = "这一镜没有醒着的人听，台词改成她自己的内心独白，不对口型。"
_INNER_MOUTH = "这句是她的内心独白，嘴没有动。"
# Spoken-aloud stage directions. 没有开口 is already quiet and must stay.
_ALOUD_CUE = re.compile(
    r"(?<!没有)开口说话"
    r"|(?<!没有)开口说(?:第\s*[0-9一二两三四五六七八九十]+\s*句)?"
    r"|说出第\s*[0-9一二两三四五六七八九十]+\s*句"
)
_LINE_ORDINAL = re.compile(r"第\s*([0-9一二两三四五六七八九十]+)\s*句")
_SHOT_TEXT_SKIP = {"id", "character_ids", "evidence_ids", "beat_id", "beat_ids", "match_cut", "shot_ids"}
_BED = re.compile(r"床|床铺|被窝|枕头")
_POSE_CARRY = re.compile(
    r"仍在睡|还在睡|还睡着|仍睡着|在睡着|睡着|朝向不变|"
    r"没动|没有动|一动不动|没翻身|没有翻身|未翻身|"
    r"没起身|没有起来|没起来|保持睡|睡姿不变|姿势没变|姿势不变"
)
_SHE_SLEEPER = re.compile(r"她(?:还|仍|也)?(?:在睡|睡着|没动|没有动|没翻身|没有翻身|没起来|没有起来)")
_STORYBOARD_CONTENT_MARKERS = (
    "背景人物没有跟着床入画",
    "背景人物朝向和前面不一致",
    "剧情卖点没有高光证明镜",
    "状态结果还停在半程",
    "结果状态没有单独的结果镜",
    "结果状态和过程写在同一镜",
    "剧情末镜写成了英雄定格",
    "剧情末镜人物对着镜头举着产品",
    "剧情末镜把家用物品带出了门",
)
# More specific orientations win. 侧躺 stays available when the plan never names a facing.
_POSE_RULES = (
    ("面向镜头", re.compile(r"面向镜头|正脸朝|正脸对着镜头")),
    ("脸朝房间", re.compile(r"脸朝房间|脸转向房间|转向房间一侧|脸朝向房间")),
    ("背对", re.compile(r"背对|后脑")),
    ("趴着", re.compile(r"趴着")),
    ("侧躺", re.compile(r"侧躺|侧卧|侧睡")),
)
_FLORAL = re.compile(r"碎花|小花|花纹")
_SIMILAR_COLORS = ("粉色", "粉白", "奶油", "米色", "浅粉", "杏色")
_LOGO_TO_CAMERA = re.compile(
    r"(?:，\s*)?(?:机身\s*)?(?:[A-Za-z][A-Za-z0-9]*\s*)?(?:字样|商标|标志|logo|Logo)\s*朝向?镜头"
)
_STORY_HOLD_LINE = "机位停住，不再推近或变焦。产品从第一帧就以近景留在故事里它该在的位置，不摆向镜头。可以留一个很小的生活动静，手或人只出一角。不出现字幕、Logo或字卡。"
_NEUTRAL_PURPOSE = "停在这一下刚发生的动作上。"


def _field(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def iter_plan_shots(container):
    """Shots on a plan, a segment, or a director board. Segments win when both exist."""
    if not isinstance(container, dict):
        return []
    segments = container.get("segments")
    if isinstance(segments, list) and segments:
        shots = []
        for segment in segments:
            if isinstance(segment, dict):
                shots.extend(shot for shot in segment.get("shots") or [] if isinstance(shot, dict))
        return shots
    shots = container.get("shots")
    if isinstance(shots, list):
        return [shot for shot in shots if isinstance(shot, dict)]
    return []


def _shot_blob(shot):
    if not isinstance(shot, dict):
        return ""
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    parts = [shot.get("description") or "", shot.get("purpose") or ""]
    parts.extend(continuity.get(key) or "" for key in ("start_state", "allowed_changes", "end_state", "camera_move"))
    return " ".join(part for part in parts if isinstance(part, str))


def _product_text(strategy, extra=""):
    parts = [extra or ""]
    if strategy is None:
        return "".join(parts)
    brief = _field(strategy, "brief")
    parts.append(_field(brief, "product_name") or "")
    for fact in _field(brief, "facts") or []:
        parts.append(_field(fact, "text") or "")
    try:
        from .line_quality import _selected, _strategy
        selected = _selected(_strategy(strategy))
    except (TypeError, ValueError, KeyError, StopIteration):
        selected = None
    if selected is not None:
        for field in ("product_expression", "payoff", "side_proof", "hook", "consumer_insight"):
            parts.append(getattr(selected, field, "") or "")
    return "".join(part for part in parts if isinstance(part, str))


def _form(strategy):
    if strategy is None:
        return None
    try:
        from .line_quality import script_form
        return script_form(strategy)
    except (TypeError, ValueError, KeyError):
        brief = _field(strategy, "brief")
        form = _field(brief, "ad_form")
        return form if isinstance(form, str) else None


def _last_shot(container):
    shots = iter_plan_shots(container)
    return shots[-1] if shots else None


def story_ending_issues(container, strategy=None, *, form=None, product_text=""):
    """Fail a 剧情 whose last shot breaks use-logic, presents the product, or says 英雄定格.

    A leftover push-in is not a new failure. The video prompt rewrites the last shot
    into a locked insert close-up. Other ad types are not checked.
    """
    form = form or _form(strategy)
    if form != "scene_story":
        return []
    shot = _last_shot(container)
    if shot is None:
        return []
    blob = _shot_blob(shot)
    goods = _product_text(strategy, product_text) + blob
    issues = []
    if _hits(_HERO, blob):
        issues.append("剧情末镜写成了英雄定格")
    if _hits(_PRESENTS, blob):
        issues.append("剧情末镜人物对着镜头举着产品")
    leaves = _EXIT.search(blob) and _HOLDING.search(blob) and _HOME_GOOD.search(goods) and not _STAYS.search(blob)
    if leaves:
        issues.append("剧情末镜把家用物品带出了门")
    return issues


def line_is_punchline(text):
    """A closing line that sums the event up, or lets the product be the subject."""
    if not isinstance(text, str) or not text.strip() or _CTA.search(text):
        return False
    return bool(_PUNCH_LINE.search(text) or _PRODUCT_SUBJECT.search(text))


def purpose_is_punchline(text):
    return bool(isinstance(text, str) and _PURPOSE_POINT.search(text))


def scrub_story_purposes(director, strategy=None):
    """Replace a 点题 purpose with an in-the-moment one. The line itself is not rewritten here."""
    if _form(strategy) != "scene_story" or not isinstance(director, dict):
        return director, False
    revised = deepcopy(director)
    changed = False
    for shot in iter_plan_shots(revised):
        purpose = shot.get("purpose")
        if purpose_is_punchline(purpose):
            shot["purpose"] = _NEUTRAL_PURPOSE
            changed = True
    return revised, changed


def _claim_tags_in(text):
    from .line_quality import _ordered_tags
    return _ordered_tags(text or "")


def proof_claims(strategy):
    """Benefits this story says it proves. No benefit means no highlight requirement."""
    if _form(strategy) != "scene_story" or strategy is None:
        return []
    try:
        from .line_quality import _selected, _strategy
        selected = _selected(_strategy(strategy))
    except (TypeError, ValueError, KeyError, StopIteration):
        return []
    blob = "".join(
        getattr(selected, field, "") or ""
        for field in ("product_expression", "payoff", "side_proof", "hook", "consumer_insight")
    )
    return _claim_tags_in(blob)


def _shot_covers_claim(shot, tag):
    blob = _shot_blob(shot)
    if not _TECHNIQUE.search(blob):
        return False
    visual = _CLAIM_VISUAL.get(tag)
    return bool(visual and visual.search(blob))


def proof_highlight_issues(container, strategy=None):
    """Each benefit a 剧情 proves needs one shot whose description names a visual technique."""
    claims = proof_claims(strategy)
    if not claims:
        return []
    shots = iter_plan_shots(container)
    missing = [tag for tag in claims if not any(_shot_covers_claim(shot, tag) for shot in shots)]
    if not missing:
        return []
    return ["剧情卖点没有高光证明镜"]


def state_references_enabled():
    """AD_STATE_REFERENCES defaults on. off, false, 0 and no plan no state stills."""
    raw = os.getenv("AD_STATE_REFERENCES", "").strip().lower()
    if not raw:
        return True
    if raw in _STATE_REF_OFF:
        return False
    if raw in _STATE_REF_ON:
        return True
    raise ValueError("AD_STATE_REFERENCES must be on or off")


_STATE_FIELDS = ("subject_label", "owner", "start", "result", "start_shot", "result_shot")


def _shot_order(shots):
    order = {}
    for index, shot in enumerate(shots):
        shot_id = shot.get("id") if isinstance(shot, dict) else None
        if isinstance(shot_id, str) and shot_id and shot_id not in order:
            order[shot_id] = index
    return order


def _plan_seconds(container):
    if not isinstance(container, dict):
        return None
    segments = container.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    total = 0.0
    seen = False
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        duration = segment.get("duration_seconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            continue
        total += float(duration)
        seen = True
    return total if seen else None


def _known_owners(container):
    production = container.get("production") if isinstance(container, dict) and isinstance(container.get("production"), dict) else {}
    owners = {"product"}
    for person in production.get("characters") or []:
        if isinstance(person, dict) and isinstance(person.get("id"), str) and person["id"].strip():
            owners.add(person["id"].strip())
    return owners


def _change_record(item):
    if not isinstance(item, dict):
        return None
    values = []
    for key in _STATE_FIELDS:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        values.append(value.strip())
    return dict(zip(_STATE_FIELDS, values))


def accepted_state_changes(container, *, enabled_only=True):
    """Keep structurally valid declared changes. Never invent one from shot prose."""
    if not isinstance(container, dict) or (enabled_only and not state_references_enabled()):
        return [], []
    raw = container.get("state_changes")
    if not raw:
        return [], []
    if not isinstance(raw, list):
        return [], ["状态变化列表无法读取，已放下。"]
    shots = iter_plan_shots(container)
    order = _shot_order(shots)
    owners = _known_owners(container)
    seconds = _plan_seconds(container)
    kept, warnings, seen = [], [], {}
    for item in raw:
        change = _change_record(item)
        if change is None:
            warnings.append("状态变化缺少主体、起止或镜头，已放下。")
            continue
        start_at, result_at = order.get(change["start_shot"]), order.get(change["result_shot"])
        if start_at is None or result_at is None:
            warnings.append("状态变化的镜头不存在，已放下。")
            continue
        if start_at >= result_at:
            warnings.append("状态变化的起始镜不在结果镜之前，已放下。")
            continue
        if change["owner"] not in owners:
            warnings.append("状态变化的主体不存在，已放下。")
            continue
        if seconds is not None and seconds <= 15 and seen.get(change["owner"]):
            warnings.append("15秒及以内每个角色只保留一条状态变化，多出来的已放下。")
            continue
        seen[change["owner"]] = True
        kept.append(change)
    return kept, warnings


def state_change_warnings(container):
    _, warnings = accepted_state_changes(container)
    return warnings


def _short_state(text):
    first = re.split(r"[，。；、]", text, maxsplit=1)[0].strip()
    return (first or text)[:16]


def _declared_label(subject, text, phase_name):
    return f"{subject}{_short_state(text)}·{phase_name}"[:40]


def state_phrases(*texts):
    """Phrases the planner wrote for this change. Used only to un-lock that wording."""
    phrases = []
    for text in texts:
        if not isinstance(text, str):
            continue
        for part in re.split(r"[，。；、]", text):
            part = part.strip()
            if part and part not in phrases:
                phrases.append(part)
    return phrases


def _change_id(change, index):
    def safe(value, fallback):
        token = re.sub(r"[^a-zA-Z0-9_]", "_", value or "")
        return token or fallback
    return f"{safe(change['owner'], 'owner')}_{safe(change['start_shot'], 'start')}_{safe(change['result_shot'], 'result')}_{index}"


def _shot_identity(shot, index):
    shot_id = shot.get("id") if isinstance(shot, dict) and isinstance(shot.get("id"), str) and shot.get("id") else f"n{index}"
    return shot_id


def _push_state(refs, shot, index, phase, label, subject, character_id, change_id, state_text, phrases):
    shot_id = _shot_identity(shot, index)
    item = {
        "key": f"state_{change_id}_{phase}",
        "kind": "start_frame",
        "name": label,
        "state_id": f"{change_id}:{phase}",
        "phase": phase,
        "shot_id": shot_id,
        "label": label,
        "subject": subject,
        "state_change_id": change_id,
        "state_text": state_text,
        "appearance_phrases": list(phrases),
    }
    if character_id:
        item["character_id"] = character_id
    refs.append(item)


def plan_state_references(container, *, limit=3):
    """One start still and one result still per declared state change.

    Shot prose is not scanned. A row is kept only when both shots exist, the
    start shot is earlier, and the owner is a known character or product.
    Anything else is dropped. At most `limit` changes. AD_STATE_REFERENCES=off
    plans nothing.
    """
    if not state_references_enabled():
        return []
    kept, _warnings = accepted_state_changes(container)
    shots = iter_plan_shots(container)
    order = _shot_order(shots)
    refs = []
    for index, change in enumerate(kept):
        if len(refs) >= limit * 2:
            break
        change_id = _change_id(change, index)
        phrases = state_phrases(change["start"], change["result"])
        character_id = None if change["owner"] == "product" else change["owner"]
        start_at, result_at = order[change["start_shot"]], order[change["result_shot"]]
        _push_state(refs, shots[start_at], start_at, "start",
                    _declared_label(change["subject_label"], change["start"], "起始"),
                    change["subject_label"], character_id, change_id, change["start"], phrases)
        _push_state(refs, shots[result_at], result_at, "result",
                    _declared_label(change["subject_label"], change["result"], "结果"),
                    change["subject_label"], character_id, change_id, change["result"], phrases)
    return refs


def _continuity_of(shot):
    continuity = shot.get("continuity") if isinstance(shot, dict) else None
    return continuity if isinstance(continuity, dict) else None


def _write_state(shot, key, value):
    continuity = _continuity_of(shot)
    if continuity is None or not isinstance(value, str) or not value or continuity.get(key) == value:
        return False
    continuity[key] = value
    return True


def align_declared_continuity(container):
    """Line up declared appearance and the next shot's start. No keyword table.

    A declared result shot opens on the result text. Every earlier shot of that
    change still ends on the start text. Elsewhere, a shot whose start and end
    are the same while an action is allowed takes the next shot's start as its
    end, and a continuous next shot copies the previous end.
    """
    if not isinstance(container, dict):
        return container, False
    revised = deepcopy(container)
    shots = iter_plan_shots(revised)
    kept, _warnings = accepted_state_changes(revised, enabled_only=False)
    order = _shot_order(shots)
    result_ids = {change["result_shot"] for change in kept}
    changed = False
    for change in kept:
        start_i, result_i = order[change["start_shot"]], order[change["result_shot"]]
        for index in range(start_i, result_i):
            changed = _write_state(shots[index], "end_state", change["start"]) or changed
            if index + 1 < result_i:
                changed = _write_state(shots[index + 1], "start_state", change["start"]) or changed
        changed = _write_state(shots[result_i], "start_state", change["result"]) or changed
        changed = _write_state(shots[result_i], "end_state", change["result"]) or changed
        previous = _continuity_of(shots[result_i - 1]) if result_i else None
        current = _continuity_of(shots[result_i])
        if (isinstance(previous, dict) and isinstance(current, dict)
                and current.get("entry_transition") == "continuous"
                and previous.get("end_state") != current.get("start_state")):
            current["entry_transition"] = "cut"
            changed = True
        if result_i + 1 < len(shots):
            following = _continuity_of(shots[result_i + 1])
            if isinstance(following, dict) and following.get("entry_transition") in (None, "", "continuous"):
                changed = _write_state(shots[result_i + 1], "start_state", change["result"]) or changed
    for index in range(len(shots) - 1):
        current_shot, nxt = shots[index], shots[index + 1]
        if isinstance(nxt, dict) and nxt.get("id") in result_ids:
            continue
        # A declared result already opens and ends on its result text. Letting
        # the next hard cut's start replace that end makes the next pass write
        # the result back, so the same board never settles.
        if isinstance(current_shot, dict) and current_shot.get("id") in result_ids:
            continue
        current, following = _continuity_of(shots[index]), _continuity_of(nxt)
        if not isinstance(current, dict) or not isinstance(following, dict):
            continue
        start = current.get("start_state") if isinstance(current.get("start_state"), str) else ""
        end = current.get("end_state") if isinstance(current.get("end_state"), str) else ""
        allowed = current.get("allowed_changes") if isinstance(current.get("allowed_changes"), str) else ""
        next_start = following.get("start_state") if isinstance(following.get("start_state"), str) else ""
        if start and end and start == end and allowed.strip() and next_start and next_start != end:
            current["end_state"] = next_start
            end = next_start
            changed = True
        if following.get("entry_transition") == "continuous" and isinstance(end, str) and end and following.get("start_state") != end:
            following["start_state"] = end
            changed = True
    return revised, changed


_COUNT_WORDS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_PROP_COUNT = re.compile(
    r"(?:剩下|只剩|还有|就剩|仅剩|剩)?([零一两二三四五六七八九十]|\d+)"
    r"(颗|只|个|枚|块|片|根|支)([\u4e00-\u9fff]{1,4})"
)
_PROP_ABSENT = re.compile(
    r"(?:没有|没了|不见了?)(?:[零一两二三四五六七八九十]|\d+)?"
    r"(?:颗|只|个|枚|块|片|根|支)?([\u4e00-\u9fff]{1,4})"
    r"|([\u4e00-\u9fff]{1,4})(?:不见了|没有了|没了)"
)
_PROP_SKIP = {"人", "她", "他", "女生", "男生"}
_PROP_TAIL = set("了的在上里和与放还已")


def _clean_prop_noun(noun):
    if not isinstance(noun, str):
        return ""
    while noun and noun[-1] in _PROP_TAIL:
        noun = noun[:-1]
    return noun.strip()


def _skip_prop_noun(noun):
    if not noun:
        return True
    return noun in _PROP_SKIP or noun.rstrip("们") in _PROP_SKIP


def _prop_counts(text):
    """Count or absence of a prop noun. People are not props."""
    found = {}
    if not isinstance(text, str) or not text:
        return found
    for match in _PROP_COUNT.finditer(text):
        noun = _clean_prop_noun(match.group(3))
        if _skip_prop_noun(noun):
            continue
        raw = match.group(1)
        found[noun] = _COUNT_WORDS[raw] if raw in _COUNT_WORDS else int(raw)
    for match in _PROP_ABSENT.finditer(text):
        noun = _clean_prop_noun(match.group(1) or match.group(2))
        if _skip_prop_noun(noun):
            continue
        found[noun] = 0
    return found


def repair_prop_handoff(container):
    """Copy a prop's end state onto the next shot when the count or presence disagrees.

    A declared result shot keeps the result it was given. The previous shot's
    ending is not rewritten, so this pass does not trade states with the
    continuity alignment.
    """
    if not isinstance(container, dict):
        return container, False
    shots = iter_plan_shots(container)
    if len(shots) < 2:
        return container, False
    kept, _warnings = accepted_state_changes(container, enabled_only=False)
    order = _shot_order(shots)
    owned = set()
    for change in kept:
        start_i, result_i = order.get(change["start_shot"]), order.get(change["result_shot"])
        if start_i is None or result_i is None:
            continue
        for index in range(start_i + 1, result_i + 1):
            shot_id = shots[index].get("id")
            if isinstance(shot_id, str):
                owned.add(shot_id)
    edits = []
    for index in range(len(shots) - 1):
        current, nxt = shots[index], shots[index + 1]
        if isinstance(nxt.get("id"), str) and nxt["id"] in owned:
            continue
        current_state, next_state = _continuity_of(current), _continuity_of(nxt)
        if not isinstance(current_state, dict) or not isinstance(next_state, dict):
            continue
        end, start = current_state.get("end_state"), next_state.get("start_state")
        if not isinstance(end, str) or not isinstance(start, str) or not end or end == start:
            continue
        left, right = _prop_counts(end), _prop_counts(start)
        if any(left[noun] != right[noun] for noun in left.keys() & right.keys()):
            edits.append(index)
    if not edits:
        return container, False
    revised = deepcopy(container)
    revised_shots = iter_plan_shots(revised)
    for index in edits:
        revised_shots[index + 1]["continuity"]["start_state"] = revised_shots[index]["continuity"]["end_state"]
    return revised, True


def state_pairing_issues(container):
    """Pairing is no longer a reason to refuse a plan."""
    return []

def state_prompt_lines(shot, roles, shot_ids=None):
    """States are already drawn on the character sheet. Do not name a separate still."""
    if not isinstance(shot, dict):
        return []
    shot_id = shot.get("id")
    start = next((role for role in roles or [] if role.get("role") == "state" and role.get("phase") == "start" and role.get("shot_id") == shot_id), None)
    result = next((role for role in roles or [] if role.get("role") == "state" and role.get("phase") == "result" and role.get("shot_id") == shot_id), None)
    lines = []
    if start and result:
        lines.append("  本镜从角色参考上的起始状态画到结果状态，中间由画面补上。")
    elif start:
        lines.append("  本镜开始对齐角色参考上的起始状态，停在过程里，不要跳到结果。")
    elif result:
        lines.append("  本镜硬切到角色参考上的结果状态，不要在这一镜里从上一状态渐变。")
    if isinstance(shot_ids, list) and shot_id in shot_ids:
        here = shot_ids.index(shot_id)
        present = [item for item in (shot.get("character_ids") or []) if isinstance(item, str)]
        for role in roles or []:
            if role.get("role") != "state" or role.get("phase") != "result":
                continue
            result_shot = role.get("shot_id")
            if not isinstance(result_shot, str) or result_shot not in shot_ids or shot_ids.index(result_shot) >= here:
                continue
            cid = role.get("character_id")
            if isinstance(cid, str) and cid and cid not in present:
                continue
            line = "  本镜人物仍用角色参考上的结果状态，不要退回起始状态。"
            if line not in lines:
                lines.append(line)
    return lines


def _hits(pattern, text):
    """A hard-gate word counts only when this shot actually states it."""
    from .picture_rules import mentioned
    return bool(text) and mentioned(pattern, text)


def story_needs_lens(shot):
    return _hits(_LOOK, _shot_blob(shot))


def story_gaze_line(shot, form):
    """剧情 defaults to no eye contact unless this shot writes that it needs the lens."""
    if form != "scene_story" or not isinstance(shot, dict):
        return None
    if not shot.get("character_ids"):
        return None
    if story_needs_lens(shot):
        return None
    return "  本镜人物不看镜头。"


def _declared_pose(text):
    if not text:
        return None
    for name, pattern in _POSE_RULES:
        if pattern.search(text):
            return name
    return None


def _person_referenced(blob, name):
    """The roster person, including 床上的人 / 睡着的人 / an unambiguous 她."""
    if not blob:
        return False
    if name and name in blob:
        return True
    if _BG_EQUIV.search(blob):
        return True
    if name and "她" in blob and (_POSE_CARRY.search(blob) or _SHE_SLEEPER.search(blob)):
        return True
    return False


def _pose_carried(blob, established):
    """A declared pose, or a phrase that keeps the pose already in force."""
    if _declared_pose(blob):
        return True
    if _POSE_CARRY.search(blob or ""):
        return True
    return bool(established and established in (blob or ""))


def _remember_pose(established, pose):
    if pose and pose != "面向镜头":
        return pose
    if pose == "面向镜头" and established is None:
        return pose
    return established


def _pose_in_force(shots, index):
    """Identity from the first appearance. Pose from the latest shot that declares one."""
    name = None
    pose = None
    for cursor, shot in enumerate(shots or []):
        if cursor > index:
            break
        blob = _shot_blob(shot)
        found = _BG_NAME.search(blob)
        if found and name is None:
            name = found.group(0)
        declared = _declared_pose(blob)
        if declared:
            pose = declared
        elif pose is None and "睡" in blob and found:
            pose = "睡着"
    return name, pose


def background_roster(shots):
    """The background person, with the pose in force at the last shot."""
    if not shots:
        return None
    name, pose = _pose_in_force(shots, len(list(shots)) - 1)
    if not name or not pose:
        return None
    return {"name": name, "pose": pose}


def _bed_in_frame(shot):
    """The bed is in this shot only when the picture or the camera says so.

    An inherited start_state can mention the bed from the previous shot. A
    hand or product close-up does not bring that bed, or the person on it, into frame.
    """
    if not isinstance(shot, dict):
        return False
    description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    camera = continuity.get("camera_move") if isinstance(continuity.get("camera_move"), str) else ""
    from .picture_rules import mentioned
    return mentioned(_BED, description) or mentioned(_BED, camera)


def _bed_problem(shot, blob, name, established):
    """absent or facing. A missing pose restatement is not a problem."""
    if not _bed_in_frame(shot):
        return None
    pose = _declared_pose(blob)
    if name is None or _background_absent(blob) or not _person_referenced(blob, name):
        return "absent"
    if pose == "面向镜头" and established and established != "面向镜头":
        return "facing"
    return None


def _walk_background(shots):
    name = None
    established = None
    for shot in shots:
        blob = _shot_blob(shot)
        found = _BG_NAME.search(blob)
        if found and name is None:
            name = found.group(0)
        pose = _declared_pose(blob)
        problem = _bed_problem(shot, blob, name, established)
        yield shot, blob, name, established, pose, problem
        established = _remember_pose(established, pose)


def background_issues(container):
    """A bed in frame keeps the person. A declared turn wins; the camera-facing turn does not.

    床上的人、睡着的人、不会认错的「她」，以及仍在睡、还在睡、没动、没翻身，都算人还在、姿势还在。
    床入画只看这一镜的画面和机位；继承来的起止状态不算床已经入画。人还在时，连续性的起止状态也算。
    没把姿势再写一遍不算失败。
    """
    shots = iter_plan_shots(container)
    if not shots or background_roster(shots) is None:
        return []
    issues = []
    for _shot, _blob, _name, _established, _pose, problem in _walk_background(shots):
        if problem == "absent":
            issues.append("背景人物没有跟着床入画")
        elif problem == "facing":
            issues.append("背景人物朝向和前面不一致")
    return list(dict.fromkeys(issues))


def background_contradiction_shots(container):
    """Shots a model rewrite may fix: the person is gone, or the pose flips to the lens."""
    shots = iter_plan_shots(container)
    if not shots or background_roster(shots) is None:
        return []
    found = []
    for shot, _blob, name, established, _pose, problem in _walk_background(shots):
        if not problem:
            continue
        found.append({
            "shot_id": shot.get("id") or "",
            "description": shot.get("description") or "",
            "problem": "背景人物朝向和前面不一致" if problem == "facing" else "背景人物没有跟着床入画",
            "name": name or "",
            "pose": established or "",
        })
    return found


def _pose_line(name, pose):
    return f"{name}仍{pose}，朝向不变，同一套衣服，仍在原来的床位"


def _append_pose_line(description, name, pose):
    if not isinstance(description, str) or not name or not pose:
        return description
    extra = _pose_line(name, pose)
    if extra in description or f"{name}仍{pose}，朝向不变" in description:
        return description
    if _declared_pose(description) or (name in description and pose in description):
        return description
    updated = description.rstrip("。").rstrip() + "。" + extra + "。"
    if len(updated) > 1500:
        return description
    return updated


def with_background_line(description, shots, index=None):
    """Carry the latest declared pose. A pose this shot already states is left alone."""
    from .picture_rules import mentioned
    if not isinstance(description, str) or not shots or not mentioned(_BED, description):
        return description
    if _background_absent(description):
        return description
    shots = [shot for shot in shots if isinstance(shot, dict)]
    if index is None:
        index = next((cursor for cursor, shot in enumerate(shots) if (shot.get("description") or "") == description), None)
        if index is None:
            index = len(shots) - 1
    name, pose = _pose_in_force(shots, index)
    if name and name in description and not mentioned(re.compile(re.escape(name)), description):
        return description
    return _append_pose_line(description, name, pose)


def stamp_background_lines(container):
    """Write the pose line onto bed shots that already refer to the person.

    A shot that never mentions the sleeper, or that turns them toward the lens, is left for rewrite.
    """
    if not isinstance(container, dict):
        return container, False
    revised = deepcopy(container)
    shots = iter_plan_shots(revised)
    if not shots or background_roster(shots) is None:
        return container, False
    changed = False
    for index, (shot, blob, name, established, _pose, problem) in enumerate(_walk_background(shots)):
        if problem or not _bed_in_frame(shot):
            continue
        if not _person_referenced(blob, name):
            continue
        if not _pose_carried(blob, established) and _declared_pose(blob) == "面向镜头":
            continue
        description = shot.get("description") if isinstance(shot.get("description"), str) else ""
        if not description or _declared_pose(description):
            continue
        forced_name, forced_pose = _pose_in_force(shots, index)
        updated = _append_pose_line(description, forced_name, forced_pose)
        if updated != description:
            shot["description"] = updated
            changed = True
    return (revised, True) if changed else (container, False)


def apply_background_rewrite(director, raw, issues):
    """Accept a description fix for the named shots, or leave the draft untouched."""
    if not isinstance(director, dict) or not isinstance(raw, dict) or not isinstance(issues, list) or not issues:
        return None
    incoming = raw.get("shots")
    if not isinstance(incoming, list):
        return None
    expected = [item.get("shot_id") for item in issues]
    if any(not shot_id for shot_id in expected):
        return None
    got = []
    by_id = {}
    for item in incoming:
        if not isinstance(item, dict):
            return None
        shot_id = item.get("shot_id")
        description = item.get("description")
        if not isinstance(shot_id, str) or not isinstance(description, str) or not description.strip():
            return None
        if shot_id in by_id:
            return None
        by_id[shot_id] = description.strip()
        got.append(shot_id)
    if set(got) != set(expected):
        return None
    revised = deepcopy(director)
    shots = {shot.get("id"): shot for shot in iter_plan_shots(revised)}
    for item in issues:
        shot = shots.get(item["shot_id"])
        if shot is None:
            return None
        source = f"{item.get('description') or ''}{item.get('name') or ''}{item.get('pose') or ''}"
        description = by_id[item["shot_id"]]
        if set(re.findall(r"\d+", description)) - set(re.findall(r"\d+", source)):
            return None
        if len(description) > 1500:
            return None
        shot["description"] = description
    if background_issues(revised):
        return None
    return revised


BACKGROUND_REWRITE_SYSTEM = """你只改列出的镜头说明，只输出 JSON 对象，对象里只有 shots，每项只有 shot_id 和 description。
床在画面里时，必须能看见列出的这个人，并写明仍保持给出的睡姿和朝向。没有写翻身，就不能改成面向镜头，也不能把人从床上拿掉。
可以写成「室友仍侧躺，朝向不变」。只使用该镜原文和列出的 name、pose，不新增功效、数字和人名。不改其他镜头，不解释。"""


def _background_rewrite_payload(issues):
    return {
        "stage": "storyboard",
        "task_kind": "background_continuity_rewrite",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["shots"],
            "properties": {
                "shots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["shot_id", "description"],
                        "properties": {
                            "shot_id": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
            },
        },
        "shots": issues,
    }


async def rewrite_background_if_needed(stage_call, director, plan):
    """Return (called, replacement). A continuous board does not call the model."""
    issues = background_contradiction_shots(plan)
    if not issues:
        issues = background_contradiction_shots(director)
    if not issues:
        return False, None
    try:
        raw = await stage_call(BACKGROUND_REWRITE_SYSTEM, _background_rewrite_payload(issues), [], 1200)
    except Exception:
        return True, None
    revised = apply_background_rewrite(director, raw, issues)
    return True, revised


_RERUN_STORYBOARD_TOKENS = (
    "结构不完整",
    "未能满足已确认要求",
    "容量仍未改善",
    "声音方式和广告类型对不上",
    "占位稿不是完成的方案",
)


def _failure_text(message="", issues=()):
    text = str(message or "")
    if isinstance(issues, dict):
        text += "".join(str(value) for value in issues.values())
    elif isinstance(issues, (list, tuple)):
        text += "".join(str(value) for value in issues)
    else:
        text += str(issues or "")
    return text


def storyboard_content_failure(message="", issues=()):
    """True when a saved storyboard failed a shot-level check and must be written again."""
    return any(token in _failure_text(message, issues) for token in _STORYBOARD_CONTENT_MARKERS)


def planning_rerun_storyboard(message="", issues=()):
    """True when a retry must write the storyboard again instead of replaying it.

    A voice mismatch and a local template are included. Replaying the board that
    already failed, or the template that replaced it, skips storyboarding and review.
    """
    text = _failure_text(message, issues)
    if any(token in text for token in _STORYBOARD_CONTENT_MARKERS):
        return True
    return any(token in text for token in _RERUN_STORYBOARD_TOKENS)


def rest_product_for_story_ending(shot):
    """A story ending leaves the product where the action put it. No logo aimed at the lens."""
    if not isinstance(shot, dict):
        return shot
    updated = {**shot, "description": _LOGO_TO_CAMERA.sub("", shot.get("description") or "")}
    continuity = shot.get("continuity")
    if isinstance(continuity, dict):
        continuity = {**continuity}
        for key in ("start_state", "allowed_changes", "end_state"):
            if isinstance(continuity.get(key), str):
                continuity[key] = _LOGO_TO_CAMERA.sub("", continuity[key])
        updated["continuity"] = continuity
    updated["description"] = re.sub(r"，{2,}", "，", updated["description"]).strip("，")
    return updated


def shared_characters(plan, character):
    """Other approved people who appear in the same shot as this one."""
    if not isinstance(plan, dict) or not isinstance(character, dict):
        return []
    approved = [person for person in ((plan.get("production") or {}).get("characters") or []) if isinstance(person, dict)]
    others = [person for person in approved if person.get("id") != character.get("id")]
    found = []
    own_name = character.get("name") if isinstance(character.get("name"), str) else ""
    for shot in iter_plan_shots(plan):
        ids = shot.get("character_ids") if isinstance(shot.get("character_ids"), list) else []
        blob = _shot_blob(shot)
        for person in others:
            if person in found:
                continue
            other_name = person.get("name") if isinstance(person.get("name"), str) else ""
            if character.get("id") in ids and person.get("id") in ids:
                found.append(person)
            elif own_name and other_name and own_name in blob and other_name in blob:
                found.append(person)
    return found


def cast_distinction_line(plan, character):
    """Image-prompt rule. Two people in one frame cannot share a floral pajama set."""
    shared = shared_characters(plan, character)
    if not shared:
        return ""
    production = (plan or {}).get("production") or {}
    cast = [person for person in production.get("characters") or [] if isinstance(person, dict)]
    lead_id = production.get("speaker_id") or (cast[0].get("id") if cast else "")
    names = "、".join(person.get("name") or "另一人" for person in shared)
    if character.get("id") == lead_id:
        wardrobe = "这是主要行动的人：上衣用纯色，不要碎花"
    else:
        wardrobe = "这是另一人：衣服用和对方明显不同的颜色，不要碎花"
    other = "；".join(
        f"{person.get('name') or '另一人'}：{person.get('description') or ''}" for person in shared)[:240]
    return (
        f"同框区分：这一人和{names}会出现在同一画面。{wardrobe}。"
        "发型长短或刘海也要和对方不同，不要两个人都是差不多的长黑发加浅色碎花睡衣。"
        "不要只把同一套碎花换成深浅。"
        f"另一人的资料：{other}。"
    )


def _wardrobes_too_similar(left, right):
    if _FLORAL.search(left or "") and _FLORAL.search(right or ""):
        return True
    return any(color in (left or "") and color in (right or "") for color in _SIMILAR_COLORS)


def cast_distinction_issues(plan):
    """Light check. Vague pajamas are left to the image prompt; matching florals are not."""
    characters = [person for person in (((plan or {}).get("production") or {}).get("characters") or []) if isinstance(person, dict)]
    if len(characters) < 2:
        return []
    issues = []
    seen = set()
    for character in characters:
        for other in shared_characters(plan, character):
            pair = tuple(sorted((str(character.get("id")), str(other.get("id")))))
            if pair in seen:
                continue
            seen.add(pair)
            if _wardrobes_too_similar(character.get("description") or "", other.get("description") or ""):
                issues.append("同框角色的衣服和发型太接近")
    return issues


def _last_narration(plan):
    script = (plan or {}).get("ad_script") if isinstance(plan, dict) else None
    beats = script.get("beats") if isinstance(script, dict) else None
    if not isinstance(beats, list) or not beats or not isinstance(beats[-1], dict):
        return ""
    text = beats[-1].get("narration")
    return text if isinstance(text, str) else ""


def story_plan_issues(plan, strategy):
    """Hard 剧情 problems only: a selling line, a look at the lens, or a pushy ending.

    Punchlines, proof shots, carrying a home good out, state pairing, background
    cast and wardrobe stay as rewrites or warnings. Other ad types are not refused here.
    """
    if _form(strategy) != "scene_story":
        return []
    issues = []
    for item in story_ending_issues(plan, strategy):
        if "英雄定格" in item or "举着产品" in item:
            issues.append(item)
    shot = _last_shot(plan)
    closing = _shot_blob(shot) if shot is not None else ""
    line = _last_narration(plan)
    if _hits(_LOGO_TO_CAMERA, closing):
        issues.append("剧情末镜把商标朝向镜头")
    if any(_hits(pattern, text) for pattern in (_CTA, _SLOGAN) for text in (closing, line) if text):
        issues.append("剧情末镜在喊口号或链接")
    if any(_hits(_LOOK, _shot_blob(item)) for item in iter_plan_shots(plan)):
        issues.append("剧情人物看着镜头")
    try:
        from .line_quality import script_line_offenses
        script = (plan or {}).get("ad_script") or {}
        offenses = script_line_offenses(script, strategy, plan=plan)
        if any("在说卖点" in (item.get("offenses") or []) for item in offenses):
            issues.append("剧情对白还在说卖点")
    except (TypeError, ValueError, KeyError):
        pass
    return list(dict.fromkeys(issues))


_LINE_ORDINALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_QUIET_LINE = re.compile(r"没说话|不说话|没有说话|未说话|不开口|没有开口|未开口|没有张嘴|嘴没有动")
_GARMENTS = ("夹克", "外套", "大衣", "风衣", "羽绒服", "卫衣", "衬衫", "毛衣", "针织衫",
             "背心", "马甲", "连衣裙", "半身裙", "牛仔裤", "短裤", "睡衣", "西装", "围巾")


def _line_number(token):
    if token.isdigit():
        return int(token)
    return _LINE_ORDINALS.get(token)


def _shot_text(shot):
    return f"{shot.get('description') or ''} {shot.get('purpose') or ''}"


def _declares_line(shot, span, number):
    from .picture_rules import mentioned
    blob = _shot_text(shot)
    text = span.get("text") if isinstance(span.get("text"), str) else ""
    text = text.strip()
    if len(text) >= 2 and mentioned(re.compile(re.escape(text)), blob):
        return True
    labels = [str(number)]
    for word, value in _LINE_ORDINALS.items():
        if value == number:
            labels.append(word)
    return any(mentioned(re.compile(rf"第\s*{re.escape(label)}\s*句"), blob) for label in labels)


def _is_quiet(shot):
    from .picture_rules import mentioned
    return mentioned(_QUIET_LINE, _shot_text(shot))


def _declaring_shot(shots, span, number):
    found = [shot for shot in shots if isinstance(shot, dict) and _declares_line(shot, span, number)]
    speakers = [shot for shot in found if not _is_quiet(shot)] or found
    if len(speakers) == 1:
        return speakers[0]
    return None


def _inside(shot, start, end):
    return float(shot["start"]) - 1e-7 <= start and end <= float(shot["end"]) + 1e-7


def _overlap(shot, start, end):
    return min(float(shot["end"]), end) - max(float(shot["start"]), start) > 1e-7


def _sync_beats(shots, groups):
    from .schemas import shot_beat_sequence
    order = [group["beat_id"] for group in groups if isinstance(group, dict) and group.get("beat_id")]
    if not order:
        return True
    for shot in shots:
        expected = [group["beat_id"] for group in groups
                    if isinstance(group, dict) and _overlap(shot, float(group["start"]), float(group["end"]))]
        if not expected:
            return False
        shot["beat_ids"] = expected
        shot["beat_id"] = expected[0]
    try:
        return shot_beat_sequence(shots) == order
    except (ValueError, KeyError, TypeError):
        return False


def _link_speech(spans, shots):
    for span in spans:
        if not isinstance(span, dict):
            continue
        try:
            start, end = float(span["start"]), float(span["end"])
        except (TypeError, ValueError, KeyError):
            return False
        beat = span.get("beat_id")
        ids = []
        for shot in shots:
            if not _overlap(shot, start, end):
                continue
            owned = shot.get("beat_ids") or [shot.get("beat_id")]
            if beat and beat not in owned:
                continue
            if shot.get("id") not in ids:
                ids.append(shot["id"])
        if not ids:
            return False
        span["shot_ids"] = ids
        for anchor in span.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            try:
                a_start, a_end = float(anchor["start"]), float(anchor["end"])
            except (TypeError, ValueError, KeyError):
                anchor["shot_ids"] = list(ids)
                continue
            anchor_ids = [shot_id for shot_id in ids
                          if any(shot.get("id") == shot_id and _overlap(shot, a_start, a_end) for shot in shots)]
            anchor["shot_ids"] = anchor_ids or list(ids)
    return True


def _background_absent(text):
    """没有人说话 means silence. 床上没有人 and 床是空的 still mean the person is gone."""
    if not isinstance(text, str) or not text:
        return False
    return _BG_ABSENT.search(_BG_SILENCE.sub("没出声", text)) is not None


def _spoken_chars(text):
    return len(_CLOCK_GAP.sub("", text or ""))


def _clock_fits(span, start, end):
    from .schemas import shot_character_limit
    text = span.get("text") if isinstance(span.get("text"), str) else ""
    return end > start and _spoken_chars(text) <= shot_character_limit(start, end)


def _fitted_duration(span, duration, room):
    """Keep the arranged length when the words fit. Widen only inside the free range."""
    if duration <= 0 or room + 1e-7 < duration:
        return None
    if _clock_fits(span, 0.0, duration):
        return duration
    steps = int((room - duration + 1e-7) / 0.1)
    for step in range(1, steps + 1):
        widened = round(duration + step * 0.1, 10)
        if widened - room > 1e-7:
            break
        if _clock_fits(span, 0.0, widened):
            return widened
    if _clock_fits(span, 0.0, room):
        return room
    return None


def _remember_group(groups, beat_id, start, end):
    group = next((item for item in groups if isinstance(item, dict) and item.get("beat_id") == beat_id), None)
    if group is None:
        return False
    group["speech_start"] = start
    group["speech_end"] = end
    return True


def _cover_group(groups, beat_id, shot):
    """Let this shot overlap the beat so the moved line may cite it."""
    group = next((item for item in groups if isinstance(item, dict) and item.get("beat_id") == beat_id), None)
    if group is None:
        return False
    try:
        g_start, g_end = float(group["start"]), float(group["end"])
        s_start, s_end = float(shot["start"]), float(shot["end"])
    except (TypeError, ValueError, KeyError):
        return False
    if _overlap(shot, g_start, g_end):
        return True
    if s_start >= g_end:
        group["end"] = min(s_end, s_start + 0.05)
    elif s_end <= g_start:
        group["start"] = max(0.0, s_end - 0.05)
    return _overlap(shot, float(group["start"]), float(group["end"]))


_HANDS_ONLY = re.compile(r"手部特写|只有手|只有她的手|只有他的手|只拍手|只见手|只拍手部")
_FACE_HERE = re.compile(r"露脸|正脸|面部|脸在画面|看得到脸|看到她的脸|看到他的脸")


def hands_only_shot(shot):
    """A close-up of hands, with no face written into the shot."""
    if not isinstance(shot, dict):
        return False
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    blob = " ".join(part for part in (shot.get("description"), continuity.get("camera_move")) if isinstance(part, str))
    if not _HANDS_ONLY.search(blob):
        return False
    return _FACE_HERE.search(blob) is None


def _production_dict(production):
    if isinstance(production, dict):
        return production
    if hasattr(production, "model_dump"):
        return production.model_dump()
    return {}


def _overlap_seconds(shot, start, end):
    try:
        return max(0.0, min(float(shot["end"]), end) - max(float(shot["start"]), start))
    except (TypeError, ValueError, KeyError):
        return 0.0


def _scene_of(shot, shots):
    for group in scene_groups(shots):
        if any(item is shot for item in group):
            return group
    return None


def _line_needs_a_face(form, production, overlapping, shots, scene_shots=None):
    """True when this line has to show a mouth, so a hands insert cannot carry it.

    A presenter line may cross a hands insert when another shot in the same
    line already shows the face. That continuation is the existing voice rule.
    A story line needs a mouth only when someone awake can hear it. The scene
    is the whole film, split only at scene_change, same as inner voice.
    """
    production = _production_dict(production)
    if production.get("speech_mode") != "character_dialogue" or not overlapping:
        return False
    if form == "scene_story":
        scene = scene_shots if scene_shots is not None else shots
        return any(shot_awake_listener(production, shot, _scene_of(shot, scene)) for shot in overlapping)
    if form == "presenter_pitch":
        if production.get("speech_delivery") == "on_and_off_screen":
            return not any(not hands_only_shot(shot) for shot in overlapping)
        return any(hands_only_shot(shot) for shot in overlapping)
    return False


def _unwanted_shots(overlapping, shots, *, form, production, scene_shots=None):
    unwanted = []
    needs_face = _line_needs_a_face(form, production, overlapping, shots, scene_shots)
    for shot in overlapping:
        if form == "presenter_pitch" and _is_quiet(shot):
            unwanted.append(shot)
            continue
        if needs_face and hands_only_shot(shot):
            unwanted.append(shot)
    return unwanted


def _homes_for(shots, overlapping, unwanted):
    homes = [shot for shot in overlapping if shot not in unwanted]
    if homes:
        return homes
    found = []
    for shot in unwanted:
        if shot not in shots:
            continue
        index = shots.index(shot)
        for neighbor in (index - 1, index + 1):
            if 0 <= neighbor < len(shots) and shots[neighbor] not in unwanted and shots[neighbor] not in found:
                found.append(shots[neighbor])
    return found


def settle_speech_coverage(segments, schedule, *, form=None, production=None):
    """Keep a spoken line off a shot that must not carry it. No model call.

    A presenter shot marked as not speaking is not covered by that line.
    A hands-only close-up does not carry a line that needs a visible mouth.
    Who can hear a story line is read from the whole film, split only when a
    shot enters by scene_change. A line that cannot move stays put. This is not a hard gate.
    """
    if not isinstance(segments, list) or not isinstance(schedule, list):
        return False
    film = [shot for segment in segments if isinstance(segment, dict)
            for shot in (segment.get("shots") or []) if isinstance(shot, dict)]
    changed = False
    for segment, booked in zip(segments, schedule):
        if not isinstance(segment, dict) or not isinstance(booked, dict):
            continue
        shots = segment.get("shots")
        spans = segment.get("speech_spans")
        groups = booked.get("groups")
        if not isinstance(shots, list) or not isinstance(spans, list) or not isinstance(groups, list):
            continue
        spoken = [span for span in spans if isinstance(span, dict) and isinstance(span.get("text"), str) and span["text"]]
        for span in spoken:
            if _clear_unwanted_coverage(span, shots, spoken, groups, form=form, production=production, scene_shots=film):
                changed = True
    return changed


def _clear_unwanted_coverage(span, shots, spoken, groups, *, form, production, scene_shots=None):
    try:
        start, end = float(span["start"]), float(span["end"])
    except (TypeError, ValueError, KeyError):
        return False
    overlapping = [shot for shot in shots if isinstance(shot, dict) and _overlap(shot, start, end)]
    unwanted = _unwanted_shots(overlapping, shots, form=form, production=production, scene_shots=scene_shots)
    if not unwanted:
        return False
    homes = _homes_for(shots, overlapping, unwanted)
    if not homes:
        return False
    home = max(homes, key=lambda shot: (_overlap_seconds(shot, start, end), -shots.index(shot)))
    if _inside(home, start, end) and span.get("shot_ids") == [home.get("id")]:
        return False
    return _snap_one_line(span, home, shots, spoken, groups)


def snap_declared_speech(segments, schedule):
    """Put each line inside the shot that says it is spoken. No model call.

    Prefer moving the cut so the existing clock falls in that shot. If the
    silent shot would become too short, move the clock into the declaring shot
    and keep the arranged speech time on the same beat.
    """
    if not isinstance(segments, list) or not isinstance(schedule, list):
        return False
    changed = False
    for segment, booked in zip(segments, schedule):
        if not isinstance(segment, dict) or not isinstance(booked, dict):
            continue
        shots = segment.get("shots")
        spans = segment.get("speech_spans")
        groups = booked.get("groups")
        if not isinstance(shots, list) or not isinstance(spans, list) or not isinstance(groups, list):
            continue
        spoken = [span for span in spans if isinstance(span, dict) and isinstance(span.get("text"), str) and span["text"]]
        for number, span in enumerate(spoken, start=1):
            shot = _declaring_shot(shots, span, number)
            if shot is None or shot not in shots:
                continue
            try:
                start, end = float(span["start"]), float(span["end"])
            except (TypeError, ValueError, KeyError):
                continue
            if _inside(shot, start, end) and span.get("shot_ids") == [shot.get("id")]:
                continue
            backup = (deepcopy(shots), deepcopy(spans), deepcopy(groups))
            if not _snap_one_line(span, shot, shots, spoken, groups):
                shots[:] = backup[0]
                spans[:] = backup[1]
                groups[:] = backup[2]
                continue
            changed = True
    return changed


def _field_snapshot(shots, spoken, groups):
    return {
        "shots": [(item, item.get("start"), item.get("end"), list(item.get("beat_ids") or []), item.get("beat_id"))
                  for item in shots],
        "spans": [(item, item.get("start"), item.get("end"), list(item.get("shot_ids") or []),
                   [(anchor, anchor.get("start"), anchor.get("end"), list(anchor.get("shot_ids") or []))
                    for anchor in item.get("anchors") or [] if isinstance(anchor, dict)])
                  for item in spoken],
        "groups": [(item, item.get("start"), item.get("end"), item.get("speech_start"), item.get("speech_end"))
                   for item in groups if isinstance(item, dict)],
    }


def _field_restore(saved):
    for shot, start, end, beats, beat in saved["shots"]:
        shot["start"], shot["end"], shot["beat_id"] = start, end, beat
        if beats:
            shot["beat_ids"] = list(beats)
    for span, start, end, shot_ids, anchors in saved["spans"]:
        span["start"], span["end"], span["shot_ids"] = start, end, list(shot_ids)
        for anchor, a_start, a_end, a_ids in anchors:
            anchor["start"], anchor["end"], anchor["shot_ids"] = a_start, a_end, list(a_ids)
    for group, start, end, speech_start, speech_end in saved["groups"]:
        group["start"], group["end"] = start, end
        group["speech_start"], group["speech_end"] = speech_start, speech_end


def _snap_one_line(span, shot, shots, spoken, groups):
    start, end = float(span["start"]), float(span["end"])
    holder = next((item for item in shots if _inside(item, start, end)), None)
    if holder is None:
        holder = next((item for item in shots if _overlap(item, start, end)), None)
    saved = _field_snapshot(shots, spoken, groups)
    if holder is not None and holder in shots and abs(shots.index(shot) - shots.index(holder)) == 1 and not _inside(shot, start, end):
        declare_at, holder_at = shots.index(shot), shots.index(holder)
        cut = start if declare_at > holder_at else end
        earlier, later = (holder, shot) if declare_at > holder_at else (shot, holder)
        if cut - float(earlier["start"]) >= 0.4 and float(later["end"]) - cut >= 0.4:
            earlier["end"] = cut
            later["start"] = cut
            if (_inside(shot, start, end) and _sync_beats(shots, groups) and _link_speech(spoken, shots)
                    and span.get("shot_ids") == [shot.get("id")]):
                return True
        _field_restore(saved)
    return _move_line_clock(span, shot, shots, spoken, groups, saved)


def _move_line_clock(span, shot, shots, spoken, groups, saved):
    start, end = float(span["start"]), float(span["end"])
    duration = end - start
    index = spoken.index(span)
    floor = float(spoken[index - 1]["end"]) if index else 0.0
    ceiling = float(spoken[index + 1]["start"]) if index + 1 < len(spoken) else float(shots[-1]["end"])
    lo = max(float(shot["start"]), floor)
    hi = min(float(shot["end"]), ceiling)
    chosen = _fitted_duration(span, duration, hi - lo)
    if chosen is None:
        _field_restore(saved)
        return False
    new_start = min(max(start, lo), hi - chosen)
    new_end = new_start + chosen
    old_len = duration
    duration = chosen
    span["start"], span["end"] = new_start, new_end
    for anchor in span.get("anchors") or []:
        if not isinstance(anchor, dict):
            continue
        try:
            a_start, a_end = float(anchor["start"]), float(anchor["end"])
        except (TypeError, ValueError, KeyError):
            anchor["start"], anchor["end"] = new_start, new_end
            continue
        anchor["start"] = new_start + (a_start - start) / old_len * duration
        anchor["end"] = new_start + (a_end - start) / old_len * duration
    if not (_cover_group(groups, span.get("beat_id"), shot)
            and _remember_group(groups, span.get("beat_id"), new_start, new_end)
            and _sync_beats(shots, groups) and _link_speech(spoken, shots)
            and span.get("shot_ids") == [shot.get("id")] and _inside(shot, new_start, new_end)):
        _field_restore(saved)
        return False
    return True


def wardrobe_warnings(plan):
    """A garment worn in a shot must already be in that person's appearance text."""
    from .picture_rules import mentioned
    characters = [person for person in (((plan or {}).get("production") or {}).get("characters") or [])
                  if isinstance(person, dict)]
    if not characters:
        return []
    warnings = []
    seen = set()
    for shot in iter_plan_shots(plan):
        ids = shot.get("character_ids") if isinstance(shot.get("character_ids"), list) else []
        people = [person for person in characters if not ids or person.get("id") in ids]
        appearance = " ".join(person.get("description") or "" for person in people if isinstance(person.get("description"), str))
        blob = shot.get("description") if isinstance(shot.get("description"), str) else ""
        for garment in _GARMENTS:
            if garment in seen or garment in appearance:
                continue
            if mentioned(re.compile(re.escape(garment)), blob):
                seen.add(garment)
                warnings.append(f"镜头里的{garment}没有写进角色外观")
    return warnings


def _asleep_text(text):
    if not isinstance(text, str) or not text:
        return False
    if _AWAKE.search(text):
        return False
    return _ASLEEP.search(text) is not None


def scene_groups(shots):
    """Shots that share a place, split only when a shot enters by scene_change."""
    groups = []
    current = []
    for shot in shots or []:
        if not isinstance(shot, dict):
            continue
        continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
        if current and continuity.get("entry_transition") == "scene_change":
            groups.append(current)
            current = []
        current.append(shot)
    if current:
        groups.append(current)
    return groups


def film_scene_shots(plan, segment):
    """Shots from the whole film, in order, when this segment is only part of a scene.

    Video generation compiles one segment at a time. Listener checks still need
    the same scene split as the plan, including a companion who appears in an
    earlier segment of the same scene. Empty when this segment already is the
    whole film shot list.
    """
    if not isinstance(segment, dict):
        return []
    own = [shot for shot in segment.get("shots") or [] if isinstance(shot, dict)]
    segments = []
    if isinstance(plan, dict) and isinstance(plan.get("segments"), list):
        segments = [item for item in plan["segments"] if isinstance(item, dict)]
    film = []
    for item in segments:
        seg_id = item.get("id") if isinstance(item.get("id"), str) else ""
        for shot in item.get("shots") or []:
            if isinstance(shot, dict):
                film.append((seg_id, shot))
    own_ids = [shot.get("id") for shot in own]
    film_ids = [shot.get("id") for _, shot in film]
    if not film or film_ids == own_ids:
        return []
    copies = []
    for seg_id, shot in film:
        continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
        entry = continuity.get("entry_transition")
        copied = {
            "id": shot.get("id"),
            "description": shot.get("description") if isinstance(shot.get("description"), str) else "",
            "character_ids": list(shot.get("character_ids") or []),
        }
        if seg_id:
            copied["segment_id"] = seg_id
        if isinstance(entry, str) and entry:
            copied["continuity"] = {"entry_transition": entry}
        copies.append(copied)
    return copies


def _is_alone_now(text):
    """True when this shot says she is by herself.

    「另一个人」 and 「不是一个人」 name company. 「对面坐着一个人」 does too.
    A separate 「她一个人」 still counts after that phrase is set aside.
    """
    if not isinstance(text, str) or not text:
        return False
    if _ALONE_NOW.search(text):
        return True
    return _ALONE_PERSON.search(_OPPOSITE_ONE.sub("", text)) is not None


def _hands_or_cake_macro(description):
    """A close-up that only shows hands or cake cannot take a full-body acting cue.

    Camera moves such as 缓慢推近 are removed as whole words before the body check.
    往柜子里推 is still her hand.
    """
    text = description if isinstance(description, str) else ""
    if not _CLOSE_UP.search(text) or not _HANDS_OR_CAKE.search(text):
        return False
    return _BODY_IN_FRAME.search(_CAMERA_MOVE_UNIT.sub("", text)) is None


def _listener_in_shot(production, shot):
    """True when this shot's own text or cast has someone awake besides the speaker.

    「一个人」 is read first. A later 「对面没有人」 or 「坐在对面的沙发」 cannot invent a listener.
    """
    if not isinstance(production, dict) or not isinstance(shot, dict):
        return False
    speaker = production.get("speaker_id")
    text = shot.get("description") if isinstance(shot.get("description"), str) else ""
    if _is_alone_now(text):
        return False
    ids = shot.get("character_ids") if isinstance(shot.get("character_ids"), list) else []
    blobs = []
    named = set()
    for person in production.get("characters") or []:
        if not isinstance(person, dict) or person.get("id") == speaker:
            continue
        name = person.get("name") if isinstance(person.get("name"), str) else ""
        if name:
            named.add(name)
        if person.get("id") not in ids and not (name and name in text):
            continue
        desc = person.get("description") if isinstance(person.get("description"), str) else ""
        blobs.append(desc + text)
    for word in _LISTENER_WORDS:
        if word in named or word not in text:
            continue
        blobs.append(text)
    if _OPPOSITE_COMPANY.search(text) and not _asleep_text(text):
        blobs.append(text)
    if not blobs:
        return False
    return any(not _asleep_text(blob) for blob in blobs)


def shot_awake_listener(production, shot, scene_shots=None):
    """True when this shot has someone awake who can hear her.

    A companion sitting in the same scene counts, including a friend opposite
    her, even when this shot is a close-up that does not list them. A shot that
    says she is alone does not borrow that companion.
    """
    if not isinstance(shot, dict):
        return False
    if _listener_in_shot(production, shot):
        return True
    text = shot.get("description") if isinstance(shot.get("description"), str) else ""
    if _is_alone_now(text):
        return False
    for other in scene_shots or []:
        if other is shot:
            continue
        if _listener_in_shot(production, other):
            return True
    return False


def awake_listener(production, shots):
    """True when any shot has someone besides the speaker who is awake."""
    for group in scene_groups(shots):
        if any(shot_awake_listener(production, shot, group) for shot in group):
            return True
    return False


def _inner_cue(match):
    token = match.group(0)
    ordinal = _LINE_ORDINAL.search(token)
    if ordinal:
        return "心里想第" + ordinal.group(1) + "句"
    return "心里想"


def _quiet_aloud(text):
    if not isinstance(text, str) or not text:
        return text
    return _ALOUD_CUE.sub(_inner_cue, text)


def _quiet_tree(node):
    """Replace aloud stage directions in every string on this shot. Returns old/new pairs."""
    pairs = []
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key in _SHOT_TEXT_SKIP:
                continue
            if isinstance(value, str):
                updated = _quiet_aloud(value)
                if updated != value:
                    node[key] = updated
                    pairs.append((value, updated))
            elif isinstance(value, (dict, list)):
                pairs.extend(_quiet_tree(value))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                pairs.extend(_quiet_tree(item))
    return pairs


def rewrite_inner_shot(description, speaking):
    """Turn an on-screen speaking cue into private thought. Idempotent.

    Footsteps, a look back, and a held breath are not written in. A close-up
    of hands or cake, and a wider shot where she is alone, only note that her
    mouth does not move. 开口说 and 说出第几句 become 心里想.
    """
    if not isinstance(description, str):
        return description
    updated = _quiet_aloud(description)
    if speaking and "嘴没有动" not in updated and "放轻脚步" not in updated:
        if updated and not updated.endswith("。"):
            updated = updated.rstrip() + "。"
        updated += _INNER_MOUTH
    return updated


def rewrite_inner_record(shot, speaking):
    """Quiet every aloud cue stored on this shot, then note that her mouth does not move.

    Description, purpose, continuity, and any other text on the shot are included.
    The returned pairs are the original wording and the wording that replaces it,
    so a copied record of this shot can be updated the same way.
    """
    if not isinstance(shot, dict):
        return []
    old_description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    pairs = _quiet_tree(shot)
    final = rewrite_inner_shot(shot.get("description") or "", speaking)
    if final != shot.get("description"):
        shot["description"] = final
    if old_description and old_description != final:
        pairs = [(old, final if old == old_description else new) for old, new in pairs]
        if not any(old == old_description for old, _ in pairs):
            pairs.append((old_description, final))
    return [(old, new) for old, new in pairs if old and old != new]


def quiet_inner_shot(shot, speaking):
    """A prompt copy. The stored shot is left for settle_inner_monologue."""
    if not isinstance(shot, dict):
        return shot
    copied = deepcopy(shot)
    rewrite_inner_record(copied, speaking)
    return copied


def _replace_pairs(text, pairs):
    if not isinstance(text, str) or not text:
        return text
    for old, new in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if old and old != new and old in text:
            text = text.replace(old, new)
    return text


def _sync_shot_records(segment, shot, pairs):
    """Copied picture notes for this shot follow the rewritten wording.

    The spoken line itself stays. Only records that quote this shot's picture change.
    """
    if not isinstance(segment, dict) or not pairs:
        return
    shot_id = shot.get("id")
    old_description = next((old for old, new in pairs if new == shot.get("description")), "")
    prompt = segment.get("prompt")
    if isinstance(prompt, str) and old_description:
        start, end = shot.get("start"), shot.get("end")
        replaced = False
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and not isinstance(start, bool):
            final = shot.get("description") or ""
            for dash in ("–", "-", "—"):
                stamped = f"{start:g}{dash}{end:g}秒：{old_description}"
                if stamped in prompt:
                    segment["prompt"] = prompt.replace(
                        stamped, f"{start:g}{dash}{end:g}秒：{final}", 1)
                    replaced = True
                    break
        if not replaced and prompt.count(old_description) == 1:
            segment["prompt"] = prompt.replace(old_description, shot.get("description") or "", 1)
    if isinstance(segment.get("purpose"), str):
        segment["purpose"] = _replace_pairs(segment["purpose"], pairs)
    if not isinstance(shot_id, str) or not shot_id:
        return
    for span in segment.get("speech_spans") or []:
        if not isinstance(span, dict):
            continue
        for anchor in span.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            ids = anchor.get("shot_ids") if isinstance(anchor.get("shot_ids"), list) else []
            if shot_id not in ids:
                continue
            evidence = anchor.get("visual_evidence")
            if not isinstance(evidence, str):
                continue
            updated = _replace_pairs(evidence, pairs)
            if ids == [shot_id]:
                updated = _quiet_aloud(updated)
            if updated != evidence:
                anchor["visual_evidence"] = updated


def _spoken_plan_spans(plan):
    found = []
    for segment in (plan or {}).get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for span in segment.get("speech_spans") or []:
            if isinstance(span, dict) and isinstance(span.get("text"), str) and span["text"].strip():
                found.append(span)
    return found


def _shot_speaks(shot, spans):
    for span in spans:
        try:
            start, end = float(span["start"]), float(span["end"])
        except (TypeError, ValueError, KeyError):
            continue
        if _overlap(shot, start, end):
            return True
    return False


def line_is_private_thought(plan, segment_id, span_id):
    """A story line said where nobody awake is listening.

    The scene is the whole film, split only at scene_change, same as inner
    voice. A friend still sitting from the previous segment counts. A shot
    that says she is alone does not borrow that friend. This is not a hard
    gate. It only tells the review that off_screen on this line is the inner
    voice already written into the shot.
    """
    data = plan.model_dump() if hasattr(plan, "model_dump") else plan
    if not isinstance(data, dict):
        return False
    try:
        if _form(data.get("strategy")) != "scene_story":
            return False
    except (TypeError, ValueError, KeyError, AttributeError):
        return False
    production = data.get("production") if isinstance(data.get("production"), dict) else {}
    if production.get("speech_mode") != "character_dialogue":
        return False
    for segment in data.get("segments") or []:
        if not isinstance(segment, dict) or segment.get("id") != segment_id:
            continue
        shots = [shot for shot in segment.get("shots") or [] if isinstance(shot, dict)]
        span = next((item for item in segment.get("speech_spans") or []
                     if isinstance(item, dict) and item.get("id") == span_id), None)
        if span is None or not shots:
            return False
        try:
            start, end = float(span["start"]), float(span["end"])
        except (TypeError, ValueError, KeyError):
            return False
        overlapping = [shot for shot in shots if _overlap(shot, start, end)]
        if not overlapping:
            return False
        film = iter_plan_shots(data) or shots
        return all(not shot_awake_listener(production, shot, _scene_of(shot, film)) for shot in overlapping)
    return False


def _stitch_continuous_starts(plan):
    """Keep a continuous shot's start equal to the previous end after a rewrite."""
    previous = None
    for shot in iter_plan_shots(plan):
        continuity = shot.get("continuity") if isinstance(shot, dict) else None
        if not isinstance(continuity, dict):
            previous = None
            continue
        if (previous is not None and continuity.get("entry_transition") == "continuous"
                and isinstance(previous.get("end_state"), str)
                and continuity.get("start_state") != previous.get("end_state")):
            continuity["start_state"] = previous["end_state"]
        previous = continuity


_SPEECH_ORDINAL = re.compile(r"第\s*(?:[0-9]{1,2}|[一二两三四五六七八九十]+)\s*句")


def _without_speech_ordinal(text):
    if not isinstance(text, str) or "句" not in text:
        return text
    return _SPEECH_ORDINAL.sub("", text)


def strip_speech_ordinals(plan):
    """Shot prose keeps who is speaking. It does not keep “第三句” as the line.

    snap_declared_speech already used that counter to place the real sentence.
    The sentence stays on the narration and speech span.
    """
    if not isinstance(plan, dict):
        return False
    changed = False

    def write(container, key):
        nonlocal changed
        current = container.get(key)
        updated = _without_speech_ordinal(current)
        if updated != current:
            container[key] = updated
            changed = True

    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        write(segment, "prompt")
        write(segment, "purpose")
        for shot in segment.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            write(shot, "description")
            write(shot, "purpose")
            continuity = shot.get("continuity")
            if isinstance(continuity, dict):
                for key in list(continuity):
                    if isinstance(continuity.get(key), str):
                        write(continuity, key)
        for span in segment.get("speech_spans") or []:
            if not isinstance(span, dict):
                continue
            for anchor in span.get("anchors") or []:
                if isinstance(anchor, dict):
                    write(anchor, "visual_evidence")
    return changed


def settle_inner_monologue(plan):
    """A shot with no awake listener becomes inner voice. No new hard gate.

    This runs when the board is compiled, before review, and again before
    release. The review therefore sees the wording that will be kept. The
    whole film still flips when nobody awake is listening. A line said in one
    shot where she is alone does too, even if another shot has someone awake.
    The stored speech mode stays character_dialogue: this is the lead's own
    voice, not a product-film voiceover. A second call does not stack the mouth note.
    """
    if not isinstance(plan, dict) or _form(plan.get("strategy")) != "scene_story":
        return []
    production = plan.get("production") if isinstance(plan.get("production"), dict) else {}
    if production.get("speech_mode") != "character_dialogue":
        return []
    spans = _spoken_plan_spans(plan)
    if not spans:
        return []
    shots = iter_plan_shots(plan)
    groups = scene_groups(shots)
    film_inner = not awake_listener(production, shots)
    owners = {}
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for shot in segment.get("shots") or []:
            if isinstance(shot, dict):
                owners[id(shot)] = segment

    def rewrite(shot, speaking):
        pairs = rewrite_inner_record(shot, speaking)
        _sync_shot_records(owners.get(id(shot)), shot, pairs)

    if film_inner:
        for shot in shots:
            rewrite(shot, _shot_speaks(shot, spans))
        _stitch_continuous_starts(plan)
        return [_INNER_NOTE]
    alone_spoken = False
    for group in groups:
        for shot in group:
            if shot_awake_listener(production, shot, group) or not _shot_speaks(shot, spans):
                continue
            alone_spoken = True
            rewrite(shot, True)
    if alone_spoken:
        _stitch_continuous_starts(plan)
        return [_SHOT_INNER_NOTE]
    return []
