"""Sentences a merchant can see on a failure card.

Three outcomes:

1. The job can correct validation, normalization, fingerprints, speech rate,
   a dropped model reply, or structure by itself. That path does not produce
   a card. If it still cannot continue, the card uses the system sentence.
2. Only the merchant can act (image, upload, balance, quota, content policy,
   wording). One plain sentence names the cause, the action, and that existing
   work stays.
3. Anything else is a recorded system problem. Technical text stays in the
   execution log.
"""
from __future__ import annotations

import re

SYSTEM_CARD = "这是系统问题，已记录；你的方案和图片都还在。"
BALANCE_CARD = "账户余额或额度不足，已有方案和图片仍保留。请补充额度后再制作。"
AUTH_CARD = "生成账户的连接需要检查，已有方案和图片仍保留。"

_TECHNICAL = re.compile(
    r"pydantic|validation|Value error|CreativeOption|请重新策划|请先重新策划|"
    r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b",
    re.IGNORECASE,
)

# Class 2 sentences. The button label is the action named in the sentence.
_MERCHANT_RULES = (
    (re.compile(r"请先上传至少一张真实商品图|尚未收到真实商品图|重新上传商品图"),
     "还没有可用的商品图，已有方案仍保留。请重新上传商品图。"),
    (re.compile(r"余额|额度|配额"), BALANCE_CARD),
    (re.compile(r"内容政策|内容审核|平台要求|违规"),
     "内容未通过平台要求，已有方案仍保留。请修改方案后再制作。"),
    (re.compile(r"配音.*(?:长于|超过)|文案.*(?:过长|太长)|请先精简文案|请缩短台词"),
     "台词比这段视频更长，已有图片仍保留。请缩短台词后再制作。"),
    (re.compile(r"所选时长不一致|请重新选择时长"),
     "方案与所选时长不一致，已有内容仍保留。请重新选择时长后再制作。"),
    (re.compile(r"请先配置|API Key"),
     "策划服务的连接或账户需要检查，已有内容已保存。"),
)


def failure_class(text: str, error_code: str | None = None) -> int:
    """1 system-recoverable, 2 merchant action, 3 recorded system problem."""
    code = error_code or ""
    if code in {"voice_too_long", "reference_image_unreadable", "image_generation_failed",
                "planning_configuration_failed"}:
        return 2
    if code in {"planning_invalid_response", "planning_response_interrupted", "planning_request_failed",
                "planning_review_incomplete"}:
        return 1
    raw = text or ""
    if any(pattern.search(raw) for pattern, _sentence in _MERCHANT_RULES):
        return 2
    if code in {"planning_content_failed", "planning_request_limit"}:
        return 2
    if _TECHNICAL.search(raw) or not raw.strip():
        return 3
    return 2


def present_text(text: str, error_code: str | None = None) -> str:
    """Card sentence. Plain Chinese the merchant can act on is kept."""
    del error_code
    raw = text or ""
    for pattern, sentence in _MERCHANT_RULES:
        if pattern.search(raw):
            return sentence
    if _TECHNICAL.search(raw) or not raw.strip():
        return SYSTEM_CARD
    return raw


def present_exception(exc) -> str:
    from pydantic import ValidationError

    from . import media

    if isinstance(exc, ValidationError):
        return SYSTEM_CARD
    if isinstance(exc, (ValueError, media.MediaError)) or getattr(exc, "safe_to_display", False):
        return present_text(str(exc), getattr(exc, "error_code", None))
    return SYSTEM_CARD
