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


_PRODUCT_NAME_ASK = re.compile(
    r"产品名称|商品名称|产品名|商品名|叫什么名字|叫什么名|名称是什么|名字是什么|产品叫什么|商品叫什么"
)


def asks_product_name(question):
    """A merchant is not asked to name the product. The category stays internal."""
    if not isinstance(question, dict):
        return False
    return bool(_PRODUCT_NAME_ASK.search(str(question.get("prompt") or "")))


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
        if asks_product_name(item):
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


class ClarificationIncomplete(ValueError):
    """The reply did not ask every question this turn still needs.

    The gateway retries once. The sentence is for that retry, not for the merchant.
    """

    def __init__(self):
        super().__init__("还缺的问题没有问全")


def missing_clarification_ids(project, messages=(), gates=(), *, planning_requested=False, existing_work=False):
    """Fixed questions this fresh offer still has to ask. Empty when nothing is missing."""
    if not isinstance(project, dict) or "ad_form" not in project:
        return []
    if not _fresh_proposal(project, gates, existing_work=existing_work):
        return []
    from .audience import should_ask_audience
    from .cta import should_ask_cta
    from .highlight import should_ask_highlight

    ids = []
    if project.get("ad_form") is None:
        ids.append("ad_form")
    if should_ask_highlight(project, messages, gates, planning_requested=planning_requested):
        ids.append("highlight")
    if should_ask_audience(project, messages, gates, planning_requested=planning_requested):
        ids.append("audience")
    if should_ask_cta(project):
        ids.append("cta_target")
    return ids


def _covers_clarification_question(item, question_id):
    if not isinstance(item, dict):
        return False
    if question_id == "highlight":
        from .highlight import displaced_highlight, spec_question
        if item.get("id") == "highlight":
            return True
        return bool(displaced_highlight(item) and not spec_question(item))
    if question_id == "audience":
        from .audience import AUDIENCE_IDS, displaced_audience
        if item.get("id") in AUDIENCE_IDS:
            return True
        return bool(displaced_audience(item))
    return item.get("id") == question_id


def clarification_questions_cover(raw, required_ids):
    """True when the model's own questions already include every missing one.

    Program-added questions do not count. A selling-point prompt counts; a
    count, size or packaging question does not.
    """
    if not required_ids:
        return True
    questions = raw.get("questions") if isinstance(raw, dict) else None
    if not isinstance(questions, list):
        return False
    return all(any(_covers_clarification_question(item, question_id) for item in questions)
               for question_id in required_ids)


def response_carries_direction(raw):
    """A creative offer was attached, whether or not the kind says so."""
    if not isinstance(raw, dict):
        return False
    if raw.get("directions") or raw.get("concept_pool"):
        return True
    if raw.get("selected_direction_id") or raw.get("recommended_direction_id"):
        return True
    return raw.get("kind") in ("directions", "plan")


def salvage_clarification_card(project, messages=(), gates=(), *, planning_requested=False):
    """The missing questions only. Direction text from the same reply is not kept."""
    questions = pack_clarification_questions(
        [], project, messages, gates, planning_requested=planning_requested)
    return {"kind": "clarify", "message": confirmation_message(questions), "questions": questions}


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
        questions = [q for q in authored_questions if q.get("id") != "ad_form" and not asks_product_name(q)]
        if form is not None and not questions and not any(asks_product_name(q) for q in authored_questions):
            # A redundant form-only clarification should not force a user to
            # answer again. The existing one-shot correction can reroute it.
            raise ValueError("广告倾向已确认，不应重复提问；根据已有信息回复或继续策划")
        result["questions"] = pack_clarification_questions(
            questions, project, messages, gates, planning_requested=planning_requested)
        if not result["questions"]:
            raise ValueError("不要向商家询问产品名称；根据已有品类继续策划")
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


_PRODUCTION_EDIT = re.compile(
    r"台词|文案|旁白|口播词|方案|分镜|脚本|方向|时长|秒钟|镜头|画面|视频|配音|音色|字幕|图片|场景图"
)
_REOPEN_CLAUSE = re.compile(
    r"(?:我想再改一下|我想改一下|再改一下|再改改|我想再改|"
    r"我想改个内容|想改个内容|我改个内容|改一下内容|改个内容|"
    r"改一下刚才的选择|改一下刚才选的|刚才的选择再改一下|刚才的选择我想改一下|"
    r"重新填一下|重填一下|重新选一下|"
    r"改一下选择|改一下回答|改一下澄清)[吧啊呀了]?"
)
_WHICH_PART = re.compile(r"哪一部分|哪一块|想改的是哪|改哪一")
_PART_SWAP = (
    re.compile(r"换表达方式|换广告倾向|换广告类型"),
    re.compile(r"换想被记住|换卖点|记住的点"),
    re.compile(r"换拍给谁|换人群|换使用情境"),
    re.compile(r"换看完|换去向|换去哪里"),
)
_CANONICAL_IDS = ("ad_form", "highlight", "audience", "cta_target")


def requests_clarification_revision(text):
    """A request to edit the last clarification, not a line, plan or duration."""
    whole = re.sub(r"\s+", "", str(text or ""))
    if not whole or explicit_ad_form(whole) is not None or unresolved_ad_form_change(whole):
        return False
    clauses = [clause for clause in re.split(r"[，,。！!；;？?\n]", whole) if clause]
    if not clauses or any(_PRODUCTION_EDIT.search(clause) for clause in clauses):
        return False
    return any(_REOPEN_CLAUSE.fullmatch(clause) for clause in clauses)


def is_which_part_menu(raw):
    """The model menu that asks which saved answer to change."""
    if not isinstance(raw, dict) or raw.get("kind") != "clarify":
        return False
    chunks = [str(raw.get("message") or "")]
    for question in raw.get("questions") or []:
        if not isinstance(question, dict):
            continue
        chunks.append(str(question.get("prompt") or ""))
        for option in question.get("options") or []:
            if isinstance(option, dict):
                chunks.append(str(option.get("label") or ""))
    blob = re.sub(r"\s+", "", "".join(chunks))
    if _WHICH_PART.search(blob):
        return True
    return sum(bool(pattern.search(blob)) for pattern in _PART_SWAP) >= 2


def _answered_clarifications(gates):
    for gate in gates or ():
        if not isinstance(gate, dict) or gate.get("status") != "answered":
            continue
        if gate.get("kind") not in (None, "clarification", "clarify"):
            continue
        yield gate


def _prefill_answer(question, answer, source_question):
    if not isinstance(answer, dict):
        return None
    question_id = question["id"]
    if answer.get("skipped"):
        return {"question_id": question_id, "skipped": True}
    text = answer.get("text")
    if isinstance(text, str) and text.strip():
        return {"question_id": question_id, "text": text.strip()[:500]}
    option_id = answer.get("option_id")
    if not isinstance(option_id, str) or not option_id:
        return None
    allowed = {item.get("id") for item in question.get("options") or [] if isinstance(item, dict)}
    if option_id in allowed:
        return {"question_id": question_id, "option_id": option_id}
    labels = {item.get("id"): item.get("label") for item in (source_question or {}).get("options") or []
              if isinstance(item, dict)}
    label = labels.get(option_id)
    if isinstance(label, str) and label.strip() and question.get("allow_custom", True):
        return {"question_id": question_id, "text": label.strip()[:500]}
    return None


def _saved_form(project, prefilled):
    answer = prefilled.get("ad_form")
    if isinstance(answer, dict):
        if answer.get("option_id") in AD_FORM_LABELS:
            return answer["option_id"]
        parsed = explicit_ad_form(answer.get("text") or "")
        if parsed:
            return parsed
    return project.get("ad_form")


def reopen_clarification_card(project, messages=(), gates=()):
    """The original four questions, filled with the choices already saved.

    A story still omits the destination. The message is the usual confirmation.
    """
    from .audience import AUDIENCE_IDS, audience_question
    from .cta import cta_question
    from .highlight import highlight_question, merchant_corpus

    saved, sources = {}, {}
    for gate in _answered_clarifications(gates):
        questions = {item.get("id"): item for item in gate.get("questions") or [] if isinstance(item, dict)}
        for answer in gate.get("answers") or []:
            if not isinstance(answer, dict):
                continue
            question_id = answer.get("question_id")
            source = questions.get(question_id)
            if question_id in AUDIENCE_IDS:
                question_id = "audience"
            if question_id not in _CANONICAL_IDS:
                continue
            saved[question_id] = answer
            sources[question_id] = source
    corpus = merchant_corpus(project, messages)
    drafted = {
        "ad_form": ad_form_question(),
        "highlight": highlight_question(corpus),
        "audience": audience_question(corpus),
        "cta_target": cta_question(),
    }
    prefilled = {}
    for question_id, question in drafted.items():
        answer = _prefill_answer(question, saved.get(question_id), sources.get(question_id))
        if answer:
            prefilled[question_id] = answer
    if "ad_form" not in prefilled and project.get("ad_form") in AD_FORM_LABELS:
        prefilled["ad_form"] = {"question_id": "ad_form", "option_id": project.get("ad_form")}
    form = _saved_form(project, prefilled)
    questions = [drafted["ad_form"], drafted["highlight"], drafted["audience"]]
    if form != "scene_story":
        questions.append(drafted["cta_target"])
        if "cta_target" not in prefilled:
            target = project.get("cta_target")
            if target in ("link", "brand", "message", "store", "ai_decide"):
                prefilled["cta_target"] = {"question_id": "cta_target", "option_id": target}
            elif isinstance(target, str) and target.strip() and target.strip() not in ("none",):
                prefilled["cta_target"] = {"question_id": "cta_target", "text": target.strip()[:500]}
    else:
        prefilled.pop("cta_target", None)
    return {
        "kind": "clarify",
        "message": confirmation_message(questions),
        "questions": questions,
        "prefilled_answers": [prefilled[item["id"]] for item in questions if item["id"] in prefilled],
    }


def clarification_revision_response(project, messages=(), gates=(), *, force=False):
    """Reopen the saved card. None when this turn is not that request."""
    if not isinstance(project, dict) or "ad_form" not in project:
        return None
    if not force:
        latest = next((str(message.get("content") or "") for message in reversed(messages or [])
                       if isinstance(message, dict) and message.get("role") == "user"), "")
        if not requests_clarification_revision(latest):
            return None
    if not any(True for _ in _answered_clarifications(gates)):
        return None
    return reopen_clarification_card(project, messages, gates)


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
