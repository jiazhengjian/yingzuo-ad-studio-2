"""Ask what the merchant wants remembered, before specs fill the card.

The question is fixed, like the destination question. It appears when the brief
never names a selling point and the merchant has not already answered or
skipped it. Count, size and packaging stay off this card unless a later round
really needs them to change direction.
"""
from __future__ import annotations

import re


_STATED = re.compile(
    r"卖点|主打|亮点|特色|优点|好处|最想|记住|突出|强调|"
    r"口感|好吃|好喝|好看|松软|不腻|新鲜|手工|工艺|夹心|"
    r"香味|香甜|奶香|好用|安静|降噪|快干|护发|颜值|透气|防水"
)
_SPEC_PROMPT = re.compile(r"种类|几个|数量|尺寸|包装|规格|多大|几寸|多少个|几块|具体是什么")
_DUPLICATE_PROMPT = re.compile(r"记住|卖点|亮点|最想让|特点")
_SKIPPED = "暂不确定，请使用谨慎表达"
_FOOD = re.compile(r"蛋糕|面包|甜点|甜品|饼干|巧克力|月饼|奶茶|咖啡|食品|零食|水果|糕点|冰淇淋|冰激凌")
_APPLIANCE = re.compile(r"吹风机|电器|耳机|手机|风扇|净化|扫地|洗衣|空调|家电|风筒|剃须刀|牙刷")
_BEAUTY = re.compile(r"口红|面霜|护肤|化妆|香水|面膜|精华")
_FASHION = re.compile(r"衣|裙|鞋|包|饰品|帽子")
_FIXED = ("ad_form", "highlight", "audience", "cta_target")
_DELEGATE = re.compile(r"你来定|你看着定|你来决定|交给你定|帮我定")


def highlight_examples(text):
    """Category-shaped examples. The merchant still answers in their own words."""
    blob = str(text or "")
    if _FOOD.search(blob):
        return "例如外观、口味、夹心、制作工艺、适合的场合"
    if _APPLIANCE.search(blob):
        return "例如外观、使用手感、功能、适合的场合"
    if _BEAUTY.search(blob):
        return "例如外观、质地、使用感受、适合的场合"
    if _FASHION.search(blob):
        return "例如外观、材质、穿法、适合的场合"
    return "例如外观、用法、特点、适合的场合"


def highlight_question(text=""):
    return {
        "id": "highlight",
        "prompt": f"最想让观众记住这个商品的哪一点？{highlight_examples(text)}。",
        "options": [],
        "allow_custom": True,
        "allow_skip": True,
    }


def merchant_corpus(project, messages=()):
    """Brief plus merchant answers. Question prompts and skips are not claims."""
    chunks = [str((project or {}).get("brief") or "")]
    for message in messages or ():
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        for line in str(message.get("content") or "").splitlines():
            if "：" in line:
                _prompt, _, answer = line.partition("：")
                answer = answer.strip()
                if answer and answer != _SKIPPED:
                    chunks.append(answer)
            elif line.strip():
                chunks.append(line.strip())
    return "\n".join(chunks)


def stated_highlight(text):
    """True when the merchant already named what the ad should remember."""
    return bool(_STATED.search(str(text or "")))


def highlight_closed(gates):
    """An answered or skipped highlight question is not asked again."""
    for gate in gates or ():
        if not isinstance(gate, dict) or gate.get("status") != "answered":
            continue
        if gate.get("kind") not in (None, "clarification", "clarify"):
            continue
        questions = gate.get("questions") or []
        if not any(isinstance(item, dict) and item.get("id") == "highlight" for item in questions):
            continue
        for answer in gate.get("answers") or []:
            if isinstance(answer, dict) and answer.get("question_id") == "highlight":
                return True
    return False


def merchant_delegated(project, messages=(), *, planning_requested=False):
    """The merchant handed the missing point to us, so it is not asked again."""
    if planning_requested:
        return True
    return bool(_DELEGATE.search(merchant_corpus(project, messages)))


def should_ask_highlight(project, messages=(), gates=(), *, planning_requested=False):
    if not isinstance(project, dict):
        return False
    if highlight_closed(gates):
        return False
    if merchant_delegated(project, messages, planning_requested=planning_requested):
        return False
    if stated_highlight(merchant_corpus(project, messages)):
        return False
    return True


def spec_question(item):
    """Count, size and packaging. They do not take a fixed-question slot."""
    if not isinstance(item, dict):
        return False
    if item.get("id") == "product_info":
        return True
    return bool(_SPEC_PROMPT.search(str(item.get("prompt") or "")))


def displaced_highlight(item):
    """Specs and a second selling-point question give up the slot."""
    if not isinstance(item, dict) or item.get("id") == "highlight":
        return False
    if spec_question(item):
        return True
    return bool(_DUPLICATE_PROMPT.search(str(item.get("prompt") or "")))


def _place_highlight(questions, question):
    merged = []
    for item in questions:
        merged.append(item)
        if isinstance(item, dict) and item.get("id") == "cta_target":
            merged.append(question)
            return merged
    merged = []
    placed = False
    for item in questions:
        merged.append(item)
        if not placed and isinstance(item, dict) and item.get("id") == "ad_form":
            merged.append(question)
            placed = True
    if not placed:
        merged.append(question)
    return merged


def cap_questions(questions, limit=4):
    """Keep the fixed questions. Extra model questions leave from the end."""
    fixed_count = sum(isinstance(item, dict) and item.get("id") in _FIXED for item in questions)
    room = max(0, limit - min(fixed_count, limit))
    kept_rest = 0
    result = []
    seen = set()
    for item in questions:
        fixed = isinstance(item, dict) and item.get("id") in _FIXED
        if fixed:
            if item.get("id") in seen:
                continue
            seen.add(item.get("id"))
            result.append(item)
        elif kept_rest < room:
            result.append(item)
            kept_rest += 1
    return result[:limit]


def attach_highlight_question(result, project, messages=(), gates=()):
    """Put the fixed highlight on a clarification that does not already have one."""
    if not isinstance(result, dict) or result.get("kind") != "clarify":
        return result
    if not should_ask_highlight(project, messages, gates):
        return result
    questions = result.get("questions")
    if not isinstance(questions, list):
        return result
    corpus = merchant_corpus(project, messages)
    kept = [item for item in questions if not displaced_highlight(item)]
    result["questions"] = cap_questions(_place_highlight(kept, highlight_question(corpus)))
    return result
