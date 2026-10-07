"""Explicit retries after a video run failed.

A 401 or 402 is a definite unpaid refusal. The merchant can fix the account and
submit the same confirmed plan and pictures again. An unknown acceptance is
never included: that one stays blocked so it cannot be charged twice.
"""
from __future__ import annotations

import re

ACCOUNT_RETRY_CODES = ("provider_auth", "provider_balance")

_INTENT = re.compile(
    r"^(?:请|帮我|麻烦)?"
    r"(?:继续生成|重新生成|再试一次)"
    r"(?:一下|视频)?"
    r"(?:吧)?"
    r"[。！!？?\s…]*$"
)

QUEUED_REPLY = "已重新排队。已确认的方案和图片都保留，会继续这次制作。"
RESUME_REPLY = "已重新排队。会继续查询已经受理的片段，失败的片段不会再次提交。"
SEGMENTS_REPLY = "已重新排队，只重试失败的片段。其余图片和成功片段继续用。"

_REFUSALS = {
    "unknown": "这次提交的受理结果还不清楚，不能再交一次，以免重复扣费。没有新的任务排队。",
    "voice": "台词比这一段更长，再交一次也过不了。请先缩短台词。没有新的任务排队。",
    "rejected": "这次请求被平台拒绝了，同一条不能直接重发。方案和图片还在。没有新的任务排队。",
    "image": "图片没有生成成功，再交一次也不会重画。请修改方案后再试。没有新的任务排队。",
    "paid_failure": "有的片段已经失败，不能整单重交。请使用重试失败片段。没有新的任务排队。",
    "other": "这一步不能直接重做。方案和图片还在。没有新的任务排队。",
}


def is_generate_retry_intent(text: str) -> bool:
    return bool(_INTENT.match(str(text or "").strip()))


def refusal_reply(reason: str) -> str:
    return _REFUSALS.get(reason or "", _REFUSALS["other"])


def no_queue(text: str) -> str:
    """A caught retry error must not sound like work was accepted."""
    raw = (text or "").strip()
    if "仍在进行" in raw:
        return "已经有任务在进行，这次没有新的任务排队。"
    if "无需恢复" in raw:
        return "这次制作已经在排队，没有再交一次。"
    if "没有新的任务排队" in raw:
        return raw
    if raw and not raw.endswith("。"):
        raw += "。"
    if not raw:
        raw = _REFUSALS["other"]
        return raw
    return raw + "没有新的任务排队。"


def cleared_progress(progress: dict | None) -> dict:
    kept = dict(progress or {})
    for key in ("error_code", "retryable", "retry_action", "failed_segment_ids"):
        kept.pop(key, None)
    return kept


def unpaid_account_step(step: dict, job_code: str | None) -> bool:
    if step.get("task_id"):
        return False
    if step.get("state") not in ("failed", "rejected"):
        return False
    result = step.get("result") if isinstance(step.get("result"), dict) else {}
    code = result.get("error_code")
    if code in ACCOUNT_RETRY_CODES:
        return True
    return not code and job_code in ACCOUNT_RETRY_CODES


def account_step_blocker(steps, job_code: str | None) -> str | None:
    """None when every non-downloaded step can stay or be dropped without a second charge."""
    for step in steps:
        state = step.get("state")
        if state == "downloaded" or (state == "submitted" and step.get("task_id")):
            continue
        if state in ("failed", "rejected") and unpaid_account_step(step, job_code):
            continue
        if state in ("failed", "rejected"):
            return "paid_failure"
        return "unknown"
    return None


def classify_generate_retry(job: dict, steps) -> tuple[str, str | None]:
    """What a short retry sentence may do. Refuse rather than describe work that will not start."""
    code = (job.get("progress") or {}).get("error_code")
    progress = job.get("progress") or {}
    if job.get("status") == "submitted_unknown":
        return "refuse", "unknown"
    if code == "voice_too_long":
        return "refuse", "voice"
    if code in ACCOUNT_RETRY_CODES:
        return "account", None
    if code == "provider_request_rejected":
        return "refuse", "rejected"
    if code == "image_generation_failed":
        return "refuse", "image"
    failed = pending = uncertain = False
    for step in steps:
        state = step.get("state")
        if state in ("submitting", "submitted_unknown"):
            uncertain = True
        elif state in ("failed", "rejected"):
            failed = True
        elif state == "submitted" and step.get("task_id"):
            pending = True
        elif state != "downloaded":
            uncertain = True
    if uncertain:
        return "refuse", "unknown"
    if progress.get("retry_action") == "retry_failed_segments" and progress.get("failed_segment_ids"):
        return "segments", None
    if failed and pending:
        return "resume", None
    if failed:
        return "refuse", "paid_failure"
    if progress.get("retryable") is False:
        return "refuse", "other"
    return "plain", None
