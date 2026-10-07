"""Ask who the ad is for, before another scene question takes the slot.

The question is fixed, like the selling point and the destination. It appears
when the brief never names an audience or a use situation, and the merchant has
not already answered or skipped it. A free answer is kept as the brief's
audience so later planning uses the merchant's words.
"""
from __future__ import annotations

import re

from .highlight import merchant_corpus, merchant_delegated


_STATED = re.compile(
    r"给谁看|拍给谁|观众是|受众是|受众为|目标受众|人群是|人群为|目标人群|适合谁|"
    r"送给|自用|自己用|自己吃|宝妈|年轻人|上班族|"
    r"生日聚会|下午茶|送礼|礼物|使用场景|使用情境|什么时候用"
)
_DUPLICATE = re.compile(r"给谁看|拍给谁|人群|受众|使用情境|使用场景|什么情境|适合谁")
_SKIPPED = "暂不确定，请使用谨慎表达"
_FOOD = re.compile(r"蛋糕|面包|甜点|甜品|饼干|巧克力|月饼|奶茶|咖啡|食品|零食|水果|糕点|冰淇淋|冰激凌")
_APPLIANCE = re.compile(r"吹风机|电器|耳机|手机|风扇|净化|扫地|洗衣|空调|家电|风筒|剃须刀|牙刷")
_BEAUTY = re.compile(r"口红|面霜|护肤|化妆|香水|面膜|精华")
_FASHION = re.compile(r"衣|裙|鞋|包|饰品|帽子")
# Older cards used whatever id the model invented. Those answers still count.
AUDIENCE_IDS = frozenset({
    "audience", "audience_scene", "audience_use", "cake_audience_scenario",
    "use_scene", "occasion", "target_scene",
})


def audience_examples(text):
    """Category-shaped examples. The merchant still answers in their own words."""
    blob = str(text or "")
    if _FOOD.search(blob):
        return "例如自己吃、送给别人、生日聚会、下午茶"
    if _APPLIANCE.search(blob):
        return "例如自己用、送给家人、上班出门前"
    if _BEAUTY.search(blob):
        return "例如自己用、送给朋友、出门前"
    if _FASHION.search(blob):
        return "例如自己穿、送给别人、日常出门"
    return "例如自己用、送给别人、具体场合"


def audience_question(text=""):
    return {
        "id": "audience",
        "prompt": f"这条广告主要拍给谁看、在什么情境下使用？{audience_examples(text)}。",
        "options": [],
        "allow_custom": True,
        "allow_skip": True,
    }


def stated_audience(text):
    """True when the merchant already named who the ad is for or the use situation."""
    return bool(_STATED.search(str(text or "")))


def displaced_audience(item):
    """A second who/where question gives up the slot to the fixed one."""
    if not isinstance(item, dict) or item.get("id") == "audience":
        return False
    if item.get("id") in AUDIENCE_IDS:
        return True
    return bool(_DUPLICATE.search(str(item.get("prompt") or "")))


def _answered_gates(gates):
    for gate in gates or ():
        if not isinstance(gate, dict) or gate.get("status") != "answered":
            continue
        if gate.get("kind") not in (None, "clarification", "clarify"):
            continue
        yield gate


def audience_closed(gates):
    """An answered or skipped audience question is not asked again."""
    for gate in _answered_gates(gates):
        for answer in gate.get("answers") or []:
            if isinstance(answer, dict) and answer.get("question_id") in AUDIENCE_IDS:
                return True
    return False


def known_audience_text(gates):
    """The latest free answer or chosen label. A skip is not a fact."""
    found = None
    for gate in _answered_gates(gates):
        questions = {item.get("id"): item for item in gate.get("questions") or [] if isinstance(item, dict)}
        for answer in gate.get("answers") or []:
            if not isinstance(answer, dict) or answer.get("question_id") not in AUDIENCE_IDS:
                continue
            if answer.get("skipped"):
                continue
            question = questions.get(answer.get("question_id")) or {}
            options = {item.get("id"): item.get("label") for item in question.get("options") or [] if isinstance(item, dict)}
            chosen = options.get(answer.get("option_id")) or answer.get("text")
            chosen = str(chosen or "").strip()
            if chosen and chosen != _SKIPPED:
                found = chosen[:500]
    return found


def should_ask_audience(project, messages=(), gates=(), *, planning_requested=False):
    if not isinstance(project, dict):
        return False
    if audience_closed(gates):
        return False
    if merchant_delegated(project, messages, planning_requested=planning_requested):
        return False
    if stated_audience(merchant_corpus(project, messages)):
        return False
    return True


def apply_known_audience(brief, gates, evidence):
    """Copy a real audience answer into the brief the later stages read."""
    if not isinstance(brief, dict):
        return brief
    text = known_audience_text(gates)
    if not text:
        return brief
    if text not in str(brief.get("audience") or ""):
        brief["audience"] = text
    if not str(brief.get("scenario") or "").strip():
        brief["scenario"] = text
    intent = brief.get("creative_intent")
    if isinstance(intent, dict) and not str(intent.get("use_context") or "").strip():
        intent["use_context"] = text
    facts = brief.get("facts")
    if not isinstance(facts, list) or len(facts) >= 12:
        return brief
    if any(isinstance(fact, dict) and text in str(fact.get("text") or "") for fact in facts):
        return brief
    if any(isinstance(fact, dict) and fact.get("id") == "known_audience" for fact in facts):
        return brief
    source_id = None
    if isinstance(evidence, dict):
        for key, value in evidence.items():
            if key == "brief" or not isinstance(value, str) or text not in value:
                continue
            source_id = key
            break
        if source_id is None and isinstance(evidence.get("brief"), str) and text in evidence["brief"]:
            source_id = "brief"
    if source_id:
        facts.append({
            "id": "known_audience",
            "text": text,
            "source_kind": "merchant",
            "source_id": source_id,
        })
    return brief
