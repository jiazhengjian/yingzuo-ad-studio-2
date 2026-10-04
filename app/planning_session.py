"""Private, durable text-stage journal. Never retries image/video submissions.

Completed calls replay in order only with identical inputs. Attempts are saved
before sending, so restarting a worker cannot reset request or retry budgets.
A saved entry whose route, model, or skill signature no longer matches is
dropped, together with every later entry, and that step is computed again.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import logging

from app.planner_runtime import safe_planner_metrics


performance_logger = logging.getLogger("uvicorn.error.planner")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class PlanningSessionError(RuntimeError):
    safe_to_display = True

    def __init__(self, message, *, error_code="planning_request_failed",
                 retryable=True, retry_action="retry_planning"):
        super().__init__(message)
        self.error_code, self.retryable, self.retry_action = error_code, retryable, retry_action


class PlanningSession:
    def __init__(self, checkpoint, save, scope, *, max_requests=12, retry_delay=0.75, on_diverge=None):
        self.save, self.cursor = save, 0
        self.max_requests, self.retry_delay = max_requests, retry_delay
        self.on_diverge = on_diverge
        self.state = deepcopy(checkpoint) if checkpoint else {
            "version": 1, "scope": fingerprint(scope), "entries": [],
            "request_count": 0, "transient_retries": 0, "empty_retries": 0,
            "manual_retries": 0}
        if self.state.get("version") != 1 or self.state.get("scope") != fingerprint(scope):
            raise PlanningSessionError("本次策划的输入或运行规则已变化，请重新发送修改需求；已有成果仍保留。",
                error_code="planning_invalid_response", retryable=False, retry_action="edit_plan")

    def persist(self):
        if self.save:
            self.save(deepcopy(self.state))

    def stop(self, entry, message, *, error_code="planning_request_failed",
             retryable=True, retry_action="retry_planning"):
        entry.update(state="terminal", failure={"message": message, "error_code": error_code,
            "retryable": retryable, "retry_action": retry_action})
        self.persist()
        raise PlanningSessionError(message, error_code=error_code,
            retryable=retryable, retry_action=retry_action)

    async def call(self, stage, signature, perform, *, on_retry=None, on_start=None):
        index = self.cursor
        self.cursor += 1
        digest = fingerprint(signature)
        entries = self.state["entries"]
        if index < len(entries):
            entry = entries[index]
            if entry["stage"] != stage or entry["input_hash"] != digest:
                # Config, model, route, or skill text changed. Keep earlier
                # matches and compute this step again inside the same job.
                dropped = len(entries) - index
                del entries[index:]
                self.persist()
                performance_logger.info("Planner request metrics: %s", json.dumps(safe_planner_metrics(
                    stage, 0, recovery="checkpoint_recompute", dropped_entries=dropped)))
                if self.on_diverge:
                    self.on_diverge(stage, dropped)
                entry = {"stage": stage, "input_hash": digest, "state": "pending", "attempts": 0}
                entries.append(entry)
            elif entry["state"] == "ready":
                return deepcopy(entry["result"])
        else:
            entry = {"stage": stage, "input_hash": digest, "state": "pending", "attempts": 0}
            entries.append(entry)
        while True:
            if entry["state"] == "terminal":
                failure = entry["failure"]
                raise PlanningSessionError(failure["message"], **{k: failure[k]
                    for k in ("error_code", "retryable", "retry_action")})
            # An interrupted in-flight text completion may be requested again,
            # but it consumes the same durable retry allowance as a timeout.
            if entry["state"] in ("running", "retryable"):
                kind = entry.get("retry_kind", "transient")
                counter = "empty_retries" if kind == "empty" else "transient_retries"
                limit = (1 if kind == "empty" else 2) + 2 * self.state["manual_retries"]
                if self.state[counter] >= limit:
                    self.stop(entry, "策划服务暂时未能完成当前步骤；需求已保留。")
                self.state[counter] += 1
                # Clear this marker before waiting: a shutdown while waiting
                # must not charge another retry without another request.
                entry["state"] = "pending"
                self.persist()
                if on_retry:
                    on_retry(kind)
                await asyncio.sleep(min(self.retry_delay * self.state[counter], 3))
            if self.state["request_count"] >= self.max_requests + 2 * self.state["manual_retries"]:
                self.stop(entry, "本轮策划已达到处理次数上限，进度与已有成果已保留。",
                    error_code="planning_request_limit", retryable=False, retry_action="continue_conversation")
            if on_start:
                on_start(entry["attempts"] == 0)
            self.state["request_count"] += 1
            entry.update(state="running", attempts=entry["attempts"] + 1)
            entry.pop("retry_kind", None)
            self.persist()
            try:
                result = await perform()
            except Exception as exc:
                kind = getattr(exc, "planning_retry_kind", None)
                if kind in ("empty", "transient"):
                    entry.update(state="retryable", retry_kind=kind)
                    self.persist()
                    continue
                # Cache only safe, owned failure metadata, never raw HTTP errors.
                entry.update(state="terminal", failure={
                    "message": str(exc) if getattr(exc, "safe_to_display", False) else "本次策划未完成，已有成果仍保留。",
                    "error_code": getattr(exc, "error_code", "planning_invalid_response"),
                    "retryable": bool(getattr(exc, "retryable", False)),
                    "retry_action": getattr(exc, "retry_action", "edit_plan")})
                self.persist()
                raise
            entry.update(state="ready", result=deepcopy(result))
            entry.pop("failure", None)
            self.persist()
            return result


def allow_manual_planning_retry(checkpoint):
    """Explicit retry extends only the unfinished request, retaining all results."""
    result = deepcopy(checkpoint)
    if not result or result.get("version") != 1:
        return result
    entries = result["entries"]
    if not entries or entries[-1].get("state") != "terminal":
        raise ValueError("当前策划没有可重新尝试的请求")
    entry = entries[-1]
    if entry.get("failure", {}).get("retry_action") != "retry_planning":
        raise ValueError("此问题需要调整需求，不能重复提交同一个请求")
    result["manual_retries"] += 1
    entry.update(state="pending")
    entry.pop("failure", None)
    entry.pop("retry_kind", None)
    return result


def reopen_rejected_stage(checkpoint):
    """Drop a stored stage that a later check rejected, then run that step again.

    The session marks a completed call ready before direction review and the
    other guards. A failed job can therefore ask for another attempt while the
    last entry is still ready. Replaying that body fails the same check.
    """
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != 1:
        return None
    result = deepcopy(checkpoint)
    entries = result.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    if entries[-1].get("state") not in ("ready", "terminal"):
        return None
    entries.pop()
    try:
        retries = int(result.get("manual_retries") or 0)
    except (TypeError, ValueError):
        retries = 0
    result["manual_retries"] = retries + 1
    return result
