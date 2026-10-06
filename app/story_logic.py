"""Story ending, punchlines, proof shots, state stills, gaze, and background cast.

These checks stay generic. A hair dryer is only one home-use example. They do
not invent a shot count, a line, or a first/last-frame video API. The video
model may still ignore a hard cut or a still.
"""
from __future__ import annotations

from copy import deepcopy
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
    "noise": re.compile(r"安静|不吵|睡|轻声|声音"),
    "smooth": re.compile(r"顺|梳|发丝|毛躁"),
    "ions": re.compile(r"离子|光点|微粒"),
}
_FROM_TO = re.compile(r"(?:由|从)([^，。；\s]{1,6}?)(?:到|变成|变为)([^，。；\s]{1,6})")
_STATEY = re.compile(r"湿|干|乱|顺|毛|关|开|满|空|皱|平|脏|净|合")
_STATE_PAIRS = (
    ("湿", ("干了", "变干", "吹干", "干燥", "干发", "已经干", "是干")),
    ("乱", ("顺了", "变顺", "梳顺", "顺滑", "已经顺")),
    ("毛躁", ("顺了", "顺滑", "不毛躁", "梳顺")),
    ("关上", ("打开", "开了", "敞开")),
    ("合上", ("打开", "敞开", "开了")),
    ("满", ("空了", "倒空", "见底")),
    ("皱", ("平整", "展平", "抚平")),
)
_LOOK = re.compile(r"(?<![不别])(?:看向镜头|看着镜头|看镜头|面向镜头|直视镜头|对着镜头)")
_BG_NAME = re.compile(r"室友|同事|朋友|旁边的人|另一个人")
_BED = re.compile(r"床|床铺|被窝|枕头")
_POSES = ("背对", "后脑", "侧躺", "趴着", "面向镜头", "正脸", "侧卧")
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

    A push-in onto a product left on its stand is allowed. Other ad types are not checked.
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
    if _HERO.search(blob):
        issues.append("剧情末镜写成了英雄定格")
    if _PRESENTS.search(blob):
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


def _statey(text):
    return bool(_STATEY.search(text or ""))


def shot_state_change(shot):
    """Return (start_label, result_label, subject) when this shot changes a visible state."""
    if not isinstance(shot, dict):
        return None
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    start = continuity.get("start_state") if isinstance(continuity.get("start_state"), str) else ""
    end = continuity.get("end_state") if isinstance(continuity.get("end_state"), str) else ""
    allowed = continuity.get("allowed_changes") if isinstance(continuity.get("allowed_changes"), str) else ""
    description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    blob = " ".join(part for part in (start, end, allowed, description) if part)
    match = _FROM_TO.search(blob)
    if match and _statey(match.group(1)) and _statey(match.group(2)):
        before, after = match.group(1), match.group(2)
    else:
        before = after = ""
        for left, rights in _STATE_PAIRS:
            if left in start and left not in end and any(right in end or right in allowed or right in description for right in rights):
                before, after = left, next(right for right in rights if right in end or right in allowed or right in description)
                break
        if not before:
            return None
    subject = "主体"
    for word in ("头发", "发丝", "盖子", "瓶盖", "杯", "门", "衣服", "商品"):
        if word in blob:
            subject = word
            break
    return before, after, subject


def plan_state_references(container, *, limit=3):
    """Start and result stills for shots whose state text alone will not execute.

    All ad types. At most `limit` changes, so the reference list can still fit.
    """
    refs = []
    for index, shot in enumerate(iter_plan_shots(container)):
        change = shot_state_change(shot)
        if change is None:
            continue
        if len(refs) >= limit * 2:
            break
        before, after, subject = change
        shot_id = shot.get("id") if isinstance(shot.get("id"), str) and shot.get("id") else f"n{index}"
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", shot_id)
        for phase, label in (("start", before), ("result", after)):
            refs.append({
                "key": f"state_{safe}_{phase}",
                "kind": "start_frame",
                "name": f"{subject}{label} · {'起始' if phase == 'start' else '结果'}",
                "state_id": f"{safe}:{phase}",
                "phase": phase,
                "shot_id": shot_id,
                "label": f"{subject}{label}",
                "subject": subject,
            })
    return refs


def state_prompt_lines(shot, roles):
    """Point a changed shot at its stills. No first/last-frame API is claimed."""
    if not isinstance(shot, dict) or shot_state_change(shot) is None:
        return []
    shot_id = shot.get("id")
    lines = []
    start = next((role for role in roles or [] if role.get("role") == "state" and role.get("phase") == "start" and role.get("shot_id") == shot_id), None)
    result = next((role for role in roles or [] if role.get("role") == "state" and role.get("phase") == "result" and role.get("shot_id") == shot_id), None)
    if start:
        lines.append(f"  本镜开始对齐起始状态参考图{start['index']}。")
    if result:
        lines.append(f"  本镜硬切到结果状态参考图{result['index']}，不要在这一镜里从上一状态渐变。")
    elif not start:
        lines.append("  本镜是状态结果，硬切，不要在这一镜里渐变。")
    return lines


def story_needs_lens(shot):
    return bool(_LOOK.search(_shot_blob(shot)))


def story_gaze_line(shot, form):
    """剧情 defaults to no eye contact unless this shot writes that it needs the lens."""
    if form != "scene_story" or not isinstance(shot, dict):
        return None
    if not shot.get("character_ids"):
        return None
    if story_needs_lens(shot):
        return None
    return "  本镜人物不看镜头。"


def background_roster(shots):
    """The first background person and the pose the story actually gave them."""
    for shot in shots or []:
        blob = _shot_blob(shot)
        name = _BG_NAME.search(blob)
        if not name:
            continue
        pose = next((item for item in _POSES if item in blob), None)
        if pose is None and "睡" not in blob:
            continue
        return {"name": name.group(0), "pose": pose or "睡着"}
    return None


def background_issues(container):
    """A bed in frame must still show the background person in the pose already established."""
    shots = iter_plan_shots(container)
    roster = background_roster(shots)
    if not roster:
        return []
    issues = []
    facing = roster["pose"] in ("背对", "后脑", "侧躺", "趴着", "侧卧", "睡着")
    for shot in shots:
        blob = _shot_blob(shot)
        if not _BED.search(blob):
            continue
        if roster["name"] not in blob or (roster["pose"] not in blob and "睡着" not in blob and "朝向不变" not in blob):
            issues.append("背景人物没有跟着床入画")
        elif facing and re.search(r"面向镜头|正脸朝", blob):
            issues.append("背景人物朝向和前面不一致")
    return list(dict.fromkeys(issues))


def with_background_line(description, shots):
    """Compiler copy. The checker still fails a plan that omitted the person."""
    roster = background_roster(shots)
    if not roster or not isinstance(description, str) or not _BED.search(description):
        return description
    if roster["name"] in description and roster["pose"] in description:
        return description
    extra = f"{roster['name']}仍{roster['pose']}，朝向不变"
    if extra in description:
        return description
    return description.rstrip("。") + "。" + extra + "。"


def story_plan_issues(plan, strategy):
    """Blocking 剧情 problems. Other ad types stay empty so a product film is not refused here."""
    if _form(strategy) != "scene_story":
        return []
    issues = []
    issues.extend(story_ending_issues(plan, strategy))
    try:
        from .line_quality import script_line_offenses
        script = (plan or {}).get("ad_script") or {}
        if any("在点题" in (item.get("offenses") or []) for item in script_line_offenses(script, strategy, plan=plan)):
            issues.append("剧情末句还在点题")
    except (TypeError, ValueError, KeyError):
        pass
    issues.extend(proof_highlight_issues(plan, strategy))
    issues.extend(background_issues(plan))
    return list(dict.fromkeys(issues))
