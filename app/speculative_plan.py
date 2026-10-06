"""Precompute the recommended direction's text plan while the user is choosing.

This calls the same text planner as a normal continuation and never submits
image or video work. The result is a stage cache. Choosing that direction
replays matching text stages; choosing another direction abandons the cache.
"""
from __future__ import annotations

import asyncio
import logging

from app.planner_transport import planner_api_key
from app.strategy import DirectionCandidates


logger = logging.getLogger(__name__)
_TASKS = {}


def direction_selection_text(label):
    """The same user line select_direction stores before the continuation."""
    return "选择创意方向：" + label


def cancel_speculation(project_id):
    task = _TASKS.pop(project_id, None)
    if task is not None and not task.done():
        task.cancel()


def _remember(project_id, task):
    current = _TASKS.get(project_id)
    if current is not None and current is not task and not current.done():
        current.cancel()
    _TASKS[project_id] = task

    def finished(done):
        if _TASKS.get(project_id) is done:
            _TASKS.pop(project_id, None)
        try:
            done.result()
        except (asyncio.CancelledError, Exception):
            return
    task.add_done_callback(finished)


def merge_speculative_plan(store, job_id, updates):
    """Update only the speculative record. An abandoned record is left unchanged."""
    return store.merge_speculative_plan(job_id, updates)


async def _run(store, gateway, settings, job_id):
    job = store.get_job(job_id)
    payload = job.get("payload") or {}
    frozen = payload.get("frozen") or {}
    candidates = DirectionCandidates.model_validate(payload.get("direction_candidates") or {})
    direction_id = candidates.recommended_direction_id
    if not direction_id or payload.get("speculative_plan", {}).get("abandoned") is True:
        return
    selected = next(direction for direction in candidates.directions if direction.id == direction_id)
    strategy = candidates.select(direction_id)
    messages = list(frozen.get("messages") or [])
    messages.append({"role": "user", "content": direction_selection_text(selected.label)})
    existing = ((payload.get("speculative_plan") or {}).get("entries") or {})
    if not isinstance(existing, dict):
        existing = {}

    def abandon():
        current = store.get_job(job_id).get("payload") or {}
        spec = current.get("speculative_plan") or {}
        return spec.get("abandoned") is True or spec.get("direction_id") not in (None, direction_id)

    def sink(digest, stage, result):
        if abandon():
            return
        existing[digest] = {"stage": stage, "result": result}
        merge_speculative_plan(store, job_id, {
            "direction_id": direction_id, "status": "running", "entries": existing})

    merge_speculative_plan(store, job_id, {
        "direction_id": direction_id, "status": "running", "entries": existing})
    try:
        if abandon():
            return
        # on_checkpoint forces the same source-byte fingerprint as a real continuation.
        await gateway.decide(frozen["project"], frozen["assets"], messages, frozen["gates"],
            frozen["project"].get("plan"), selected_strategy=strategy.model_dump(),
            stage_result_cache=existing, stage_cache_sink=sink, abandon_check=abandon,
            on_checkpoint=lambda _checkpoint: None, job_id=job_id)
    except asyncio.CancelledError:
        # Selection, supersede, or shutdown cancelled this task. Do not write a
        # status here: a newer run or an abandoned record owns the payload.
        raise
    except Exception as exc:
        if abandon():
            return
        logger.warning("Speculative text plan stopped: %s", type(exc).__name__)
        merge_speculative_plan(store, job_id, {"status": "failed", "direction_id": direction_id})
        return
    if not abandon():
        merge_speculative_plan(store, job_id, {"status": "ready", "direction_id": direction_id, "entries": existing})


def schedule_speculation(store, gateway, settings, job_id):
    if not getattr(settings, "planner_speculate_recommended", False):
        return None
    if not planner_api_key(settings):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(_run(store, gateway, settings, job_id))
    _remember(store.get_job(job_id)["project_id"], task)
    return task


def kick_speculation(store, gateway, settings):
    """Resume an unfinished recommended-direction text plan after a restart."""
    if not getattr(settings, "planner_speculate_recommended", False) or not planner_api_key(settings):
        return
    for job in store.jobs_awaiting_direction():
        spec = (job.get("payload") or {}).get("speculative_plan") or {}
        if spec.get("abandoned") is True or spec.get("status") in ("ready", "failed", "canceled"):
            continue
        project_id = job["project_id"]
        current = _TASKS.get(project_id)
        if current is not None and not current.done():
            continue
        schedule_speculation(store, gateway, settings, job["id"])
