"""The only image-release decision, and the hard-gate table it consults.

Eight gates stay blocking. The degenerate-plan check sits on the same table
as a structure check. Image and video spend always re-run this table. A stored
label from planning time, including passed, repaired, and conservative_fallback,
does not skip that check. A later edit or an older saved board can change the
words and leave the label. Merchant-insisted lines are the only exemption.

A failed check is repaired here. The merchant is not asked to fix a board the
model wrote. Their own locked line is still a choice. When the overall review
still fails, the hard-gate passer with the fewest remaining issues is released
and recorded. That record is not shown.
"""
from __future__ import annotations

import json
import logging
import math
import re
from copy import deepcopy
from dataclasses import dataclass

logger = logging.getLogger("yingzuo.auto_release")


def configure_auto_release_log():
    """Print the auto_release line on stderr under the standard uvicorn command.

    That logger is not on the uvicorn tree. Uvicorn's default setup leaves it
    without a level or a handler, so an INFO record never reaches the terminal.
    """
    logger.setLevel(logging.INFO)
    if any(getattr(handler, "yingzuo_auto_release", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.yingzuo_auto_release = True
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)


configure_auto_release_log()

from .line_quality import (
    brand_stack_remaining, brand_visual_claim_overflow,
    script_form, script_line_offenses, voice_identity_issues,
)
from .schemas import script_character_limit
from .story_logic import story_plan_issues


@dataclass(frozen=True)
class HardGate:
    """One blocking check. applies_to names the ad forms it covers."""

    id: str
    applies_to: tuple[str, ...]
    summary: str
    kind: str = "hard"


# The eight gates stay blocking. Degenerate is the extra structure check.
RELEASE_TABLE = (
    HardGate("story_selling_line", ("scene_story",), "剧情对白还在说卖点"),
    HardGate("story_looks_at_camera", ("scene_story",), "剧情人物看着镜头"),
    HardGate("story_hero_freeze", ("scene_story",), "剧情末镜写成了英雄定格"),
    HardGate("story_presents_product", ("scene_story",), "剧情末镜人物对着镜头举着产品"),
    HardGate("story_logo_to_camera", ("scene_story",), "剧情末镜把商标朝向镜头"),
    HardGate("story_slogan_or_link", ("scene_story",), "剧情末镜在喊口号或链接"),
    HardGate("voice_identity", ("presenter_pitch", "brand_tvc", "scene_story"), "声音和广告类型对不上"),
    HardGate("brand_claim_stack", ("brand_tvc",), "产品片旁白还在堆卖点"),
    HardGate("degenerate_plan", ("presenter_pitch", "brand_tvc", "scene_story"), "占位稿不是完成的方案", kind="structure"),
)

_GATE_BY_SUMMARY = {gate.summary: gate for gate in RELEASE_TABLE}
_VOICE_SUMMARIES = {
    "口播被写成了旁白": "声音和广告类型对不上",
    "产品展示被写成了对口型推销": "声音和广告类型对不上",
    "剧情被写成了整条旁白": "声音和广告类型对不上",
}

_ORDINALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_INSIST = re.compile(r"就按这句来|就用这句|坚持这句")
_REWRITE = re.compile(r"让系统改写")
_SHORTEN = re.compile(r"缩短这句")
_LENGTHEN = re.compile(r"把视频加长|加长视频|视频加长")
_QUOTE = re.compile(r"[“\"「『]([^”\"」』\n]+)[”\"」』]")
_EDIT = re.compile(r"我自己改")
_OPTION_TAILS = frozenset({
    "让系统改写这句", "我自己改", "就按这句来",
    "缩短这句，意思留着", "把视频加长，会多花一些",
    "暂不确定，请使用谨慎表达",
})


@dataclass(frozen=True)
class GateFinding:
    gate_id: str
    summary: str
    line: str | None = None
    detail: str = ""


class MerchantLineChoice(Exception):
    """The merchant's own locked line needs a choice. This is not a fix list."""

    def __init__(self, message, questions):
        super().__init__(message)
        self.questions = questions


def gate_by_id(gate_id):
    return next(gate for gate in RELEASE_TABLE if gate.id == gate_id)


_CONCRETE_FORMS = ("scene_story", "brand_tvc", "presenter_pitch")
_UNKNOWN_FORM = "还没确定广告类型，这版不能放行。"
SPEND_RECHECK = "这版方案需要重新检查。请在对话里改完再制作，这次不会生成图片或视频。"


class ReleaseRecheck(ValueError):
    """The board failed the spend-time gate. Go back to chat. Do not call a provider."""

    error_code = "release_recheck"
    retryable = False
    retry_action = "edit_plan"

    def __init__(self):
        super().__init__(SPEND_RECHECK)


def _concrete_form(value):
    return value if value in _CONCRETE_FORMS else None


def _form_of(strategy, plan=None, project=None):
    """Ad type on the plan, then the project record. Free-text briefs are not guessed."""
    if strategy is not None:
        try:
            form = script_form(strategy)
        except (TypeError, ValueError, KeyError):
            form = None
        if form:
            return form
    brief = None
    if isinstance(strategy, dict):
        brief = strategy.get("brief") if isinstance(strategy.get("brief"), dict) else None
        if brief is None and isinstance(strategy.get("strategy"), dict):
            brief = strategy["strategy"].get("brief")
    if isinstance(plan, dict) and brief is None:
        record = plan.get("strategy") if isinstance(plan.get("strategy"), dict) else None
        brief = record.get("brief") if isinstance(record, dict) else None
    found = _concrete_form(brief.get("ad_form")) if isinstance(brief, dict) else None
    if found:
        return found
    if isinstance(project, dict):
        found = _concrete_form(project.get("ad_form"))
        if found:
            return found
        project_brief = project.get("brief")
        if isinstance(project_brief, dict):
            return _concrete_form(project_brief.get("ad_form"))
    return None


def _strategy_of(plan, strategy):
    if strategy is not None:
        return strategy
    if isinstance(plan, dict):
        return plan.get("strategy")
    return None


def insisted_lines(plan):
    """Lines the merchant insisted on. Only these skip a hard gate."""
    found = []
    if not isinstance(plan, dict):
        return found
    for line in plan.get("merchant_insisted_lines") or []:
        if isinstance(line, str) and line.strip() and line.strip() not in found:
            found.append(line.strip())
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for span in segment.get("speech_spans") or []:
            if isinstance(span, dict) and span.get("merchant_insisted") and isinstance(span.get("text"), str):
                text = span["text"].strip()
                if text and text not in found:
                    found.append(text)
    return found


def _spoken_lines(plan):
    lines = []
    script = (plan or {}).get("ad_script") if isinstance(plan, dict) else None
    beats = script.get("beats") if isinstance(script, dict) else None
    if isinstance(beats, list):
        for beat in beats:
            if isinstance(beat, dict) and isinstance(beat.get("narration"), str) and beat["narration"].strip():
                lines.append(beat["narration"].strip())
    if lines:
        return lines
    for segment in (plan or {}).get("segments") or []:
        if not isinstance(segment, dict):
            continue
        spans = segment.get("speech_spans")
        if not isinstance(spans, list):
            continue
        for span in spans:
            if isinstance(span, dict) and isinstance(span.get("text"), str) and span["text"].strip():
                lines.append(span["text"].strip())
    return lines


_REVIEW_STAMPS = ("plan_review", "review_status", "review_status_note", "auto_release")


def _beat_narrations(plan):
    found = {}
    script = plan.get("ad_script") if isinstance(plan, dict) and isinstance(plan.get("ad_script"), dict) else {}
    for index, beat in enumerate(script.get("beats") or []):
        if not isinstance(beat, dict):
            continue
        key = beat.get("id") if isinstance(beat.get("id"), str) and beat.get("id") else index
        text = beat.get("narration") if isinstance(beat.get("narration"), str) else ""
        found[key] = text.strip()
    return found


def _every_spoken_text(plan):
    found = []
    if not isinstance(plan, dict):
        return found
    for text in _beat_narrations(plan).values():
        if text:
            found.append(text)
    script = plan.get("script")
    if isinstance(script, str) and script.strip():
        found.append(script.strip())
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        narration = segment.get("narration")
        if isinstance(narration, str) and narration.strip():
            found.append(narration.strip())
        for span in segment.get("speech_spans") or []:
            if isinstance(span, dict) and isinstance(span.get("text"), str) and span["text"].strip():
                found.append(span["text"].strip())
        for shot in segment.get("shots") or []:
            if isinstance(shot, dict) and isinstance(shot.get("narration"), str) and shot["narration"].strip():
                found.append(shot["narration"].strip())
    return found


def _without_spoken(plan):
    """Board with review stamps and spoken text removed, for a structure compare."""
    copied = deepcopy(plan) if isinstance(plan, dict) else {}
    for key in _REVIEW_STAMPS:
        copied.pop(key, None)
    copied["script"] = ""
    script = copied.get("ad_script")
    if isinstance(script, dict):
        for beat in script.get("beats") or []:
            if isinstance(beat, dict):
                beat["narration"] = ""
    for segment in copied.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        segment["narration"] = ""
        for span in segment.get("speech_spans") or []:
            if isinstance(span, dict):
                span["text"] = ""
        for shot in segment.get("shots") or []:
            if isinstance(shot, dict):
                shot["narration"] = ""
    return copied


def _spoken_places(plan):
    places = {}
    if not isinstance(plan, dict):
        return places
    script = plan.get("ad_script") if isinstance(plan.get("ad_script"), dict) else {}
    for index, beat in enumerate(script.get("beats") or []):
        if not isinstance(beat, dict):
            continue
        key = beat.get("id") if isinstance(beat.get("id"), str) and beat.get("id") else index
        text = beat.get("narration") if isinstance(beat.get("narration"), str) else ""
        places[("beat", key)] = text.strip()
    for index, segment in enumerate(plan.get("segments") or []):
        if not isinstance(segment, dict):
            continue
        segment_key = segment.get("id") if isinstance(segment.get("id"), str) and segment.get("id") else index
        for span_index, span in enumerate(segment.get("speech_spans") or []):
            if not isinstance(span, dict):
                continue
            span_key = span.get("id") if isinstance(span.get("id"), str) and span.get("id") else span_index
            text = span.get("text") if isinstance(span.get("text"), str) else ""
            places[("span", segment_key, span_key)] = text.strip()
        for shot_index, shot in enumerate(segment.get("shots") or []):
            if not isinstance(shot, dict) or "narration" not in shot:
                continue
            shot_key = shot.get("id") if isinstance(shot.get("id"), str) and shot.get("id") else shot_index
            text = shot.get("narration") if isinstance(shot.get("narration"), str) else ""
            places[("shot", segment_key, shot_key)] = text.strip()
    return places


def _insisted_rewritten(before, after):
    """True when a merchant-dictated line no longer sits where they put it."""
    insisted = insisted_lines(before)
    if not insisted:
        return False
    before_places = _spoken_places(before)
    after_places = _spoken_places(after)
    for line in insisted:
        held = [key for key, text in before_places.items() if text == line]
        if any(after_places.get(key) != line for key in held):
            return True
        if not held and line not in after_places.values() and line not in _every_spoken_text(after):
            return True
    return False


def spoken_line_changes(before, after):
    """Spoken-line edits when the rest of the board is the same.

    Returns a list of (old, new) pairs. None means this is not a line-only
    repair: the board was replaced, or a merchant-dictated line was rewritten.
    An empty list means the spoken lines themselves did not change.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if _insisted_rewritten(before, after):
        return None
    if _without_spoken(before) != _without_spoken(after):
        return None
    pairs = []
    before_beats = _beat_narrations(before)
    after_beats = _beat_narrations(after)
    for key in dict.fromkeys([*before_beats, *after_beats]):
        old = before_beats.get(key, "")
        new = after_beats.get(key, "")
        if old != new:
            pairs.append((old, new))
    if pairs or before_beats or after_beats:
        return [(old, new) for old, new in pairs if old or new]
    old_script = before.get("script") if isinstance(before.get("script"), str) else ""
    new_script = after.get("script") if isinstance(after.get("script"), str) else ""
    if old_script.strip() != new_script.strip():
        return [(old_script.strip(), new_script.strip())]
    return []


_PLAIN_LINE_REASONS = {
    "剧情对白还在说卖点": "这句在剧情片里像在推销，改得更自然了",
    "产品片旁白还在堆卖点": "这句像在堆卖点，改得更自然了",
    "剧情人物看着镜头": "这句对着镜头说，改成对着戏里的人了",
    "剧情末镜在喊口号或链接": "这句像在喊口号，改得更像一句对话了",
    "声音和广告类型对不上": "声音和这条片子对不上，改成了现在这种说法",
}
_PLAIN_BLOCK_REASONS = {
    "剧情对白还在说卖点": "这句在剧情里像在推销，上一版还在。",
    "产品片旁白还在堆卖点": "这句把好几个卖点叠在一起了，上一版还在。",
    "剧情人物看着镜头": "人物看着镜头，上一版还在。",
    "剧情末镜写成了英雄定格": "结尾停成了摆拍，上一版还在。",
    "剧情末镜人物对着镜头举着产品": "结尾对着镜头举着产品，上一版还在。",
    "剧情末镜把商标朝向镜头": "结尾把商标朝向镜头，上一版还在。",
    "剧情末镜在喊口号或链接": "结尾在喊口号或链接，上一版还在。",
    "声音和广告类型对不上": "声音和这条广告对不上，上一版还在。",
    "占位稿不是完成的方案": "这版还不是一份能拍的方案，上一版还在。",
}
_BLAME_WORDS = ("硬门", "检查没", "没有通过", "没通过", "不是最好", "过不了")


def _plain_line_reason(summary):
    """A spoken-line note the merchant can read. Never a failed-check label."""
    if not isinstance(summary, str) or not summary.strip():
        return "改得更自然了"
    text = summary.strip()
    if text in _PLAIN_LINE_REASONS:
        return _PLAIN_LINE_REASONS[text]
    if any(token in text for token in _BLAME_WORDS):
        return "改得更自然了"
    if "堆卖点" in text:
        return "这句像在堆卖点，改得更自然了"
    if "卖点" in text or "推销" in text:
        return "这句在剧情片里像在推销，改得更自然了"
    return "改得更自然了"


def _plain_block_reason(findings, *, form_known=True):
    """Why a hand edit stays off the confirmed board, without blaming a check."""
    if not form_known:
        return "还没确定这是哪种广告，上一版还在。"
    for item in findings or []:
        summary = item.summary if hasattr(item, "summary") else ""
        if summary in _PLAIN_BLOCK_REASONS:
            return _PLAIN_BLOCK_REASONS[summary]
        if "堆卖点" in summary:
            return "这句把好几个卖点叠在一起了，上一版还在。"
        if "卖点" in summary or "推销" in summary:
            return "这句在剧情里像在推销，上一版还在。"
    return "这版和上一版对不上，上一版还在。"


def spoken_repair_message(before, changes):
    """Plain chat line naming each rewritten spoken line and why it changed."""
    findings = []
    try:
        findings = evaluate_release_table(before, exempt_lines=insisted_lines(before))
    except (TypeError, ValueError, KeyError, AttributeError):
        findings = []
    parts = []
    for old, new in changes:
        reason = next((item.summary for item in findings if item.line and item.line == old), None)
        if reason is None and findings:
            reason = findings[0].summary
        because = _plain_line_reason(reason)
        if old and new:
            parts.append(f"「{old}」改成了「{new}」。{because}")
        elif old:
            parts.append(f"收掉了「{old}」。{because}")
        elif new:
            parts.append(f"补上了「{new}」。{because}")
    if not parts:
        return ""
    lead = "出图前改了这句台词：" if len(parts) == 1 else "出图前改了这几句台词："
    return lead + "；".join(parts) + "。"


def _clip_merchant(text, limit=36):
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _shot_descriptions(plan):
    found = []
    if not isinstance(plan, dict):
        return found
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for shot in segment.get("shots") or []:
            if isinstance(shot, dict):
                found.append(shot.get("description") if isinstance(shot.get("description"), str) else "")
    return found


def adjustment_notice(before, after):
    """Tell the merchant the board changed, and what, without saying why.

    The previous version is not described as failed, unchecked, or second best.
    """
    items = []

    def add(text):
        if text and text not in items and len(items) < 6:
            items.append(text)

    old_lines = _spoken_lines(before)
    new_lines = _spoken_lines(after)
    for index in range(max(len(old_lines), len(new_lines))):
        old = old_lines[index] if index < len(old_lines) else ""
        new = new_lines[index] if index < len(new_lines) else ""
        if old == new:
            continue
        if old and new:
            add(f"台词从「{_clip_merchant(old)}」改成「{_clip_merchant(new)}」")
        elif old:
            add(f"收掉了台词「{_clip_merchant(old)}」")
        else:
            add(f"台词改成「{_clip_merchant(new)}」")
    old_shots = _shot_descriptions(before)
    new_shots = _shot_descriptions(after)
    for index in range(max(len(old_shots), len(new_shots))):
        old = old_shots[index] if index < len(old_shots) else ""
        new = new_shots[index] if index < len(new_shots) else ""
        if old.strip() == new.strip():
            continue
        if old.strip() and new.strip():
            add(f"镜头从「{_clip_merchant(old)}」改成「{_clip_merchant(new)}」")
        elif old.strip():
            add(f"收掉了镜头「{_clip_merchant(old)}」")
        else:
            add(f"镜头改成「{_clip_merchant(new)}」")
    labels = {"character_dialogue": "出镜说话", "voiceover": "画外旁白", "none": "无人声"}

    def mode(plan):
        production = plan.get("production") if isinstance(plan, dict) and isinstance(plan.get("production"), dict) else {}
        return production.get("speech_mode") if isinstance(production.get("speech_mode"), str) else ""

    old_mode, new_mode = mode(before), mode(after)
    if old_mode != new_mode and (old_mode or new_mode):
        add(f"声音从{labels.get(old_mode, '原来的方式')}改成{labels.get(new_mode, '现在的方式')}")
    lead = "方案做了一些调整，确认后开始出图。"
    if not items:
        return lead
    return lead + "".join(f"{item}。" for item in items)


def _with_form(strategy, form):
    """Attach a project ad type the plan itself did not carry."""
    if hasattr(strategy, "model_copy") and getattr(strategy, "brief", None) is not None:
        brief = strategy.brief.model_copy(update={"ad_form": form})
        return strategy.model_copy(update={"brief": brief})
    if isinstance(strategy, dict):
        copied = deepcopy(strategy)
        brief = dict(copied.get("brief")) if isinstance(copied.get("brief"), dict) else {}
        brief["ad_form"] = form
        copied["brief"] = brief
        return copied
    return {"brief": {"ad_form": form}}


def evaluate_release_table(plan, strategy=None, *, exempt_lines=(), project=None):
    """Run the table. exempt_lines are merchant-insisted texts only."""
    from .progressive_planning import is_degenerate_plan
    if not isinstance(plan, dict):
        return [GateFinding("degenerate_plan", "占位稿不是完成的方案", detail="没有方案")]
    strategy = _strategy_of(plan, strategy)
    form = _form_of(strategy, plan, project)
    exempt = {line.strip() for line in exempt_lines or () if isinstance(line, str) and line.strip()}
    findings = []
    if form is None:
        if is_degenerate_plan(plan):
            findings.append(GateFinding("degenerate_plan", "占位稿不是完成的方案"))
        return findings
    if _form_of(strategy, plan) != form:
        strategy = _with_form(strategy, form)

    def keep(gate_id, summary, line=None, detail=""):
        if line and line.strip() in exempt:
            return
        findings.append(GateFinding(gate_id, summary, line.strip() if isinstance(line, str) and line.strip() else None, detail))

    if form == "scene_story":
        selling = []
        try:
            script = plan.get("ad_script") or {}
            for item in script_line_offenses(script, strategy, (), plan):
                if "在说卖点" in (item.get("offenses") or []):
                    selling.append(item.get("narration") or "")
        except (TypeError, ValueError, KeyError):
            selling = []
        issues = story_plan_issues(plan, strategy)
        if "剧情对白还在说卖点" in issues:
            attributed = [line for line in selling if line.strip() and line.strip() not in exempt]
            if not selling:
                keep("story_selling_line", "剧情对白还在说卖点")
            for line in attributed:
                keep("story_selling_line", "剧情对白还在说卖点", line)
        for summary, gate_id in (
            ("剧情人物看着镜头", "story_looks_at_camera"),
            ("剧情末镜写成了英雄定格", "story_hero_freeze"),
            ("剧情末镜人物对着镜头举着产品", "story_presents_product"),
            ("剧情末镜把商标朝向镜头", "story_logo_to_camera"),
            ("剧情末镜在喊口号或链接", "story_slogan_or_link"),
        ):
            if summary not in issues:
                continue
            line = None
            if gate_id == "story_slogan_or_link":
                spoken = _spoken_lines(plan)
                if spoken:
                    from .story_logic import _CTA, _SLOGAN, _hits
                    last = spoken[-1]
                    if _hits(_CTA, last) or _hits(_SLOGAN, last):
                        line = last
            keep(gate_id, summary, line)

    if form in ("presenter_pitch", "brand_tvc", "scene_story"):
        production = plan.get("production")
        for detail in voice_identity_issues(strategy, production):
            keep("voice_identity", "声音和广告类型对不上", detail=detail)

    if form == "brand_tvc":
        script = plan.get("ad_script") or {}
        duration = _plan_duration(plan)
        try:
            stacked = brand_stack_remaining(script, strategy, exempt, plan, duration) or brand_visual_claim_overflow(
                plan, strategy, exempt, duration)
        except (TypeError, ValueError, KeyError):
            stacked = False
        if stacked:
            offending = []
            try:
                for item in script_line_offenses(script, strategy, exempt, plan, duration):
                    names = item.get("offenses") or []
                    if any(name in ("旁白在堆卖点", "一句里塞了两个卖点", "标签式旁白") for name in names):
                        text = item.get("narration") or ""
                        if text.strip() and text.strip() not in exempt:
                            offending.append(text.strip())
            except (TypeError, ValueError, KeyError):
                offending = []
            if offending:
                for line in offending:
                    keep("brand_claim_stack", "产品片旁白还在堆卖点", line)
            else:
                keep("brand_claim_stack", "产品片旁白还在堆卖点")

    if is_degenerate_plan(plan):
        findings.append(GateFinding("degenerate_plan", "占位稿不是完成的方案"))
    return findings


def _plan_duration(plan):
    segments = plan.get("segments") if isinstance(plan, dict) else None
    if not isinstance(segments, list):
        return None
    total = 0
    for segment in segments:
        if not isinstance(segment, dict) or type(segment.get("duration_seconds")) is not int:
            return None
        total += segment["duration_seconds"]
    return total or None


def images_blocked_reason(plan, *, strategy=None, project=None):
    """The only decision before image or video spend. None means it may start.

    The stored label is ignored. Planning wrote passed, repaired, or
    conservative_fallback earlier; a later edit or an older saved board can
    change the words while leaving that label. This check runs the eight hard
    gates and the placeholder check again. Merchant-insisted lines, already
    recorded on the plan, are the only exemption. The ad type is the plan's
    own type, then the project's. auto does not count. No concrete type blocks.
    """
    if not isinstance(plan, dict):
        return SPEND_RECHECK
    if _form_of(_strategy_of(plan, strategy), plan, project) is None:
        return SPEND_RECHECK
    findings = evaluate_release_table(
        plan, strategy, exempt_lines=insisted_lines(plan), project=project)
    if findings:
        return SPEND_RECHECK
    return None


_INCOMPLETE_REVIEW_NOTE = "没有完整的综合检查，硬门已通过"
_OLD_INCOMPLETE_NOTES = {
    _INCOMPLETE_REVIEW_NOTE,
    "没有完整的综合检查，硬门已经通过。这条已记下。",
}
_CHECK_LABELS = {
    "product_relevance": "商品关联",
    "audience_relevance": "受众关联",
    "factual_support": "商品事实依据",
    "audiovisual_complement": "声画互补",
    "natural_speech": "台词自然度",
    "narrative_progression": "叙事推进",
}


def _review_record(plan):
    review = plan.get("plan_review") if isinstance(plan, dict) else None
    return review if isinstance(review, dict) else None


def _review_ran_and_failed(review):
    """A finished review with ready false. A missing or cut-off record is not this."""
    if not isinstance(review, dict) or review.get("ready") is not False:
        return False
    checks = review.get("checks")
    execution = review.get("execution_check")
    alignment = review.get("alignment")
    if not isinstance(checks, list) or not checks:
        return False
    if not all(isinstance(item, dict) and "criterion" in item and "passed" in item for item in checks):
        return False
    if not isinstance(execution, dict) or "action_capacity" not in execution or "state_continuity" not in execution:
        return False
    if not isinstance(alignment, dict) or "ad_expression_passed" not in alignment:
        return False
    return True


def _failed_review_labels(review):
    labels = []
    for item in review.get("checks") or []:
        if isinstance(item, dict) and item.get("passed") is False:
            labels.append(_CHECK_LABELS.get(item.get("criterion"), "内容检查"))
    execution = review.get("execution_check") or {}
    if execution.get("action_capacity") is False:
        labels.append("动作与台词容量")
    if execution.get("state_continuity") is False:
        labels.append("前后状态衔接")
    alignment = review.get("alignment") or {}
    if alignment.get("ad_expression_passed") is False:
        labels.append("广告表达")
    shots = alignment.get("shots") if isinstance(alignment.get("shots"), list) else []
    spans = alignment.get("spans") if isinstance(alignment.get("spans"), list) else []
    if any(isinstance(item, dict) and item.get("passed") is False for item in shots):
        labels.append("镜头")
    if any(isinstance(item, dict) and item.get("passed") is False for item in spans):
        labels.append("台词与画面")
    return list(dict.fromkeys(labels))


def _fit_failure_note(labels):
    if not labels:
        return "综合检查没通过，硬门已通过"
    body = "、".join(labels)
    text = f"综合检查没通过（{body}），硬门已通过"
    if len(text) <= 200:
        return text
    kept = []
    for label in labels:
        trial = "、".join([*kept, label, "等"])
        if len(f"综合检查没通过（{trial}），硬门已通过") > 200:
            break
        kept.append(label)
    if not kept:
        return "综合检查没通过，硬门已通过"
    return f"综合检查没通过（{'、'.join(kept)}等），硬门已通过"


def conservative_fallback_note(plan):
    """Say whether the full review failed, or never finished.

    This sentence is a backend record. It is not shown to the merchant.
    """
    review = _review_record(plan)
    if not _review_ran_and_failed(review):
        return _INCOMPLETE_REVIEW_NOTE
    return _fit_failure_note(_failed_review_labels(review))


def review_issue_rank(plan):
    """Lower is better. A clean review, then fewer failed items, then an unfinished review."""
    review = _review_record(plan)
    if isinstance(review, dict) and review.get("ready") is True and review.get("checks"):
        return (0, 0)
    if _review_ran_and_failed(review):
        return (1, len(_failed_review_labels(review)))
    return (2, 0)


def choose_hard_gate_release(candidates, *, strategy=None, project=None):
    """Among boards that pass every hard gate, keep the fewest remaining review issues.

    A later candidate wins a tie. A board that fails a hard gate is not eligible.
    """
    best = None
    best_rank = None
    for plan in candidates or ():
        if not isinstance(plan, dict):
            continue
        if images_blocked_reason(plan, strategy=strategy or plan.get("strategy"), project=project):
            continue
        rank = review_issue_rank(plan)
        if best is None or rank <= best_rank:
            best = plan
            best_rank = rank
    return best


def should_record_auto_release(plan):
    """Only a hard-gate pass whose overall review actually failed.

    A clean pass, a missing review, and any board that still fails a hard gate
    are not this record. The log line itself is unchanged.
    """
    if not isinstance(plan, dict) or isinstance(plan.get("auto_release"), dict):
        return False
    if images_blocked_reason(plan, strategy=plan.get("strategy")):
        return False
    return _review_ran_and_failed(_review_record(plan))


def record_auto_release(plan, *, project_id=None, ad_form=None, repair_rounds=0, failed_checks=None):
    """Store one queryable record and write one log line. Nothing here is shown."""
    if not isinstance(plan, dict):
        return plan
    review = _review_record(plan)
    if failed_checks is None:
        failed_checks = _failed_review_labels(review) if _review_ran_and_failed(review) else []
    form = ad_form or _form_of(_strategy_of(plan, None), plan)
    note = conservative_fallback_note(plan)
    rounds = int(repair_rounds or 0)
    plan["auto_release"] = {
        "note": note,
        "failed_checks": list(failed_checks),
        "repair_rounds": rounds,
        "ad_form": form,
        "project_id": project_id,
    }
    logger.info("auto_release %s", json.dumps({
        "project_id": project_id,
        "ad_form": form,
        "failed_checks": list(failed_checks),
        "repair_rounds": rounds,
        "note": note,
    }, ensure_ascii=False))
    return plan


def _sync_spoken(plan, narrations, previous):
    lines = [text for text in narrations.values() if isinstance(text, str) and text.strip()]
    if lines:
        plan["script"] = "\n".join(lines)
    replaced = {
        previous[beat_id]: narrations[beat_id]
        for beat_id in narrations
        if beat_id in previous and previous[beat_id] != narrations[beat_id]
    }
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for span in segment.get("speech_spans") or []:
            if not isinstance(span, dict):
                continue
            if span.get("beat_id") in narrations:
                span["text"] = narrations[span["beat_id"]]
            elif span.get("text") in replaced:
                span["text"] = replaced[span["text"]]
        for shot in segment.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            if shot.get("beat_id") in narrations and "narration" in shot:
                shot["narration"] = narrations[shot["beat_id"]]
            elif shot.get("narration") in replaced:
                shot["narration"] = replaced[shot["narration"]]


def local_hard_gate_repair(plan, strategy=None, project=None):
    """One local rewrite of a board that failed a hard gate. None when nothing changed."""
    if not isinstance(plan, dict):
        return None
    strategy = _strategy_of(plan, strategy)
    copied = deepcopy(plan)
    try:
        from .line_quality import collapse_brand_claims
        script = copied.get("ad_script")
        if not isinstance(script, dict):
            return None
        revised, changed = collapse_brand_claims(script, insisted_lines(copied), _plan_duration(copied))
        if not changed or not isinstance(revised, dict):
            return None
        previous = {}
        for beat in script.get("beats") or []:
            if isinstance(beat, dict) and isinstance(beat.get("id"), str):
                previous[beat["id"]] = beat.get("narration") or ""
        narrations = {}
        for beat in revised.get("beats") or []:
            if isinstance(beat, dict) and isinstance(beat.get("id"), str):
                narrations[beat["id"]] = beat.get("narration") or ""
        copied["ad_script"] = revised
        _sync_spoken(copied, narrations, previous)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    copied.pop("plan_review", None)
    copied.pop("review_status", None)
    copied.pop("review_status_note", None)
    copied.pop("auto_release", None)
    stamp_honest_label(copied, strategy, project=project)
    return copied


def settle_before_spend(plan, *, strategy=None, project=None, project_id=None, duration=None,
                        generation_strategy=None, asset_ids=None, cta_target=None):
    """Return a board that passes the eight hard gates.

    One local repair is tried first. If that board passes, it continues.
    If it does not, the fallback is the hard-gate passer with the fewest
    remaining review issues. Nothing is returned for a provider call until a
    board passes. The merchant is not asked to rewrite a model failure.
    MerchantLineChoice still propagates when the blocked words are theirs.
    """
    from .merchant_cards import NOT_PRODUCED_CARD, SYSTEM_CARD, NotProduced
    if not isinstance(plan, dict):
        raise ValueError(SYSTEM_CARD)
    strategy = _strategy_of(plan, strategy)
    form = _form_of(strategy, plan, project)
    if form is None:
        raise ValueError(SYSTEM_CARD)
    if images_blocked_reason(plan, strategy=strategy, project=project) is None:
        return plan
    repaired = local_hard_gate_repair(plan, strategy, project)
    if repaired is not None and images_blocked_reason(repaired, strategy=strategy, project=project) is None:
        if should_record_auto_release(repaired):
            record_auto_release(repaired, project_id=project_id, ad_form=form, repair_rounds=1)
        return repaired
    locked = _kept_lines(plan.get("merchant_locked_lines"))
    insisted = insisted_lines(plan)
    if duration is None:
        duration = _plan_duration(plan)
    if generation_strategy is None:
        generation_strategy = plan.get("generation_strategy") or "legacy_10s"
    if asset_ids is None:
        asset_ids = []
        for segment in plan.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            for aid in segment.get("reference_asset_ids") or []:
                if aid not in asset_ids:
                    asset_ids.append(aid)
    conservative = None
    if type(duration) is int and strategy is not None:
        try:
            conservative = conservative_usable_plan(
                strategy, duration, generation_strategy, asset_ids, locked, cta_target,
                insisted_lines_=insisted)
        except MerchantLineChoice:
            raise
        except (TypeError, ValueError, KeyError, AttributeError):
            conservative = None
    chosen = choose_hard_gate_release(
        [item for item in (repaired, conservative) if item is not None],
        strategy=strategy, project=project)
    if chosen is None or images_blocked_reason(chosen, strategy=strategy, project=project):
        # Repair and the conservative board both miss a hard gate. Nothing is
        # released and nothing is submitted.
        raise NotProduced(NOT_PRODUCED_CARD)
    if should_record_auto_release(chosen):
        record_auto_release(chosen, project_id=project_id, ad_form=form, repair_rounds=1)
    return chosen


def stamp_honest_label(plan, strategy=None, *, repairs=0, project=None):
    """Write the review label the table actually supports. Returns the status."""
    if not isinstance(plan, dict):
        return None
    if plan.get("review_status") in ("passed", "repaired"):
        return plan["review_status"]
    if _form_of(_strategy_of(plan, strategy), plan, project) is None:
        plan["review_status"] = "not_ready"
        plan["review_status_note"] = _UNKNOWN_FORM
        return "not_ready"
    findings = evaluate_release_table(plan, strategy, exempt_lines=insisted_lines(plan), project=project)
    if findings:
        plan["review_status"] = "not_ready"
        if any(item.gate_id == "ad_form_unknown" for item in findings):
            plan["review_status_note"] = _UNKNOWN_FORM
        else:
            plan["review_status_note"] = "硬门没有通过，这版不会拿去确认。"
        return "not_ready"
    review = _review_record(plan)
    if isinstance(review, dict) and review.get("ready") is True and review.get("checks"):
        plan["review_status"] = "repaired" if repairs else "passed"
        plan.pop("review_status_note", None)
        return plan["review_status"]
    kept = plan.get("review_status_note") if plan.get("review_status") == "conservative_fallback" else None
    plan["review_status"] = "conservative_fallback"
    if isinstance(kept, str) and kept not in _OLD_INCOMPLETE_NOTES:
        plan["review_status_note"] = kept
    else:
        plan["review_status_note"] = conservative_fallback_note(plan)
    return "conservative_fallback"


def classify_release(plan, strategy=None, *, locked_lines=(), insisted_lines_=(), project=None):
    """ok, ask, or repair. ask is only when every hit is a locked, uninsisted line."""
    insisted = {line.strip() for line in insisted_lines_ or () if isinstance(line, str) and line.strip()}
    locked = {line.strip() for line in locked_lines or () if isinstance(line, str) and line.strip()}
    findings = evaluate_release_table(plan, strategy, exempt_lines=insisted, project=project)
    if not findings:
        return "ok", []
    line_hits = [item for item in findings if item.line and item.line in locked and item.line not in insisted]
    other = [item for item in findings if item not in line_hits]
    if line_hits and not other:
        return "ask", line_hits
    return "repair", findings


def _quotes(text):
    return [item.strip() for item in _QUOTE.findall(text or "") if item.strip()]


def line_locks(brief, conversation, current_plan=None):
    """Locked lines, and the subset the merchant insisted on.

    A later “让系统改写” drops that lock. Insisted lines stay locked and are
    the only hard-gate exemption.
    """
    from .progressive_planning import fixed_speech
    locked = list(fixed_speech(brief, conversation, current_plan))
    if isinstance(current_plan, dict):
        for line in list(current_plan.get("merchant_locked_lines") or []) + list(current_plan.get("merchant_insisted_lines") or []):
            if isinstance(line, str) and line.strip() and line.strip() not in locked:
                locked.append(line.strip())
    insisted, released = [], []
    texts = [brief] + [item.get("content", "") for item in conversation or [] if item.get("role") == "user"]
    for text in texts:
        if not isinstance(text, str):
            continue
        quotes = _quotes(text)
        if _REWRITE.search(text) or _SHORTEN.search(text) or _EDIT.search(text):
            released.extend(quotes)
        if _INSIST.search(text):
            insisted.extend(quotes)
        if "碰到了这条规则" in text or "放不进现在的" in text:
            tail = text.split("：")[-1].strip()
            if tail and tail not in _OPTION_TAILS and not _INSIST.search(text):
                released.extend(quotes)
                if tail not in locked:
                    locked.append(tail)
    released = [line for line in released if line not in insisted]
    locked = [line for line in locked if line not in released]
    for line in insisted:
        if line not in locked:
            locked.append(line)
    insisted = [line for line in insisted if line in locked]
    return locked, insisted


def mark_insisted_lines(plan, insisted):
    texts = [line.strip() for line in insisted or () if isinstance(line, str) and line.strip()]
    if not texts or not isinstance(plan, dict):
        return plan
    plan["merchant_insisted_lines"] = list(dict.fromkeys(texts))
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for span in segment.get("speech_spans") or []:
            if isinstance(span, dict) and isinstance(span.get("text"), str) and span["text"].strip() in texts:
                span["merchant_insisted"] = True
    return plan


def _tenth(strategy):
    try:
        from .ad_form_playbook import speech_profile_for_strategy
        return speech_profile_for_strategy(strategy).characters_per_tenth
    except (TypeError, ValueError, KeyError, AttributeError):
        return 29


def duration_needed(line, tenth):
    if not line:
        return 10
    return max(10, math.ceil(len(line) * 10 / max(int(tenth), 1)))


def line_fits_duration(line, duration, tenth):
    if type(duration) is not int:
        return True
    return len(line or "") <= script_character_limit(duration, tenth)


def duration_question(line, duration, tenth):
    """A card when a locked line cannot fit. No lengthen offer at 60 or past 60."""
    needed = duration_needed(line, tenth)
    if line_fits_duration(line, duration, tenth):
        return None
    options = []
    if type(duration) is int and duration < 60 and needed <= 60:
        options.append({"id": "lengthen", "label": "把视频加长，会多花一些"})
    options.append({"id": "shorten", "label": "缩短这句，意思留着"})
    prompt = f"这句「{line}」放不进现在的{duration}秒。"
    if any(item["id"] == "lengthen" for item in options):
        prompt += f"可以加长到{max(duration + 1, needed)}秒，最长60秒，时长加了会多花一些；也可以让系统把这句缩短，意思留着。"
    elif type(duration) is int and duration >= 60:
        prompt += "已经到60秒，不能再加长。可以让系统把这句缩短，意思留着。"
    else:
        prompt += "加到60秒也放不下这句。可以让系统把这句缩短，意思留着。"
    return {"id": "locked_line_duration", "prompt": prompt[:500], "options": options,
            "allow_custom": False, "allow_skip": False}


def line_gate_questions(findings):
    questions = []
    seen = set()
    for item in findings:
        if not item.line or item.line in seen:
            continue
        seen.add(item.line)
        questions.append({
            "id": f"locked_line_{len(questions)}",
            "prompt": f"第{len(questions) + 1}句「{item.line}」碰到了这条规则：{item.summary}。可以让系统改写，你自己改一句，或就按这句来。"[:500],
            "options": [
                {"id": "rewrite", "label": "让系统改写这句"},
                {"id": "edit", "label": "我自己改"},
                {"id": "insist", "label": "就按这句来"},
            ],
            "allow_custom": True,
            "allow_skip": False,
        })
        if len(questions) == 4:
            break
    if not questions:
        return None
    return MerchantLineChoice("这句台词碰到了一条不能直接放行的规则。", questions)


def _rate_strategy(strategy, project=None):
    """Prefer a strategy that already names a form; otherwise the project record."""
    if _form_of(strategy):
        return strategy
    form = _form_of(None, None, project)
    if form:
        return {"brief": {"ad_form": form}}
    return strategy


def duration_hold(locked_lines, duration, strategy, project=None):
    tenth = _tenth(_rate_strategy(strategy, project))
    for line in locked_lines or ():
        if isinstance(line, str) and line.strip() and not line_fits_duration(line.strip(), duration, tenth):
            question = duration_question(line.strip(), duration, tenth)
            if question is None:
                continue
            return MerchantLineChoice(question["prompt"], [question])
    return None


def lengthen_target(messages, duration, brief="", current_plan=None, project=None):
    """Seconds to grow to after the merchant picks lengthen, or None."""
    texts = [brief] + [item.get("content", "") for item in messages or [] if isinstance(item, dict) and item.get("role") == "user"]
    if not any(isinstance(text, str) and _LENGTHEN.search(text) for text in texts):
        return None
    if type(duration) is not int or duration >= 60:
        return None
    locked, _insisted = line_locks(brief, messages, current_plan)
    strategy = current_plan.get("strategy") if isinstance(current_plan, dict) else None
    tenth = _tenth(_rate_strategy(strategy, project))
    needed = duration
    for line in locked:
        if not line_fits_duration(line, duration, tenth):
            needed = max(needed, duration_needed(line, tenth))
    if needed <= duration or needed > 60:
        return None
    return needed


def changed_lines(previous, submitted):
    """Speech the merchant just wrote. Those lines stay locked."""
    def gather(plan):
        found = []
        if not isinstance(plan, dict):
            return found
        for line in _spoken_lines(plan):
            if line not in found:
                found.append(line)
        return found
    before = set(gather(previous))
    return [line for line in gather(submitted) if line not in before]


def _strip_client_review(plan):
    """Drop review labels and insist markers the client is not allowed to set."""
    plan.pop("review_status", None)
    plan.pop("review_status_note", None)
    plan.pop("plan_review", None)
    plan.pop("merchant_insisted_lines", None)
    plan.pop("merchant_locked_lines", None)
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for span in segment.get("speech_spans") or []:
            if isinstance(span, dict):
                span.pop("merchant_insisted", None)
    return plan


def settle_manual_edit(previous, submitted, duration, strategy=None, project=None):
    """Label a hand edit, or say which locked line needs a choice.

    Returns (plan, error). error is a plain sentence when the edit must not
    be shown for confirmation. Review labels and insist markers on the
    submitted plan are ignored. An insist marker is copied only from the
    plan already stored for this project.
    """
    if not isinstance(submitted, dict):
        return None, "这版读不出来，上一版还在。"
    plan = _strip_client_review(deepcopy(submitted))
    strategy = strategy or plan.get("strategy")
    spoken = set(_spoken_lines(plan))
    previous_locks = []
    if isinstance(previous, dict):
        for line in previous.get("merchant_locked_lines") or []:
            if isinstance(line, str) and line.strip() and line.strip() in spoken:
                previous_locks.append(line.strip())
    changed = changed_lines(previous, plan)
    locked = list(dict.fromkeys([*previous_locks, *changed]))
    insisted = [line for line in insisted_lines(previous) if line in spoken]
    plan["merchant_locked_lines"] = locked
    if insisted:
        mark_insisted_lines(plan, insisted)
    hold = duration_hold(locked, duration, strategy, project)
    if hold is not None:
        return None, hold.questions[0]["prompt"]
    kind, findings = classify_release(
        plan, strategy, locked_lines=locked, insisted_lines_=insisted, project=project)
    if kind == "ask":
        parts = []
        for item in findings:
            if not item.line:
                continue
            if "堆卖点" in (item.summary or ""):
                why = "像在堆卖点"
            elif "卖点" in (item.summary or "") or "推销" in (item.summary or ""):
                why = "在剧情里像在推销"
            else:
                why = _PLAIN_BLOCK_REASONS.get(item.summary, "和这条片子对不上")
                why = why.replace("，上一版还在。", "").replace("上一版还在。", "")
            parts.append(f"「{item.line}」{why}")
        return None, ("；".join(parts) + "。可以让系统改写，你自己改一句，或在对话里说就按这句来。")[:500]
    status = stamp_honest_label(plan, strategy, project=project)
    if status == "not_ready" or images_blocked_reason(plan, strategy=strategy, project=project):
        known = _form_of(_strategy_of(plan, strategy), plan, project) is not None
        return None, _plain_block_reason(findings, form_known=known)
    return plan, None


def note_merchant_confirmation(payload, revision):
    if isinstance(payload, dict) and type(revision) is int:
        payload["merchant_confirmed_revision"] = revision
    return payload


def assert_spend_confirmed(job, project):
    """Before a paid image or video submit, the merchant confirmed this revision."""
    if not isinstance(job, dict) or not isinstance(project, dict):
        raise ValueError("这一版方案还没有确认，不会提交图片或视频。")
    if job.get("revision") != project.get("revision"):
        raise ValueError("方案已更新，请先确认这一版再制作。")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    if payload.get("merchant_confirmed_revision") != project.get("revision"):
        raise ValueError("这一版方案还没有确认，不会提交图片或视频。")


def _strategy_copy(strategy):
    if hasattr(strategy, "model_dump"):
        return strategy.model_dump()
    return deepcopy(strategy)


def _align_voice(strategy):
    """Return a strategy whose speech mode matches the ad type, or None."""
    from .strategy import StrategyRecord
    data = _strategy_copy(strategy)
    if not isinstance(data, dict):
        return None
    form = _form_of(data)
    directions = data.get("directions")
    if not isinstance(directions, list):
        return strategy if hasattr(strategy, "directions") else None
    selected_id = data.get("selected_direction_id")
    selected = next((item for item in directions if isinstance(item, dict) and item.get("id") == selected_id), None)
    if selected is None:
        return None
    production = selected.get("production")
    if not isinstance(production, dict):
        return StrategyRecord.model_validate(data)
    mode = production.get("speech_mode")
    wanted = {"presenter_pitch": "character_dialogue", "brand_tvc": "voiceover", "scene_story": "character_dialogue"}.get(form)
    if wanted is None or mode == wanted or (form == "brand_tvc" and mode == "none") or (form == "scene_story" and mode == "none"):
        return StrategyRecord.model_validate(data)
    if wanted == "voiceover":
        production["speech_mode"] = "voiceover"
        production["speaker_id"] = ""
        production.pop("speech_delivery", None)
        if not (production.get("voice_description") or "").strip():
            production["voice_description"] = "自然普通话，画外轻轻说。"
    else:
        people = [person for person in production.get("characters") or [] if isinstance(person, dict)]
        speaker = next((person for person in people if person.get("appearance") != "partial_only" and person.get("id")), None)
        if speaker is None:
            return None
        production["speech_mode"] = "character_dialogue"
        production["speaker_id"] = speaker["id"]
        production.pop("speech_delivery", None)
        if not (production.get("voice_description") or "").strip():
            production["voice_description"] = "自然普通话，在场景里说短句。"
    return StrategyRecord.model_validate(data)


def _kept_lines(locked_lines):
    found = []
    for line in locked_lines or ():
        if isinstance(line, str) and line.strip() and line.strip() not in found:
            found.append(line.strip())
    return found


def _hold_description(product, index):
    """Distinct, non-placeholder shots. A locked line never shares a shot to make room."""
    options = (
        f"{product}放在桌上，光从侧面过来。",
        f"{product}留在原处，镜头停近一些，没有人把它举起来。",
        f"{product}仍在桌上，人看着手边，机位不动。",
        f"桌面还是{product}，光线不变，人没有转向镜头。",
    )
    if index < len(options):
        return options[index]
    return f"{product}还在桌上，这是第{index + 1}个停顿，机位不动，人看着手边。"


def _speech_mode(strategy):
    selected = next(item for item in strategy.directions if item.id == strategy.selected_direction_id)
    if selected.production is None:
        return "voiceover"
    return selected.production.speech_mode


def _ask_to_keep(lines, summary):
    findings = [GateFinding("merchant_line", summary, line) for line in lines]
    choice = line_gate_questions(findings)
    if choice is not None:
        raise choice


def _conservative_director(strategy, locked_lines=()):
    """A plain product hold that is not the two-line placeholder.

    Merchant-locked lines are copied unchanged. Filler lines are used only
    when the merchant did not lock any line.
    """
    selected = next(item for item in strategy.directions if item.id == strategy.selected_direction_id)
    evidence = list(dict.fromkeys(selected.evidence_ids)) or [fact.id for fact in strategy.brief.facts[:1]]
    characters = list(selected.production.characters) if selected.production else []
    speech = selected.production.speech_mode if selected.production else "voiceover"
    speaker = ""
    if speech == "character_dialogue":
        speaker = selected.production.speaker_id or (characters[0].id if characters else "")
    character_ids = [speaker] if speaker else ([characters[0].id] if characters else [])
    form = _form_of(strategy)
    locked = _kept_lines(locked_lines)
    if locked:
        lines = locked
    elif speech == "none":
        lines = []
    elif form == "scene_story":
        lines = ["先放这儿。", "就这样。"]
    elif form == "brand_tvc":
        lines = ["看清这件。"]
    else:
        lines = ["看一下这个。", "我拿着。"]
    product = (strategy.brief.product_name or "商品").strip() or "商品"
    timing = {"visual_seconds": 2, "speech_overlap": True, "action_plan": "商品留在原处，供人看清",
              "start_state": "商品在画面里", "end_state": "商品在画面里", "time_transition": "continuous"}
    beats, shots = [], []
    count = max(len(lines), 1)
    for index in range(count):
        line = "" if speech == "none" else (lines[index] if index < len(lines) else "")
        beats.append({"id": f"b{index}", "purpose": "看清商品", "message": "看清商品外观",
                      "visual_role": "商品留在画面里", "narration": line,
                      "evidence_ids": list(evidence), "timing": deepcopy(timing)})
        shots.append({"id": f"shot_{index}", "coverage": [{"beat_id": f"b{index}", "weight": 1}],
                      "description": _hold_description(product, index), "product_visible": True,
                      "purpose": "看清商品", "evidence_ids": list(evidence), "character_ids": list(character_ids),
                      "continuity": {"start_state": "商品留在桌上", "allowed_changes": "商品保持不动",
                                     "end_state": "商品留在桌上", "camera_move": "中景，机位固定",
                                     "entry_transition": "opening" if index == 0 else "continuous",
                                     "entry_reason": "看清商品"}})
    return {"title": product[:20], "video_flow": "先看见商品，再停住。",
            "ad_script": {"core_message": "看清商品", "audience_takeaway": "记住商品",
                          "product_role": "商品出现在画面里", "evidence_ids": list(evidence), "beats": beats},
            "visual_direction": {"product_anchor_ids": list(evidence[:1]), "scene": "实际使用的场所",
                                 "lighting": "侧面自然光", "color_palette": "保留商品原色",
                                 "camera_style": "中景固定机位", "sound": "环境声很轻"},
            "shots": shots}


def conservative_usable_plan(strategy, duration, generation_strategy, asset_ids, locked_lines=(), cta_target=None,
                             insisted_lines_=()):
    """A usable board that passes the hard-gate table, or None.

    Every merchant-locked line is kept verbatim. If one does not fit the
    duration, or a silent film cannot speak it, or it still fails a hard gate
    and the merchant has not insisted, this raises MerchantLineChoice instead
    of dropping the line. Insisted lines stay exempt from the hard gates.
    """
    from .progressive_planning import compile_director
    aligned = _align_voice(strategy)
    if aligned is None:
        return None
    locked = _kept_lines(locked_lines)
    insisted = _kept_lines(insisted_lines_)
    tenth = _tenth(aligned)
    speech = _speech_mode(aligned)
    if locked:
        overflow = [line for line in locked if not line_fits_duration(line, duration, tenth)]
        if overflow:
            question = duration_question(overflow[0], duration, tenth)
            if question is not None:
                raise MerchantLineChoice(question["prompt"], [question])
        if speech == "none":
            _ask_to_keep(locked, "这条广告没有旁白，这句不能悄悄删掉")
    try:
        director = _conservative_director(aligned, locked)
        plan, _schedule, _coordinated = compile_director(
            director, aligned, duration, generation_strategy, list(asset_ids or []), locked, cta_target)
    except (TypeError, ValueError, KeyError, AttributeError):
        if locked:
            _ask_to_keep(locked, "这句放不进现在的保守方案")
        return None
    if not isinstance(plan, dict):
        return None
    spoken = _spoken_lines(plan)
    missing = [line for line in locked if line not in spoken]
    if missing:
        _ask_to_keep(missing, "这句放不进现在的保守方案")
    plan["merchant_locked_lines"] = list(locked)
    mark_insisted_lines(plan, [line for line in insisted if line in spoken])
    plan["review_status_note"] = "硬门没有通过，已换成一份能通过检查的保守方案。这条已记下。"
    plan.pop("review_status", None)
    plan.pop("plan_review", None)
    kind, findings = classify_release(plan, aligned, locked_lines=locked, insisted_lines_=insisted)
    if kind == "ask":
        choice = line_gate_questions(findings)
        if choice is not None:
            raise choice
    status = stamp_honest_label(plan, aligned)
    if status != "conservative_fallback" or images_blocked_reason(plan, strategy=aligned):
        return None
    return plan


def present_checked_plan(plan, strategy=None, *, duration=None, generation_strategy=None,
                         asset_ids=(), locked_lines=(), cta_target=None, note=None, repairs=0,
                         project=None, insisted_lines_=()):
    """Label a board honestly, or substitute one that passes. None means do not show it."""
    board = deepcopy(plan) if isinstance(plan, dict) else None
    if isinstance(board, dict) and note and board.get("review_status") not in ("passed", "repaired", "not_ready"):
        board["review_status"] = "conservative_fallback"
        board["review_status_note"] = note
    use_locked = _kept_lines(locked_lines)
    use_insisted = _kept_lines(insisted_lines_)
    if isinstance(plan, dict):
        if not use_locked:
            use_locked = _kept_lines(plan.get("merchant_locked_lines"))
        if not use_insisted:
            use_insisted = insisted_lines(plan)
    conservative = None
    choice = None
    if strategy is not None and type(duration) is int:
        try:
            conservative = conservative_usable_plan(
                strategy, duration, generation_strategy or "legacy_10s", asset_ids, use_locked, cta_target,
                insisted_lines_=use_insisted)
        except MerchantLineChoice as exc:
            choice = exc
        except (TypeError, ValueError, KeyError, AttributeError):
            conservative = None
    shown = finalize_unlabeled_plan(board, strategy, repairs=repairs, conservative=conservative, project=project)
    if shown is not None:
        return shown
    if choice is not None:
        raise choice
    return None


def finalize_unlabeled_plan(plan, strategy=None, *, repairs=0, conservative=None, project=None):
    """Give an unlabeled or fallback board an honest label, or a substitute that passes.

    Returns the plan to show, or None when nothing usable passed the table.
    """
    from .progressive_planning import is_degenerate_plan
    if not isinstance(plan, dict):
        plan = None
    elif is_degenerate_plan(plan):
        plan = None
    elif stamp_honest_label(plan, strategy, repairs=repairs, project=project) == "not_ready":
        plan = None
    elif images_blocked_reason(plan, strategy=strategy, project=project):
        plan = None
    if plan is not None:
        return plan
    if conservative is None:
        return None
    return conservative if images_blocked_reason(conservative, strategy=strategy, project=project) is None else None
