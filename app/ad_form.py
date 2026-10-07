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


def pack_clarification_questions(model_questions, project, messages=(), gates=(), *, planning_requested=False):
    """Fixed questions take the card first. The model fills only the slots left.

    Order: ad form, selling point, audience, then destination. A story omits
    the destination. While the form is still open, the destination stays on
    the card and the client hides it if the merchant picks a story. Spec
    questions do not take a slot on a card that already has one of these.
    """
    from .audience import AUDIENCE_IDS, audience_question, displaced_audience, should_ask_audience
    from .cta import cta_question, should_ask_cta
    from .highlight import displaced_highlight, highlight_question, merchant_corpus, should_ask_highlight, spec_question

    corpus = merchant_corpus(project, messages)
    fixed = []
    if isinstance(project, dict) and "ad_form" in project and project.get("ad_form") is None:
        fixed.append(ad_form_question())
    ask_highlight = should_ask_highlight(project, messages, gates, planning_requested=planning_requested)
    ask_audience = should_ask_audience(project, messages, gates, planning_requested=planning_requested)
    if ask_highlight:
        fixed.append(highlight_question(corpus))
    if ask_audience:
        fixed.append(audience_question(corpus))
    if should_ask_cta(project):
        fixed.append(cta_question())
    fixed = fixed[:4]
    blocked = {"ad_form", "highlight", "audience", "cta_target", *AUDIENCE_IDS}
    rest = []
    for item in model_questions or []:
        if not isinstance(item, dict) or item.get("id") in blocked:
            continue
        if ask_highlight and displaced_highlight(item):
            continue
        if fixed and spec_question(item):
            continue
        if ask_audience and displaced_audience(item):
            continue
        rest.append(item)
    return [*fixed, *rest[: 4 - len(fixed)]]


def confirmation_message(questions):
    labels = {
        "ad_form": "广告怎么拍",
        "highlight": "最想让观众记住的一点",
        "audience": "拍给谁看、在什么情境下使用",
        "cta_target": "看完去哪里",
    }
    parts = [labels[item["id"]] for item in questions if isinstance(item, dict) and item.get("id") in labels]
    if not parts:
        return "先确认还缺的信息，再继续准备方案。"
    return f"先确认{'、'.join(parts)}，再继续准备方案。"


def _fresh_proposal(project, gates, *, existing_work=False):
    """A first offer. An existing plan, or a direction already chosen, stays put."""
    if existing_work:
        return False
    if isinstance(project, dict) and project.get("plan"):
        return False
    for gate in gates or ():
        if (isinstance(gate, dict) and gate.get("kind") == "direction"
                and gate.get("status") == "answered" and gate.get("selected_option_id")):
            return False
    return True


def intake_strategy_response(raw, project, *, ask_preference=True, messages=(), gates=(),
                             planning_requested=False, existing_work=False):
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
    from .audience import should_ask_audience
    from .cta import should_ask_cta
    from .highlight import should_ask_highlight
    form = project.get("ad_form")
    kind = result.get("kind")
    if ask_preference and kind == "clarify":
        authored_questions = result.get("questions", [])
        if not isinstance(authored_questions, list) or any(not isinstance(q, dict) for q in authored_questions):
            raise ValueError("澄清问题须为对象列表")
        questions = [q for q in authored_questions if q.get("id") != "ad_form"]
        if form is not None and not questions:
            # A redundant form-only clarification should not force a user to
            # answer again. The existing one-shot correction can reroute it.
            raise ValueError("广告倾向已确认，不应重复提问；根据已有信息回复或继续策划")
        result["questions"] = pack_clarification_questions(
            questions, project, messages, gates, planning_requested=planning_requested)
    elif ask_preference and kind in ("directions", "plan"):
        # A fresh offer still asks a missing selling point or audience, even
        # when the model skipped clarify and returned directions. Editing or
        # reselecting a plan that is already saved does not reopen the card.
        missing_story = _fresh_proposal(project, gates, existing_work=existing_work) and (
            should_ask_highlight(project, messages, gates, planning_requested=planning_requested)
            or should_ask_audience(project, messages, gates, planning_requested=planning_requested))
        if needs_ad_form_choice(project) or should_ask_cta(project) or missing_story:
            questions = pack_clarification_questions(
                [], project, messages, gates, planning_requested=planning_requested)
            result = {"kind": "clarify", "message": confirmation_message(questions), "questions": questions}
    brief = result.get("brief")
    if isinstance(brief, dict):
        bind_brief_ad_form(brief, project)
        from .audience import apply_known_audience
        from .planning import evidence_sources
        apply_known_audience(brief, gates, evidence_sources(project, messages, gates))
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


_CONFLICT_AXIS = re.compile(r"不敢按|不敢开|合租|怕吵醒|别吵醒|吵醒|冲突|争执")
_EFFICACY_LECTURE = re.compile(r"第三人称|旁白讲|画外讲|讲功效|讲.{0,8}(?:快|顺|静|干|负离子|护发)")
_LIFE_PAIN = re.compile(r"痛点|赶时间|赶出门|头发厚")
_ON_CAMERA = re.compile(r"出镜|真人出镜|有人出镜")
_PRESENTER_TALK = re.compile(r"对观众|对着观众|口播|讲解给")
_AXIS_FIELDS = ("brief", "consumer_insight", "scenario", "product_expression", "hook", "payoff")


def _axis_blob(card):
    parts = []
    for key in _AXIS_FIELDS:
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


def classify_narrative_axis(card):
    """Pick the ad form from the narrative spine, not from a person or a bed.

    A selling point the picture proves is a product film, even when the setup
    is a daily pain and the voice is third-person. A conflict or relationship,
    with the benefit only as a side result, is a story. An explicit merchant
    choice wins. Presenter detection stays on the existing keyword path.
    """
    if not isinstance(card, dict):
        return None
    explicit = card.get("explicit_form")
    if explicit is None and card.get("ad_form") in ("brand_tvc", "scene_story", "presenter_pitch"):
        explicit = card.get("ad_form")
    if explicit in ("brand_tvc", "scene_story", "presenter_pitch"):
        return explicit
    brief = card.get("brief") if isinstance(card.get("brief"), str) else ""
    named = explicit_ad_form(brief) or infer_ad_form(brief)
    if named == "presenter_pitch":
        return "presenter_pitch"
    blob = _axis_blob(card)
    speech = card.get("speech_mode")
    expression = card.get("product_expression") if isinstance(card.get("product_expression"), str) else ""
    if speech == "character_dialogue" and _PRESENTER_TALK.search(blob) and not _CONFLICT_AXIS.search(blob):
        return "presenter_pitch"
    lecture = bool(_EFFICACY_LECTURE.search(expression) or (
        speech == "voiceover" and _EFFICACY_LECTURE.search(blob)))
    conflict = bool(_CONFLICT_AXIS.search(blob))
    if conflict and not lecture:
        return "scene_story"
    if lecture or (speech == "voiceover" and _LIFE_PAIN.search(blob)) or (
            _ON_CAMERA.search(blob) and not conflict):
        return "brand_tvc"
    return None


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
