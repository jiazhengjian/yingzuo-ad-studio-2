"""Merchant-owned creative preference, independent of production capabilities.

Text recognition deliberately accepts explicit, single-form declarations only.
It is not a semantic intent classifier; comparison/negation remains dialogue.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Literal

AdForm = Literal["auto", "brand_tvc", "scene_story", "presenter_pitch"]
AD_FORM_LABELS = {
    "brand_tvc": "产品展示广告",
    "scene_story": "剧情／场景广告",
    "presenter_pitch": "人物讲解广告（口播）",
    "auto": "根据商品推荐",
}

_ALIASES = {
    # Keep the stored key and old labels readable without rewriting history.
    "brand_tvc": r"(?:产品|商品)(?:展示|演示)(?:型)?广告(?:片)?|TVC(?:广告大片|大片|广告)?|广告大片",
    "scene_story": r"剧情[／/]场景(?:向)?广告(?:片)?|剧情(?:向)?广告(?:片)?|场景(?:向)?广告(?:片)?",
    "presenter_pitch": r"人物讲解广告(?:[（(]口播[）)])?|人物讲解|人物口播广告(?:片)?|人物口播|口播广告(?:片)?",
    "auto": r"根据商品推荐|由系统推荐|让(?:系统|AI)推荐(?:广告(?:类型|倾向|形式))?|广告(?:类型|形式|倾向)你来推荐|你来推荐广告(?:类型|形式|倾向)",
}

_DISCUSSION = r"还是|或者|是否|如果|例如|比如|相比|比较|是不是|能不能|要不要|怎么样|好不好|吗|[？?]"
_NEGATIVE = r"不要|不想|不是|不用|不做|不换|不改|别|未决定|没想好|不确定"
_STOP_CHANGE = r"(?:现在|这次|先|暂时)?(?:不要|不用|别|先不|暂不|不)(?:再)?(?:改|换)(?:了)?(?:[，,。！!；;]|$)|(?:不改|不换|保持)(?:广告)?(?:倾向|类型|形式|表达方式)(?:不变)?|(?:广告)?(?:倾向|类型|形式|表达方式)不变"
_SUBJECT = r"(?:(?:请|麻烦|帮我|我|我们|这次|现在|本次|想要|想|希望|决定|要|明确|先|再|就))*"
_CHANGE_VERB = r"切换到|切换成|切换为|调整成|调整为|变更为|改成|改为|换成|换为|换|改"
_FORM_DOMAIN = r"广告(?:倾向|类型|形式|表达方式)|表达方式"
_SHORT_NAMES = {"brand_tvc": "产品展示|商品展示|产品演示",
                "scene_story": "剧情[／/]场景|剧情|场景",
                "presenter_pitch": "口播|人物讲解"}


def _clauses(text):
    return re.split(r"[，,。！!；;\n\r]", re.sub(r"\s+", "", str(text)))


def unresolved_ad_form_change(text):
    """Recognize a change *request*, never guess its new value.

    Ordinary edits to a character, line or story are not form changes. This
    fallback only returns a clarification obligation for this turn.
    """
    whole = re.sub(r"\s+", "", str(text))
    if explicit_ad_form(whole) is not None or re.search(_DISCUSSION + "|" + _STOP_CHANGE, whole, re.I):
        return False
    names = "|".join(_ALIASES.values())
    for clause in _clauses(whole):
        if re.search(_NEGATIVE, clause):
            continue
        # The domain must be the object of the change, not a sentence's topic
        # (e.g. '口播广告里改一下台词'). Bare '剧情' is deliberately excluded.
        if re.fullmatch(_SUBJECT + r"(?:把)?(?:" + _FORM_DOMAIN + r")(?:先|再|想|希望|要)?(?:" + _CHANGE_VERB + r").{0,60}", clause):
            return True
        if re.fullmatch(_SUBJECT + r"(?:" + _CHANGE_VERB + r")(?:一下|一种|一个|个|一条|种|下)?(?:" + _FORM_DOMAIN + "|" + names + r")(?:吧|一下|下)?", clause, re.I):
            return True
        if re.fullmatch(_SUBJECT + r"(?:" + _CHANGE_VERB + r").{0,35}(?:广告(?:倾向|类型|形式)?|表达方式)", clause, re.I):
            return True
    return False


def ad_form_confirmation(project):
    label = AD_FORM_LABELS.get(project.get("ad_form"))
    saved = f"当前保存的广告倾向是「{label}」。" if label else "广告倾向还没有确定。"
    return {"kind": "clarify", "message": saved + "请确认这次希望采用的主要表达方式。",
            "questions": [ad_form_question()]}


def guard_ad_form_confirmation(raw, project):
    """Prevent an unresolved request or a contradictory switch announcement
    from being published as a completed preference change. Narrow statements
    only: explanations, recommendations and quoted examples remain dialogue.
    """
    if not isinstance(raw, dict) or "ad_form" not in project:
        return raw
    if project.get("ad_form_confirmation_required"):
        return ad_form_confirmation(project)
    questions = raw.get("questions")
    if (raw.get("kind") == "clarify" and isinstance(questions, list)
            and any(isinstance(q, dict) and q.get("id") == "ad_form" for q in questions)
            and re.search(r"(?:确认|确定|明确).{0,12}(?:更换|切换|改选|修改).{0,8}(?:广告倾向|广告类型|广告形式|表达方式)|(?:更换|切换|改选|修改).{0,8}(?:广告倾向|广告类型|广告形式|表达方式).{0,12}(?:确认|确定|明确)", str(raw.get("message", "")))):
        # Let the existing strategy call clarify an unrecognized change.
        # The stored preference remains authoritative until the user answers.
        return ad_form_confirmation(project)
    if project.get("ad_form") == "auto":
        return raw
    start = r"(?:(?:好的|好|明白|收到)[：:]?)?(?:(?:本次|这次|现在|我们|我)?已(?:经)?(?:为你|帮你|给你)?(?:(?:将|把)广告(?:倾向|类型|形式))?(?:改成|改为|换成|换为|切换为|切换成)|(?:这一版|这次|本次|接下来|现在|新版)(?:就|会)?按)"
    end = r"(?:的)?(?:方向|风格|方式|形式)?(?:来做|来拍|来制作|继续策划|继续制作|策划|制作|生成|执行)?(?:了|吧)?"
    for clause in _clauses(raw.get("message", "")):
        for form, aliases in _ALIASES.items():
            names = aliases + "|" + _SHORT_NAMES.get(form, "(?!)")
            if form != project.get("ad_form") and re.fullmatch(start + "(?:" + names + ")" + end, clause, re.I):
                return ad_form_confirmation(project)
    return raw


_INFER_FORMS = (
    ("presenter_pitch", re.compile(r"口播|讲解|主播")),
    ("scene_story", re.compile(r"剧情短视频|小剧情|剧情|故事")),
    ("brand_tvc", re.compile(r"产品展示|不出镜|品牌片|质感|TVC", re.I)),
)
_NOT_A_FORM_CHOICE = re.compile(r"里的|台词|演员|文案|改一下|缩短")


def plain_merchant_text(text):
    """Merchant-facing wording. Internal field names stay out of the reply."""
    if not isinstance(text, str) or not text:
        return text
    result = text
    for old, new in (
        ("保存值为空的偏好", "还没选定广告类型"),
        ("还差一项保存的偏好", "还需要先选定广告类型"),
        ("保存的偏好", "广告类型"),
        ("保存值", "已选的广告类型"),
    ):
        result = result.replace(old, new)
    return result


def infer_ad_form(text):
    """Use an explicit choice, otherwise one clear keyword in the brief."""
    chosen = explicit_ad_form(text)
    if chosen is not None:
        return chosen
    whole = re.sub(r"\s+", "", str(text))
    if not whole or re.search(_DISCUSSION + "|" + _NEGATIVE, whole, re.I):
        return None
    if _NOT_A_FORM_CHOICE.search(whole):
        return None
    hits = []
    for clause in re.split(r"[，,。！!；;\n\r]", whole):
        if re.search(_NEGATIVE + r"|不一定|未必|不见得", clause):
            continue
        hits.extend(form for form, pattern in _INFER_FORMS if pattern.search(clause))
    if len(set(hits)) != 1:
        return None
    return hits[0]


def explicit_ad_form(text):
    """Recognize affirmative declarations, not conditional/comparative mentions."""
    whole = re.sub(r"\s+", "", str(text))
    if re.search(_DISCUSSION + "|" + _STOP_CHANGE, whole, re.IGNORECASE):
        return None
    matches = set()
    for clause in re.split(r"[，,。！!；;\n\r]", whole):
        if re.search(_NEGATIVE, clause):
            continue
        mentioned = [form for form, aliases in _ALIASES.items()
                     if re.search(aliases + "|" + _SHORT_NAMES.get(form, "(?!)"), clause, re.I)]
        if len(mentioned) > 1 and re.search(r"既|又|和|与|兼顾|混合|结合|同时|[+＋]", clause):
            return None
        for form, aliases in _ALIASES.items():
            tail = r"(?:" + aliases + r")(?:风格)?(?:吧|就好|就行)?"
            prefix = r"(?:" + _FORM_DOMAIN + r")(?:选|选择|用|设为|是|：|:|" + _CHANGE_VERB + ")"
            request = _SUBJECT + r"(?:把(?:" + _FORM_DOMAIN + r"))?(?:做|拍|制作|选|选择|用|采用|" + _CHANGE_VERB + ")"
            # Qualifiers are bounded and belong after a positive creation verb;
            # this accepts duration/product/scene details, not arbitrary mentions.
            qualified = request + r"[^，,。；;？?]{0,40}?"
            for_product = r"(?:我|我们)?(?:想要|想|希望|决定|要)?(?:给|为)[^，,。；;？?]{1,30}(?:做|拍|制作)(?:一条|一个)?"
            from_form = _SUBJECT + r"(?:从|把)(?:现在的|当前的|原来的|之前的)?(?:" + "|".join(_ALIASES.values()) + r")(?:" + _CHANGE_VERB + ")"
            if any(re.fullmatch(start + tail, clause, re.IGNORECASE)
                   for start in ("", prefix, qualified, for_product, from_form)):
                matches.add(form)
    if len(matches) != 1:
        return None
    chosen = next(iter(matches))
    return None if rejects_current_ad_form(text, chosen) else chosen


def rejects_current_ad_form(text, current):
    """A narrow explicit withdrawal clears a choice without selecting another."""
    if current not in _ALIASES or current == "auto":
        return False
    whole = re.sub(r"\s+", "", str(text))
    if re.search(r"还是|或者|是否|如果|例如|比如|[？?]", whole):
        return False
    aliases = "(?:" + _ALIASES[current] + ({"scene_story": "|剧情|剧情片|场景广告", "presenter_pitch": "|口播"}.get(current, "")) + ")"
    return any(re.fullmatch(r"(?:(?:我|我们)?(?:现在|这次|这条广告)?)?(?:不想(?:做|拍|用)?|不要|不用|不做|不拍|不选|取消)" + aliases + r"(?:了|吧)?", clause, re.IGNORECASE)
               or re.fullmatch(aliases + r"(?:不做|不要|不用|不拍|不选)了?", clause, re.IGNORECASE)
               for clause in re.split(r"[，,。！!；;\n\r]", whole))


def needs_ad_form_choice(project):
    return ("ad_form" in project and project.get("ad_form") is None
        and (not project.get("plan") or project.get("ad_form_updated_context_version", 0)
             > project.get("plan_context_version", 0)))


def ad_form_question():
    return {"id": "ad_form", "prompt": "这条广告主要采用哪种表达方式？可以混合使用，以一种为主。",
        "options": [{"id": form, "label": label} for form, label in AD_FORM_LABELS.items()],
        "allow_custom": True, "allow_skip": True}


def answer_ad_form(answer):
    if answer.get("skipped"):
        return "auto"
    chosen = answer.get("option_id")
    if chosen in AD_FORM_LABELS:
        return chosen
    return explicit_ad_form(answer.get("text", ""))


def intake_strategy_response(raw, project, *, ask_preference=True):
    """Canonicalize one preference card before validating model output.

    Existing frozen jobs without the new project field retain their contract.
    Only actual planning/clarification branches get the card; greetings don't.
    Unknown free text is left for clarification, not silently mapped to a form.
    """
    result = deepcopy(raw)
    if not isinstance(result, dict) or "ad_form" not in project:
        return result
    if ask_preference:
        guarded = guard_ad_form_confirmation(result, project)
        if guarded is not result:
            return guarded
    form = project.get("ad_form")
    kind = result.get("kind")
    if ask_preference and kind == "clarify":
        authored_questions = result.get("questions", [])
        if not isinstance(authored_questions, list) or any(not isinstance(q, dict) for q in authored_questions):
            raise ValueError("澄清问题须为对象列表")
        questions = [q for q in authored_questions if q.get("id") != "ad_form"]
        if form is None:
            result["questions"] = [ad_form_question(), *questions[:2]]
        elif questions:
            result["questions"] = questions
        else:
            # A redundant form-only clarification should not force a user to
            # answer again. The existing one-shot correction can reroute it.
            raise ValueError("广告倾向已确认，不应重复提问；根据已有信息回复或继续策划")
        from .cta import attach_cta_question
        result = attach_cta_question(result, project)
    elif ask_preference and kind in ("directions", "plan") and needs_ad_form_choice(project):
        result = {"kind": "clarify", "message": "先确认这条广告的表达倾向，再继续准备方案。",
                  "questions": [ad_form_question()]}
    brief = result.get("brief")
    if isinstance(brief, dict):
        bind_brief_ad_form(brief, project)
    return result


def bind_brief_ad_form(brief, project):
    """Bind server-owned preference; reject an authored conflicting value."""
    if "ad_form" not in project:
        return brief
    expected = project.get("ad_form")
    proposed = brief.get("ad_form")
    if proposed is not None and proposed != expected:
        raise ValueError("广告倾向由用户确认，策划回复不能擅自更改")
    if expected is not None:
        brief["ad_form"] = expected
    return brief


def strip_ad_form_declarations(text):
    """Keep raw conversation, but don't cite recognized preferences as facts."""
    parts = re.split(r"([，,。！!；;\n\r])", str(text))
    for index in range(0, len(parts), 2):
        clause = parts[index]
        form = explicit_ad_form(clause)
        if form is not None:
            # A declaration may also carry product identity/attributes. Remove
            # only its form label, preserving original contiguous fact text.
            found = re.search(r"(?:" + _ALIASES[form].replace("TVC", r"TVC\s*") + r")(?:风格)?(?:吧|就好|就行)?\s*$", clause, re.IGNORECASE)
            parts[index] = clause[:found.start()] if found else clause
    return "".join(parts)
