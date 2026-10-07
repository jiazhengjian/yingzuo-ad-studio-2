"""The only image-release decision, and the hard-gate table it consults.

Eight gates stay blocking. The degenerate-plan check sits on the same table
as a structure check. A plan with no review record is not released. A
conservative fallback is released only when this table passes and the board
is not a placeholder. Merchant-insisted lines are the only exemption.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass

from .line_quality import (
    brand_stack_remaining, brand_visual_claim_overflow, line_asserts_product_claim,
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


def _form_of(strategy, plan=None):
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
    if isinstance(brief, dict) and brief.get("ad_form") in ("scene_story", "brand_tvc", "presenter_pitch"):
        return brief["ad_form"]
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


def evaluate_release_table(plan, strategy=None, *, exempt_lines=()):
    """Run the table. exempt_lines are merchant-insisted texts only."""
    from .progressive_planning import is_degenerate_plan
    if not isinstance(plan, dict):
        return [GateFinding("degenerate_plan", "占位稿不是完成的方案", detail="没有方案")]
    strategy = _strategy_of(plan, strategy)
    form = _form_of(strategy, plan)
    exempt = {line.strip() for line in exempt_lines or () if isinstance(line, str) and line.strip()}
    findings = []

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


def images_blocked_reason(plan, *, strategy=None):
    """The only decision: None means this plan may start image generation.

    No review record is not released. passed and repaired already cleared the
    review. conservative_fallback is released only when the hard-gate table
    passes and the board is not a degenerate placeholder. Merchant-insisted
    lines are the only lines skipped by that check.
    """
    if not isinstance(plan, dict) or "review_status" not in plan:
        return "方案还没有检查记录，不能准备图片。"
    status = plan.get("review_status")
    if status in ("passed", "repaired"):
        return None
    if status == "conservative_fallback":
        findings = evaluate_release_table(plan, strategy, exempt_lines=insisted_lines(plan))
        if findings:
            return "方案检查还没有通过，已有方案保留。请修改后再准备图片。"
        return None
    return "方案检查还没有通过，已有方案保留。请修改后再准备图片。"


def stamp_honest_label(plan, strategy=None, *, repairs=0):
    """Write the review label the table actually supports. Returns the status."""
    if not isinstance(plan, dict):
        return None
    if plan.get("review_status") in ("passed", "repaired"):
        return plan["review_status"]
    findings = evaluate_release_table(plan, strategy, exempt_lines=insisted_lines(plan))
    if findings:
        plan["review_status"] = "not_ready"
        plan["review_status_note"] = "硬门没有通过，这版不会拿去确认。"
        return "not_ready"
    review = plan.get("plan_review") if isinstance(plan.get("plan_review"), dict) else None
    if isinstance(review, dict) and review.get("ready") is True and review.get("checks"):
        plan["review_status"] = "repaired" if repairs else "passed"
        plan.pop("review_status_note", None)
        return plan["review_status"]
    note = plan.get("review_status_note") if plan.get("review_status") == "conservative_fallback" else None
    plan["review_status"] = "conservative_fallback"
    plan["review_status_note"] = note or "没有完整的综合检查，硬门已经通过。这条已记下。"
    return "conservative_fallback"


def classify_release(plan, strategy=None, *, locked_lines=(), insisted_lines_=()):
    """ok, ask, or repair. ask is only when every hit is a locked, uninsisted line."""
    insisted = {line.strip() for line in insisted_lines_ or () if isinstance(line, str) and line.strip()}
    locked = {line.strip() for line in locked_lines or () if isinstance(line, str) and line.strip()}
    findings = evaluate_release_table(plan, strategy, exempt_lines=insisted)
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


def duration_hold(locked_lines, duration, strategy):
    tenth = _tenth(strategy)
    for line in locked_lines or ():
        if isinstance(line, str) and line.strip() and not line_fits_duration(line.strip(), duration, tenth):
            question = duration_question(line.strip(), duration, tenth)
            if question is None:
                continue
            return MerchantLineChoice(question["prompt"], [question])
    return None


def lengthen_target(messages, duration, brief="", current_plan=None):
    """Seconds to grow to after the merchant picks lengthen, or None."""
    texts = [brief] + [item.get("content", "") for item in messages or [] if isinstance(item, dict) and item.get("role") == "user"]
    if not any(isinstance(text, str) and _LENGTHEN.search(text) for text in texts):
        return None
    if type(duration) is not int or duration >= 60:
        return None
    locked, _insisted = line_locks(brief, messages, current_plan)
    tenth = 29
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


def settle_manual_edit(previous, submitted, duration, strategy=None):
    """Label a hand edit, or say which locked line needs a choice.

    Returns (plan, error). error is a plain sentence when the edit must not
    be shown for confirmation.
    """
    if not isinstance(submitted, dict):
        return None, "这版方案没能通过检查，上一版还在。"
    plan = deepcopy(submitted)
    locked = changed_lines(previous, plan)
    existing = [line for line in (plan.get("merchant_locked_lines") or []) if isinstance(line, str) and line.strip()]
    plan["merchant_locked_lines"] = list(dict.fromkeys([*existing, *locked]))
    insisted = insisted_lines(plan)
    hold = duration_hold(locked, duration, strategy or plan.get("strategy"))
    if hold is not None:
        return None, hold.questions[0]["prompt"]
    kind, findings = classify_release(plan, strategy or plan.get("strategy"), locked_lines=locked, insisted_lines_=insisted)
    if kind == "ask":
        parts = [f"「{item.line}」碰到了这条规则：{item.summary}" for item in findings if item.line]
        return None, ("；".join(parts) + "。可以让系统改写，你自己改一句，或在对话里说就按这句来。")[:500]
    status = stamp_honest_label(plan, strategy or plan.get("strategy"))
    if status == "not_ready" or images_blocked_reason(plan, strategy=strategy or plan.get("strategy")):
        return None, "这版方案没能通过检查，上一版还在。"
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


def _conservative_director(strategy, locked_lines=()):
    """A plain product hold that is not the two-line placeholder."""
    selected = next(item for item in strategy.directions if item.id == strategy.selected_direction_id)
    evidence = list(dict.fromkeys(selected.evidence_ids)) or [fact.id for fact in strategy.brief.facts[:1]]
    characters = list(selected.production.characters) if selected.production else []
    speech = selected.production.speech_mode if selected.production else "voiceover"
    speaker = ""
    if speech == "character_dialogue":
        speaker = selected.production.speaker_id or (characters[0].id if characters else "")
    character_ids = [speaker] if speaker else ([characters[0].id] if characters else [])
    form = _form_of(strategy)
    lines = [line.strip() for line in locked_lines or () if isinstance(line, str) and line.strip()]
    if speech == "none":
        lines = []
    elif form == "scene_story":
        lines = [line for line in lines if not line_asserts_product_claim(line)] or ["先放这儿。", "就这样。"]
    elif form == "brand_tvc":
        lines = lines[:1] or ["看清这件。"]
    else:
        lines = lines or ["看一下这个。", "我拿着。"]
    lines = lines[:4]
    product = (strategy.brief.product_name or "商品").strip() or "商品"
    timing = {"visual_seconds": 2, "speech_overlap": True, "action_plan": "商品留在原处，供人看清",
              "start_state": "商品在画面里", "end_state": "商品在画面里", "time_transition": "continuous"}
    shots_text = [
        f"{product}放在桌上，光从侧面过来。",
        f"{product}留在原处，镜头停近一些，没有人把它举起来。",
        f"{product}仍在桌上，人看着手边，机位不动。",
        f"桌面还是{product}，光线不变，人没有转向镜头。",
    ]
    beats, shots = [], []
    count = max(len(lines), 1)
    for index in range(count):
        line = "" if speech == "none" else (lines[index] if index < len(lines) else "")
        beats.append({"id": f"b{index}", "purpose": "看清商品", "message": "看清商品外观",
                      "visual_role": "商品留在画面里", "narration": line,
                      "evidence_ids": list(evidence), "timing": deepcopy(timing)})
        shots.append({"id": f"shot_{index}", "coverage": [{"beat_id": f"b{index}", "weight": 1}],
                      "description": shots_text[index % len(shots_text)], "product_visible": True,
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


def conservative_usable_plan(strategy, duration, generation_strategy, asset_ids, locked_lines=(), cta_target=None):
    """A usable board that passes the hard-gate table, or None."""
    from .progressive_planning import compile_director
    aligned = _align_voice(strategy)
    if aligned is None:
        return None
    tenth = _tenth(aligned)
    safe_locked = [line for line in locked_lines or () if isinstance(line, str) and line_fits_duration(line, duration, tenth)]
    try:
        director = _conservative_director(aligned, safe_locked)
        plan, _schedule, _coordinated = compile_director(
            director, aligned, duration, generation_strategy, list(asset_ids or []), safe_locked, cta_target)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if not isinstance(plan, dict):
        return None
    plan["review_status_note"] = "硬门没有通过，已换成一份能通过检查的保守方案。这条已记下。"
    plan.pop("review_status", None)
    plan.pop("plan_review", None)
    status = stamp_honest_label(plan, aligned)
    if status != "conservative_fallback" or images_blocked_reason(plan, strategy=aligned):
        return None
    return plan


def present_checked_plan(plan, strategy=None, *, duration=None, generation_strategy=None,
                         asset_ids=(), locked_lines=(), cta_target=None, note=None, repairs=0):
    """Label a board honestly, or substitute one that passes. None means do not show it."""
    board = deepcopy(plan) if isinstance(plan, dict) else None
    if isinstance(board, dict) and note and board.get("review_status") not in ("passed", "repaired", "not_ready"):
        board["review_status"] = "conservative_fallback"
        board["review_status_note"] = note
    conservative = None
    if strategy is not None and type(duration) is int:
        try:
            conservative = conservative_usable_plan(
                strategy, duration, generation_strategy or "legacy_10s", asset_ids, locked_lines, cta_target)
        except (TypeError, ValueError, KeyError, AttributeError):
            conservative = None
    return finalize_unlabeled_plan(board, strategy, repairs=repairs, conservative=conservative)


def finalize_unlabeled_plan(plan, strategy=None, *, repairs=0, conservative=None):
    """Give an unlabeled or fallback board an honest label, or a substitute that passes.

    Returns the plan to show, or None when nothing usable passed the table.
    """
    from .progressive_planning import is_degenerate_plan
    if not isinstance(plan, dict):
        plan = None
    elif is_degenerate_plan(plan):
        plan = None
    elif stamp_honest_label(plan, strategy, repairs=repairs) == "not_ready":
        plan = None
    elif images_blocked_reason(plan, strategy=strategy):
        plan = None
    if plan is not None:
        return plan
    if conservative is None:
        return None
    return conservative if images_blocked_reason(conservative, strategy=strategy) is None else None
