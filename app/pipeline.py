"""A bounded real-provider workflow. Tests inject gateways; production has no fake fallback."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import math
import time
from pathlib import Path

from pydantic import ValidationError

from .db import StaleRevision, encoded, identifier
from . import media
from .schemas import AgentDecision, Plan, Segment, segment_durations
from .video_prompt import expected_grouped_speech
from .generation_policy import validate_generation_durations
from .directing import direction_context
from .production import required_designs, saved_character_sheet_is_stale, validate_design_assets


def _segment_closes_film(plan, segment) -> bool:
    """True when this request is the film's last segment, or the plan has no segment list."""
    segments = plan.get("segments") if isinstance(plan, dict) else None
    if not isinstance(segments, list) or not segments or not isinstance(segment, dict):
        return True
    last = segments[-1]
    if not isinstance(last, dict):
        return True
    segment_id = segment.get("id")
    last_id = last.get("id")
    if isinstance(segment_id, str) and segment_id and isinstance(last_id, str) and last_id:
        return segment_id == last_id
    return True


def video_submission_spec(plan, segment, assets=None):
    """Freeze the same confirmed visual contract for every provider group."""
    spec = {**segment, "audio_strategy": plan["audio_strategy"],
            "generation_strategy": plan.get("generation_strategy", "legacy_10s"),
            "closes_film": _segment_closes_film(plan, segment)}
    from .ad_form_playbook import LEGACY_TENTH, speech_profile_for_strategy
    strategy = plan.get("strategy") if isinstance(plan, dict) else getattr(plan, "strategy", None)
    tenth = speech_profile_for_strategy(strategy).characters_per_tenth
    if tenth != LEGACY_TENTH:
        spec["speech_characters_per_tenth"] = tenth
    brief = strategy.get("brief") if isinstance(strategy, dict) else getattr(strategy, "brief", None)
    form = brief.get("ad_form") if isinstance(brief, dict) else getattr(brief, "ad_form", None)
    # Story prompts avoid telling the character to look at the camera.
    # Presenter and product films use it to keep the voice track on the right person.
    # A presenter plan with no production must stay unmarked: the compiler
    # refuses a presenter request that never named its speaker.
    if form == "scene_story" or (form in ("presenter_pitch", "brand_tvc") and plan.get("production")):
        spec["ad_form"] = form
    director = direction_context(plan)
    if director is not None:
        spec["director_context"] = director
    if plan.get("production") is not None:
        spec["production"] = copy.deepcopy(plan["production"])
    facts = plan.get("facts") if isinstance(plan.get("facts"), dict) else None
    if isinstance(facts, dict) and isinstance(facts.get("name"), str) and facts["name"].strip():
        spec["facts"] = {"name": facts["name"].strip()}
    if assets is not None:
        spec["reference_asset_ids"] = [asset["id"] for asset in assets]
        visible = [asset for asset in assets if asset["kind"] in ("product", "scene", "character") or (
            asset["kind"] == "start_frame" and (asset.get("metadata") or {}).get("state_reference"))]
        spec["reference_roles"] = []
        for index, asset in enumerate(visible, 1):
            meta = asset.get("metadata") or {}
            if asset["kind"] == "start_frame":
                role = {"index": index, "role": "state", "phase": meta.get("phase"),
                        "shot_id": meta.get("shot_id"), "label": meta.get("state_label")}
                if isinstance(meta.get("character_id"), str) and meta.get("character_id"):
                    role["character_id"] = meta["character_id"]
                state_layout = meta.get("sheet_layout")
                if state_layout in ("horizontal_16x9", "grid_2x2"):
                    role["sheet_layout"] = state_layout
            else:
                role = {"index": index, "role": asset["kind"]}
            if asset["kind"] == "character":
                role["character_id"] = asset["metadata"]["character_id"]
                role["name"] = asset["metadata"].get("character_name", asset["name"])
                layout = asset["metadata"].get("sheet_layout")
                if layout in ("horizontal_16x9", "grid_2x2"):
                    role["sheet_layout"] = layout
            spec["reference_roles"].append(role)
    return spec


def video_input_label(asset):
    """Short name of one reference image that was sent with a video. No URLs."""
    if not isinstance(asset, dict):
        return "reference", "参考图"
    kind = asset.get("kind")
    meta = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
    if kind == "product":
        return "product", "商品原图"
    if kind == "scene":
        return "scene", "场景参考"
    if kind == "character":
        name = meta.get("character_name") or asset.get("name")
        if isinstance(name, str) and name.strip():
            return "character", name.strip() + "·角色参考"
        return "character", "角色参考"
    if kind == "start_frame" or meta.get("state_reference"):
        label = meta.get("state_label")
        if isinstance(label, str) and label.strip():
            return "state", label.strip()
        return "state", "状态参考"
    return "reference", "参考图"


def submitted_video_images(segment, assets):
    """Images attached to the provider request, in that order. None when the set cannot be reconstructed."""
    if not isinstance(segment, dict) or not isinstance(assets, list):
        return None
    ids = segment.get("reference_asset_ids")
    if not isinstance(ids, list):
        return None
    by_id = {asset["id"]: asset for asset in assets if isinstance(asset, dict) and isinstance(asset.get("id"), str)}
    ordered = [by_id[aid] for aid in ids if isinstance(aid, str) and aid in by_id]
    products = [asset for asset in ordered if asset.get("kind") == "product" and not (asset.get("metadata") or {}).get("generated")]
    scene = next((asset for asset in ordered if asset.get("kind") == "scene" and (asset.get("metadata") or {}).get("provider_url")), None)
    if not products or scene is None:
        return None
    if segment.get("reference_roles") is None:
        products = products[:1]
    design = [scene]
    if segment.get("production") is not None:
        try:
            design = validate_design_assets(segment, ordered)
        except ValueError:
            return None
    return products + design


def video_input_records(segment, assets):
    images = submitted_video_images(segment, assets)
    if images is None:
        return None
    records = []
    for asset in images:
        role, label = video_input_label(asset)
        record = {"role": role, "label": label}
        asset_id = asset.get("id") if isinstance(asset, dict) else None
        if isinstance(asset_id, str) and asset_id.strip():
            record["asset_id"] = asset_id.strip()
        records.append(record)
    return records


def video_generation_metadata(segment, assets, request):
    """The prompt actually sent, saved on the clip. Absent when that request was not stored."""
    if not isinstance(request, dict):
        return {}
    prompt = request.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}
    meta = {"video_prompt": prompt}
    model = request.get("model")
    if isinstance(model, str) and model.strip():
        meta["model"] = model.strip()
    records = video_input_records(segment, assets)
    if records is not None:
        meta["video_inputs"] = records
    return meta


def render_prompt_metadata(assets):
    """Copy the clip prompt onto a composed file only when that file is one model clip."""
    clips = [asset for asset in assets if isinstance(asset, dict) and asset.get("kind") == "segment"]
    if len(clips) != 1:
        return {}
    meta = clips[0].get("metadata") if isinstance(clips[0].get("metadata"), dict) else {}
    prompt = meta.get("video_prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {}
    record = {"video_prompt": prompt}
    model = meta.get("model")
    if isinstance(model, str) and model.strip():
        record["model"] = model.strip()
    inputs = meta.get("video_inputs")
    if isinstance(inputs, list):
        record["video_inputs"] = copy.deepcopy(inputs)
    return record


_PLANNER_STREAM_LABELS = {
    "strategy": "正在接收需求判断",
    "direction_review": "正在接收方向检查",
    "ad_script": "正在接收广告内容",
    "script_review": "正在接收内容检查",
    "storyboard": "正在接收画面安排",
    "storyboard_repair": "正在接收画面校正",
    "alignment_review": "正在接收声画检查",
    "plan_review": "正在接收方案检查",
    "plan_refinement": "正在接收方案调整",
}


class SubmissionUnknown(RuntimeError):
    pass


class VoiceTooLong(ValueError):
    error_code = "voice_too_long"
    retryable = False


STRUCTURE_EXHAUSTED_CARD = "这次没生成出来"


def _auto_retry_count(payload, key):
    try:
        count = int((payload or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return count if count > 0 else 0


def _structure_auto_failure(exc) -> bool:
    """The plan-structure check. Strategy and transport failures stay on their own caps."""
    if isinstance(exc, PlanStructureError):
        return True
    return "规划结果未通过结构校验" in str(exc)


def _content_auto_failure(exc) -> bool:
    code = getattr(exc, "error_code", None)
    message = str(exc)
    return code in ("planning_content_failed", "planning_review_incomplete") or any(
        token in message for token in ("结构不完整", "未能满足已确认要求", "容量仍未改善"))


class NeedsReconfirm(Exception):
    """The saved board was replaced. Image and video spend wait for another 确认."""


class PlanStructureError(ValueError):
    """A segment/plan shape the merchant should not have to edit by hand."""

    error_code = "planning_invalid_response"
    retryable = True
    retry_action = "retry_planning"

    def __init__(self, message, details):
        super().__init__(message)
        self.validation_errors = details


# Segment validators the server can correct without another model call.
_SEGMENT_FIXABLE = {
    "镜头时段必须从0连续排列，且不超过片段时长",
    "镜头计划须覆盖完整片段",
    "镜头内容节拍须非空且不重复，beat_id须保留为beat_ids首项",
    "旁白时段须在片段内按顺序排列且不重叠，完整句不能跨生成段",
    "旁白及锚点时段须由对应镜头覆盖",
    "旁白及锚点时段须与每个引用镜头的时段重叠",
    "旁白及锚点只能引用本段存在且不重复的镜头ID",
    "旁白及锚点引用的镜头须按顺序连续排列",
    "旁白及锚点只能跨同一内容节拍的镜头",
    "台词锚点只能引用其旁白时段对应的镜头",
}


def _validation_details(error, prefix=()):
    details = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        details.append({"loc": tuple(prefix) + tuple(item.get("loc") or ()),
                        "type": item.get("type", "validation_error"), "msg": item.get("msg", "")})
    return details


def _detail_messages(details):
    return [str(item.get("msg", "")).removeprefix("Value error, ") for item in details]


def _record_plan_validation(settings, details):
    if settings is None:
        return
    from .providers.gateway import save_failed_planner_output
    save_failed_planner_output(settings, {"stage": "plan_validation",
        "raw_output": {"check": "segment"}, "validation_errors": details})


def _fail_plan_structure(settings, message, details):
    _record_plan_validation(settings, details)
    raise PlanStructureError(message, details) from None


def _segment_validation_errors(plan):
    """Validate each segment at the plan's own speech rate, not the historical 2.9 cap."""
    from .ad_form_playbook import bind_plan_speech_tenth, reset_plan_speech_tenth
    token = bind_plan_speech_tenth(plan.get("strategy"))
    try:
        for index, segment in enumerate(plan["segments"]):
            try:
                Segment.model_validate(segment)
            except ValidationError as error:
                return _validation_details(error, ("segments", index))
    finally:
        reset_plan_speech_tenth(token)
    return []


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _covers(shots, duration):
    previous = 0.0
    for shot in shots:
        start, end = shot.get("start"), shot.get("end")
        if not _finite(start) or not _finite(end):
            return False
        if abs(start - previous) > 0.05 or end <= start or end > duration + 1e-6:
            return False
        previous = end
    return abs(previous - duration) <= 0.05


def _shift_window(item, origin):
    for key in ("start", "end"):
        value = item.get(key)
        if _finite(value):
            item[key] = round(value - origin, 6)


def _rebase_segment_clock(segment):
    """Turn a whole-film clock into this segment's 0..duration clock."""
    shots = segment.get("shots")
    if not isinstance(shots, list) or not shots or not isinstance(shots[0], dict):
        return
    origin = shots[0].get("start")
    if not _finite(origin) or origin <= 0.05:
        return
    for shot in shots:
        if isinstance(shot, dict):
            _shift_window(shot, origin)
    for span in segment.get("speech_spans") or []:
        if not isinstance(span, dict) or not _finite(span.get("start")) or span["start"] + 1e-6 < origin:
            continue
        _shift_window(span, origin)
        for anchor in span.get("anchors") or []:
            if isinstance(anchor, dict):
                _shift_window(anchor, origin)


def _snap_shot_coverage(shots, duration):
    """Pin the first frame to 0, close gaps, and make the last frame end at duration."""
    if not shots:
        return False
    if not _covers(shots, duration):
        previous = 0.0
        for shot in shots:
            start, end = shot.get("start"), shot.get("end")
            if not _finite(start) or not _finite(end):
                return False
            if abs(start - previous) > 0.05:
                start = previous
            end = min(float(duration), max(float(end), start))
            if end <= start:
                end = min(float(duration), start + 0.1)
            shot["start"], shot["end"] = start, end
            previous = end
        shots[0]["start"] = 0.0
        if shots[0]["end"] <= shots[0]["start"]:
            shots[0]["end"] = min(float(duration), 0.1)
        if abs(shots[-1]["end"] - duration) > 0.05:
            shots[-1]["end"] = float(duration)
        if shots[-1]["end"] <= shots[-1]["start"]:
            shots[-1]["start"] = max(0.0, float(duration) - 0.1)
            shots[-1]["end"] = float(duration)
    if _covers(shots, duration):
        return True
    weights = [max(float(shot["end"]) - float(shot["start"]), 0.1) for shot in shots]
    total = sum(weights)
    cursor = 0.0
    for index, shot in enumerate(shots):
        shot["start"] = cursor
        if index == len(shots) - 1:
            shot["end"] = float(duration)
        else:
            room = float(duration) - 0.05 * (len(shots) - index - 1)
            cursor = min(room, max(cursor + 0.05, round(cursor + duration * weights[index] / total, 4)))
            shot["end"] = cursor
    return _covers(shots, duration)


def _fix_beat_membership(shot):
    ids = shot.get("beat_ids")
    if not isinstance(ids, list):
        return
    cleaned = []
    for beat in ids:
        if not isinstance(beat, str):
            continue
        beat = beat.strip()
        if beat and len(beat) <= 80 and beat not in cleaned:
            cleaned.append(beat)
    if not cleaned:
        return
    shot["beat_ids"] = cleaned
    shot["beat_id"] = cleaned[0]


def _overlapping_shot_ids(shots, start, end, beat_id=None):
    matched = []
    for shot in shots:
        if not isinstance(shot, dict) or not isinstance(shot.get("id"), str) or not shot["id"].strip():
            continue
        if not _finite(shot.get("start")) or not _finite(shot.get("end")):
            continue
        if min(shot["end"], end) - max(shot["start"], start) <= 1e-7:
            continue
        if beat_id and beat_id not in (shot.get("beat_ids") or [shot.get("beat_id", "")]):
            continue
        if shot["id"] not in matched:
            matched.append(shot["id"])
    return matched


def _retarget_speech(segment):
    shots = segment.get("shots")
    spans = segment.get("speech_spans")
    duration = segment.get("duration_seconds")
    if not isinstance(shots, list) or not isinstance(spans, list) or type(duration) is not int:
        return
    for span in spans:
        if not isinstance(span, dict) or not _finite(span.get("start")) or not _finite(span.get("end")):
            continue
        span["start"] = min(max(float(span["start"]), 0.0), duration)
        span["end"] = min(max(float(span["end"]), 0.0), duration)
        if span["end"] <= span["start"]:
            continue
        overlapping = _overlapping_shot_ids(shots, span["start"], span["end"], span.get("beat_id"))
        if overlapping:
            span["shot_ids"] = overlapping
        allowed = set(span.get("shot_ids") or [])
        for anchor in span.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            ids = [shot_id for shot_id in anchor.get("shot_ids") or [] if shot_id in allowed]
            if not ids and span.get("shot_ids"):
                ids = list(span["shot_ids"])
            if ids:
                anchor["shot_ids"] = ids


def normalize_plan_structure(plan, *, retarget_speech=False):
    """Rebase, snap and repair beat membership. Returns a copy, or None if nothing changed."""
    if not isinstance(plan, dict) or not isinstance(plan.get("segments"), list):
        return None
    result = copy.deepcopy(plan)
    for segment in result["segments"]:
        if not isinstance(segment, dict):
            return None
        shots = segment.get("shots")
        duration = segment.get("duration_seconds")
        if not isinstance(shots, list) or not shots or type(duration) is not int:
            return None
        _rebase_segment_clock(segment)
        if not _snap_shot_coverage(shots, duration):
            return None
        for shot in shots:
            if isinstance(shot, dict):
                _fix_beat_membership(shot)
        if retarget_speech:
            _retarget_speech(segment)
    if result == plan:
        return None
    return result


def validate_plan(plan, duration_seconds=None, *, settings=None):
    if not isinstance(plan, dict) or plan.get("language") != "zh-CN" or plan.get("aspect_ratio") != "9:16":
        raise ValueError("P0方案必须使用中文与9:16画幅")
    if not str(plan.get("script", "")).strip() and (plan.get("production") or {}).get("speech_mode") != "none":
        raise ValueError("方案缺少实际台词")
    segments = plan.get("segments")
    strategy = plan.get("generation_strategy", "legacy_10s")
    if strategy not in ("legacy_10s", "continuity_v1"):
        raise ValueError("不支持的视频生成策略")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 6:
        raise ValueError("商品广告需要一至六段真实视频")
    ids = set()
    for segment in segments:
        sid = segment.get("id")
        duration = segment.get("duration_seconds")
        if not isinstance(sid, str) or not sid or sid in ids:
            raise ValueError("生成段ID必须唯一且非空")
        ids.add(sid)
        minimum, maximum = (2, 30) if strategy == "continuity_v1" else (1, 10)
        if type(duration) is not int or not minimum <= duration <= maximum:
            raise ValueError("视频生成时长无效")
        if not str(segment.get("prompt", "")).strip():
            raise ValueError("视频生成段缺少完整提示词")
        if not isinstance(segment.get("reference_asset_ids"), list) or not segment["reference_asset_ids"]:
            raise ValueError("每段视频都必须绑定真实商品参考")
    details = _segment_validation_errors(plan)
    if details:
        messages = _detail_messages(details)
        if all(message in _SEGMENT_FIXABLE for message in messages):
            retarget = any(token in message for message in messages for token in ("旁白", "锚点", "台词"))
            repaired = normalize_plan_structure(plan, retarget_speech=retarget)
            if repaired is not None:
                repaired_details = _segment_validation_errors(repaired)
                if not repaired_details:
                    plan = repaired
                    details = []
                else:
                    details = repaired_details
        if details:
            _fail_plan_structure(settings, "视频段镜头时轴或素材结构无效", details)
    durations = [s["duration_seconds"] for s in plan["segments"]]
    validate_generation_durations(durations, strategy)
    requested = duration_seconds if duration_seconds is not None else sum(durations)
    segment_durations(requested)  # Validates the product's 10–60 second selection.
    if (strategy == "legacy_10s" and durations != segment_durations(requested)) or sum(durations) != requested:
        raise ValueError("方案与所选时长不一致，已有内容仍保留。请重新选择时长后再制作。")
    if plan.get("audio_strategy") not in ("voiceover", "native"):
        raise ValueError("不支持的音频策略")
    if plan.get("production") is not None or any(segment.get("speech_spans") is not None for segment in plan["segments"]):
        try:
            Plan.model_validate(plan)
        except ValidationError as error:
            _fail_plan_structure(settings, "独立旁白方案的声音策略、内容或跨段对应无效",
                                 _validation_details(error))
    result = copy.deepcopy(plan)
    result.pop("cta", None)
    from .ad_form_playbook import stamp_plan_speech_rate
    stamp_plan_speech_rate(result)
    return result


def merchant_facing_error(exc):
    """Merchants see one plain Chinese sentence. Technical text stays in diagnostics."""
    from .merchant_cards import present_exception
    return present_exception(exc)


def _plan_words(plan):
    """Spoken lines, shot text, and speech mode. Labels and reviews are not content."""
    if not isinstance(plan, dict):
        return None
    script = plan.get("ad_script") if isinstance(plan.get("ad_script"), dict) else {}
    lines = []
    for beat in script.get("beats") or []:
        if isinstance(beat, dict):
            lines.append(beat.get("narration") or "")
    shots = []
    for segment in plan.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for shot in segment.get("shots") or []:
            if isinstance(shot, dict):
                shots.append(shot.get("description") or "")
    production = plan.get("production") if isinstance(plan.get("production"), dict) else {}
    return (tuple(lines), tuple(shots), production.get("speech_mode"), plan.get("script") or "")


def _submission_failure_result(exc, gateway=None, code=None):
    """Status and a redacted excerpt for the step row. Never the merchant card."""
    result = {}
    if isinstance(code, str) and code:
        result["error_code"] = code
    status = getattr(exc, "status_code", None)
    if type(status) is int:
        result["provider_status"] = status
    excerpt = getattr(exc, "excerpt", None)
    if isinstance(excerpt, str) and excerpt:
        result["provider_excerpt"] = excerpt[:180]
    refusals = getattr(gateway, "_submission_refusals", None) if gateway is not None else None
    if isinstance(refusals, list) and refusals:
        result["submission_refusals"] = list(refusals)
    return result or None


def _review_binding_drift(exc: PlanStructureError) -> bool:
    return any("综合审核记录必须对应" in str(item.get("msg", "")) for item in exc.validation_errors or [])


class Pipeline:
    def __init__(self, store, gateway, settings):
        self.store, self.gateway, self.settings = store, gateway, settings

    def _guard(self, job_id, require_current=False):
        job = self.store.get_job(job_id)
        if self.store.is_cancelled(job_id):
            raise media.MediaCanceled("已停止后续制作；已受理任务可能仍计费")
        if require_current and self.store.get_project(job["project_id"])["revision"] != job["revision"]:
            raise StaleRevision("方案已更新，旧任务不再提交新的生成请求")
        if require_current:
            self.store.assert_job_current(job_id)
        return job

    def _phase(self, job_id, phase, **progress):
        job = self.store.get_job(job_id)
        detail = dict(job.get("progress") or {})
        detail.update(progress)
        detail["video_submissions"] = job["submission_count"]
        self.store.update_job(job_id, phase=phase, progress=detail,
                              lease_until=time.time() + self.settings.provider_timeout_seconds + 120)

    def _payload(self, job_id, **updates):
        job = self.store.get_job(job_id)
        payload = dict(job.get("payload") or {})
        payload.update(updates)
        self.store.update_job(job_id, payload=payload)
        return payload

    def _file(self, job, suffix):
        # Both directory components are generated IDs, never user prompt/name/path components.
        folder = self.store.media_root / job["project_id"] / job["id"] / identifier("version")
        folder.mkdir(parents=True, exist_ok=False)
        return folder / f"output{suffix}"

    async def execute(self, job_id):
        while not self.store.claim_job(job_id, self.settings.max_active_runs,
                                       self.settings.provider_timeout_seconds + 120):
            if self.store.get_job(job_id)["status"] != "queued":
                return self.store.get_job(job_id)
            await asyncio.sleep(0.2)
        job = self.store.get_job(job_id)
        try:
            self._guard(job_id, require_current=True)
            if job["kind"] == "plan":
                await self._plan(job)
            elif job["kind"] == "generate":
                await self._generate(job)
            elif job["kind"] in ("recompose", "cta"):
                # Legacy CTA jobs can be resumed only as removal of the retired layer.
                await self._recompose(job)
        except SubmissionUnknown:
            self.store.update_job(job_id, status="submitted_unknown", phase="needs_reconcile",
                                  error="供应商可能已受理，已暂停；请核对任务回执，不能直接重新提交。", lease_until=0)
            self.store.set_project_status(job["project_id"], job["revision"], "submitted_unknown")
        except media.MediaCanceled as exc:
            self.store.update_job(job_id, status="canceled", error=str(exc), lease_until=0)
            self.store.set_project_status(job["project_id"], job["revision"], "canceled")
        except NeedsReconfirm:
            # The replacement plan is already the current revision. This job
            # stays on the revision the merchant confirmed, so 确认 can start
            # a new one. Nothing was submitted.
            self.store.update_job(job_id, status="canceled",
                                  error="方案已换成能通过检查的一版，请再确认后再制作。", lease_until=0)
            current = self.store.get_project(job["project_id"])
            self.store.set_project_status(job["project_id"], current["revision"], "awaiting_generate")
        except StaleRevision as exc:
            self.store.update_job(job_id, status="needs_review", error=str(exc), lease_until=0)
        except asyncio.CancelledError:
            # A process/task interruption must never turn an uncertain POST into a safe retry.
            unresolved = any(self.store.get_step(job_id, f"video:{s['id']}") and
                             self.store.get_step(job_id, f"video:{s['id']}")["state"] in ("submitting", "submitted_unknown")
                             for s in (job.get("payload", {}).get("plan", {}).get("segments", [])))
            self.store.update_job(job_id, status="submitted_unknown" if unresolved else "queued", lease_until=0)
            raise
        except Exception as exc:
            # Keep public errors actionable but never include a raw provider response or pydantic text.
            if isinstance(exc, ValidationError):
                from .providers.gateway import save_failed_planner_output
                save_failed_planner_output(self.settings, {
                    "stage": "generate_validation" if job["kind"] == "generate" else "validation",
                    "raw_output": {"check": "model"}, "validation_errors": _validation_details(exc)})
            message = merchant_facing_error(exc)
            changes = {"status": "failed", "error": message[:500], "lease_until": 0}
            held_submission = getattr(exc, "error_code", None) in (
                "provider_balance", "provider_auth", "provider_request_rejected")
            from .merchant_cards import NOT_PRODUCED_CARD
            not_produced = (getattr(exc, "error_code", None) == "planning_not_produced"
                            or message == NOT_PRODUCED_CARD)
            if not_produced:
                # Hard gates never cleared. The card can start a new text plan.
                # Image and video submission did not happen.
                progress = dict(self.store.get_job(job_id).get("progress") or {})
                progress.update(error_code="planning_not_produced", retryable=True, retry_action="retry_planning")
                changes["error"] = NOT_PRODUCED_CARD
                changes["progress"] = progress
            elif job["kind"] == "plan":
                from .planner_recovery import AUTO_RETRY_CAP
                progress = dict(self.store.get_job(job_id).get("progress") or {})
                retryable = bool(getattr(exc, "retryable", False))
                retry_action = getattr(exc, "retry_action", "edit_plan")
                payload_now = self.store.get_job(job_id).get("payload") or {}
                # Two automatic structure retries already ran. The merchant can
                # start a new generation. They are not asked to edit the plan.
                if (_structure_auto_failure(exc)
                        and _auto_retry_count(payload_now, "structure_auto_retries") >= AUTO_RETRY_CAP):
                    message = STRUCTURE_EXHAUSTED_CARD
                    changes["error"] = message
                    retryable, retry_action = True, "retry_planning"
                    progress["structure_retries_exhausted"] = True
                elif isinstance(exc, PlanStructureError) and payload_now.get("plan_structure_replayed"):
                    retryable, retry_action = False, None
                progress.update(error_code=getattr(exc, "error_code", "planning_invalid_response"),
                    retryable=retryable, retry_action=retry_action)
                # Only our bounded planning errors provide these owned summaries.
                if getattr(exc, "error_code", None) in ("planning_content_failed", "planning_review_incomplete"):
                    progress.update(issue_summary=getattr(exc, "issue_summary", [])[:8],
                        repair_attempts=getattr(exc, "repair_attempts", 0))
                changes["progress"] = progress
            elif job["kind"] == "generate" and held_submission:
                from .generate_retry import ACCOUNT_RETRY_CODES
                progress = dict(self.store.get_job(job_id).get("progress") or {})
                # 401/402 did not accept a task. The merchant can fix the account
                # and submit this same plan again. A rejected request stays blocked.
                user_fixable = exc.error_code in ACCOUNT_RETRY_CODES
                progress.update(error_code=exc.error_code, retryable=user_fixable,
                                retry_action="retry_existing" if user_fixable else None)
                changes["progress"] = progress
            elif job["kind"] == "generate":
                recovery = self.store.video_retry_state(job_id)
                if recovery["failed_segment_ids"]:
                    progress = dict(self.store.get_job(job_id).get("progress") or {})
                    progress.update(error_code=recovery["error_code"], retry_action=recovery["retry_action"],
                                    failed_segment_ids=recovery["failed_segment_ids"], retryable=bool(recovery["retry_action"]))
                    changes["progress"] = progress
                    changes["error"] = ("参考图片无法读取；已保留其他素材，重试前会重新检查图片链接。"
                        if recovery["error_code"] == "reference_image_unreadable" else
                        "部分视频片段生成失败；已保留其他任务与素材，可重试失败片段。")
                    if not recovery["retry_action"]:
                        changes["error"] += "当前还有未确认的提交记录，请先检查后重新确认方案。"
            if getattr(exc, "error_code", None) == "voice_too_long":
                current = self.store.get_job(job_id)
                progress = dict(current.get("progress") or {})
                progress.update(error_code="voice_too_long", retryable=False)
                changes["progress"] = progress
                script = current.get("payload", {}).get("plan", {}).get("script", "")
                self._payload(job_id, failed_voice_script_hash=hashlib.sha256(script.encode()).hexdigest())
            elif getattr(exc, "error_code", None) == "image_generation_failed":
                progress = dict(self.store.get_job(job_id).get("progress") or {})
                progress.update(error_code="image_generation_failed", retryable=False, retry_action=None)
                changes["progress"] = progress
            elif getattr(exc, "error_code", None) == "image_submission_unavailable":
                progress = dict(self.store.get_job(job_id).get("progress") or {})
                progress.update(error_code="image_submission_unavailable", retryable=True, retry_action="retry_existing")
                changes["progress"] = progress
            self.store.update_job(job_id, **changes)
            if self._auto_repair_plan_job(job_id, exc):
                return self.store.get_job(job_id)
            current_revision = self.store.get_project(job["project_id"])["revision"]
            self.store.set_project_status(job["project_id"], current_revision, "partial" if self.store.get_job(job_id)["submission_count"] else "failed")
        return self.store.get_job(job_id)

    def _auto_repair_plan_job(self, job_id, exc):
        """Requeue a model failure. Each category stops after AUTO_RETRY_CAP rounds."""
        from .planner_recovery import AUTO_RETRY_CAP, merchant_stop
        from .planning_session import drop_storyboard_checkpoint, reopen_rejected_stage
        job = self.store.get_job(job_id)
        if job["kind"] != "plan" or job["status"] not in ("failed", "partial"):
            return False
        code = getattr(exc, "error_code", None)
        message = str(exc)
        issues = getattr(exc, "issue_summary", ()) or ()
        if merchant_stop(code, message, issues, getattr(exc, "retry_action", None)):
            return False
        if code == "planning_not_produced" or message.strip() == "这次没生成出来":
            return False
        payload = dict(job.get("payload") or {})
        if _content_auto_failure(exc):
            key = "content_auto_retries"
            regenerate = True
        elif _structure_auto_failure(exc):
            key = "structure_auto_retries"
            regenerate = True
        else:
            return False
        if _auto_retry_count(payload, key) >= AUTO_RETRY_CAP:
            return False
        payload[key] = _auto_retry_count(payload, key) + 1
        if regenerate:
            checkpoint = payload.get("planning_checkpoint")
            reopened = None
            if isinstance(checkpoint, dict):
                reopened = drop_storyboard_checkpoint(checkpoint) or reopen_rejected_stage(checkpoint)
            if reopened is not None:
                payload["planning_checkpoint"] = reopened
            else:
                payload.pop("planning_checkpoint", None)
            payload["auto_retry_prepared"] = True
        self.store.update_job(job_id, payload=payload)
        try:
            self.store.retry_planning_job(job["project_id"], job_id)
        except Exception:
            return False
        return True

    async def _plan(self, job):
        def planning_progress(phase):
            self._guard(job["id"], require_current=True)
            self._phase(job["id"], phase)
        def used_skill(trace):
            self._guard(job["id"], require_current=True)
            payload = self.store.get_job(job["id"])["payload"]
            self._payload(job["id"], skill_trace=[*payload.get("skill_trace", []), trace])
            self.store.add_event(job["project_id"], job["id"], "step",
                ("已载入共用参考 " if trace.get("execution_kind") == "shared_reference" else "已载入阶段指令 ")
                + str(trace.get("display_name", {"product-evidence": "商品事实规则", "ad-form-contract": "广告表达规则"}.get(trace.get("id"), trace.get("id", "Ads Video"))))[:100]
                + " · " + str(trace.get("version", ""))[:40])
        planning_gateway = getattr(self.gateway, "supports_strategy_planning", False)
        callbacks = {"on_progress": planning_progress} if planning_gateway else {}
        stream_callbacks = {}
        if planning_gateway:
            def note_planner_stream(stage):
                label = _PLANNER_STREAM_LABELS.get(stage)
                if not label:
                    return
                self._guard(job["id"], require_current=True)
                self.store.add_event(job["project_id"], job["id"], "step", label, "running")
            stream_callbacks["on_planner_stream"] = note_planner_stream
            payload = job["payload"]
            cache = payload.get("stage_result_cache")
            selected_id = (payload.get("selected_strategy") or {}).get("selected_direction_id")
            if isinstance(cache, dict) and payload.get("stage_result_cache_direction_id") == selected_id:
                stream_callbacks["stage_result_cache"] = cache
        if (not job["payload"].get("conversation") and getattr(self.gateway, "supports_direction_selection", False)
                and getattr(self.settings, "direction_selection_enabled", False)):
            detail = self.store.project_detail(job["project_id"])
            payload = {**job["payload"], "conversation": True, "planning_requested": True,
                "frozen": {key: detail[key] for key in ("project", "assets", "messages", "gates")}}
            job = self.store.update_job(job["id"], payload=payload)
        if job["payload"].get("conversation"):
            self.store.set_project_status(job["project_id"], job["revision"], "planning")
            resuming = bool(job["payload"].get("planning_checkpoint", {}).get("entries"))
            self._phase(job["id"], "resuming_plan" if resuming else "understanding")
            frozen = job["payload"].get("frozen")
            if not isinstance(frozen, dict) or not all(key in frozen for key in ("project", "assets", "messages", "gates")):
                raise ValueError("缺少本轮对话快照，请重新发送需求")
            if not resuming:
                self._phase(job["id"], "deciding")
            from .release_gate import lengthen_target
            frozen_project = frozen["project"] if isinstance(frozen.get("project"), dict) else {}
            longer = lengthen_target(
                frozen.get("messages"), frozen_project.get("duration_seconds", 20),
                frozen_project.get("brief", ""), frozen_project.get("plan"), project=frozen_project)
            if longer:
                job = self.store.extend_plan_duration(job["id"], longer)
                frozen = job["payload"]["frozen"]
            if getattr(self.gateway, "supports_direction_selection", False):
                callbacks.update(selected_strategy=job["payload"].get("selected_strategy"),
                    inherited_skill_trace=job["payload"].get("inherited_skill_trace", []),
                    force_plan=bool(job["payload"].get("planning_requested")), on_skill_trace=used_skill)
            if getattr(self.gateway, "supports_planning_checkpoints", False):
                def save_checkpoint(checkpoint):
                    self._guard(job["id"], require_current=True)
                    self._payload(job["id"], planning_checkpoint=checkpoint)
                callbacks.update(planning_checkpoint=job["payload"].get("planning_checkpoint"),
                    on_checkpoint=save_checkpoint)
            raw = await self.gateway.decide(frozen["project"], frozen["assets"], frozen["messages"],
                                            frozen["gates"], frozen["project"].get("plan"),
                                            job_id=job["id"], **callbacks, **stream_callbacks)
            self._guard(job["id"], require_current=True)
            try:
                decision = AgentDecision.model_validate(raw).model_dump()
            except ValidationError:
                raise ValueError("策划回复未通过对话结构校验；没有自动创建问题或开始制作") from None
            if decision["kind"] == "plan":
                decision["plan"] = validate_plan(decision["plan"], frozen["project"].get("duration_seconds", 20),
                                                  settings=self.settings)
                self._references(job["project_id"], decision["plan"])
                for option in decision["creative_options"]:
                    self._references(job["project_id"], {"segments": [option["segment"]]})
            self.store.finish_decision(job["id"], decision)
            if decision["kind"] == "directions" and getattr(self.settings, "planner_speculate_recommended", False):
                from app.speculative_plan import schedule_speculation
                schedule_speculation(self.store, self.gateway, self.settings, job["id"])
            return
        self._phase(job["id"], "planning")
        self.store.set_project_status(job["project_id"], job["revision"], "planning")
        detail = self.store.project_detail(job["project_id"])
        if not any(a["kind"] == "product" and not a.get("metadata", {}).get("generated") for a in detail["assets"]):
            raise ValueError("请先上传至少一张真实商品图")
        legacy_callbacks = {**callbacks, "gates": detail["gates"]} if callbacks else {}
        validated = validate_plan(await self.gateway.plan(detail["project"], detail["assets"], detail["messages"], **legacy_callbacks),
                             detail["project"].get("duration_seconds", 20), settings=self.settings)
        plan = validated
        from .release_gate import MerchantLineChoice, present_checked_plan
        try:
            plan = present_checked_plan(
                plan, plan.get("strategy"), duration=detail["project"].get("duration_seconds", 20),
                generation_strategy=plan.get("generation_strategy"),
                asset_ids=[item["id"] for item in detail["assets"] if item.get("kind") == "product"],
                project=detail["project"])
        except MerchantLineChoice as choice:
            raise ValueError(str(choice)) from None
        if plan is None:
            from .merchant_cards import NotProduced
            raise NotProduced()
        self._guard(job["id"], require_current=True)
        self._references(job["project_id"], plan)
        self.store.assert_job_current(job["id"])
        versions = [validated, plan] if _plan_words(validated) != _plan_words(plan) else [plan]
        if len(versions) > 1:
            project = self.store.save_released_plans(
                job["project_id"], versions, expected_revision=job["revision"],
                expected_context_version=job["payload"].get("context_version"))
        else:
            project = self.store.update_plan(job["project_id"], plan, expected_revision=job["revision"],
                                             expected_context_version=job["payload"].get("context_version"))
        self.store.add_message(job["project_id"], "assistant", "方案已准备好，请检查商品事实与台词后生成视频。", job_id=job["id"])
        self.store.update_job(job["id"], revision=project["revision"], status="ready", phase="plan_ready", lease_until=0)

    def _references(self, project_id, plan):
        result = {}
        for segment in plan["segments"]:
            assets = []
            for aid in segment["reference_asset_ids"]:
                asset = self.store.get_asset(aid)
                path = Path(asset["path"]).resolve()
                if asset["project_id"] != project_id or not path.is_relative_to(self.store.media_root) or not path.is_file():
                    raise ValueError("参考素材缺失或不属于当前项目")
                if asset["kind"] not in ("product", "person", "scene", "character", "start_frame"):
                    raise ValueError("视频参考必须为已登记的图像素材")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != asset["metadata"]["sha256"]:
                    raise ValueError("参考文件已变化，请重新上传并确认方案")
                assets.append(asset)
            if not any(a["kind"] == "product" and not a.get("metadata", {}).get("generated") for a in assets):
                raise ValueError("每个视频段至少需要一张原始商品参考图")
            if plan.get("production") is not None:
                order = {character["id"]: index for index, character in enumerate(plan["production"]["characters"])}
                kind_rank = {"product": 0, "scene": 1, "character": 2, "start_frame": 3}
                assets.sort(key=lambda asset: (kind_rank.get(asset["kind"], 4),
                                              order.get(asset["metadata"].get("character_id"), len(order))))
            result[segment["id"]] = assets
        return result

    async def _audio(self, job, plan):
        payload = self.store.get_job(job["id"])["payload"]
        total = sum(s["duration_seconds"] for s in plan["segments"])
        if payload.get("failed_voice_script_hash") == hashlib.sha256(plan["script"].encode()).hexdigest():
            raise VoiceTooLong("配音文案过长，请先精简文案再生成；已保存的图片会保留。直接恢复同样文案不能解决时长问题。")
        if payload.get("audio_asset_id"):
            saved = self.store.get_asset(payload["audio_asset_id"])
            if saved["metadata"].get("duration_seconds", 0) > total + .05:
                raise VoiceTooLong("已保存的配音长于视频计划，请先精简文案；图片会保留。")
            return saved
        self._guard(job["id"], require_current=True)
        self._phase(job["id"], "synthesizing_voice")
        path = self._file(job, ".wav")
        self._payload(job["id"], voice_request={"text": plan["script"], "voice": str(self.settings.tts_voice), "path": str(path), "target_duration_seconds": total})
        result = await self.gateway.synthesize_voice(plan["script"], path, target_duration_seconds=total)
        info = await asyncio.to_thread(media.probe, path, self.settings.ffmpeg_path)
        if not info["has_audio"]:
            raise ValueError("配音服务未返回可解码音频")
        if info["duration_seconds"] > total + 0.05:
            raise VoiceTooLong("实测配音长于视频计划，请先精简文案再生成；图片已保留，本次尚未提交视频。")
        metadata = {**info, "provider": str(result.get("provider", "unknown")), "script": plan["script"],
                    "alignment": "estimated_sentence_timing", "captions": media.caption_cues(plan["script"], info["duration_seconds"])}
        if isinstance(result.get("captions"), list):
            cues, previous_end = [], 0
            for cue in result["captions"]:
                start = round(float(cue["start"]) * 24)
                end = round(float(cue["end"]) * 24)
                if not (previous_end <= start < end <= math.ceil(info["duration_seconds"] * 24) + 1):
                    raise ValueError("配音句段时轴无效，不能生成错误字幕")
                cues.append({"start_frame": start, "end_frame": end, "text": str(cue["text"])})
                previous_end = end
            if not cues:
                raise ValueError("配音返回了空句段时轴")
            metadata["captions"] = cues
            metadata["alignment"] = "measured_sentence_timing"
        asset = self.store.add_asset(job["project_id"], "audio", "中文配音", path, "audio/wav", job["revision"], metadata)
        self._payload(job["id"], audio_asset_id=asset["id"])
        self.store.add_event(job["project_id"], job["id"], "artifact", "中文配音已生成并测量时长")
        return asset

    def _creative_options(self, job, plan):
        from .ad_form_playbook import stamp_plan_speech_rate
        stamp_plan_speech_rate(plan)
        first = plan["segments"][0]
        return [{"id": "current", "label": "图片没问题，生成视频", "description": "使用已确认的商品原图与参考图片制作当前方案", "segment": copy.deepcopy(first)}]

    async def _generation_plan(self, job, project):
        """Accept a saved plan. Server-only stamps do not force another planning round."""
        raw = copy.deepcopy(job["payload"]["plan"])
        duration = project.get("duration_seconds", 20)
        try:
            plan = validate_plan(raw, duration, settings=self.settings)
        except PlanStructureError as exc:
            if not _review_binding_drift(exc) or (job.get("payload") or {}).get("plan_review_rechecked"):
                raise
            self.store.add_event(job["project_id"], job["id"], "step", "已自动校正，继续制作中")
            self._payload(job["id"], plan_review_rechecked=True)
            refresh = getattr(self.gateway, "review_saved_plan", None)
            if refresh is None:
                raise
            plan = validate_plan(await refresh(raw), duration, settings=self.settings)
        else:
            previous = (raw.get("plan_review") or {}).get("reviewed_plan_sha256")
            current = (plan.get("plan_review") or {}).get("reviewed_plan_sha256")
            if previous and current and previous != current:
                self.store.add_event(job["project_id"], job["id"], "step", "已自动校正，继续制作中")
        return plan

    def _settle_before_spend(self, job, plan):
        """Re-run the hard gates. A line-only repair keeps this confirmation.

        Replacing the whole board with the conservative plan saves that board
        and waits for another 确认. It does not submit images or video.
        """
        from .release_gate import (
            MerchantLineChoice, settle_before_spend, spoken_line_changes, spoken_repair_message,
        )
        project = self.store.get_project(job["project_id"])
        try:
            settled = settle_before_spend(plan, project=project, project_id=job["project_id"])
        except MerchantLineChoice as choice:
            prompt = choice.questions[0]["prompt"] if choice.questions else str(choice)
            raise ValueError(prompt) from choice
        if not isinstance(settled, dict) or settled is plan:
            return settled
        changes = spoken_line_changes(plan, settled)
        previous = job["revision"]
        if changes is None:
            project = self.store.save_repaired_plan(job["project_id"], previous, settled)
            self.store.add_message(
                job["project_id"], "assistant",
                "硬门没有通过，已换成一份能通过检查的方案。请再按确认，确认之后才会出图。",
                job["id"])
            raise NeedsReconfirm()
        project = self.store.save_repaired_plan(job["project_id"], previous, settled)
        current = self.store.get_job(job["id"])
        payload = dict(current.get("payload") or {})
        payload["plan"] = settled
        payload["repaired_from_revision"] = previous
        payload["merchant_confirmed_revision"] = project["revision"]
        self.store.update_job(job["id"], revision=project["revision"], payload=payload)
        if changes:
            note = spoken_repair_message(plan, changes)
            if note:
                self.store.add_message(job["project_id"], "assistant", note, job["id"])
        return settled

    async def _generate(self, job):
        project = self.store.get_project(job["project_id"])
        plan = await self._generation_plan(job, project)
        plan = self._settle_before_spend(job, plan)
        job = self.store.get_job(job["id"])
        project = self.store.get_project(job["project_id"])
        from .release_gate import assert_spend_confirmed
        assert_spend_confirmed(job, project)
        if project["route"] == "presenter" and not getattr(self.settings, "presenter_verified", False):
            raise ValueError("中文出镜口播能力尚未验证，目前不能生成口播路线")
        budget = self._submission_budget(job)
        needed = sum(self.store.get_step(job["id"], f"video:{spec['id']}") is None for spec in plan["segments"])
        if needed + job["submission_count"] > budget:
            raise ValueError("计划段数超过本次提交上限，请调整配置或重新选择时长后确认")
        self._payload(job["id"], video_submission_budget=budget)
        pause_for_creative = (job["payload"].get("pause_for_creative") or plan.get("production") is not None) and not job["payload"].get("creative_confirmed")
        references = self._references(job["project_id"], plan)
        self.store.set_project_status(job["project_id"], job["revision"], "generating")
        plan = await self._designs(job, plan)
        references = self._references(job["project_id"], plan)
        if pause_for_creative:
            validate_design_assets(plan, [self.store.get_asset(aid) for aid in self.store.get_job(job["id"])["payload"].get("design_asset_ids", [])])
            self._guard(job["id"], require_current=True)
            self.store.create_creative_gate(job["id"], self._creative_options(job, plan),
                                            "商品原图已保留，参考图片已准备好。请检查图片，确认后再生成视频。")
            return
        audio = await self._audio(job, plan) if plan["audio_strategy"] == "voiceover" else None
        # Persist each reliable receipt before submitting the next segment. All
        # provider tasks can then render concurrently without concurrent POSTs.
        resume_known_only = job["payload"].get("resume_known_tasks_only") or any(
            (self.store.get_step(job["id"], f"video:{spec['id']}") or {}).get("state") in ("failed", "rejected")
            for spec in plan["segments"])
        submission_errors, submission_unknown = [], False
        for spec in plan["segments"]:
            step = self.store.get_step(job["id"], f"video:{spec['id']}")
            if step and step["state"] in ("failed", "rejected"):
                continue
            if not step and resume_known_only:
                continue
            try:
                await self._submit_video(job, video_submission_spec(plan, spec, references[spec["id"]]), references[spec["id"]])
            except SubmissionUnknown:
                # Stop all further POSTs, but do not strand earlier paid tasks.
                submission_unknown = True
                break
            except (StaleRevision, media.MediaCanceled):
                raise
            except Exception as exc:
                step = self.store.get_step(job["id"], f"video:{spec['id']}")
                if not step or step["state"] not in ("failed", "rejected"):
                    raise
                submission_errors.append(exc)
                # Do not expand a failed attempt with another paid submission;
                # still collect every earlier receipt below.
                break
        # A terminal failure in one remote task must not strand another paid
        # task. Collect every accepted segment before reporting the failed run.
        collected, errors = {}, list(submission_errors)
        for spec in plan["segments"]:
            step = self.store.get_step(job["id"], f"video:{spec['id']}")
            if not step:
                errors.append(ValueError("有片段尚未受理；本次仅恢复已知任务，没有新增提交"))
                continue
            if step["state"] in ("submitting", "submitted_unknown") or (
                    step["state"] not in ("failed", "rejected") and not step["task_id"]):
                submission_unknown = True
                continue
            if step["state"] in ("failed", "rejected"):
                errors.append(ValueError("视频片段已明确失败，需要重试失败片段"))
                continue
            try:
                collected[spec["id"]] = await self._video(job, video_submission_spec(plan, spec, references[spec["id"]]), references[spec["id"]])
            except SubmissionUnknown:
                submission_unknown = True
            except (StaleRevision, media.MediaCanceled):
                raise
            except Exception as exc:
                errors.append(exc)
        if submission_unknown:
            raise SubmissionUnknown()
        if errors:
            raise errors[0]
        segments, expected_speech_timeline, cursor = [], [], 0
        no_speech = media.permits_missing_native_audio(plan)
        for index, spec in enumerate(plan["segments"]):
            asset = collected[spec["id"]]
            frames = round(spec["duration_seconds"] * 24)
            segments.append({"segment_id": spec["id"], "asset_id": asset["id"], "source_in_frame": 0,
                             "source_out_frame": frames, "timeline_in_frame": cursor, "timeline_out_frame": cursor + frames})
            if plan["audio_strategy"] == "native" and not no_speech:
                # These are approved directing instructions, never measured speech timestamps.
                if spec.get("speech_spans") is not None:
                    expected_speech_timeline.extend(expected_grouped_speech(spec, cursor))
                for shot_index, shot in enumerate(spec["shots"] if spec.get("speech_spans") is None else []):
                    expected_speech_timeline.append({
                        "start_frame": cursor + round(shot["start"] * 24),
                        "end_frame": cursor + round(shot["end"] * 24),
                        "segment_id": spec["id"], "shot_index": shot_index,
                        "visual_description": shot["description"], "narration": shot.get("narration", ""),
                    })
            cursor += frames
            self._phase(job["id"], "segments_ready", completed_segments=index + 1, total_segments=len(plan["segments"]))
        manifest = {"schema_version": 1, "revision": job["revision"], "fps": 24, "width": 720, "height": 1280,
                    "total_frames": cursor, "segments": segments, "audio_strategy": plan["audio_strategy"],
                    "generation_strategy": plan.get("generation_strategy", "legacy_10s"),
                    "audio_asset_id": audio["id"] if audio else None,
                    "captions": audio["metadata"]["captions"] if audio else [],
                    "caption_alignment": audio["metadata"]["alignment"] if audio else
                        ("no_speech_requested" if no_speech else "native_audio_untranscribed"),
                    "manual_review_required": True,
                    "review_note": ("仅通过媒体规格检查；请人工复核商品、台词与原生声音。尚未转写音轨，本版未添加字幕。" if not audio
                                    else "仅通过媒体规格检查；商品、台词、字幕时序与声音效果仍需人工复核。")}
        if plan.get("production") is not None:
            manifest["production"] = {"speech_mode": plan["production"]["speech_mode"]}
        if no_speech:
            manifest["review_note"] = "仅通过媒体规格检查；请复核商品、画面和环境声音，并确认没有意外人声。本方案无台词，不作台词对齐和声纹判定；缺音轨时仅补本地静音。"
        if plan["audio_strategy"] == "native":
            manifest["expected_speech_timeline"] = expected_speech_timeline
            manifest["audio_alignment_status"] = "not_applicable" if no_speech else "not_verified"
        manifest["continuity_review"] = {"voice_identity": "not_applicable" if no_speech else "not_verified", "visual_transitions": "not_verified",
                                          "generation_groups": len(segments), "provider_seams": max(0, len(segments) - 1)}
        if no_speech:
            manifest["continuity_review"].update(environmental_audio="not_verified", unexpected_speech="not_verified")
        director = direction_context(plan)
        if director is not None:
            manifest["visual_direction"] = director
            manifest["expected_shot_continuity"] = [{"segment_id": segment["id"], "shot_id": shot["id"],
                "start": shot["start"], "end": shot["end"], "continuity": shot["continuity"]}
                for segment in plan["segments"] for shot in segment["shots"]]
        self._payload(job["id"], manifest=manifest)
        await self._render(job, manifest)

    async def _designs(self, job, plan):
        """Gateway owns durable image receipts in this fixed, run-specific directory."""
        create = getattr(self.gateway, "create_designs", None)
        payload = self.store.get_job(job["id"])["payload"]
        # Historical generated product images must never replace the merchant's original.
        clean = copy.deepcopy(plan)
        for segment in clean["segments"]:
            segment["reference_asset_ids"] = [aid for aid in segment["reference_asset_ids"]
                if not ((a := self.store.get_asset(aid))["kind"] == "product" and a["metadata"].get("generated"))]
        plan = clean
        saved = [self.store.get_asset(aid) for aid in dict.fromkeys(payload.get("design_asset_ids", []))]
        saved = [asset for asset in saved if asset["kind"] in ("scene", "character") or (
            asset["kind"] == "start_frame" and asset.get("metadata", {}).get("state_reference"))]
        for asset in saved:
            path = Path(asset["path"]).resolve()
            if (asset["project_id"] != job["project_id"] or not asset["metadata"].get("generated")
                    or not path.is_relative_to(self.store.media_root) or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != asset["metadata"].get("sha256")):
                raise ValueError("已保存的场景图或角色图缺失或已经变化，请先恢复图片")
        try:
            complete = validate_design_assets(plan, saved) if saved else []
        except ValueError:
            if payload.get("creative_confirmed"):
                raise
            complete = []
        replace_keys = None
        if complete:
            current_job = self.store.get_job(job["id"])
            video_submitted = bool(current_job.get("submission_count")) or bool(
                payload.get("frozen_video_requests") or payload.get("frozen_video_specs"))
            stale = [asset for asset in complete if asset["kind"] == "character"
                     and saved_character_sheet_is_stale(asset.get("metadata"))]
            # A merchant who already confirmed the pictures, or a job that already
            # paid for video, keeps the saved sheet. Only an unconfirmed, unsubmitted
            # job replaces a sheet from before the horizontal Chinese-default template.
            if not stale or payload.get("creative_confirmed") or video_submitted:
                saved_ids = [asset["id"] for asset in complete]
                resumed = copy.deepcopy(plan)
                from app.planner_recovery import append_reference_ids
                for segment in resumed["segments"]:
                    segment["reference_asset_ids"] = append_reference_ids(segment["reference_asset_ids"], saved_ids)
                self._payload(job["id"], plan=resumed, design_asset_ids=saved_ids)
                progress = dict(self.store.get_job(job["id"]).get("progress") or {})
                self.store.update_job(job["id"], progress={**progress, "design_asset_ids": saved_ids})
                return resumed
            stale_ids = {asset["id"] for asset in stale}
            saved = [asset for asset in saved if asset["id"] not in stale_ids]
            replace_keys = ["character_" + asset["metadata"]["character_id"] for asset in stale]
            for segment in plan["segments"]:
                segment["reference_asset_ids"] = [aid for aid in segment.get("reference_asset_ids", []) if aid not in stale_ids]
        if payload.get("creative_confirmed") and plan.get("production") is not None:
            raise ValueError("已确认的场景图与角色图不完整，不能重新出图或提交视频")
        if not callable(create):
            if plan.get("production") is not None:
                raise ValueError("当前图片服务不能准备方案所需的场景与角色参考图")
            self._payload(job["id"], plan=plan)
            return plan
        self._guard(job["id"], require_current=True)
        folder = self.store.media_root / job["project_id"] / job["id"] / "designs"
        folder.mkdir(parents=True, exist_ok=True)
        self._payload(job["id"], designs_directory=str(folder), design_request=copy.deepcopy(plan))
        self._phase(job["id"], "creating_designs")
        project = self.store.get_project(job["project_id"])
        # Uploads added after confirmation must not silently change this run's inputs.
        frozen_ids = list(dict.fromkeys(aid for segment in plan["segments"] for aid in segment["reference_asset_ids"]))
        assets = [self.store.get_asset(aid) for aid in frozen_ids]
        def used_media_skill(trace):
            current = self.store.get_job(job["id"])["payload"].get("media_skill_trace", [])
            if trace not in current:
                self._payload(job["id"], media_skill_trace=[*current, copy.deepcopy(trace)])

        designs = []
        expected_characters = {item["character_id"]: item["character"] for item in required_designs(plan) if item["kind"] == "character"}

        def save_design(result):
            # Register every verified completion before a later image can fail.
            # The final returned list remains supported for older/test gateways.
            self._guard(job["id"], require_current=True)
            kind = result.get("kind")
            if kind not in ("scene", "character", "start_frame"):
                raise ValueError("图片服务只能返回场景图、角色四视图或状态参考，不能重新生成商品")
            if kind == "start_frame" and not (result.get("metadata") or {}).get("state_reference"):
                raise ValueError("状态参考必须标明它是哪一镜的起始或结果")
            metadata = {**(result.get("metadata") or {}), "generated": True, "review_required": True, "job_id": job["id"]}
            if kind == "character":
                character = expected_characters.get(metadata.get("character_id"))
                if not character or metadata.get("view_layout") != "four_view":
                    raise ValueError("角色图必须对应本方案已确认角色并采用组合四视图")
                metadata["character_name"] = character["name"]
            asset = self.store.find_asset_by_path(result["source_path"])
            if asset and asset["project_id"] != job["project_id"]:
                raise ValueError("设计图不属于当前项目")
            if not asset:
                suffix = Path(result["source_path"]).suffix.lower()
                mime = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(suffix, "image/png")
                asset = self.store.add_asset(job["project_id"], kind, result.get("name", "视觉设计图"),
                                             result["source_path"], mime, job["revision"], metadata)
            result["id"] = asset["id"]
            if asset["kind"] != kind or not asset["metadata"].get("generated"):
                raise ValueError("场景图与角色图必须是本次真实生成并登记的素材")
            if kind == "character" and (asset["metadata"].get("character_id") != metadata["character_id"]
                    or asset["metadata"].get("view_layout") != "four_view"):
                raise ValueError("已登记角色图与生成回执的角色不一致，请核对已保存图片")
            if not any(item["id"] == asset["id"] for item in designs):
                designs.append(asset)
            design_ids = list(dict.fromkeys(asset["id"] for asset in [*saved, *designs]))
            self._payload(job["id"], design_asset_ids=design_ids)
            progress = dict(self.store.get_job(job["id"]).get("progress") or {})
            self.store.update_job(job["id"], progress={**progress, "design_asset_ids": design_ids})

        # Existing/offline gateways need not implement the provenance callback.
        callback = {"on_skill": used_media_skill} if "on_skill" in inspect.signature(create).parameters else {}
        if "on_asset" in inspect.signature(create).parameters:
            callback["on_asset"] = save_design
        if replace_keys and "replace_keys" in inspect.signature(create).parameters:
            callback["replace_keys"] = replace_keys
        plan = self._settle_before_spend(job, plan)
        self.gateway._submission_busy_notice = lambda: self.store.add_event(
            job["project_id"], job["id"], "step", "图片平台繁忙，正在自动重新提交")
        try:
            results = await create(project, assets, plan, folder,
                                   cancelled=lambda: self.store.is_cancelled(job["id"]) or
                                   self.store.get_project(job["project_id"])["revision"] != job["revision"], **callback)
        except Exception as exc:
            self._guard(job["id"], require_current=True)
            if getattr(exc, "uncertain", False):
                raise SubmissionUnknown() from exc
            if getattr(exc, "safe_to_display", False):
                raise
            raise ValueError("视觉设计图生成失败，方案已保留；重试会优先恢复已记录的图片任务") from exc
        finally:
            self.gateway._submission_busy_notice = None
        self._guard(job["id"], require_current=True)
        if not results:
            raise ValueError("图片服务没有返回真实场景图；不能用商品上传图冒充")
        for result in results:
            save_design(result)
        # Keep successfully registered partial results visible; a resume recovers
        # the gateway's durable image receipts instead of discarding them.
        design_ids = list(dict.fromkeys(asset["id"] for asset in [*saved, *designs]))
        self._payload(job["id"], design_asset_ids=design_ids)
        progress = dict(self.store.get_job(job["id"]).get("progress") or {})
        self.store.update_job(job["id"], progress={**progress, "design_asset_ids": design_ids})
        design_ids = [asset["id"] for asset in validate_design_assets(plan, [self.store.get_asset(aid) for aid in design_ids])]
        plan = copy.deepcopy(plan)
        for segment in plan["segments"]:
            from app.planner_recovery import append_reference_ids
            segment["reference_asset_ids"] = append_reference_ids(segment["reference_asset_ids"], design_ids)
        self._payload(job["id"], design_asset_ids=design_ids, plan=plan)
        progress = dict(self.store.get_job(job["id"]).get("progress") or {})
        self.store.update_job(job["id"], progress={**progress, "design_asset_ids": design_ids})
        state_count = sum(1 for item in required_designs(plan) if item["kind"] == "start_frame")
        if replace_keys:
            saved_note = "角色参考已按当前横版模板重新生成，场景图仍用已保存的"
        elif state_count and expected_characters:
            saved_note = "场景、角色与状态参考图已保存，商品继续使用原图"
        elif expected_characters:
            saved_note = "场景与角色参考图已保存，商品继续使用原图"
        elif state_count:
            saved_note = "场景与状态参考图已保存，商品继续使用原图"
        else:
            saved_note = "场景图已保存，商品继续使用原图"
        self.store.add_event(job["project_id"], job["id"], "artifact", saved_note)
        if not payload.get("pause_for_creative"):
            self.store.add_message(job["project_id"], "assistant", "真实视觉设计图已生成，商品外观仍需核对；正在制作视频。", job_id=job["id"])
        return plan

    def _submission_budget(self, job):
        return min(self.settings.max_video_submissions, len(job.get("payload", {}).get("plan", {}).get("segments", [])),
                   job.get("payload", {}).get("video_submission_budget", 6))

    async def _submit_video(self, job, spec, assets):
        """Reserve once, then POST the frozen request. Only a definite unpaid 4xx may be posted again."""
        jid, key = job["id"], f"video:{spec['id']}"
        self._guard(jid, require_current=True)
        frozen_spec = job.get("payload", {}).get("frozen_video_specs", {}).get(spec["id"])
        if frozen_spec is not None:
            original_assets = job["payload"].get("frozen_video_assets", {}).get(spec["id"])
            actual_assets = [{"id": asset["id"], "sha256": asset["metadata"]["sha256"]} for asset in assets]
            if frozen_spec.get("id") != spec["id"] or original_assets != actual_assets:
                raise ValueError("重试参考图片与原视频请求不一致，请重新确认方案")
            spec = copy.deepcopy(frozen_spec)
        step = self.store.get_step(jid, key)
        if step and step["state"] == "downloaded":
            return step
        if step and step["state"] in ("submitting", "submitted_unknown"):
            raise SubmissionUnknown()
        if step and step["state"] in ("rejected", "failed"):
            raise ValueError("该视频任务已明确失败，原run不自动重新生成；需要新的明确授权与方案版本")
        if not step:
            fresh = self.store.get_job(jid)
            self._settle_before_spend(fresh, (fresh.get("payload") or {}).get("plan"))
            fresh = self.store.get_job(jid)
            from .release_gate import assert_spend_confirmed
            assert_spend_confirmed(fresh, self.store.get_project(job["project_id"]))
            allowed = job.get("payload", {}).get("retry_segment_ids")
            if allowed is not None and spec["id"] not in allowed:
                raise ValueError("本次只允许重新提交明确失败的片段")
            self._guard(jid, require_current=True)
            snapshot = {"segment": copy.deepcopy(spec), "assets": [{"id": a["id"], "sha256": a["metadata"]["sha256"],
                         "provider_url": a.get("metadata", {}).get("provider_url")} for a in assets],
                        "video_model": self.settings.video_model, "provider_duration_seconds": spec["duration_seconds"] if spec.get("generation_strategy") == "continuity_v1" else 10,
                        "idempotency_key": f"{jid}:{spec['id']}", "revision": job["revision"]}
            prepare = getattr(self.gateway, "prepare_video_request", None)
            if callable(prepare):
                # Explicit retries keep the original compiled bytes even when a
                # Skill has changed since the first paid request.
                frozen = job.get("payload", {}).get("frozen_video_requests", {}).get(spec["id"])
                snapshot["video_request"] = copy.deepcopy(frozen if frozen is not None else prepare(spec))
                snapshot["video_model"] = snapshot["video_request"].get("model", snapshot["video_model"])
                snapshot["provider_duration_seconds"] = snapshot["video_request"].get("duration", snapshot["provider_duration_seconds"])
            if not self.store.reserve_submission(jid, key, snapshot, self._submission_budget(job)):
                raise SubmissionUnknown()
            self._phase(jid, "submitting_video", segment_id=spec["id"])
            self.gateway._submission_busy_notice = lambda: self.store.add_event(
                job["project_id"], jid, "step", "视频平台繁忙，正在自动重新提交")
            try:
                kwargs = {"prepared_request": snapshot["video_request"]} if "video_request" in snapshot else {}
                receipt = await self.gateway.submit_video(spec, assets, snapshot["idempotency_key"], **kwargs)
                task_id = receipt.get("task_id")
                if not task_id:
                    raise SubmissionUnknown()
                # The durable receipt precedes polling, cancellation handling, and downloads.
                result = {"provider": str(receipt.get("provider", "unknown")),
                          **{k: receipt[k] for k in ("skill_trace", "prompt_sha256") if k in receipt}}
                refusals = receipt.get("submission_refusals")
                if not isinstance(refusals, list):
                    refusals = getattr(self.gateway, "_submission_refusals", None)
                if isinstance(refusals, list) and refusals:
                    result["submission_refusals"] = refusals
                self.store.update_step(jid, key, state="submitted", task_id=str(task_id), result=result)
                self.store.add_event(job["project_id"], jid, "step", "视频任务已受理，回执已保存")
            except BaseException as exc:
                if isinstance(exc, (asyncio.CancelledError, SubmissionUnknown)) or getattr(exc, "uncertain", True):
                    self.store.update_step(jid, key, state="submitted_unknown", error="供应商受理结果未知",
                                           result=_submission_failure_result(exc))
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise SubmissionUnknown() from exc
                code = getattr(exc, "error_code", None)
                if code == "reference_image_unreadable":
                    message = "参考图片检查未通过，视频尚未提交"
                elif code in ("provider_balance", "provider_auth", "provider_request_rejected"):
                    message = str(exc)
                else:
                    code = "provider_video_rejected"
                    message = "视频请求未获受理"
                self.store.update_step(jid, key, state="rejected", error=message,
                                       result=_submission_failure_result(exc, self.gateway, code))
                if getattr(exc, "safe_to_display", False):
                    raise
                raise ValueError("供应商拒绝了视频请求；已保留提交记录，不自动重发") from exc
            finally:
                self.gateway._submission_busy_notice = None
            step = self.store.get_step(jid, key)
        if not step["task_id"]:
            raise SubmissionUnknown()
        return step

    async def _video(self, job, spec, assets):
        jid, key = job["id"], f"video:{spec['id']}"
        step = self.store.get_step(jid, key)
        if not step:
            step = await self._submit_video(job, spec, assets)
        if step["state"] == "downloaded":
            saved = self.store.get_asset(step["result"]["asset_id"])
            if (spec.get("audio_strategy") == "native" and not saved["metadata"].get("has_audio")
                    and not media.permits_missing_native_audio(spec)):
                raise ValueError("已保存的视频片段没有原生音轨，不能导出有声广告；没有改用系统配音")
            return saved
        if step["state"] in ("rejected", "failed"):
            raise ValueError("该视频任务已明确失败，原run不自动重新生成；需要新的明确授权与方案版本")
        if step["state"] in ("submitting", "submitted_unknown") or not step["task_id"]:
            raise SubmissionUnknown()
        started = time.monotonic()
        self._phase(jid, "waiting_provider", segment_id=spec["id"], provider_progress=None)
        while True:
            self._guard(jid)
            if time.monotonic() - started > self.settings.provider_timeout_seconds:
                raise ValueError("供应商任务等待超时；重试将继续查询原任务，不重新生成")
            try:
                result = await self.gateway.poll_video(step["task_id"])
            except Exception as exc:
                if not getattr(exc, "retryable", True):
                    raise
                # Queries are side-effect free. Keep the durable remote task and retry conservatively.
                await asyncio.sleep(max(0.01, self.settings.poll_seconds))
                continue
            self._phase(jid, "waiting_provider", segment_id=spec["id"],
                        provider_progress=result.get("progress"))
            if result.get("status") == "failed":
                code = "reference_image_unreadable" if result.get("error_code") == "reference_image_unreadable" else "provider_video_failed"
                message = "视频模型无法读取参考图片" if code == "reference_image_unreadable" else "供应商视频任务失败"
                self.store.update_step(jid, key, state="failed", error=message,
                                       result={**(step.get("result") or {}), "error_code": code})
                raise ValueError(message + "；保留其他素材，不自动重新扣费生成")
            if result.get("status") == "succeeded":
                url = result.get("download_url")
                if not url:
                    raise ValueError("供应商已完成但未返回下载地址；可继续查询原任务")
                break
            await asyncio.sleep(max(0.01, self.settings.poll_seconds))
        self._guard(jid)
        self._phase(jid, "downloading_video", segment_id=spec["id"])
        path = self._file(job, ".mp4")
        await self.gateway.download_video(url, path)
        info = await asyncio.to_thread(media.probe, path, self.settings.ffmpeg_path)
        if not info["has_video"] or info.get("video_duration_seconds", info["duration_seconds"]) + 1 / 24 < spec["duration_seconds"]:
            raise ValueError("下载的视频缺失或短于计划，不能作为完整片段使用")
        if (spec.get("audio_strategy") == "native" and not info.get("has_audio")
                and not media.permits_missing_native_audio(spec)):
            raise ValueError("供应商返回的视频片段没有原生音轨，不能导出有声广告；没有改用系统配音")
        saved_step = self.store.get_step(jid, key) or step
        request = (saved_step.get("snapshot") or {}).get("video_request") if isinstance(saved_step, dict) else None
        asset = self.store.add_asset(job["project_id"], "segment", f"视频段 {spec['id']}", path, "video/mp4", job["revision"],
                                     {**info, "task_id": step["task_id"], "segment_id": spec["id"], "source": "provider",
                                      "requested_duration_seconds": spec["duration_seconds"],
                                      "generation_strategy": spec.get("generation_strategy", "legacy_10s"),
                                      "provider_duration_seconds": spec["duration_seconds"] if spec.get("generation_strategy") == "continuity_v1" else 10,
                                      **video_generation_metadata(spec, assets, request)})
        self.store.update_step(jid, key, state="downloaded", result={**(step.get("result") or {}), "asset_id": asset["id"]})
        self.store.add_event(job["project_id"], jid, "artifact", "真实视频片段已取回并检查时长")
        return asset

    async def _recompose(self, job):
        base = self.store.get_render(job["payload"]["base_render_id"])
        if base["project_id"] != job["project_id"]:
            raise ValueError("不能重新合成其他项目的成片")
        if self.store.get_project(job["project_id"])["current_render_id"] != base["id"]:
            raise StaleRevision("当前成片已变化，请刷新后重新合成")
        manifest = copy.deepcopy(base["manifest"])
        manifest.pop("cta", None)
        manifest.update(revision=job["revision"], base_render_id=base["id"])
        self._payload(job["id"], manifest=manifest)
        await self._render(job, manifest)

    async def _render(self, job, manifest):
        manifest = copy.deepcopy(manifest)
        manifest.pop("cta", None)
        self._guard(job["id"])
        self._phase(job["id"], "composing")
        self.store.set_project_status(job["project_id"], job["revision"], "composing")
        ids = [s["asset_id"] for s in manifest["segments"]]
        if manifest.get("audio_asset_id"):
            ids.append(manifest["audio_asset_id"])
        assets = [self.store.get_asset(aid) for aid in ids]
        if any(a["project_id"] != job["project_id"] for a in assets):
            raise ValueError("不能合成其他项目的素材")
        for asset in assets:
            path = Path(asset["path"]).resolve()
            if not path.is_relative_to(self.store.media_root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["metadata"]["sha256"]:
                raise ValueError("合成源素材缺失或已改变，请恢复原文件")
        destination = self._file(job, ".mp4")
        info = await asyncio.to_thread(media.compose, manifest, {a["id"]: a["path"] for a in assets}, destination,
                                       self.settings.ffmpeg_path, lambda: self.store.is_cancelled(job["id"]),
                                       getattr(self.settings, "subtitle_font", "PingFang SC"))
        if "local_silence_asset_ids" in info:
            manifest["local_silence_asset_ids"] = info["local_silence_asset_ids"]
            self._payload(job["id"], manifest=manifest)
        asset = self.store.add_asset(job["project_id"], "render", "广告成片", destination, "video/mp4", job["revision"],
                                     {**info, "technical_checks": "pass", "manual_review_required": True,
                                      **render_prompt_metadata(assets)})
        render = self.store.publish_render(job["id"], asset["id"], manifest)
        ready_message = ("视频已导出并保留原生声音。请复核商品外观、台词和声音；尚未转写音轨，本版未添加字幕。" if manifest["audio_strategy"] == "native"
                         else "视频已导出。已检查文件与规格，请复核商品外观、台词、字幕和声音。")
        if media.permits_missing_native_audio(manifest):
            ready_message = "视频已按无人声方案导出。请复核商品外观、转场及环境声音，并确认没有意外人声。"
        self.store.add_message(job["project_id"], "assistant", ready_message if render["status"] == "ready" else "旧版本结果已保存，没有覆盖当前方案。", job_id=job["id"])
