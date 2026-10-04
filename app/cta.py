"""Where the viewer should go after the ad, only when the merchant said so.

An explicit target (link, brand page, message, store, or their own words) must
be spoken in every ad type. 「让 AI 决定」 and an unanswered brief add no
guidance line. Prices that are not in the merchant's facts are removed here,
not shown back as an error.
"""
from __future__ import annotations

import re

from .ad_script import AdScript


_OPEN = frozenset({"", "none", "ai_decide"})
_KNOWN = frozenset({"link", "brand", "message", "store"})
_GUIDANCE = re.compile(
    r"点下面|下方链接|链接放|小黄车|点链接|购买链接|私信|到店|门店|品牌页|产品详情|详情页|关注一下|点个关注|去看看"
)
_PRICE = re.compile(
    r"(?:价格|售价|到手价|优惠价)\s*(?:¥|￥)?\s*(?:\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万两]+)"
    r"|(?:¥|￥)\s*\d+(?:\.\d+)?"
    r"|\d+(?:\.\d+)?\s*(?:元|块钱|块)"
    r"|[零〇一二三四五六七八九十百千万两]{2,}\s*(?:元|块钱|块)"
    r"|(?<![\d.])\d{2,4}(?![\d.])(?!\s*(?:分钟|秒钟|秒|转|瓦|伏|克|毫升|米|寸|倍|次|档))"
    r"|[零〇一二三四五六七八九百千]{3,}(?!\s*(?:分钟|秒钟|秒|转))"
    r"|\d+(?:\.\d+)?\s*折"
    r"|[零一二三四五六七八九十两]\s*折"
    r"|优惠"
)
_CLOSES = {
    "presenter_pitch": "这款就这些。",
    "brand_tvc": "还是这一只。",
    "scene_story": "就放这儿吧。",
}
_LINES = {
    "link": "想要的话，点下面链接看看。",
    "brand": "想了解，去产品详情看看。",
    "message": "想问的话，私信我。",
    "store": "欢迎到店看看。",
}
_MARKS = {
    "link": re.compile(r"点下面|下方链接|链接|小黄车"),
    "brand": re.compile(r"品牌页|产品详情|详情"),
    "message": re.compile(r"私信"),
    "store": re.compile(r"到店|门店"),
}


def stated_cta(text):
    """A destination the merchant already wrote. None when the brief is silent."""
    whole = str(text or "")
    if re.search(r"私信", whole):
        return "message"
    if re.search(r"到店|门店", whole):
        return "store"
    if re.search(r"品牌页|产品详情", whole):
        return "brand"
    if re.search(r"关注", whole):
        return "关注"
    if re.search(r"点链接|购买链接|下方链接|点下面|小黄车", whole):
        return "link"
    return None


def is_explicit_cta(value):
    return isinstance(value, str) and value not in _OPEN and bool(value.strip())


def is_open_cta(value):
    return value in _OPEN


def project_cta_mode(project):
    """None leaves existing lines alone. A stored column decides the ending."""
    if not isinstance(project, dict) or "cta_target" not in project:
        return None
    value = project.get("cta_target")
    if is_explicit_cta(value) or value == "ai_decide":
        return value
    return stated_cta(project.get("brief")) or "none"


def cta_question():
    return {
        "id": "cta_target",
        "prompt": "看完视频后，希望观众去哪里？",
        "options": [
            {"id": "link", "label": "商品页 / 购买链接"},
            {"id": "brand", "label": "品牌页 / 产品详情"},
            {"id": "message", "label": "私信 / 咨询购买"},
            {"id": "store", "label": "到店 / 门店地址"},
            {"id": "ai_decide", "label": "让 AI 决定"},
        ],
        "allow_custom": True,
        "allow_skip": True,
    }


def answer_cta(answer):
    if not isinstance(answer, dict) or answer.get("skipped"):
        return "ai_decide"
    option = answer.get("option_id")
    if option in _KNOWN or option == "ai_decide":
        return option
    text = str(answer.get("text") or "").strip()
    if not text:
        return "ai_decide"
    return stated_cta(text) or text[:80]


def should_ask_cta(project):
    if not isinstance(project, dict) or "cta_target" not in project:
        return False
    if project.get("cta_target") == "ai_decide" or is_explicit_cta(project.get("cta_target")):
        return False
    if stated_cta(project.get("brief")):
        return False
    return True


def attach_cta_question(result, project):
    """Bundle the destination question into a clarification that is already happening."""
    if not isinstance(result, dict) or result.get("kind") != "clarify" or not should_ask_cta(project):
        return result
    questions = result.get("questions")
    if not isinstance(questions, list):
        return result
    kept = [item for item in questions if not isinstance(item, dict) or item.get("id") != "cta_target"]
    question = cta_question()
    merged = []
    placed = False
    for item in kept:
        merged.append(item)
        if isinstance(item, dict) and item.get("id") == "ad_form" and not placed:
            merged.append(question)
            placed = True
    if not placed:
        merged.append(question)
    result["questions"] = merged[:3]
    return result


def cta_line(target):
    if target in _LINES:
        return _LINES[target]
    text = str(target or "").strip()
    if not text or text in _OPEN:
        return ""
    if text[-1] not in "。！？":
        text += "。"
    return text[:40]


def _grounded(token, source):
    compact = re.sub(r"\s+", "", token)
    if compact and compact in source:
        return True
    numbers = re.findall(r"\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万两]+", compact)
    return bool(numbers) and all(number and number in source for number in numbers)


def scrub_ungrounded_prices(text, source):
    """Drop a price, discount or bare number the merchant did not supply."""
    if not isinstance(text, str) or not text:
        return text
    source = source or ""

    def replace(match):
        token = match.group(0)
        return token if _grounded(token, source) else ""

    cleaned = _PRICE.sub(replace, text)
    cleaned = re.sub(r"[，,、]{2,}", "，", cleaned)
    cleaned = re.sub(r"^[，,、。！？\s]+", "", cleaned)
    cleaned = re.sub(r"[，,、]+([。！？])", r"\1", cleaned)
    cleaned = re.sub(r"\s{2,}", "", cleaned)
    if cleaned and cleaned[-1] not in "。！？":
        cleaned += "。"
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cleaned):
        return ""
    return cleaned


def _clauses(text):
    parts = re.split(r"([。！？])", text)
    rows = []
    index = 0
    while index < len(parts):
        body = parts[index]
        mark = parts[index + 1] if index + 1 < len(parts) else ""
        if body.strip() or mark:
            rows.append(body + mark)
        index += 2
    return rows


def strip_guidance(text):
    kept = [clause for clause in _clauses(text) if clause.strip() and not _GUIDANCE.search(clause)]
    cleaned = "".join(kept).strip()
    cleaned = re.sub(r"^[，,、]+", "", cleaned)
    if cleaned and cleaned[-1] not in "。！？":
        cleaned += "。"
    return cleaned


def _matches(text, target):
    if target in _MARKS:
        return bool(_MARKS[target].search(text or ""))
    needle = str(target or "").strip("。！？ ")
    return bool(needle) and needle in (text or "")


def _fact_blob(strategy):
    brief = getattr(strategy, "brief", None)
    if brief is None and isinstance(strategy, dict):
        brief = strategy.get("brief")
    if brief is None:
        return ""
    if isinstance(brief, dict):
        parts = [brief.get("product_name") or "", brief.get("audience") or "", brief.get("scenario") or ""]
        parts.extend(fact.get("text") or "" for fact in brief.get("facts") or [] if isinstance(fact, dict))
        return "".join(parts)
    parts = [getattr(brief, "product_name", "") or "", getattr(brief, "audience", "") or "",
             getattr(brief, "scenario", "") or ""]
    parts.extend(getattr(fact, "text", "") or "" for fact in getattr(brief, "facts", []) or [])
    return "".join(parts)


def _form(strategy):
    try:
        from .ad_form_playbook import form_for_strategy
        return form_for_strategy(strategy)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _locked(text, locked):
    return any(isinstance(item, str) and item and item in (text or "") for item in locked or ())


def apply_spoken_policy(script, strategy, cta_target, locked=()):
    """Remove invented prices. Add or remove the ending line for a known target."""
    data = script.model_dump() if hasattr(script, "model_dump") else script
    if not isinstance(data, dict):
        return script
    source = _fact_blob(strategy)
    beats = data.get("beats") or []
    for beat in beats:
        if not isinstance(beat, dict):
            continue
        narration = beat.get("narration") or ""
        if not narration or _locked(narration, locked):
            continue
        beat["narration"] = scrub_ungrounded_prices(narration, source)
    if cta_target is None:
        return AdScript.model_validate(data) if hasattr(script, "model_dump") else data
    spoken = [beat for beat in beats if isinstance(beat, dict) and (beat.get("narration") or "").strip()
              and not _locked(beat.get("narration"), locked)]
    if not spoken:
        return AdScript.model_validate(data) if hasattr(script, "model_dump") else data
    form = _form(strategy)
    if is_open_cta(cta_target):
        for beat in spoken:
            cleaned = strip_guidance(beat.get("narration") or "")
            beat["narration"] = cleaned
        last = spoken[-1]
        if not (last.get("narration") or "").strip():
            last["narration"] = _CLOSES.get(form, "看这里。")
    elif is_explicit_cta(cta_target):
        if not any(_matches(beat.get("narration"), cta_target) for beat in beats if isinstance(beat, dict)):
            spoken[-1]["narration"] = cta_line(cta_target)
    return AdScript.model_validate(data) if hasattr(script, "model_dump") else data
