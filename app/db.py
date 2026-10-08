"""Small durable store. No provider call belongs inside a database transaction."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


ACTIVE = ("queued", "running", "cancel_requested")
PHASE_LABELS = {"resuming_plan": "正在接续已保存的策划进度", "reviewing_plan": "正在综合检查商品表达、动作与声画计划", "refining_script": "正在精简动作并重排脚本时间", "refining_storyboard": "正在依据检查结果调整画面与动作", "retrying_planner": "策划服务未返回内容，正在重试当前步骤", "retrying_planner_request": "策划服务暂时中断，正在继续当前步骤", "using_backup_planner": "改用备用策划服务，继续这一步", "recovering_strategy": "系统仍在处理这一步，需求已保留", "reviewing_directions": "正在检查创意故事与商品关系", "writing_script": "正在策划广告内容", "reviewing_script": "正在检查商品表达与台词", "storyboarding": "正在安排画面和台词", "repairing_storyboard": "正在校正画面与旁白的时间对应", "checking_plan": "正在核对脚本与分镜时间", "checking_alignment": "正在检查画面与旁白是否对应", "queued": "任务已排队", "understanding": "正在读取需求与商品素材", "deciding": "正在判断是否需要补充信息", "planning": "正在准备广告方案", "plan_ready": "广告方案已准备好", "synthesizing_voice": "正在准备中文配音", "creating_designs": "正在生成参考图", "submitting_video": "正在提交视频任务", "waiting_provider": "正在等待供应商生成视频", "downloading_video": "正在取回视频", "segments_ready": "视频片段已取回", "composing": "正在合成广告视频", "complete": "成片已保存", "needs_reconcile": "提交结果需要核对"}


class StaleRevision(ValueError):
    pass


def identifier(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _public_media_skill(trace, stage):
    """Project only durable compiler provenance, never its inputs or prompt."""
    if not isinstance(trace, dict) or trace.get("mode") != "compiler" or trace.get("stage") != stage:
        return None
    keys = ("id", "display_name", "version", "mode", "stage", "content_sha256")
    if not all(isinstance(trace.get(key), str) for key in keys):
        return None
    if (not re.fullmatch(r"ads-video-[a-z0-9-]{1,70}", trace["id"])
            or not re.fullmatch(r"Ads Video [A-Za-z0-9 -]{1,90}", trace["display_name"])
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", trace["version"])
            or not re.fullmatch(r"[a-f0-9]{64}", trace["content_sha256"])):
        return None
    return {key: trace[key] for key in keys}


class Store:
    def __init__(self, db_path: Path, media_root: Path):
        self.db_path = Path(db_path).resolve()
        self.media_root = Path(media_root).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        with self._connection() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects(
                id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,route TEXT NOT NULL,
                status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,plan TEXT,
                current_render_id TEXT,created_at REAL,updated_at REAL);
            CREATE TABLE IF NOT EXISTS revisions(
                project_id TEXT,revision INTEGER,plan TEXT,created_at REAL,
                PRIMARY KEY(project_id,revision));
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY,project_id TEXT,role TEXT,content TEXT,created_at REAL);
            CREATE TABLE IF NOT EXISTS assets(
                id TEXT PRIMARY KEY,project_id TEXT,kind TEXT,name TEXT,path TEXT UNIQUE,
                mime TEXT,revision INTEGER,metadata TEXT,created_at REAL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,project_id TEXT,revision INTEGER,kind TEXT,status TEXT,
                phase TEXT,error TEXT,progress TEXT,payload TEXT,lease_until REAL DEFAULT 0,
                cancel_requested INTEGER DEFAULT 0,submission_count INTEGER DEFAULT 0,
                created_at REAL,updated_at REAL);
            CREATE INDEX IF NOT EXISTS jobs_project ON jobs(project_id,revision,kind);
            CREATE TABLE IF NOT EXISTS steps(
                job_id TEXT,step_key TEXT,state TEXT,snapshot TEXT,task_id TEXT,
                result TEXT,error TEXT,created_at REAL,updated_at REAL,
                PRIMARY KEY(job_id,step_key));
            CREATE TABLE IF NOT EXISTS renders(
                id TEXT PRIMARY KEY,project_id TEXT,asset_id TEXT,revision INTEGER,
                status TEXT,manifest TEXT,created_at REAL);
            CREATE TABLE IF NOT EXISTS gates(
                id TEXT PRIMARY KEY,project_id TEXT,job_id TEXT,kind TEXT,status TEXT,
                message TEXT,questions TEXT,options TEXT,answers TEXT,selected_option_id TEXT,note TEXT,
                context_version INTEGER,revision INTEGER,created_at REAL,answered_at REAL);
            CREATE INDEX IF NOT EXISTS gates_project ON gates(project_id,status);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT,job_id TEXT,kind TEXT,
                label TEXT,state TEXT,created_at REAL);
            CREATE INDEX IF NOT EXISTS events_project ON events(project_id,seq);
            CREATE TABLE IF NOT EXISTS turn_requests(
                project_id TEXT,client_turn_id TEXT,kind TEXT,job_id TEXT,
                PRIMARY KEY(project_id,client_turn_id));
            """)
            for table, column, definition in (("projects", "context_version", "INTEGER NOT NULL DEFAULT 0"),
                                               ("projects", "plan_context_version", "INTEGER NOT NULL DEFAULT 0"),
                                               ("projects", "duration_seconds", "INTEGER NOT NULL DEFAULT 20"),
                                               ("projects", "ad_form", "TEXT"),
                                               ("projects", "ad_form_updated_context_version", "INTEGER NOT NULL DEFAULT 0"),
                                               ("projects", "cta_target", "TEXT"),
                                               ("messages", "job_id", "TEXT"),
                                               ("jobs", "started_at", "REAL")):
                columns = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def _connection(self, write=False):
        c = sqlite3.connect(str(self.db_path), timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _dict(row):
        if row is None:
            return None
        result = dict(row)
        for field in ("plan", "progress", "payload", "metadata", "manifest", "snapshot", "result", "questions", "options", "answers"):
            if field in result:
                result[field] = json.loads(result[field]) if result[field] else None
        if "path" in result:
            result["url"] = f"/media/{result['id']}"
        if "asset_id" in result and "manifest" in result:
            result["url"] = f"/media/{result['asset_id']}"
        return result

    def create_project(self, brief, route, title=None, duration_seconds=20, ad_form=None):
        from .schemas import segment_durations
        segment_durations(duration_seconds)
        from .ad_form import AD_FORM_LABELS, infer_ad_form
        from .cta import stated_cta
        if ad_form is not None and ad_form not in AD_FORM_LABELS:
            raise ValueError("广告倾向无效")
        ad_form = ad_form or infer_ad_form(brief)
        if route not in ("product", "presenter"):
            raise ValueError("不支持的制作路线")
        if not str(brief).strip():
            raise ValueError("请填写商品或广告要求")
        pid, now = identifier("project"), time.time()
        with self._connection(True) as c:
            c.execute("INSERT INTO projects(id,title,brief,route,status,revision,plan,current_render_id,created_at,updated_at,duration_seconds,ad_form,cta_target) VALUES(?,?,?,?,?,0,NULL,NULL,?,?,?,?,?)",
                      (pid, title or str(brief).strip()[:32], brief, route, "draft", now, now, duration_seconds, ad_form, stated_cta(brief)))
        return self.get_project(pid)

    def get_project(self, project_id):
        with self._connection() as c:
            return self._project(c, project_id)

    def list_projects(self):
        with self._connection() as c:
            return [self._project_result_state(c, self._dict(r))
                    for r in c.execute("SELECT * FROM projects ORDER BY updated_at DESC")]

    def update_project_title(self, project_id, title):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            raise ValueError("项目标题需要1–80个字符")
        with self._connection(True) as c:
            changed = c.execute("UPDATE projects SET title=?,updated_at=? WHERE id=?",
                                (title.strip(), time.time(), project_id))
            if not changed.rowcount:
                raise KeyError("项目不存在")
            return self._project(c, project_id)

    def project_detail(self, project_id):
        with self._connection() as c:
            c.execute("BEGIN")
            return self._detail(c, project_id)

    def add_message(self, project_id, role, content, job_id=None):
        with self._connection(True) as c:
            self._project(c, project_id)
            message = self._message(c, project_id, role, content, job_id)
            if role == "user":
                c.execute("UPDATE projects SET context_version=context_version+1,updated_at=? WHERE id=?", (time.time(), project_id))
            return message

    def add_asset(self, project_id, kind, name, path, mime, revision=None, metadata=None):
        project = self.get_project(project_id)
        path = Path(path).resolve()
        if not path.is_relative_to(self.media_root) or not path.is_file():
            raise ValueError("媒体必须是媒体目录内的实际文件")
        if kind not in ("product", "person", "scene", "character", "start_frame", "segment", "audio", "render"):
            raise ValueError("无效素材类型")
        aid = identifier("asset")
        data = copy.deepcopy(metadata or {})
        data["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._connection(True) as c:
            c.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)", (
                aid, project_id, kind, name, str(path), mime,
                project["revision"] if revision is None else revision, encoded(data), time.time()))
            if kind == "product" and not data.get("generated"):
                c.execute("UPDATE projects SET context_version=context_version+1,updated_at=? WHERE id=?", (time.time(), project_id))
                version = c.execute("SELECT context_version FROM projects WHERE id=?", (project_id,)).fetchone()["context_version"]
                # A later product image is more material for the current step.
                # Keep the open question or direction card, and keep its job answerable.
                pending = self._sync_pending_context(c, project_id, version)
                if pending:
                    self._event(c, project_id, pending[0]["job_id"], "gate", "商品图已保存，当前确认和已选方向仍然有效", "done")
            else:
                self._event(c, project_id, data.get("job_id"), "artifact", "已保存" + name, "done")
        return self.get_asset(aid)

    def get_asset(self, asset_id):
        with self._connection() as c:
            row = c.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError("素材不存在")
        return self._dict(row)

    def find_asset_by_path(self, path):
        with self._connection() as c:
            return self._dict(c.execute("SELECT * FROM assets WHERE path=?", (str(Path(path).resolve()),)).fetchone())

    def update_plan(self, project_id, plan, expected_revision=None, expected_context_version=None):
        with self._connection(True) as c:
            row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError("项目不存在")
            if expected_revision is not None and row["revision"] != expected_revision:
                raise StaleRevision("方案已修改，旧任务结果不会覆盖新版")
            if expected_context_version is not None and row["context_version"] != expected_context_version:
                raise StaleRevision("需求已更新，旧规划结果不会覆盖当前需求")
            brief = (plan.get("strategy") or {}).get("brief")
            if isinstance(brief, dict) and brief.get("ad_form") != row["ad_form"]:
                raise ValueError("方案须沿用当前广告倾向，请先按新偏好更新方案")
            self._validate_plan_duration(row, plan)
            revision, now = row["revision"] + 1, time.time()
            c.execute("INSERT INTO revisions VALUES(?,?,?,?)", (project_id, revision, encoded(plan), now))
            c.execute("UPDATE projects SET plan=?,revision=?,plan_context_version=context_version,status='awaiting_generate',updated_at=? WHERE id=?",
                      (encoded(plan), revision, now, project_id))
        return self.get_project(project_id)

    def create_job(self, project_id, kind, revision, payload=None):
        if kind not in ("plan", "generate", "recompose"):
            raise ValueError("不支持的任务类型")
        payload = copy.deepcopy(payload or {})
        with self._connection(True) as c:
            p = self._dict(c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
            if p is None:
                raise KeyError("项目不存在")
            if p["revision"] != revision:
                raise StaleRevision("项目已修改，请刷新后重试")
            if kind == "generate" and (p.get("plan") or {}).get("production") is not None:
                # A new cast decision includes generated reference sheets that
                # must share the existing picture confirmation before video.
                payload["pause_for_creative"] = True
            if kind == "generate" and self._pending_gate(c, project_id, "direction"):
                raise StaleRevision("请先选择当前创意方向，再确认方案")
            if kind == "generate" and "pause_for_creative" in payload:
                self._require_fresh_plan(c, p)
            if kind in ("plan", "generate"):
                statuses = ("queued", "running", "cancel_requested", "submitted_unknown")
                if kind == "generate":
                    # A repeated button press cannot allocate a fresh provider budget.
                    # Safe recovery keeps this job; regeneration requires a new revision.
                    statuses += ("ready", "failed", "partial", "canceled", "needs_review", "awaiting_input")
                query = "SELECT * FROM jobs WHERE project_id=? AND revision=? AND kind=? AND status IN (" + ",".join("?" for _ in statuses) + ") ORDER BY created_at DESC LIMIT 1"
                old = c.execute(query, (project_id, revision, kind, *statuses)).fetchone()
                if old:
                    return self._dict(old)
            if kind == "recompose":
                self._require_idle(c, project_id)
                self._require_fresh_plan(c, p)
                if self._pending_gate(c, project_id):
                    raise StaleRevision("请先完成当前问题或创意选择")
                if payload.get("base_render_id", p["current_render_id"]) != p["current_render_id"]:
                    raise StaleRevision("当前成片已改变，不能重新合成其他版本")
                base = self._dict(c.execute("SELECT * FROM renders WHERE id=?", (p["current_render_id"],)).fetchone())
                if not base or base["project_id"] != project_id or base["status"] != "ready":
                    raise ValueError("请先生成一条完整广告")
                if base["revision"] != revision:
                    raise StaleRevision("当前方案尚未出片，不能把旧成片当作当前方案重新合成")
                if not isinstance(p["plan"], dict):
                    raise ValueError("当前成片缺少可复用的方案")
                plan = copy.deepcopy(p["plan"])
                plan.pop("cta", None)
                self._validate_plan_duration(p, plan)
                revision += 1
                payload = {"base_render_id": base["id"], "plan": plan}
                c.execute("INSERT INTO revisions VALUES(?,?,?,?)", (project_id, revision, encoded(plan), time.time()))
                c.execute("UPDATE projects SET revision=?,plan=?,status='composing',updated_at=? WHERE id=?",
                          (revision, encoded(plan), time.time(), project_id))
            elif kind == "generate":
                if not p["plan"]:
                    raise ValueError("请先生成并确认方案")
                payload.setdefault("plan", p["plan"])
                self._validate_plan_duration(p, payload["plan"])
                sources = c.execute("SELECT payload FROM jobs WHERE project_id=? AND revision=? AND kind='plan' AND status='ready' ORDER BY created_at DESC", (project_id, revision)).fetchall()
                candidates = next((json.loads(s["payload"])["creative_options"] for s in sources if json.loads(s["payload"]).get("creative_options")), [])
                payload.setdefault("creative_options", candidates)
            elif kind == "plan" and self._pending_gate(c, project_id):
                raise StaleRevision("请先完成当前问题或创意选择，或在对话中补充新需求")
            payload.setdefault("context_version", p["context_version"])
            payload["duration_seconds"] = p["duration_seconds"]
            if kind == "generate":
                payload["video_submission_budget"] = len(payload["plan"]["segments"])
                payload["merchant_confirmed_revision"] = revision
            jid, now = identifier("job"), time.time()
            c.execute("INSERT INTO jobs(id,project_id,revision,kind,status,phase,progress,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (jid, project_id, revision, kind, "queued", "queued", "{}", encoded(payload), now, now))
            self._event(c, project_id, jid, "status", "任务已排队", "waiting")
        return self.get_job(jid)

    def create_continuity_upgrade(self, project_id, accepted_revision, source_job_id, request_id):
        """Save a new approved grouping and run atomically, preserving old receipts."""
        from .continuity import upgrade_continuity_plan
        from .schemas import Plan

        with self._connection(True) as c:
            prior = self._request_job(c, project_id, request_id, "continuity_upgrade")
            if prior:
                if (prior["payload"].get("source_job_id") != source_job_id or
                        prior["payload"].get("accepted_revision") != accepted_revision):
                    raise StaleRevision("该请求编号已用于另一版连续性生成")
                return prior
            p = self._project(c, project_id)
            if p["revision"] != accepted_revision:
                raise StaleRevision("方案已修改，请刷新后重新确认")
            self._require_idle(c, project_id)
            self._require_fresh_plan(c, p)
            if self._pending_gate(c, project_id):
                raise StaleRevision("请先完成当前图片或需求确认")
            source = self._dict(c.execute("SELECT * FROM jobs WHERE id=? AND project_id=?", (source_job_id, project_id)).fetchone())
            if not source:
                raise KeyError("任务不存在")
            self._guard_job(c, source_job_id)
            if source["kind"] != "generate" or source["status"] != "ready":
                raise StaleRevision("仅能升级当前已完成的视频版本")
            if (source["payload"].get("context_version") != p["context_version"] or
                    not source["payload"].get("creative_confirmed")):
                raise StaleRevision("缺少当前需求与图片的确认，不能直接生成")
            if c.execute("SELECT 1 FROM jobs WHERE project_id=? AND status='submitted_unknown'", (project_id,)).fetchone() or c.execute(
                    "SELECT 1 FROM steps s JOIN jobs j ON j.id=s.job_id WHERE j.project_id=? AND s.state IN ('submitting','submitted_unknown')", (project_id,)).fetchone():
                raise StaleRevision("存在受理状态未知的视频任务，请先核对原任务")
            base = self._dict(c.execute("SELECT * FROM renders WHERE id=? AND project_id=?", (p["current_render_id"], project_id)).fetchone())
            if not base or base["revision"] != accepted_revision or base["status"] != "ready":
                raise StaleRevision("当前版本尚无完整成片，不能作为连续性升级来源")
            scene_ids = self._verified_scene_ids(c, source)
            original_plan = Plan.model_validate(p["plan"]).model_dump()
            source_plan = Plan.model_validate(source["payload"]["plan"]).model_dump()
            for segment in source_plan["segments"]:
                segment["reference_asset_ids"] = [aid for aid in segment["reference_asset_ids"] if aid not in scene_ids]
            if source_plan != original_plan:
                raise StaleRevision("已完成任务与当前方案内容不同，已有内容仍保留。请在对话里说明要改的地方。")
            for segment in source["payload"]["plan"]["segments"]:
                step = self._dict(c.execute("SELECT * FROM steps WHERE job_id=? AND step_key=?", (source_job_id, "video:" + segment["id"])).fetchone())
                if not step or step["state"] != "downloaded" or not step["task_id"]:
                    raise StaleRevision("原任务回执尚未完成核对，不能重复生成")
            plan = upgrade_continuity_plan(original_plan)
            self._validate_plan_duration(p, plan)
            # References remain the exact merchant uploads; reuse scene bytes
            # only after verifying the original image-confirmation receipt.
            for segment in plan["segments"]:
                for aid in segment["reference_asset_ids"]:
                    asset = self._dict(c.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (aid, project_id)).fetchone())
                    if not asset or asset["kind"] != "product" or asset["metadata"].get("generated"):
                        raise ValueError("连续性升级只能复用已确认的原始商品图")
                    path = Path(asset["path"]).resolve()
                    if not path.is_relative_to(self.media_root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["metadata"].get("sha256"):
                        raise ValueError("商品原图缺失或已经变化，请重新确认")
            revision, jid, now = accepted_revision + 1, identifier("job"), time.time()
            run_plan = copy.deepcopy(plan)
            for segment in run_plan["segments"]:
                from app.planner_recovery import append_reference_ids
                segment["reference_asset_ids"] = append_reference_ids(segment["reference_asset_ids"], scene_ids)
            payload = {"plan": run_plan, "source_job_id": source_job_id, "accepted_revision": accepted_revision,
                       "base_render_id": base["id"], "context_version": p["context_version"],
                       "duration_seconds": p["duration_seconds"], "video_submission_budget": len(plan["segments"]),
                       "pause_for_creative": True, "creative_confirmed": True, "design_asset_ids": scene_ids,
                       "merchant_confirmed_revision": revision}
            c.execute("INSERT INTO revisions VALUES(?,?,?,?)", (project_id, revision, encoded(plan), now))
            c.execute("UPDATE projects SET revision=?,plan=?,plan_context_version=context_version,status='queued',updated_at=? WHERE id=?", (revision, encoded(plan), now, project_id))
            c.execute("INSERT INTO jobs(id,project_id,revision,kind,status,phase,progress,payload,created_at,updated_at) VALUES(?,?,?,'generate','queued','queued',?,?,?,?)",
                      (jid, project_id, revision, encoded({"design_asset_ids": scene_ids}), encoded(payload), now, now))
            c.execute("INSERT INTO turn_requests VALUES(?,?,?,?)", (project_id, request_id, "continuity_upgrade", jid))
            self._event(c, project_id, jid, "status", "已按连续性重新安排生成，复用确认过的图片", "waiting")
            self._message(c, project_id, "assistant", "已保留原版，将完整分镜和旁白合并为一次生成，改善跨段声音与画面衔接。", jid)
        return self.get_job(jid)

    def extend_plan_duration(self, job_id, seconds):
        """Grow the frozen duration for this planning turn. Does not bump context version."""
        if type(seconds) is not int or not 10 <= seconds <= 60:
            raise ValueError("时长须在10到60秒之间")
        with self._connection(True) as c:
            job, project = self._guard_job(c, job_id)
            if seconds <= project["duration_seconds"]:
                return job
            now = time.time()
            c.execute("UPDATE projects SET duration_seconds=?,updated_at=? WHERE id=?", (seconds, now, project["id"]))
            payload = job["payload"]
            payload["duration_seconds"] = seconds
            frozen = payload.get("frozen")
            if isinstance(frozen, dict) and isinstance(frozen.get("project"), dict):
                frozen["project"]["duration_seconds"] = seconds
            c.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?", (encoded(payload), now, job_id))
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self._connection() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("任务不存在")
        return self._dict(row)

    def retry_planning_job(self, project_id, job_id):
        """Requeue an unfinished text request without overwriting newer work.

        The status check, budget extension and enqueue share a write transaction:
        a duplicate click must never replay an earlier checkpoint over a worker's
        already-running request or a completed result.
        """
        from .planner_recovery import merchant_stop
        from .planning_session import (allow_manual_planning_retry, completed_storyboard_checkpoint,
                                       drop_storyboard_checkpoint, reopen_rejected_stage)

        with self._connection(True) as c:
            job = self._dict(c.execute("SELECT * FROM jobs WHERE id=? AND project_id=?",
                                       (job_id, project_id)).fetchone())
            if not job:
                raise KeyError("任务不存在")
            if job["kind"] != "plan" or job["status"] not in ("failed", "partial"):
                raise StaleRevision("当前策划无需重新尝试，请查看最新进度")
            self._guard_job(c, job_id)
            self._require_idle(c, project_id)
            progress = job.get("progress") or {}
            payload = dict(job.get("payload") or {})
            checkpoint = payload.get("planning_checkpoint")
            # Older jobs stored retryable=false for system failures. Recompute from
            # the current classification; only a genuine merchant stop stays blocked.
            if merchant_stop(progress.get("error_code"), job.get("error"),
                             progress.get("issue_summary"), progress.get("retry_action")):
                raise StaleRevision("本次方案需要调整，请通过输入框说明修改要求；已有成果仍保留")
            from .story_logic import planning_rerun_storyboard
            # The worker already chose the next checkpoint. A click after two
            # structure retries starts a new generation instead of replaying it.
            if progress.get("structure_retries_exhausted"):
                payload = {key: value for key, value in payload.items()
                           if key not in ("planning_checkpoint", "structure_auto_retries",
                                          "auto_retry_prepared", "content_auto_retries")}
            elif payload.get("auto_retry_prepared"):
                payload = {key: value for key, value in payload.items() if key != "auto_retry_prepared"}
            # A shot-level content failure, a voice mismatch, or a local template
            # is stored after the storyboard call already returned. Reopening only
            # the last dialogue rewrite would replay that board and skip review.
            elif planning_rerun_storyboard(job.get("error"), progress.get("issue_summary")):
                if isinstance(checkpoint, dict):
                    resumed = drop_storyboard_checkpoint(checkpoint)
                    payload = {**payload, "planning_checkpoint": resumed} if resumed is not None else {
                        key: value for key, value in payload.items() if key != "planning_checkpoint"}
                else:
                    payload = {key: value for key, value in payload.items() if key != "planning_checkpoint"}
            # The plan was rejected after storyboard and review had both been saved.
            # Replay those results instead of opening a new job from the brief.
            elif (progress.get("error_code") == "planning_invalid_response"
                    and progress.get("retry_action") in ("edit_plan", "retry_planning")
                    and not payload.get("plan_structure_replayed")
                    and completed_storyboard_checkpoint(checkpoint)):
                payload = {**payload, "plan_structure_replayed": True}
            elif not isinstance(checkpoint, dict) or checkpoint.get("version") != 1:
                # The card already offered another attempt. Start this job again
                # from the saved brief instead of refusing before any event.
                payload = {key: value for key, value in payload.items() if key != "planning_checkpoint"}
            else:
                try:
                    checkpoint = allow_manual_planning_retry(checkpoint)
                except (KeyError, TypeError, ValueError):
                    checkpoint = reopen_rejected_stage(checkpoint)
                    if checkpoint is None:
                        raise StaleRevision("当前策划进度不能重新尝试，请通过输入框说明修改要求；已有成果仍保留") from None
                payload = {**payload, "planning_checkpoint": checkpoint}
            progress = {key: value for key, value in progress.items()
                        if key not in ("error_code", "retryable", "retry_action", "issue_summary")}
            now = time.time()
            c.execute("UPDATE jobs SET status='queued',phase='queued',error=NULL,payload=?,progress=?,lease_until=0,updated_at=? WHERE id=?",
                      (encoded(payload), encoded(progress), now, job_id))
            c.execute("UPDATE projects SET status='queued',updated_at=? WHERE id=?", (now, project_id))
            self._event(c, project_id, job_id, "status", "策划已重新排队，将继续未完成的步骤", "waiting")
            return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def resume_saved_planning(self, project_id):
        """Start one new text job from the saved brief and any chosen direction.

        This is the last-resort control. It does not submit image or video work
        and does not ask the merchant to type the same request again.
        """
        from .strategy import DirectionCandidates

        with self._connection(True) as c:
            self._require_idle(c, project_id)
            p = self._project(c, project_id)
            detail = self._detail(c, project_id)
            extra = {"planning_requested": True}
            for gate in reversed(detail["gates"]):
                if gate.get("kind") != "direction" or gate.get("status") != "answered":
                    continue
                if not gate.get("selected_option_id"):
                    continue
                source = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (gate["job_id"],)).fetchone())
                raw = ((source or {}).get("payload") or {}).get("direction_candidates")
                if not isinstance(raw, dict):
                    break
                try:
                    strategy = DirectionCandidates.model_validate(raw).select(gate["selected_option_id"])
                except (KeyError, TypeError, ValueError):
                    break
                extra["selected_strategy"] = strategy.model_dump()
                trace = ((source.get("payload") or {}).get("decision") or {}).get("skill_trace")
                if trace:
                    extra["inherited_skill_trace"] = trace
                break
            jid = self._new_conversation_job(c, p, extra)
            self._event(c, project_id, jid, "status", "按已保存的需求继续策划", "waiting")
            return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def create_failed_video_retry(self, project_id, source_job_id):
        """Allocate one explicit retry budget; never erase or recycle old receipts."""
        with self._connection(True) as c:
            source = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (source_job_id,)).fetchone())
            if not source or source["project_id"] != project_id:
                raise KeyError("任务不存在")
            if source["kind"] != "generate":
                raise ValueError("只有视频制作任务可以重试失败片段")
            _, project = self._guard_job(c, source_job_id)
            request_id = "failed-video-retry:" + source_job_id
            existing = self._request_job(c, project_id, request_id, "retry_failed_video")
            if existing:
                return existing
            if source["status"] not in ("failed", "partial"):
                raise StaleRevision("当前任务不能重试失败片段")
            self._require_idle(c, project_id)
            self._require_fresh_plan(c, project)
            if self._pending_gate(c, project_id):
                raise StaleRevision("请先完成当前图片或需求确认")
            payload = copy.deepcopy(source["payload"])
            if payload.get("context_version") != project["context_version"]:
                raise StaleRevision("需求已更新，请使用当前方案")
            plan = payload.get("plan")
            self._validate_plan_duration(project, plan)
            if payload.get("pause_for_creative") and not payload.get("creative_confirmed"):
                raise StaleRevision("请先确认图片，不能跳过确认重新生成")
            design_ids = self._verified_scene_ids(c, source)
            payload["design_asset_ids"] = design_ids
            failed, reusable, frozen_video_requests = [], [], {}
            frozen_video_specs, frozen_video_assets = {}, {}
            for segment in plan["segments"]:
                key = "video:" + segment["id"]
                step = self._dict(c.execute("SELECT * FROM steps WHERE job_id=? AND step_key=?", (source_job_id, key)).fetchone())
                if not step:
                    raise StaleRevision("有片段缺少提交记录，不能作为已失败片段重新提交")
                if step["state"] in ("submitting", "submitted_unknown"):
                    raise StaleRevision("存在受理状态未知的片段，请先核对原任务，不能重新提交")
                if step["state"] in ("failed", "rejected"):
                    failed.append(segment["id"])
                    prepared = (step.get("snapshot") or {}).get("video_request")
                    if prepared is not None:
                        frozen_video_requests[segment["id"]] = copy.deepcopy(prepared)
                        # Keep the input contract frozen with its compiled prompt.
                        # New compiler fields must not change a historical retry's hash.
                        snapshot = step["snapshot"]
                        if isinstance(snapshot.get("segment"), dict):
                            frozen_video_specs[segment["id"]] = copy.deepcopy(snapshot["segment"])
                            frozen_video_assets[segment["id"]] = [
                                {"id": asset["id"], "sha256": asset["sha256"]}
                                for asset in snapshot.get("assets", [])]
                elif step["state"] in ("submitted", "downloaded") and step["task_id"]:
                    if step["state"] == "downloaded":
                        asset_id = (step.get("result") or {}).get("asset_id")
                        asset = c.execute("SELECT project_id FROM assets WHERE id=?", (asset_id,)).fetchone()
                        if not asset or asset["project_id"] != project_id:
                            raise ValueError("已完成片段的素材记录无效")
                    reusable.append(step)
                else:
                    raise StaleRevision("片段回执不完整，请先核对原任务")
            if not failed:
                raise StaleRevision("没有明确失败的片段，请恢复原任务查询或下载")
            for key in ("manifest", "resume_known_tasks_only", "failed_voice_script_hash"):
                payload.pop(key, None)
            payload.update(source_job_id=source_job_id, merchant_confirmed_revision=source["revision"],
                           retry_segment_ids=failed,
                           frozen_video_requests=frozen_video_requests,
                           frozen_video_specs=frozen_video_specs, frozen_video_assets=frozen_video_assets,
                           video_submission_budget=len(failed))
            jid, now = identifier("job"), time.time()
            progress = {"retrying_segment_ids": failed, "reused_segments": len(reusable),
                        "design_asset_ids": list(design_ids)}
            c.execute("INSERT INTO jobs(id,project_id,revision,kind,status,phase,progress,payload,created_at,updated_at) VALUES(?,?,?,'generate','queued','queued',?,?,?,?)",
                      (jid, project_id, source["revision"], encoded(progress), encoded(payload), now, now))
            for step in reusable:
                c.execute("INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?)", (jid, step["step_key"], step["state"],
                          encoded(step["snapshot"]), step["task_id"], encoded(step["result"]), None, now, now))
            c.execute("INSERT INTO turn_requests VALUES(?,?,?,?)", (project_id, request_id, "retry_failed_video", jid))
            c.execute("UPDATE projects SET status='queued',updated_at=? WHERE id=?", (now, project_id))
            self._event(c, project_id, jid, "status", "仅重试已失败片段，其他图片与任务继续复用", "waiting")
        return self.get_job(jid)

    def retry_generate_job(self, project_id, job_id):
        """Queue the same generate job again after the merchant asks.

        Account and balance refusals drop only the unpaid step that never
        received a task id. Confirmed plan, revision and saved pictures stay.
        A step that may already have been accepted is not posted again.
        """
        from .generate_retry import ACCOUNT_RETRY_CODES

        with self._connection(True) as c:
            job = self._dict(c.execute("SELECT * FROM jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone())
            if not job:
                raise KeyError("任务不存在")
            if job["kind"] != "generate":
                raise ValueError("只有视频制作任务可以这样恢复")
            if job["status"] == "submitted_unknown":
                raise StaleRevision("平台接单状态未知，不能自动重新提交；请核对平台任务以避免重复扣费")
            if job["status"] not in ("failed", "partial"):
                raise StaleRevision("当前任务无需恢复")
            code = (job.get("progress") or {}).get("error_code")
            if code == "voice_too_long":
                raise StaleRevision("配音文案过长，请先精简文案后重新确认；直接恢复同样文案不能解决时长问题，已保存的图片会保留")
            project = self._project(c, project_id)
            if project["revision"] != job["revision"]:
                raise StaleRevision("这是旧版本任务，请使用当前方案")
            self._guard_job(c, job_id)
            self._require_idle(c, project_id)
            if code in ACCOUNT_RETRY_CODES:
                return self._requeue_account_generate(c, job)
            return self._requeue_known_generate(c, job)

    def _requeue_known_generate(self, c, job):
        steps = self._job_steps(c, job["id"])
        failed, pending, uncertain = [], [], []
        for step in steps:
            state = step.get("state")
            if state in ("submitting", "submitted_unknown"):
                uncertain.append(step["step_key"])
            elif state in ("failed", "rejected"):
                failed.append(step["step_key"])
            elif state == "submitted" and step.get("task_id"):
                pending.append(step["step_key"])
            elif state != "downloaded":
                uncertain.append(step["step_key"])
        if uncertain:
            raise StaleRevision("存在受理状态未知的片段，请先核对原任务")
        if failed and not pending:
            raise StaleRevision("原任务已有明确失败片段，请选择重试失败片段；恢复任务不会重新提交")
        if failed and pending:
            payload = {**(job.get("payload") or {}), "resume_known_tasks_only": True}
            c.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?", (encoded(payload), time.time(), job["id"]))
        return self._mark_generate_queued(c, job["id"], job["project_id"], job.get("progress") or {}, clear_error_fields=False)

    def has_open_generate_failure(self, project_id):
        """True when a short retry sentence should be handled here, not by the planner."""
        with self._connection() as c:
            project = self._project(c, project_id)
            if self._pending_gate(c, project_id):
                return False
            job = self._latest_generate(c, project_id, project["revision"])
            return bool(job and job["status"] in ("failed", "partial", "submitted_unknown"))

    def _latest_generate(self, c, project_id, revision):
        return self._dict(c.execute(
            "SELECT * FROM jobs WHERE project_id=? AND revision=? AND kind='generate' ORDER BY created_at DESC LIMIT 1",
            (project_id, revision)).fetchone())

    def _job_steps(self, c, job_id):
        return [self._dict(row) for row in c.execute(
            "SELECT * FROM steps WHERE job_id=? ORDER BY created_at", (job_id,))]

    def _requeue_account_generate(self, c, job):
        from .generate_retry import account_step_blocker, cleared_progress, unpaid_account_step

        code = (job.get("progress") or {}).get("error_code")
        steps = self._job_steps(c, job["id"])
        blocker = account_step_blocker(steps, code)
        if blocker == "unknown":
            raise StaleRevision("存在受理状态未知的片段，请先核对原任务")
        if blocker == "paid_failure":
            raise StaleRevision("原任务已有明确失败片段，请选择重试失败片段；恢复任务不会重新提交")
        image_blocker = self._account_image_blocker(job)
        if image_blocker == "unknown":
            raise StaleRevision("存在受理状态未知的图片任务，请先核对原任务")
        if image_blocker == "image":
            raise StaleRevision("图片没有生成成功，请修改方案后再试；已有图片仍保留")
        released = 0
        for step in steps:
            if unpaid_account_step(step, code):
                c.execute("DELETE FROM steps WHERE job_id=? AND step_key=?", (job["id"], step["step_key"]))
                released += 1
        self._drop_unpaid_account_receipts(job)
        now = time.time()
        c.execute("UPDATE jobs SET submission_count=MAX(0, submission_count-?) WHERE id=?", (released, job["id"]))
        self._event(c, job["project_id"], job["id"], "status", "已重新排队，沿用已确认的方案和图片", "waiting")
        return self._mark_generate_queued(c, job["id"], job["project_id"], cleared_progress(job.get("progress")), clear_error_fields=True, now=now)

    def _mark_generate_queued(self, c, job_id, project_id, progress, clear_error_fields, now=None):
        now = now or time.time()
        if clear_error_fields:
            c.execute("UPDATE jobs SET status='queued',phase='queued',error=NULL,progress=?,lease_until=0,updated_at=? WHERE id=?",
                      (encoded(progress), now, job_id))
        else:
            c.execute("UPDATE jobs SET status='queued',error=NULL,lease_until=0,updated_at=? WHERE id=?", (now, job_id))
        c.execute("UPDATE projects SET status='queued',updated_at=? WHERE id=?", (now, project_id))
        return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def _design_receipt_dir(self, job):
        payload = job.get("payload") or {}
        raw = payload.get("designs_directory")
        root = (self.media_root / job["project_id"] / job["id"]).resolve()
        folder = Path(raw).resolve() if isinstance(raw, str) and raw else (root / "designs")
        try:
            if not folder.is_relative_to(root):
                return None
        except ValueError:
            return None
        return folder if folder.is_dir() else None

    def _account_image_blocker(self, job):
        from .generate_retry import ACCOUNT_RETRY_CODES

        if (job.get("payload") or {}).get("creative_confirmed"):
            return None
        folder = self._design_receipt_dir(job)
        if folder is None:
            return None
        for path in folder.glob("*_task.json"):
            receipt = self._read_receipt(path)
            if receipt is None:
                return "unknown"
            state = receipt.get("state")
            if state in ("submitting", "submitted_unknown") and not receipt.get("task_id"):
                return "unknown"
            if state != "failed":
                continue
            if not receipt.get("task_id") and receipt.get("error_code") in ACCOUNT_RETRY_CODES:
                continue
            return "image"
        return None

    @staticmethod
    def _read_receipt(path):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        return receipt if isinstance(receipt, dict) else None

    def _drop_unpaid_account_receipts(self, job):
        from .generate_retry import ACCOUNT_RETRY_CODES

        folder = self._design_receipt_dir(job)
        if folder is None:
            return
        for path in folder.glob("*_task.json"):
            receipt = self._read_receipt(path)
            if not isinstance(receipt, dict):
                continue
            if (receipt.get("state") == "failed" and not receipt.get("task_id")
                    and receipt.get("error_code") in ACCOUNT_RETRY_CODES):
                path.unlink(missing_ok=True)

    def answer_generate_retry_turn(self, project_id, body):
        """Queue a failed video run from a short chat sentence, or say that nothing was queued."""
        from .generate_retry import (QUEUED_REPLY, RESUME_REPLY, SEGMENTS_REPLY, classify_generate_retry,
                                    is_generate_retry_intent, no_queue, refusal_reply)

        if not is_generate_retry_intent(body.get("content") or ""):
            return None
        if body.get("reply_to_gate_id") or body.get("answers") or body.get("reference_asset_ids") or body.get("ad_form"):
            return None
        action = None
        source_id = None
        content = str(body.get("content") or "").strip()
        turn_id = body.get("client_turn_id")
        with self._connection(True) as c:
            prior = self._request_job(c, project_id, turn_id, "turn")
            if prior:
                return prior
            project = self._project(c, project_id)
            if body.get("expected_context_version") != project["context_version"]:
                raise StaleRevision("需求已变化，请刷新后提交")
            self._require_idle(c, project_id)
            if self._pending_gate(c, project_id):
                return None
            job = self._latest_generate(c, project_id, project["revision"])
            if not job or job["status"] not in ("failed", "partial", "submitted_unknown"):
                return None
            action, reason = classify_generate_retry(job, self._job_steps(c, job["id"]))
            source_id = job["id"]
            if action == "refuse":
                self._record_retry_chat(c, project_id, source_id, content, refusal_reply(reason), turn_id)
                return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (source_id,)).fetchone())
            if action != "segments":
                try:
                    target = (self._requeue_account_generate(c, job) if action == "account"
                              else self._requeue_known_generate(c, job))
                    reply = RESUME_REPLY if action == "resume" else QUEUED_REPLY
                except StaleRevision as exc:
                    target, reply = job, no_queue(str(exc))
                self._record_retry_chat(c, project_id, target["id"], content, reply, turn_id)
                return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (target["id"],)).fetchone())
        reply = SEGMENTS_REPLY
        try:
            target = self.create_failed_video_retry(project_id, source_id)
        except (StaleRevision, ValueError, KeyError) as exc:
            target = None
            reply = no_queue(str(exc))
        with self._connection(True) as c:
            prior = self._request_job(c, project_id, turn_id, "turn")
            if prior:
                return prior
            job_id = target["id"] if target is not None else source_id
            self._record_retry_chat(c, project_id, job_id, content, reply, turn_id)
            return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def _record_retry_chat(self, c, project_id, job_id, content, reply, turn_id):
        self._message(c, project_id, "user", content, job_id)
        self._message(c, project_id, "assistant", reply, job_id)
        c.execute("INSERT INTO turn_requests VALUES(?,?,?,?)", (project_id, turn_id, "turn", job_id))

    def update_job(self, job_id, **changes):
        allowed = {"status", "phase", "error", "progress", "payload", "revision", "lease_until"}
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("无效任务字段")
        changes["updated_at"] = time.time()
        values = [encoded(v) if k in ("progress", "payload") else v for k, v in changes.items()]
        with self._connection(True) as c:
            old = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not old:
                raise KeyError("任务不存在")
            c.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in changes) + " WHERE id=?", (*values, job_id))
            status = changes.get("status", old["status"])
            if status != old["status"] and status in ("ready", "failed", "partial", "canceled", "needs_review", "submitted_unknown"):
                label = {"ready": "本次任务已完成", "failed": "任务需要处理", "partial": "任务部分完成", "canceled": "任务已停止", "needs_review": "旧结果需要复核", "submitted_unknown": "提交结果需要核对"}[status]
                self._event(c, old["project_id"], job_id, "terminal", label, "done" if status == "ready" else "canceled" if status == "canceled" else "failed")
            elif changes.get("phase", old["phase"]) != old["phase"]:
                self._event(c, old["project_id"], job_id, "step", PHASE_LABELS.get(changes["phase"], "正在执行制作步骤"), "running")
        return self.get_job(job_id)

    def claim_job(self, job_id, max_active_runs=1, lease_seconds=1200):
        now = time.time()
        with self._connection(True) as c:
            j = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not j:
                raise KeyError("任务不存在")
            if j["status"] not in ("queued", "failed", "partial") or j["cancel_requested"]:
                return False
            active = c.execute("SELECT count(*) FROM jobs WHERE status='running' AND lease_until>?", (now,)).fetchone()[0]
            if active >= max_active_runs:
                return False
            c.execute("UPDATE jobs SET status='running',error=NULL,lease_until=?,updated_at=?,started_at=COALESCE(started_at,?) WHERE id=?", (now + lease_seconds, now, now, job_id))
            self._event(c, j["project_id"], job_id, "status", "开始执行任务", "running")
            return True

    def set_project_status(self, project_id, revision, status):
        with self._connection(True) as c:
            c.execute("UPDATE projects SET status=?,updated_at=? WHERE id=? AND revision=?", (status, time.time(), project_id, revision))

    def request_cancel(self, project_id):
        with self._connection(True) as c:
            affected = c.execute("SELECT * FROM jobs WHERE project_id=? AND status IN ('queued','running','awaiting_input')", (project_id,)).fetchall()
            c.execute("UPDATE jobs SET cancel_requested=1,status=CASE WHEN status IN ('queued','awaiting_input') THEN 'canceled' ELSE 'cancel_requested' END,updated_at=? WHERE project_id=? AND status IN ('queued','running','awaiting_input')", (time.time(), project_id))
            c.execute("UPDATE gates SET status='superseded',answered_at=? WHERE project_id=? AND status='pending'", (time.time(), project_id))
            for j in affected:
                self._abandon_speculative_plan(c, self._dict(j))
                self._event(c, project_id, j["id"], "terminal" if j["status"] != "running" else "status", "任务已停止" if j["status"] != "running" else "已请求停止后续工作", "canceled" if j["status"] != "running" else "waiting")
            row = c.execute("SELECT 1 FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.project_id=? AND j.revision=p.revision AND j.status='cancel_requested'", (project_id,)).fetchone()
            canceled = c.execute("SELECT 1 FROM jobs j JOIN projects p ON p.id=j.project_id WHERE j.project_id=? AND j.revision=p.revision AND j.status='canceled'", (project_id,)).fetchone()
            if row or canceled:
                c.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?",
                          ("cancel_requested" if row else "canceled", time.time(), project_id))
        from app.speculative_plan import cancel_speculation
        cancel_speculation(project_id)

    def is_cancelled(self, job_id):
        j = self.get_job(job_id)
        return bool(j["cancel_requested"] or j["status"] in ("canceled", "cancel_requested"))

    def active_jobs(self):
        with self._connection() as c:
            return [self._dict(r) for r in c.execute("SELECT * FROM jobs WHERE status IN ('queued','running','cancel_requested') ORDER BY created_at")]

    def recover_jobs(self):
        with self._connection(True) as c:
            rows = c.execute("SELECT * FROM jobs WHERE status IN ('running','cancel_requested')").fetchall()
            for row in rows:
                unresolved = c.execute("SELECT 1 FROM steps WHERE job_id=? AND state IN ('submitting','submitted_unknown') AND task_id IS NULL", (row["id"],)).fetchone()
                status = "submitted_unknown" if unresolved else ("canceled" if row["cancel_requested"] else "queued")
                c.execute("UPDATE jobs SET status=?,lease_until=0,updated_at=? WHERE id=?", (status, time.time(), row["id"]))
                if unresolved:
                    c.execute("UPDATE steps SET state='submitted_unknown' WHERE job_id=? AND state='submitting'", (row["id"],))
        return [j for j in self.active_jobs() if j["status"] == "queued"]

    def get_step(self, job_id, step_key):
        with self._connection() as c:
            return self._dict(c.execute("SELECT * FROM steps WHERE job_id=? AND step_key=?", (job_id, step_key)).fetchone())

    def video_retry_state(self, job_id):
        """Safe action metadata computed from durable receipts, not error prose."""
        job = self.get_job(job_id)
        segments = job.get("payload", {}).get("plan", {}).get("segments", [])
        failed, pending, missing, uncertain, codes = [], [], [], [], []
        for segment in segments:
            sid = segment["id"]
            step = self.get_step(job_id, "video:" + sid)
            if not step:
                missing.append(sid)
            elif step["state"] in ("failed", "rejected"):
                failed.append(sid)
                codes.append((step.get("result") or {}).get("error_code"))
            elif step["state"] == "submitted" and step["task_id"]:
                pending.append(sid)
            elif step["state"] != "downloaded":
                uncertain.append(sid)
        action = "resume" if pending else "retry_failed_segments" if failed and not missing and not uncertain else None
        if uncertain:
            action = None
        return {"failed_segment_ids": failed, "pending_segment_ids": pending,
                "missing_segment_ids": missing, "uncertain_segment_ids": uncertain,
                "retry_action": action,
                "error_code": "reference_image_unreadable" if "reference_image_unreadable" in codes else "video_generation_failed"}

    def reserve_submission(self, job_id, step_key, snapshot, max_submissions):
        """Persist uncertainty BEFORE POST. An existing step is never resubmitted."""
        with self._connection(True) as c:
            j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            old = c.execute("SELECT * FROM steps WHERE job_id=? AND step_key=?", (job_id, step_key)).fetchone()
            if old:
                return False
            if j["cancel_requested"] or j["status"] != "running":
                raise ValueError("任务已取消或尚未领取")
            if j["kind"] != "generate":
                raise ValueError("当前任务不允许提交视频生成")
            p = c.execute("SELECT revision,context_version,duration_seconds FROM projects WHERE id=?", (j["project_id"],)).fetchone()
            payload = json.loads(j["payload"])
            if payload.get("retry_segment_ids") is not None and step_key not in {
                    "video:" + sid for sid in payload["retry_segment_ids"]}:
                raise ValueError("本次只批准重试已失败的视频片段")
            context = payload.get("context_version")
            if p["revision"] != j["revision"] or context is not None and context != p["context_version"]:
                raise StaleRevision("方案已改变，停止旧版后续生成")
            segments = payload.get("plan", {}).get("segments", [])
            budget = min(max_submissions, len(segments), payload.get("video_submission_budget", 6))
            if j["submission_count"] >= budget:
                raise ValueError("本次视频提交额度已用尽；不能自动重新生成")
            if step_key not in {"video:" + segment["id"] for segment in segments}:
                raise ValueError("视频提交必须属于已确认方案")
            now = time.time()
            c.execute("INSERT INTO steps VALUES(?,?, 'submitting',?,NULL,NULL,NULL,?,?)", (job_id, step_key, encoded(snapshot), now, now))
            c.execute("UPDATE jobs SET submission_count=submission_count+1,updated_at=? WHERE id=?", (now, job_id))
        return True

    def update_step(self, job_id, step_key, **changes):
        allowed = {"state", "task_id", "result", "error"}
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("无效步骤字段")
        changes["updated_at"] = time.time()
        values = [encoded(v) if k == "result" else v for k, v in changes.items()]
        with self._connection(True) as c:
            c.execute("UPDATE steps SET " + ",".join(f"{k}=?" for k in changes) + " WHERE job_id=? AND step_key=?", (*values, job_id, step_key))

    def get_render(self, render_id):
        with self._connection() as c:
            r = self._dict(c.execute("SELECT * FROM renders WHERE id=?", (render_id,)).fetchone())
        if not r:
            raise KeyError("成片不存在")
        return r

    def publish_render(self, job_id, asset_id, manifest):
        """Commit the render and current pointer together; late/canceled output stays historical."""
        with self._connection(True) as c:
            j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            p = c.execute("SELECT * FROM projects WHERE id=?", (j["project_id"],)).fetchone()
            context = json.loads(j["payload"]).get("context_version")
            current = p["revision"] == j["revision"] and not j["cancel_requested"] and (context is None or context == p["context_version"])
            rid, now = identifier("render"), time.time()
            status = "ready" if current else "needs_review"
            c.execute("INSERT INTO renders VALUES(?,?,?,?,?,?,?)", (rid, j["project_id"], asset_id, j["revision"], status, encoded(manifest), now))
            if current:
                c.execute("UPDATE projects SET current_render_id=?,status='ready',updated_at=? WHERE id=?", (rid, now, p["id"]))
            c.execute("UPDATE jobs SET status=?,phase='complete',lease_until=0,updated_at=? WHERE id=?", ("canceled" if j["cancel_requested"] else status, now, job_id))
            self._event(c, j["project_id"], job_id, "artifact", "广告成片已保存", "done")
            self._event(c, j["project_id"], job_id, "terminal", "本次任务已完成" if current else "结果已保存为历史版本", "done" if current else "canceled")
        return self.get_render(rid)

    def _project(self, c, project_id):
        value = self._dict(c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
        if value is None:
            raise KeyError("项目不存在")
        return self._project_result_state(c, value)

    @staticmethod
    def _project_result_state(c, project):
        """Separate a published result from ongoing work without rewriting history.

        ``status`` remains the workflow contract used by existing clients. These
        derived fields also handle older projects whose latest edit failed while
        their published render is still ready; no data migration is necessary.
        Never promote a historical or late render just because it exists.
        """
        render = c.execute(
            "SELECT revision FROM renders WHERE id=? AND project_id=? AND status='ready'",
            (project["current_render_id"], project["id"]),
        ).fetchone() if project["current_render_id"] else None
        project["work_status"] = project["status"]
        project["result_status"] = "ready" if render else "none"
        project["result_revision"] = render["revision"] if render else None
        return project

    @staticmethod
    def _validate_plan_duration(project, plan):
        from .schemas import segment_durations
        from .generation_policy import validate_generation_durations
        expected = segment_durations(project["duration_seconds"])
        segments = plan.get("segments") if isinstance(plan, dict) else None
        if not isinstance(segments, list) or any(not isinstance(s, dict) for s in segments):
            raise ValueError("方案必须包含完整视频分段")
        actual = [s.get("duration_seconds") for s in segments]
        strategy = plan.get("generation_strategy", "legacy_10s")
        if strategy == "continuity_v1":
            validate_generation_durations(actual, strategy)
            if not 1 <= len(actual) <= 6 or any(type(duration) is not int or not 2 <= duration <= 30 for duration in actual) or sum(actual) != project["duration_seconds"]:
                raise ValueError(f"方案总长必须为所选{project['duration_seconds']}秒，每组连续视频为2至30秒")
            return
        if strategy != "legacy_10s":
            raise ValueError("不支持的视频生成策略")
        if any(type(duration) is not int for duration in actual) or actual != expected:
            raise ValueError(f"方案总长必须为所选{project['duration_seconds']}秒，按10秒分段并保留末段余数")

    def _detail(self, c, project_id):
        result = {"project": self._project(c, project_id)}
        for table in ("assets", "messages", "jobs", "renders", "gates"):
            result[table] = [self._dict(r) for r in c.execute(f"SELECT * FROM {table} WHERE project_id=? ORDER BY created_at,rowid", (project_id,))]
        self._attach_media_skill_usage(c, project_id, result)
        result["events"] = [dict(r) for r in c.execute("SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,))]
        result["event_cursor"] = result["events"][-1]["seq"] if result["events"] else 0
        return result

    def _attach_media_skill_usage(self, c, project_id, detail):
        """Read historical evidence without inventing events or execution times."""
        jobs = {job["id"]: job for job in detail["jobs"] if job["kind"] == "generate"}

        def collect(job_id, traces, stage):
            if not isinstance(job_id, str) or job_id not in jobs or not isinstance(traces, list):
                return
            for trace in traces:
                safe = _public_media_skill(trace, stage)
                if safe is not None:
                    usage = jobs[job_id].setdefault("media_skill_usage", [])
                    if safe not in usage:
                        usage.append(safe)

        for job in jobs.values():
            payload = job.get("payload") or {}
            collect(job["id"], payload.get("media_skill_trace"), "asset_prompts")
        for asset in detail["assets"]:
            metadata = asset.get("metadata") or {}
            request = metadata.get("design_request")
            if asset["kind"] in ("scene", "character") and metadata.get("generated") and isinstance(request, dict):
                collect(metadata.get("job_id"), request.get("skill_trace"), "asset_prompts")
        for row in c.execute("SELECT s.job_id,s.snapshot FROM steps s JOIN jobs j ON j.id=s.job_id "
                             "WHERE j.project_id=? AND j.kind='generate' AND s.step_key LIKE 'video:%' "
                             "ORDER BY s.created_at,s.rowid", (project_id,)):
            try:
                snapshot = json.loads(row["snapshot"] or "{}")
            except (TypeError, ValueError):
                continue
            request = snapshot.get("video_request") if isinstance(snapshot, dict) else None
            if isinstance(request, dict):
                collect(row["job_id"], request.get("skill_trace"), "video_prompts")

    def _event(self, c, project_id, job_id, kind, label, state="done"):
        if kind not in ("status", "step", "message", "gate", "artifact", "terminal") or state not in ("running", "done", "waiting", "failed", "canceled"):
            raise ValueError("事件类型无效")
        cursor = c.execute("INSERT INTO events(project_id,job_id,kind,label,state,created_at) VALUES(?,?,?,?,?,?)", (project_id, job_id, kind, str(label)[:500], state, time.time()))
        return dict(c.execute("SELECT * FROM events WHERE seq=?", (cursor.lastrowid,)).fetchone())

    def add_event(self, project_id, job_id, kind, label, state="done"):
        with self._connection(True) as c:
            self._project(c, project_id)
            return self._event(c, project_id, job_id, kind, label, state)

    def merge_speculative_plan(self, job_id, updates):
        """Update only the speculative text-plan record inside one write transaction.

        A job that has already left direction selection, or whose record was
        abandoned, is left unchanged so a late writer cannot replace the
        continuation pointer or the chosen direction's cache.
        """
        allowed = {"direction_id", "status", "entries"}
        if not isinstance(updates, dict) or not updates or not set(updates).issubset(allowed):
            raise ValueError("预生成记录无效")
        if "status" in updates and updates["status"] not in ("running", "ready", "failed"):
            raise ValueError("预生成记录无效")
        if "entries" in updates and not isinstance(updates["entries"], dict):
            raise ValueError("预生成记录无效")
        with self._connection(True) as c:
            job = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if not job:
                raise KeyError("任务不存在")
            payload = dict(job.get("payload") or {})
            spec = dict(payload.get("speculative_plan") or {})
            if (job["status"] != "awaiting_input" or job["phase"] != "awaiting_direction"
                    or spec.get("abandoned") is True):
                return job
            if (spec.get("direction_id") and updates.get("direction_id")
                    and spec["direction_id"] != updates["direction_id"]):
                return job
            spec.update(updates)
            payload["speculative_plan"] = spec
            c.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?", (encoded(payload), time.time(), job_id))
            return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs_awaiting_direction(self):
        with self._connection() as c:
            return [self._dict(row) for row in c.execute(
                "SELECT * FROM jobs WHERE status='awaiting_input' AND phase='awaiting_direction' ORDER BY created_at")]

    def _abandon_speculative_plan(self, c, job):
        payload = dict(job.get("payload") or {})
        if not isinstance(payload.get("speculative_plan"), dict):
            return payload
        spec = dict(payload["speculative_plan"])
        if spec.get("abandoned") is True:
            return payload
        spec["abandoned"] = True
        payload["speculative_plan"] = spec
        c.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?", (encoded(payload), time.time(), job["id"]))
        job["payload"] = payload
        return payload

    def events_after(self, project_id, after=0, limit=200):
        with self._connection() as c:
            self._project(c, project_id)
            events = [dict(r) for r in c.execute("SELECT * FROM events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?", (project_id, after, min(500, max(1, limit))))]
        return {"events": events, "cursor": events[-1]["seq"] if events else after}

    def _message(self, c, project_id, role, content, job_id=None):
        mid, now = identifier("message"), time.time()
        c.execute("INSERT INTO messages(id,project_id,role,content,created_at,job_id) VALUES(?,?,?,?,?,?)", (mid, project_id, role, content, now, job_id))
        self._event(c, project_id, job_id, "message", "需求已保存" if role == "user" else "收到助手回复", "done")
        return {"id": mid, "project_id": project_id, "role": role, "content": content, "created_at": now, "job_id": job_id}

    def _sync_pending_context(self, c, project_id, context_version):
        """Keep an open card on the new brief version without canceling its job."""
        pending = c.execute("SELECT id,job_id FROM gates WHERE project_id=? AND status='pending'", (project_id,)).fetchall()
        now = time.time()
        for gate in pending:
            c.execute("UPDATE gates SET context_version=? WHERE id=?", (context_version, gate["id"]))
            job = c.execute("SELECT id,status,payload FROM jobs WHERE id=?", (gate["job_id"],)).fetchone()
            if job and job["status"] == "awaiting_input" and job["payload"]:
                payload = json.loads(job["payload"])
                payload["context_version"] = context_version
                c.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?", (encoded(payload), now, job["id"]))
        return pending

    def _pending_gate(self, c, project_id, kind=None):
        query, values = "SELECT * FROM gates WHERE project_id=? AND status='pending'", [project_id]
        if kind:
            query += " AND kind=?"
            values.append(kind)
        return self._dict(c.execute(query + " ORDER BY created_at DESC LIMIT 1", values).fetchone())

    def pending_gate(self, project_id, kind=None):
        with self._connection() as c:
            return self._pending_gate(c, project_id, kind)

    def _require_idle(self, c, project_id):
        if c.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running','cancel_requested')", (project_id,)).fetchone():
            raise StaleRevision("当前任务仍在进行，请先停止或等待完成")

    def _require_fresh_plan(self, c, p):
        if self._pending_gate(c, p["id"], "direction"):
            raise StaleRevision("请先选择当前创意方向，再确认方案")
        if self._pending_gate(c, p["id"], "clarification"):
            raise StaleRevision("请先回答当前澄清问题")
        if p["plan_context_version"] != p["context_version"]:
            raise StaleRevision("需求或商品图已更新，请先更新广告方案")

    def _guard_job(self, c, job_id):
        job = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if job is None:
            raise KeyError("任务不存在")
        p = self._project(c, job["project_id"])
        if job["cancel_requested"] or job["status"] in ("canceled", "cancel_requested"):
            raise StaleRevision("任务已停止，不能应用晚到结果")
        context = (job["payload"] or {}).get("context_version")
        if p["revision"] != job["revision"] or (context is not None and p["context_version"] != context):
            raise StaleRevision("需求或方案已更新，旧任务结果不会覆盖当前版本")
        return job, p

    def assert_job_current(self, job_id):
        with self._connection() as c:
            return self._guard_job(c, job_id)[0]

    def _request_job(self, c, project_id, request_id, kind):
        prior = c.execute("SELECT * FROM turn_requests WHERE project_id=? AND client_turn_id=?", (project_id, request_id)).fetchone()
        if not prior:
            return None
        if prior["kind"] != kind:
            raise StaleRevision("该请求编号已用于另一项操作")
        return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (prior["job_id"],)).fetchone())

    def _new_conversation_job(self, c, p, extra=None):
        jid, now = identifier("job"), time.time()
        detail = self._detail(c, p["id"])
        payload = {"conversation": True, "context_version": p["context_version"], "duration_seconds": p["duration_seconds"], "frozen": {key: detail[key] for key in ("project", "assets", "messages", "gates")}}
        # Before the first script succeeds there may be no saved plan. Retain
        # the selected offer's intent in this new frozen input too; do not
        # migrate or rewrite the historical gate or its source job.
        for gate in payload["frozen"]["gates"]:
            if gate.get("kind") != "direction" or gate.get("status") != "answered" or not gate.get("selected_option_id"):
                continue
            source_job = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (gate["job_id"],)).fetchone())
            candidates = (((source_job or {}).get("payload") or {}).get("decision") or {}).get("direction_candidates") or {}
            intent = (candidates.get("brief") or {}).get("creative_intent")
            if intent:
                gate["creative_intent"] = copy.deepcopy(intent)
        payload.update(extra or {})
        if payload.pop("ad_form_confirmation_required", False):
            # This is a per-turn obligation, not a change to the saved choice.
            payload["frozen"]["project"]["ad_form_confirmation_required"] = True
        c.execute("INSERT INTO jobs(id,project_id,revision,kind,status,phase,progress,payload,created_at,updated_at) VALUES(?,?,?,'plan','queued','queued','{}',?,?,?)", (jid, p["id"], p["revision"], encoded(payload), now, now))
        c.execute("UPDATE projects SET status='queued',updated_at=? WHERE id=?", (now, p["id"]))
        self._event(c, p["id"], jid, "status", "需求已排队处理", "waiting")
        return jid

    def create_turn(self, project_id, body_dict):
        from .schemas import TurnInput
        from .ad_form import (AD_FORM_LABELS, answer_ad_form, explicit_ad_form, infer_ad_form,
                             rejects_current_ad_form, unresolved_ad_form_change)
        from .cta import answer_cta, is_explicit_cta, stated_cta
        from .planning import keeps_selected_direction, product_images_are_new
        body = TurnInput.model_validate(body_dict).model_dump()
        answered = self.answer_generate_retry_turn(project_id, body)
        if answered is not None:
            return answered
        stop_speculation = False
        retain_offer = False
        with self._connection(True) as c:
            prior = self._request_job(c, project_id, body["client_turn_id"], "turn")
            if prior:
                return prior
            p = self._project(c, project_id)
            if body["expected_context_version"] != p["context_version"]:
                raise StaleRevision("需求已变化，请刷新后提交")
            self._require_idle(c, project_id)
            refs = list(dict.fromkeys(body["reference_asset_ids"]))
            names = []
            for aid in refs:
                a = c.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (aid, project_id)).fetchone()
                if not a:
                    raise ValueError("引用素材不属于当前项目")
                names.append(a["name"])
            content = body["content"].strip()
            text_form = explicit_ad_form(content)
            if text_form is None and p.get("ad_form") is None:
                text_form = infer_ad_form(content) or infer_ad_form(p.get("brief") or "")
            if body.get("ad_form") and text_form and body["ad_form"] != text_form:
                raise ValueError("广告倾向选择与本次文字要求不一致，请保留一个选择")
            chosen_form = body.get("ad_form") or text_form
            confirm_form = unresolved_ad_form_change(content)
            withdraw_form = rejects_current_ad_form(content, p.get("ad_form"))
            if withdraw_form and chosen_form == p.get("ad_form"):
                raise ValueError("广告倾向选择与撤回要求不一致，请保留一个选择")
            if names:
                content = "【参考素材：" + "、".join(names) + "】" + content
            pending = self._pending_gate(c, project_id)
            pending_candidates = None
            if pending and pending["kind"] == "direction":
                pending_job = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (pending["job_id"],)).fetchone())
                pending_candidates = pending_job["payload"].get("direction_candidates")
            base_plan_current = bool(p["plan"] and p["plan_context_version"] == p["context_version"] and not pending)
            chosen_cta = stated_cta(body.get("content") or "")
            if body["reply_to_gate_id"]:
                gate = self._dict(c.execute("SELECT * FROM gates WHERE id=? AND project_id=?", (body["reply_to_gate_id"], project_id)).fetchone())
                if not gate or gate["status"] != "pending" or gate["kind"] != "clarification" or gate["context_version"] != p["context_version"] or gate["revision"] != p["revision"]:
                    raise StaleRevision("这组问题已经失效，请回答当前问题")
                answers = body["answers"]
                by_id = {a["question_id"]: a for a in answers}
                if len(by_id) != len(answers) or set(by_id) != {q["id"] for q in gate["questions"]}:
                    raise ValueError("请完整回答每个问题且不要重复")
                lines = []
                for q in gate["questions"]:
                    a = by_id[q["id"]]
                    options = {o["id"]: o["label"] for o in q["options"]}
                    if a["option_id"] and a["option_id"] not in options:
                        raise ValueError("选择的答案不存在")
                    if a["text"] and not q["allow_custom"] or a["skipped"] and not q["allow_skip"]:
                        raise ValueError("这个问题不允许该答复方式")
                    if q["id"] == "ad_form":
                        gate_form = answer_ad_form(a)
                        if chosen_form is not None and gate_form is not None and chosen_form != gate_form:
                            raise ValueError("广告倾向选择与本次文字要求不一致，请保留一个选择")
                        chosen_form = chosen_form or gate_form
                        if gate_form is None:
                            confirm_form = True
                    if q["id"] == "cta_target":
                        chosen_cta = answer_cta(a)
                    lines.append(q["prompt"] + "：" + (options[a["option_id"]] if a["option_id"] else a["text"] if a["text"] else "暂不确定，请使用谨慎表达"))
                content = (content + "\n" if content else "") + "\n".join(lines)
                c.execute("UPDATE gates SET status='answered',answers=?,answered_at=? WHERE id=?", (encoded(answers), time.time(), gate["id"]))
                c.execute("UPDATE jobs SET status='ready',phase='clarification_answered',lease_until=0,updated_at=? WHERE id=?", (time.time(), gate["job_id"]))
                self._event(c, project_id, gate["job_id"], "gate", "问题已回答", "done")
            elif body["answers"]:
                raise ValueError("答案必须关联当前问题")
            elif pending:
                if not content and chosen_form is None:
                    raise ValueError("请回答问题或补充你的需求")
                if pending["kind"] == "direction" and chosen_form is None and not confirm_form and not withdraw_form:
                    rows = c.execute("SELECT * FROM assets WHERE project_id=?", (project_id,)).fetchall()
                    brief = pending_candidates.get("brief") if isinstance(pending_candidates, dict) else None
                    retain_offer = keeps_selected_direction(
                        [{"role": "user", "content": content}],
                        has_new_image=product_images_are_new([self._dict(row) for row in rows], brief=brief, since=pending.get("created_at")))
                if not retain_offer:
                    if pending["kind"] == "direction" and pending_job:
                        self._abandon_speculative_plan(c, pending_job)
                        stop_speculation = True
                    c.execute("UPDATE gates SET status='superseded',answered_at=? WHERE id=?", (time.time(), pending["id"]))
                    c.execute("UPDATE jobs SET status='canceled',cancel_requested=1,lease_until=0,updated_at=? WHERE id=? AND status='awaiting_input'", (time.time(), pending["job_id"]))
                    self._event(c, project_id, pending["job_id"], "gate", "新的需求已替代这次选择", "canceled")
            if chosen_form is not None and rejects_current_ad_form(body["content"], chosen_form):
                raise ValueError("广告倾向选择与撤回要求不一致，请保留一个选择")
            form_changed = (chosen_form is not None and chosen_form != p.get("ad_form")) or (withdraw_form and chosen_form is None)
            # 剧情不念去向。同一张卡上选的去向作废，并记成已决定，避免换类型后再问一遍。
            if chosen_form == "scene_story" and is_explicit_cta(chosen_cta):
                chosen_cta = "ai_decide"
            if chosen_cta:
                c.execute("UPDATE projects SET cta_target=? WHERE id=?", (chosen_cta, project_id))
            if form_changed:
                c.execute("UPDATE projects SET ad_form=?,ad_form_updated_context_version=context_version+1 WHERE id=?", (chosen_form, project_id))
                base_plan_current = False
                pending_candidates = None
            confirm_form = confirm_form and chosen_form is None and not withdraw_form
            if confirm_form:
                base_plan_current = False
                pending_candidates = None
            if chosen_form is not None and not content:
                content = "广告倾向：" + AD_FORM_LABELS[chosen_form]
            c.execute("UPDATE projects SET context_version=context_version+1,updated_at=? WHERE id=?", (time.time(), project_id))
            message = self._message(c, project_id, "user", content) if content else None
            p = self._project(c, project_id)
            if retain_offer:
                # The direction card stays. Record the note on that same waiting job
                # and do not start another strategy request.
                self._sync_pending_context(c, project_id, p["context_version"])
                jid = pending["job_id"]
                if message:
                    c.execute("UPDATE messages SET job_id=? WHERE id=?", (jid, message["id"]))
                    c.execute("UPDATE events SET job_id=? WHERE project_id=? AND kind='message' AND job_id IS NULL AND created_at>=?", (jid, project_id, message["created_at"]))
                self._event(c, project_id, jid, "gate", "商品信息已记下，请继续从当前方向里选择", "waiting")
            else:
                jid = self._new_conversation_job(c, p, {"reference_asset_ids": refs, "base_plan_current": base_plan_current,
                    **({"ad_form_confirmation_required": True} if confirm_form else {}),
                    **({"pending_direction_candidates": pending_candidates} if pending_candidates else {})})
                if message:
                    c.execute("UPDATE messages SET job_id=? WHERE id=?", (jid, message["id"]))
                    c.execute("UPDATE events SET job_id=? WHERE project_id=? AND kind='message' AND job_id IS NULL AND created_at>=?", (jid, project_id, message["created_at"]))
            c.execute("INSERT INTO turn_requests VALUES(?,?,?,?)", (project_id, body["client_turn_id"], "turn", jid))
            created = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        if stop_speculation:
            from app.speculative_plan import cancel_speculation
            cancel_speculation(project_id)
        return created

    def finish_decision(self, job_id, decision_dict):
        from .schemas import AgentDecision
        with self._connection(True) as c:
            job, p = self._guard_job(c, job_id)
            if job["status"] != "running":
                raise StaleRevision("该回合不再等待规划结果")
            from .ad_form import bind_brief_ad_form, guard_ad_form_confirmation
            frozen_project = job["payload"].get("frozen", {}).get("project", p)
            decision = AgentDecision.model_validate(guard_ad_form_confirmation(decision_dict, frozen_project)).model_dump()
            plan_brief = ((decision.get("plan") or {}).get("strategy") or {}).get("brief")
            if isinstance(plan_brief, dict) and plan_brief.get("ad_form") != p.get("ad_form"):
                raise ValueError("方案须沿用本轮已确认的广告倾向，不能修改已审核方案的归属")
            for brief in (decision.get("planning_brief"),
                          (decision.get("direction_candidates") or {}).get("brief")):
                if isinstance(brief, dict):
                    bind_brief_ad_form(brief, p)
            now, kind = time.time(), decision["kind"]
            payload = job["payload"]
            restoring_pending_candidates = kind == "reply" and bool(payload.get("pending_direction_candidates"))
            if restoring_pending_candidates:
                # Answering a question about a pending offer must not make its
                # choices disappear. Reissue against the new context version.
                decision = AgentDecision.model_validate({**decision, "kind": "directions",
                    "direction_candidates": payload["pending_direction_candidates"]}).model_dump()
                kind = "directions"
            payload["decision"] = decision
            if kind == "clarify":
                gid = identifier("gate")
                c.execute("INSERT INTO gates VALUES(?,?,?,'clarification','pending',?,?,?,'[]',NULL,NULL,?,?,?,NULL)", (gid, p["id"], job_id, decision["message"], encoded(decision["questions"]), "[]", p["context_version"], p["revision"], now))
                c.execute("UPDATE jobs SET status='awaiting_input',phase='awaiting_input',payload=?,lease_until=0,updated_at=? WHERE id=?", (encoded(payload), now, job_id))
                c.execute("UPDATE projects SET status='awaiting_input',updated_at=? WHERE id=?", (now, p["id"]))
                self._message(c, p["id"], "assistant", decision["message"], job_id)
                self._event(c, p["id"], job_id, "gate", "请确认几个信息", "waiting")
            elif kind == "directions":
                from .strategy import DirectionCandidates, build_direction_candidates, select_rules, validate_brief
                from .planning import evidence_sources
                frozen = payload["frozen"]
                originals = {a["id"] for a in frozen["assets"] if a["kind"] == "product" and not a.get("metadata", {}).get("generated")}
                if not originals:
                    raise ValueError("请先上传商品原图，再选择广告方向")
                candidates = DirectionCandidates.model_validate(decision["direction_candidates"])
                validate_brief(candidates.brief, evidence_sources(frozen["project"], frozen["messages"], frozen["gates"]), originals)
                if not restoring_pending_candidates:
                    checked = build_direction_candidates(candidates.brief, candidates.directions,
                        candidates.recommended_direction_id, select_rules(None), direction_review=candidates.direction_review)
                    if checked.model_dump() != candidates.model_dump():
                        raise ValueError("方向候选的规则来源不正确")
                # A reply reissues only the server-frozen, previously validated
                # offer above. Preserve its historical rule version/provenance;
                # applying today's catalog would rewrite or reject old choices.
                # New model-authored offers always take the current checks.
                gid = identifier("gate")
                options = [{**d.model_dump(), "recommended": d.id == candidates.recommended_direction_id} for d in candidates.directions]
                c.execute("INSERT INTO gates VALUES(?,?,?,'direction','pending',?,'[]',?,'[]',NULL,NULL,?,?,?,NULL)",
                    (gid, p["id"], job_id, decision["message"], encoded(options), p["context_version"], p["revision"], now))
                payload["direction_candidates"] = candidates.model_dump()
                c.execute("UPDATE jobs SET status='awaiting_input',phase='awaiting_direction',payload=?,lease_until=0,updated_at=? WHERE id=?",
                    (encoded(payload), now, job_id))
                c.execute("UPDATE projects SET status='awaiting_direction',updated_at=? WHERE id=?", (now, p["id"]))
                self._message(c, p["id"], "assistant", decision["message"], job_id)
                self._event(c, p["id"], job_id, "gate", "请选择一个创意方向", "waiting")
            else:
                revision = p["revision"]
                if kind == "plan":
                    plan = decision["plan"]
                    from .release_gate import images_blocked_reason, stamp_honest_label
                    if not plan.get("review_status"):
                        stamp_honest_label(plan, plan.get("strategy"), project=p)
                    blocked = images_blocked_reason(plan, strategy=plan.get("strategy"), project=p)
                    if blocked:
                        from .release_gate import MerchantLineChoice, settle_before_spend
                        try:
                            plan = settle_before_spend(plan, project=p, project_id=p["id"])
                        except MerchantLineChoice as choice:
                            prompt = choice.questions[0]["prompt"] if choice.questions else str(choice)
                            raise ValueError(prompt) from choice
                        decision["plan"] = plan
                    self._validate_plan_duration(p, plan)
                    for option in decision["creative_options"]:
                        self._validate_plan_duration(p, {"generation_strategy": plan.get("generation_strategy", "legacy_10s"), "segments": [option["segment"], *plan["segments"][1:]]})
                    originals = {a["id"] for a in payload["frozen"]["assets"] if a["kind"] == "product" and not a["metadata"].get("generated")}
                    segments = plan["segments"] + [o["segment"] for o in decision["creative_options"]]
                    if any(not s["reference_asset_ids"] or not set(s["reference_asset_ids"]).issubset(originals) for s in segments):
                        raise ValueError("方案与创意必须引用本轮真实商品图")
                    revision += 1
                    c.execute("INSERT INTO revisions VALUES(?,?,?,?)", (p["id"], revision, encoded(plan), now))
                    c.execute("UPDATE projects SET plan=?,revision=?,plan_context_version=context_version,status='awaiting_generate',updated_at=? WHERE id=?", (encoded(plan), revision, now, p["id"]))
                    payload["creative_options"] = decision["creative_options"]
                else:
                    status = "ready" if p["current_render_id"] else "awaiting_generate" if p["plan"] else "draft"
                    if payload.get("base_plan_current"):
                        c.execute("UPDATE projects SET status=?,plan_context_version=context_version,updated_at=? WHERE id=?", (status, now, p["id"]))
                    else:
                        c.execute("UPDATE projects SET status=?,updated_at=? WHERE id=?", (status, now, p["id"]))
                self._message(c, p["id"], "assistant", decision["message"], job_id)
                progress = dict(job["progress"] or {})
                if kind == "plan":
                    progress["creative_options_ready"] = True
                c.execute("UPDATE jobs SET status='ready',phase=?,revision=?,payload=?,progress=?,lease_until=0,updated_at=? WHERE id=?", ("plan_ready" if kind == "plan" else "reply_ready", revision, encoded(payload), encoded(progress), now, job_id))
                self._event(c, p["id"], job_id, "terminal", "广告方案已准备好" if kind == "plan" else "回复已完成", "done")
            return self._project(c, p["id"])

    def select_direction(self, project_id, body_dict):
        """Resolve one immutable offer and queue only its selected script work."""
        from .schemas import DirectionSelectInput
        from .strategy import DirectionCandidates
        body = DirectionSelectInput.model_validate(body_dict).model_dump()
        with self._connection(True) as c:
            prior = self._request_job(c, project_id, body["client_turn_id"], "direction_select")
            if prior:
                request = prior["payload"].get("direction_selection_request", {})
                if any(request.get(key) != body[key] for key in ("gate_id", "direction_id", "expected_context_version", "accepted_revision")):
                    raise StaleRevision("该请求编号已经用于另一项方向选择")
                return prior
            p = self._project(c, project_id)
            gate = self._dict(c.execute("SELECT * FROM gates WHERE id=? AND project_id=?", (body["gate_id"], project_id)).fetchone())
            if not gate or gate["kind"] != "direction" or gate["status"] not in ("pending", "answered"):
                raise StaleRevision("这组方向已失效，请选择当前方向")
            if gate["revision"] != body["accepted_revision"] or gate["context_version"] != body["expected_context_version"]:
                raise StaleRevision("这组方向已经变化，请刷新后选择")
            source = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (gate["job_id"],)).fetchone())
            if gate["status"] == "answered":
                continuation = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (source["payload"].get("direction_continuation_job_id", ""),)).fetchone())
                if (gate["selected_option_id"] == body["direction_id"] and continuation
                        and continuation["payload"].get("context_version") == p["context_version"]
                        and continuation["revision"] == p["revision"] and not continuation["cancel_requested"]):
                    c.execute("INSERT INTO turn_requests VALUES(?,?,?,?)", (project_id, body["client_turn_id"], "direction_select", continuation["id"]))
                    return continuation
                raise StaleRevision("方向已选择或需求已更新，请在对话中提出修改")
            self._require_idle(c, project_id)
            source, p = self._guard_job(c, gate["job_id"])
            if source["status"] != "awaiting_input" or source["phase"] != "awaiting_direction":
                raise StaleRevision("该回合已不再等待方向选择")
            candidates = DirectionCandidates.model_validate(source["payload"]["direction_candidates"])
            strategy = candidates.select(body["direction_id"])
            selected = next(d for d in candidates.directions if d.id == body["direction_id"])
            now = time.time()
            c.execute("UPDATE gates SET status='answered',selected_option_id=?,answered_at=? WHERE id=?", (selected.id, now, gate["id"]))
            c.execute("UPDATE projects SET context_version=context_version+1,updated_at=? WHERE id=?", (now, project_id))
            self._event(c, project_id, source["id"], "gate", "创意方向已选择", "done")
            from app.speculative_plan import direction_selection_text
            self._message(c, project_id, "user", direction_selection_text(selected.label), source["id"])
            p = self._project(c, project_id)
            trace = source["payload"].get("decision", {}).get("skill_trace")
            source_payload = source["payload"]
            spec = source_payload.get("speculative_plan") if isinstance(source_payload.get("speculative_plan"), dict) else None
            extra = {"selected_strategy": strategy.model_dump(),
                "direction_gate_id": gate["id"], "direction_selection_request": body,
                "inherited_skill_trace": trace or []}
            if (spec and spec.get("abandoned") is not True and spec.get("direction_id") == selected.id
                    and isinstance(spec.get("entries"), dict) and spec["entries"]):
                extra["stage_result_cache"] = copy.deepcopy(spec["entries"])
                extra["stage_result_cache_direction_id"] = selected.id
            jid = self._new_conversation_job(c, p, extra)
            source_payload["direction_continuation_job_id"] = jid
            if spec is not None:
                source_payload["speculative_plan"] = {**spec, "abandoned": True}
            c.execute("UPDATE jobs SET status='ready',phase='direction_selected',payload=?,lease_until=0,updated_at=? WHERE id=?", (encoded(source_payload), now, source["id"]))
            c.execute("INSERT INTO turn_requests VALUES(?,?,?,?)", (project_id, body["client_turn_id"], "direction_select", jid))
            created = self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        from app.speculative_plan import cancel_speculation
        cancel_speculation(project_id)
        return created

    def create_creative_gate(self, job_id, options, message):
        from .schemas import CreativeOption
        options = [CreativeOption.model_validate(o).model_dump() for o in options]
        if not options or len(options) > 3 or len({o["id"] for o in options}) != len(options):
            raise ValueError("创意选项无效")
        with self._connection(True) as c:
            job, p = self._guard_job(c, job_id)
            old = self._dict(c.execute("SELECT * FROM gates WHERE job_id=? AND kind='creative' ORDER BY created_at DESC LIMIT 1", (job_id,)).fetchone())
            if old:
                return old
            if job["kind"] != "generate" or job["status"] != "running" or job["submission_count"]:
                raise ValueError("创意确认必须发生在视频提交之前")
            self._verified_scene_ids(c, job)
            gid, now = identifier("gate"), time.time()
            c.execute("INSERT INTO gates VALUES(?,?,?,'creative','pending',?,'[]',?,'[]',NULL,NULL,?,?,?,NULL)", (gid, p["id"], job_id, str(message)[:3000], encoded(options), p["context_version"], p["revision"], now))
            c.execute("UPDATE jobs SET status='awaiting_input',phase='awaiting_creative',lease_until=0,updated_at=? WHERE id=?", (now, job_id))
            c.execute("UPDATE projects SET status='awaiting_creative',updated_at=? WHERE id=?", (now, p["id"]))
            self._message(c, p["id"], "assistant", str(message)[:3000], job_id)
            self._event(c, p["id"], job_id, "gate", "参考图片已准备好，请确认图片", "waiting")
            return self._dict(c.execute("SELECT * FROM gates WHERE id=?", (gid,)).fetchone())

    def _verified_scene_ids(self, c, job):
        """Legacy method name; verify the complete saved scene-and-cast set."""
        from .production import validate_design_assets
        designs = []
        for aid in dict.fromkeys(job["payload"].get("design_asset_ids", [])):
            asset = self._dict(c.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (aid, job["project_id"])).fetchone())
            if not asset:
                raise ValueError("场景图或角色图记录缺失，请先恢复图片")
            state_still = asset["kind"] == "start_frame" and asset["metadata"].get("state_reference")
            if asset["kind"] not in ("scene", "character") and not state_still:
                continue
            path = Path(asset["path"]).resolve()
            if not asset["metadata"].get("generated") or not path.is_relative_to(self.media_root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["metadata"].get("sha256"):
                raise ValueError("场景图或角色图缺失或已经变化，请先恢复图片")
            designs.append(asset)
        return [asset["id"] for asset in validate_design_assets(job["payload"].get("plan") or {}, designs)]

    def confirm_creative(self, project_id, body_dict):
        from .schemas import CreativeConfirmInput, Plan
        body = CreativeConfirmInput.model_validate(body_dict).model_dump()
        with self._connection(True) as c:
            prior = self._request_job(c, project_id, body["client_turn_id"], "creative")
            if prior:
                return prior
            self._require_idle(c, project_id)
            gate = self._dict(c.execute("SELECT * FROM gates WHERE id=? AND project_id=?", (body["gate_id"], project_id)).fetchone())
            if not gate or gate["kind"] != "creative" or gate["status"] != "pending":
                raise StaleRevision("创意选择已失效，请刷新当前任务")
            job, p = self._guard_job(c, gate["job_id"])
            if p["revision"] != body["accepted_revision"] or gate["revision"] != p["revision"] or gate["context_version"] != p["context_version"]:
                raise StaleRevision("方案已变化，请刷新后选择")
            selected = next((o for o in gate["options"] if o["id"] == body["option_id"]), None)
            if not selected:
                raise ValueError("创意选项不存在")
            if job["status"] != "awaiting_input" or job["submission_count"]:
                raise StaleRevision("该任务已开始执行，不能重复确认")
            scene_ids = self._verified_scene_ids(c, job)
            now, note = time.time(), body["note"].strip()
            c.execute("UPDATE gates SET status='answered',selected_option_id=?,note=?,answered_at=? WHERE id=?", (selected["id"], note, now, gate["id"]))
            c.execute("UPDATE projects SET context_version=context_version+1,updated_at=? WHERE id=?", (now, project_id))
            user_message = "图片修改意见：" + note if note else ("图片已确认，生成视频" if selected["id"] == "current" else "选择开场：" + selected["label"])
            self._message(c, project_id, "user", user_message, job["id"])
            self._event(c, project_id, job["id"], "gate", "图片已确认" if not note else "图片修改意见已送回策划", "done")
            p = self._project(c, project_id)
            if note:
                c.execute("UPDATE jobs SET status='canceled',cancel_requested=1,lease_until=0,updated_at=? WHERE id=?", (now, job["id"]))
                jid = self._new_conversation_job(c, p, {"creative_feedback": {"selected_option": selected, "note": note}})
            else:
                plan = copy.deepcopy(p["plan"])
                plan["segments"][0] = copy.deepcopy(selected["segment"])
                plan = Plan.model_validate(plan).model_dump()
                self._validate_plan_duration(p, plan)
                # Keep the public plan on its original product references; only the run consumes generated design IDs.
                original_ids = {r["id"] for r in c.execute("SELECT * FROM assets WHERE project_id=? AND kind='product'", (project_id,)) if not json.loads(r["metadata"]).get("generated")}
                if any(not set(s["reference_asset_ids"]).issubset(original_ids) for s in plan["segments"]):
                    # The current option may carry generated design references; retain its approved original references.
                    if selected["id"] == "current":
                        plan["segments"][0]["reference_asset_ids"] = p["plan"]["segments"][0]["reference_asset_ids"]
                    else:
                        raise ValueError("创意选项必须引用原始商品图")
                revision = p["revision"] + 1
                c.execute("INSERT INTO revisions VALUES(?,?,?,?)", (project_id, revision, encoded(plan), now))
                c.execute("UPDATE projects SET plan=?,revision=?,plan_context_version=context_version,status='generating',updated_at=? WHERE id=?", (encoded(plan), revision, now, project_id))
                payload = job["payload"]
                payload.update(plan=copy.deepcopy(plan), creative_confirmed=True, selected_option_id=selected["id"],
                               context_version=p["context_version"], merchant_confirmed_revision=revision)
                design_ids = scene_ids
                payload["design_asset_ids"] = scene_ids
                if design_ids:
                    for segment in payload["plan"]["segments"]:
                        from app.planner_recovery import append_reference_ids
                        segment["reference_asset_ids"] = append_reference_ids(segment["reference_asset_ids"], design_ids)
                jid = job["id"]
                c.execute("UPDATE jobs SET revision=?,payload=?,status='queued',phase='queued',error=NULL,lease_until=0,started_at=NULL,updated_at=? WHERE id=?", (revision, encoded(payload), now, jid))
                self._event(c, project_id, jid, "status", "已确认，等待制作视频", "waiting")
            c.execute("INSERT INTO turn_requests VALUES(?,?,?,?)", (project_id, body["client_turn_id"], "creative", jid))
            return self._dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
