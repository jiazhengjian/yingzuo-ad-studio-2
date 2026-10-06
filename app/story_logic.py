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
# Reached only when a result word is present and a not-yet word is not.
# This stays small on purpose: process words are not a third state.
_UNFINISHED = re.compile(r"仍|还是|还没|未干|没有干|半湿|半干|一半|湿着|是湿")
_REACHED = re.compile(r"已经|干了|吹干|干透|干燥|顺了|顺开|打开|空了|平整|见底")
_STATEY = re.compile(r"湿|干|乱|顺|毛|关|开|满|空|皱|平|脏|净|合")
_OBJECT_SUBJECTS = frozenset({"杯", "盖子", "瓶盖", "门", "商品"})
_STATE_REF_ON = frozenset({"1", "true", "on", "yes"})
_STATE_REF_OFF = frozenset({"0", "false", "off", "no"})
_SLOGAN = re.compile(r"口号|标语")
_LOOK = re.compile(r"(?<![不别])(?:看向镜头|看着镜头|看镜头|面向镜头|直视镜头|对着镜头)")
_BG_NAME = re.compile(r"室友|同事|朋友|旁边的人|另一个人")
_BG_EQUIV = re.compile(r"床上的人|床上那个人|睡着的人|睡熟的人")
_BG_ABSENT = re.compile(r"没有人|床是空的|空无一人|人不在床|不在床上|已经起床|已经起来|下了床|离开了床")
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
_STORY_HOLD_LINE = "机位停住，人物和手都不再动，商品按放下时的样子留在原位，不摆向镜头，不放下、不移出画面，不出现字幕、Logo或字卡。"
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


def _reached(text):
    if not isinstance(text, str) or not text.strip() or _UNFINISHED.search(text):
        return False
    return bool(_REACHED.search(text))


def _continuity_field(shot, key):
    continuity = shot.get("continuity") if isinstance(shot, dict) and isinstance(shot.get("continuity"), dict) else {}
    value = continuity.get(key)
    return value if isinstance(value, str) else ""


def _named_subject(*parts):
    blob = " ".join(part for part in parts if isinstance(part, str))
    for word in ("头发", "发丝", "盖子", "瓶盖", "杯", "门", "衣服", "商品"):
        if word in blob:
            return "头发" if word in ("头发", "发丝") else word
    return "主体"


def _state_subject(shot):
    start = _continuity_field(shot, "start_state")
    end = _continuity_field(shot, "end_state")
    description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    if not _STATEY.search(" ".join((start, end, description))):
        return None
    return _named_subject(start, end, description)


def _short_declared(text, subject):
    clause = re.split(r"[，。；]", (text or "").strip(), maxsplit=1)[0].strip()
    if subject and clause.startswith(subject):
        clause = clause[len(subject):].lstrip("的")
    return clause[:24]


def _start_label(shot, subject):
    start = _continuity_field(shot, "start_state")
    end = _continuity_field(shot, "end_state")
    text = start if start and not _reached(start) else end
    return _short_declared(text, subject) or _short_declared(start or end, subject)


def _result_label(shot, subject):
    end = _continuity_field(shot, "end_state")
    start = _continuity_field(shot, "start_state")
    return _short_declared(end if _reached(end) else start, subject)


def _state_character_id(container, shot, subject):
    if subject in _OBJECT_SUBJECTS or not isinstance(shot, dict):
        return None
    ids = [item for item in (shot.get("character_ids") or []) if isinstance(item, str) and item]
    production = container.get("production") if isinstance(container, dict) and isinstance(container.get("production"), dict) else {}
    speaker = production.get("speaker_id")
    if isinstance(speaker, str) and speaker in ids:
        return speaker
    return ids[0] if ids else None


def _shot_identity(shot, index):
    shot_id = shot.get("id") if isinstance(shot, dict) and isinstance(shot.get("id"), str) and shot.get("id") else f"n{index}"
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", shot_id)
    return shot_id, safe


def _push_state(refs, shot, index, phase, label, subject, character_id=None):
    shot_id, safe = _shot_identity(shot, index)
    item = {
        "key": f"state_{safe}_{phase}",
        "kind": "start_frame",
        "name": f"{subject}{label} · {'起始' if phase == 'start' else '结果'}",
        "state_id": f"{safe}:{phase}",
        "phase": phase,
        "shot_id": shot_id,
        "label": f"{subject}{label}",
        "subject": subject,
    }
    if character_id:
        item["character_id"] = character_id
    refs.append(item)


def shot_state_change(shot):
    """One shot that opens not-yet and closes on the result. A cut between shots stays None."""
    if not isinstance(shot, dict):
        return None
    start = _continuity_field(shot, "start_state")
    end = _continuity_field(shot, "end_state")
    description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    if not start or not end or not _STATEY.search(start + end + description):
        return None
    if _reached(start) or not _reached(end):
        return None
    subject = _named_subject(start, end, description)
    before, after = _short_declared(start, subject), _short_declared(end, subject)
    if not before or not after:
        return None
    return before, after, subject


def plan_state_references(container, *, limit=3):
    """One start still and one result still per subject. The model fills the gap.

    The shot that still shows the not-yet state gets the start. The first shot
    whose declared start or end has reached the result gets the result. Both
    can sit on the same shot. A subject that never reaches still gets one start
    still and does not fail the plan. At most `limit` subjects. All ad types.
    AD_STATE_REFERENCES=off plans nothing.
    """
    if not state_references_enabled():
        return []
    groups, order = {}, []
    for index, shot in enumerate(iter_plan_shots(container)):
        subject = _state_subject(shot)
        if subject is None:
            continue
        if subject not in groups:
            order.append(subject)
            groups[subject] = []
        groups[subject].append((index, shot))
    refs = []
    for subject in order:
        if len(refs) >= limit * 2:
            break
        chain = groups[subject]
        result = next((
            (index, shot) for index, shot in chain
            if _reached(_continuity_field(shot, "end_state")) or _reached(_continuity_field(shot, "start_state"))
        ), None)
        if result is None:
            index, shot = chain[0]
            label = _start_label(shot, subject)
            if label:
                _push_state(refs, shot, index, "start", label, subject, _state_character_id(container, shot, subject))
            continue
        result_index, result_shot = result
        earlier = [
            (index, shot) for index, shot in chain
            if index < result_index and not _reached(_continuity_field(shot, "end_state"))
        ]
        if earlier:
            start_index, start_shot = earlier[-1]
        elif not _reached(_continuity_field(result_shot, "start_state")):
            start_index, start_shot = result_index, result_shot
        else:
            start_shot = None
        if start_shot is not None:
            label = _start_label(start_shot, subject)
            if label:
                _push_state(refs, start_shot, start_index, "start", label, subject,
                            _state_character_id(container, start_shot, subject))
        result_label = _result_label(result_shot, subject)
        if result_label:
            _push_state(refs, result_shot, result_index, "result", result_label, subject,
                        _state_character_id(container, result_shot, subject))
    return refs


def state_pairing_issues(container):
    """Pairing is no longer a reason to refuse a plan."""
    return []

def state_prompt_lines(shot, roles):
    """A later result shot hard-cuts. A shot that owns both stills uses them as the two ends."""
    if not isinstance(shot, dict):
        return []
    shot_id = shot.get("id")
    start = next((role for role in roles or [] if role.get("role") == "state" and role.get("phase") == "start" and role.get("shot_id") == shot_id), None)
    result = next((role for role in roles or [] if role.get("role") == "state" and role.get("phase") == "result" and role.get("shot_id") == shot_id), None)
    lines = []
    if start and result:
        lines.append(
            f"  本镜从起始状态参考图{start['index']}到结果状态参考图{result['index']}，中间由画面补上。")
    elif start:
        lines.append(f"  本镜开始对齐起始状态参考图{start['index']}，停在过程里，不要跳到结果。")
    elif result:
        lines.append(f"  本镜硬切到结果状态参考图{result['index']}，不要在这一镜里从上一状态渐变。")
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


def _bed_problem(blob, name, established):
    """absent or facing. A missing pose restatement is not a problem."""
    if not _BED.search(blob or ""):
        return None
    pose = _declared_pose(blob)
    if name is None or _BG_ABSENT.search(blob) or not _person_referenced(blob, name):
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
        problem = _bed_problem(blob, name, established)
        yield shot, blob, name, established, pose, problem
        established = _remember_pose(established, pose)


def background_issues(container):
    """A bed in frame keeps the person. A declared turn wins; the camera-facing turn does not.

    床上的人、睡着的人、不会认错的「她」，以及仍在睡、还在睡、没动、没翻身，都算人还在、姿势还在。
    连续性的起止状态也算。没把姿势再写一遍不算失败。
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
    if not isinstance(description, str) or not shots or not _BED.search(description):
        return description
    if _BG_ABSENT.search(description):
        return description
    shots = [shot for shot in shots if isinstance(shot, dict)]
    if index is None:
        index = next((cursor for cursor, shot in enumerate(shots) if (shot.get("description") or "") == description), None)
        if index is None:
            index = len(shots) - 1
    name, pose = _pose_in_force(shots, index)
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
        if problem or not _BED.search(blob):
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


def storyboard_content_failure(message="", issues=()):
    """True when a saved storyboard failed a shot-level check and must be written again."""
    text = str(message or "")
    if isinstance(issues, dict):
        text += "".join(str(value) for value in issues.values())
    elif isinstance(issues, (list, tuple)):
        text += "".join(str(value) for value in issues)
    else:
        text += str(issues or "")
    return any(token in text for token in _STORYBOARD_CONTENT_MARKERS)


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
    if _LOGO_TO_CAMERA.search(closing):
        issues.append("剧情末镜把商标朝向镜头")
    if any(pattern.search(text) for pattern in (_CTA, _SLOGAN) for text in (closing, line) if text):
        issues.append("剧情末镜在喊口号或链接")
    if any(_LOOK.search(_shot_blob(item)) for item in iter_plan_shots(plan)):
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
