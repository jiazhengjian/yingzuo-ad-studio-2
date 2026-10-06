"""Picture constraints that stay checkable: carrier, hair state, and short actions.

These helpers do not invent a line of dialogue or a fixed shot count. They only
decide whether a described change is allowed, and they keep a prompt from
asking the video model for an effect it will draw the wrong way.
"""
from __future__ import annotations

from copy import deepcopy
import re


_OFF_CAMERA = re.compile(r"不出镜|不要出镜|别出镜|无人出镜")
_BUNDLE = re.compile(r"发束|假发|发片|发条")
_REAL_TEST = re.compile(r"真人护发|护发实测|真人使用结果|实测效果")
_HEAD = re.compile(r"人头|露脸|她的脸|他的脸|模特的头")
_FORCE_BUNDLE = re.compile(r"必须发束|只能用发束|强制发束|不要真人")

_VAPOR = re.compile(
    r"筒口(?:喷|冒)(?:出)?(?:白烟|烟|蒸汽|雾)"
    r"|喷嘴(?:喷|冒)(?:出)?(?:白烟|烟|蒸汽|雾)"
    r"|喷白烟|喷出白烟|白烟|蒸汽|水蒸气"
)
_WET_LOCK = re.compile(r"发尾微湿")
_WET_END = re.compile(r"湿塌|仍然湿|还是湿|发尾还湿|一直湿|湿着塌")
_TRANSITION = re.compile(r"由湿到干|从湿到干|湿发变干|发丝由湿")
_DRY_OR_SMOOTH = re.compile(r"吹干|干了|变干|干燥|是干|顺滑|不毛躁|是顺|顺了|头发顺|发丝顺")

_SLAM = re.compile(r"摔门|砸门|猛地(?:把)?门|把门摔|重重(?:地)?(?:把)?门")
_QUIET = re.compile(r"安静|不吵|声音小|静音|压声|轻声|别吵")
_DOOR = re.compile(r"带上门|带门|关门|掩门")
_NEGATED_PREFIX = re.compile(r"(?:不要|别要|没有|没|不|别|未|勿|莫)[^，。；\s]{0,3}$")
_ACTION_QUIET = re.compile(r"压声|把声音压低|声音压低")
_ACTION_HAND = re.compile(r"手部|手指")
_ACTION_TURN = re.compile(r"翻(?:了个|个)?身")
_ACTION_DOOR = re.compile(r"门")


def off_camera_locked(*texts) -> bool:
    """True when the merchant locked a no-person film."""
    return any(isinstance(text, str) and _OFF_CAMERA.search(text) for text in texts)


def carrier_issues(*, brief="", characters=(), picture="") -> list[str]:
    """Appearance follows the lock. A hair bundle is not a live test.

    不出镜 allows an independent carrier and forbids a cast. Without that lock,
    the film may show a real result and must not be forced onto a bundle.
    A bundle does not grow a head.
    """
    picture = picture or ""
    issues = []
    if off_camera_locked(brief):
        if characters:
            issues.append("不出镜仍安排了真人")
        if _REAL_TEST.search(picture):
            issues.append("发束被当成真人护发实测")
        if _BUNDLE.search(picture) and _HEAD.search(picture):
            issues.append("独立发束被写成了人头")
        return issues
    if _FORCE_BUNDLE.search(picture):
        issues.append("未锁定不出镜却强制发束")
    return issues


def claims_dry_or_smooth(text) -> bool:
    return bool(_DRY_OR_SMOOTH.search(text or ""))


def scrub_vapor(text):
    """Wetness is a change in the hair, not smoke at the nozzle."""
    if not isinstance(text, str) or not text:
        return text
    return _VAPOR.sub("发丝形态变化", text)


def unlock_wet_identity(text, spoken=""):
    """An identity reference must not freeze a damp tip once the spot claims dry or smooth."""
    if not isinstance(text, str) or not text:
        return text
    if claims_dry_or_smooth(spoken) and _WET_LOCK.search(text):
        return _WET_LOCK.sub("发丝湿度不锁定", text)
    return text


def state_change_issues(shot, spoken="") -> list[str]:
    """A dry or smooth line needs a visible change. Nozzle smoke is not that change."""
    if not isinstance(shot, dict):
        return []
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    fields = [shot.get("description") or ""]
    fields.extend(continuity.get(key) or "" for key in ("start_state", "allowed_changes", "end_state"))
    blob = " ".join(part for part in fields if isinstance(part, str))
    issues = []
    if _VAPOR.search(blob):
        issues.append("喷嘴喷烟")
    end = continuity.get("end_state") or ""
    allowed = continuity.get("allowed_changes") or ""
    described = " ".join(part for part in (allowed, end, shot.get("description") or "") if isinstance(part, str))
    if claims_dry_or_smooth(spoken) and isinstance(end, str) and _WET_END.search(end) and not _TRANSITION.search(described):
        issues.append("声称干顺但镜末仍湿且没有状态变化")
    return issues


def ensure_hair_transition(shot, spoken=""):
    """Write wet-to-dry into allowed changes and drop nozzle-smoke wording."""
    revised = deepcopy(shot) if isinstance(shot, dict) else {}
    if isinstance(revised.get("description"), str):
        revised["description"] = unlock_wet_identity(scrub_vapor(revised["description"]), spoken)
    continuity = revised.get("continuity") if isinstance(revised.get("continuity"), dict) else None
    if not continuity:
        return revised
    for field in ("start_state", "allowed_changes", "end_state", "camera_move"):
        if isinstance(continuity.get(field), str):
            continuity[field] = scrub_vapor(continuity[field])
    if not claims_dry_or_smooth(spoken):
        return revised
    start = continuity.get("start_state") or ""
    allowed = continuity.get("allowed_changes") or ""
    if isinstance(start, str) and re.search(r"湿", start) and not _TRANSITION.search(allowed if isinstance(allowed, str) else ""):
        prefix = allowed if isinstance(allowed, str) and allowed else ""
        continuity["allowed_changes"] = (prefix + "；" if prefix else "") + "发丝由湿到干"
    return revised


def quiet_claim(*texts) -> bool:
    return any(isinstance(text, str) and _QUIET.search(text) for text in texts)


def soften_actions(text, claim_text=""):
    """A door in a quiet spot is eased shut. Slamming it fights the claim."""
    if not isinstance(text, str) or not text:
        return text
    updated = _SLAM.sub("轻轻带上门", text)
    if _DOOR.search(updated) and "轻轻" not in updated:
        updated = _DOOR.sub("轻轻带上门", updated, count=1)
    return updated


def action_issues(text, claim_text="") -> list[str]:
    issues = []
    if isinstance(text, str) and _SLAM.search(text) and (quiet_claim(text) or quiet_claim(claim_text)):
        issues.append("摔门砸了安静")
    if isinstance(text, str) and re.search(r"带门|关门|掩门|摔门", text) and "轻轻" not in text:
        issues.append("带门要轻")
    return issues


def _affirmed(pattern, source):
    """True when the action occurs without 没/不/别/未 immediately in front of it."""
    text = source or ""
    found = False
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 6):match.start()]
        if _NEGATED_PREFIX.search(prefix):
            continue
        found = True
    return found


def stitch_actions(description, allowed):
    """Key actions enter the state line as short units, not as a new scene."""
    source = description or ""
    units = []
    if _affirmed(_ACTION_QUIET, source):
        units.append("压低声音")
    if _affirmed(_ACTION_HAND, source):
        units.append("手部动作")
    if _affirmed(_ACTION_TURN, source):
        units.append("翻身")
    if _affirmed(_ACTION_DOOR, source):
        units.append("轻轻带门")
    existing = allowed or ""
    missing = [unit for unit in units if unit not in existing]
    if not missing:
        return existing
    return (existing + "；" if existing else "") + "；".join(missing)


def prepare_prompt_shot(shot, spoken="", claim_text=""):
    """Prompt view of one shot: hair state, no nozzle smoke, gentle door, short actions."""
    revised = ensure_hair_transition(shot, spoken)
    claim = f"{spoken or ''} {claim_text or ''}"
    if isinstance(revised.get("description"), str):
        revised["description"] = soften_actions(revised["description"], claim)
    continuity = revised.get("continuity") if isinstance(revised.get("continuity"), dict) else None
    if isinstance(continuity, dict):
        for field in ("start_state", "allowed_changes", "end_state", "camera_move"):
            if isinstance(continuity.get(field), str):
                continuity[field] = soften_actions(continuity[field], claim)
        continuity["allowed_changes"] = stitch_actions(
            revised.get("description") or "", continuity.get("allowed_changes") or "")
    return revised
