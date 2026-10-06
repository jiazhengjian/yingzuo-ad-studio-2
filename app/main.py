from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import fcntl
import html
import io
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlparse
import uuid

from dotenv import set_key
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import ROOT, Settings
from app.db import Store, StaleRevision
from app.pipeline import Pipeline
from app.providers import Gateway
from app.planner_transport import planner_api_key, planner_label, planner_provider
from app.schemas import ContinuityInput, CredentialsInput, CreativeConfirmInput, DirectionSelectInput, GenerateInput, MessageInput, NewProject, PlanInput, ProjectTitleInput, RetryInput, TurnInput


PRIVATE_FIELDS = {"path", "source_path", "payload", "snapshot", "raw", "provider_url", "download_url", "lease_until"}
_WORKBENCH_ORIGINS = {
    "http://127.0.0.1:8787", "http://localhost:8787",
    "http://127.0.0.1:5173", "http://localhost:5173",
}


def _same_workbench_origin(origin: str, host: str | None) -> bool:
    """Allow the fixed dev ports, or this request's own loopback host:port.

    A second local instance is not cross-site. The origin host and port must
    equal the Host header, and the host must still be 127.0.0.1 or localhost.
    """
    if origin in _WORKBENCH_ORIGINS:
        return True
    if not host or any(char in host for char in " \t\r\n/\\"):
        return False
    parsed = urlparse(origin)
    if (parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        return False
    hostname = parsed.hostname
    if hostname not in ("127.0.0.1", "localhost"):
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        return False
    return host.lower() == f"{hostname}:{port}"


def attachment_name(asset, path: Path):
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", asset.get("name") or path.stem)
    stem = Path(name).stem.strip(" .")[:100] or "素材"
    if asset["kind"] in {"render", "segment"} and asset.get("revision") is not None:
        stem += f"_V{asset['revision']}"
    return stem + path.suffix


def public(value):
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if k not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def create_app(settings=None, gateway=None, run_worker=True):
    settings = settings or Settings.from_env()
    store = Store(settings.database_path, settings.media_root)
    gateway = gateway or Gateway(settings)
    pipeline = Pipeline(store, gateway, settings)
    csrf_token = secrets.token_urlsafe(32)

    async def worker():
        store.recover_jobs()
        if getattr(settings, "planner_speculate_recommended", False):
            from app.speculative_plan import kick_speculation
            kick_speculation(store, gateway, settings)
        while True:
            for job in store.active_jobs():
                if job["status"] == "queued":
                    await pipeline.execute(job["id"])
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        lock = None
        if run_worker:
            lock = (settings.data_root / "worker.lock").open("a")
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise RuntimeError("同一数据目录已有工作进程；请先停止旧服务，不能多实例恢复同一批任务") from None
        task = asyncio.create_task(worker()) if run_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

    app = FastAPI(title="映作 Ad Studio", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.store, app.state.settings, app.state.gateway = store, settings, gateway
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def same_origin(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and not _same_workbench_origin(origin, request.headers.get("host")):
                return JSONResponse({"detail": "仅允许本机工作台提交请求"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith(("/api/", "/setup")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "项目、素材或任务不存在"}, status_code=404)

    @app.exception_handler(StaleRevision)
    async def stale(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Default Pydantic errors can echo submitted secrets; only expose field names and safe messages.
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        return JSONResponse({"detail": "输入格式不正确，请检查：" + fields}, status_code=422)

    def idle(project_id):
        if any(j["project_id"] == project_id for j in store.active_jobs()):
            raise HTTPException(409, "当前任务仍在进行，请先停止或等待完成")

    def require_planner():
        if not planner_api_key(settings):
            raise HTTPException(409, "尚未配置策划服务密钥，请打开本地连接设置")

    def require_video():
        if not settings.tudou_api_key:
            raise HTTPException(409, "尚未配置土豆 API Key，请打开本地连接设置")

    @app.get("/api/health")
    def health():
        from app.runtime_skills import registry_metadata
        try:
            skill_registry = registry_metadata()
        except (OSError, ValueError):
            skill_registry = {"status": "unavailable"}
        routes = getattr(settings, "planner_stage_routes", {}) or {}
        route_labels = {stage: item["provider"] + ":" + item["model"] + ((":" + item["effort"]) if item.get("effort") else "")
                        for stage, item in sorted(routes.items()) if isinstance(item, dict)}
        return {"ok": True, "configured": {"planner": bool(planner_api_key(settings)), "video": bool(settings.tudou_api_key)},
                "skill_registry": skill_registry,
                "active_jobs": len(store.active_jobs()), "providers": {"planner": planner_label(settings), "video": "土豆 API · Wan 3.0"},
                "capabilities": {"planner_provider": planner_provider(settings), "planner_model": settings.planner_model,
                                 "video_model": settings.video_model, "max_video_submissions": settings.max_video_submissions,
                                 "max_image_submissions": 9, "duration_min_seconds": 10, "duration_max_seconds": 60,
                                 "default_duration_seconds": 20,
                                 "planner_stream": settings.planner_stream,
                                 "planner_fallback": bool(settings.planner_fallback_on),
                                 "planner_stage_routes": route_labels,
                                 "planner_speculate_recommended": settings.planner_speculate_recommended,
                                 "planner_max_refinements": settings.planner_max_refinements,
                                 "planner_refinement_format": settings.planner_refinement_format,
                                 "planner_request_deadline_seconds": settings.planner_request_deadline_seconds,
                                 "state_references": bool(getattr(settings, "state_references_enabled", True))},
                "limitations": ["支持10至60秒商品广告；独立主播入口暂未开放，商品广告可按方向安排角色说话", "原生声音、台词与口型需检查成片，暂不自动生成字幕", "商品使用上传原图；每方案一张场景图。状态参考默认开着：会变化的人物用起始状态四视图当身份，其他人一张组合四视图。关掉后每位只出一张普通四视图。一起确认后生成视频"]}

    @app.get("/api/projects")
    def projects():
        return {"projects": public(store.list_projects())}

    @app.post("/api/projects", status_code=201)
    def create_project(body: NewProject):
        if body.route == "presenter":
            raise HTTPException(409, "真人口型种草尚在下一阶段，当前请选择商品广告")
        project = store.create_project(body.brief, body.route, body.title, duration_seconds=body.duration_seconds, ad_form=body.ad_form)
        store.add_message(project["id"], "user", body.brief)
        return public(store.get_project(project["id"]))

    @app.get("/api/projects/{project_id}")
    def project(project_id: str):
        return public(store.project_detail(project_id))

    @app.patch("/api/projects/{project_id}")
    def edit_project_title(project_id: str, body: ProjectTitleInput):
        return public(store.update_project_title(project_id, body.title))

    @app.post("/api/projects/{project_id}/assets", status_code=201)
    async def upload(project_id: str, file: UploadFile = File(...)):
        idle(project_id)
        store.get_project(project_id)
        existing = [a for a in store.project_detail(project_id)["assets"] if not a["metadata"].get("generated") and a["kind"] == "product"]
        if len(existing) >= 3:
            raise HTTPException(422, "首版每个项目最多上传3张商品图")
        raw = await file.read(15 * 1024 * 1024 + 1)
        if len(raw) > 15 * 1024 * 1024:
            raise HTTPException(413, "商品图不能超过15MB")
        try:
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in ("JPEG", "PNG", "WEBP") or source.width * source.height > 24_000_000:
                    raise ValueError("unsupported image")
                im = ImageOps.exif_transpose(source).convert("RGB")
                im.thumbnail((2048, 2048))
                path = settings.media_root / project_id / "uploads" / (uuid.uuid4().hex + ".jpg")
                path.parent.mkdir(parents=True, exist_ok=True)
                im.save(path, "JPEG", quality=94)
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise HTTPException(422, "请上传有效的JPEG、PNG或WebP图片，尺寸不超过2400万像素") from exc
        name = Path(file.filename or "商品图").name[:100]
        asset = store.add_asset(project_id, "product", name, path, "image/jpeg", metadata={"generated": False, "width": im.width, "height": im.height})
        if store.get_project(project_id)["plan"]:
            store.add_message(project_id, "system", "商品图已保存。已选方向保持不变；写上商品信息并发送后，才会把新图并进当前方案。")
        return public(asset)

    @app.post("/api/projects/{project_id}/messages", status_code=201)
    def message(project_id: str, body: MessageInput):
        idle(project_id)
        if body.asset_id and store.get_asset(body.asset_id)["project_id"] != project_id:
            raise HTTPException(422, "素材不属于当前项目")
        return store.add_message(project_id, "user", body.content)

    @app.post("/api/projects/{project_id}/turns")
    def turn(project_id: str, body: TurnInput):
        require_planner()
        return {"job": public(store.create_turn(project_id, body.model_dump()))}

    @app.post("/api/projects/{project_id}/creative-confirm")
    def creative_confirm(project_id: str, body: CreativeConfirmInput):
        if body.note.strip():
            require_planner()
        else:
            require_video()
        return {"job": public(store.confirm_creative(project_id, body.model_dump()))}

    @app.post("/api/projects/{project_id}/direction-select")
    def direction_select(project_id: str, body: DirectionSelectInput):
        require_planner()
        job = store.select_direction(project_id, body.model_dump())
        return {"job": public(job), "snapshot": public(store.project_detail(project_id))}

    @app.get("/api/projects/{project_id}/events")
    def project_events(project_id: str, after: int = 0):
        if after < 0:
            raise ValueError("事件游标无效")
        return store.events_after(project_id, after)

    @app.get("/api/projects/{project_id}/events/stream")
    async def project_event_stream(project_id: str, request: Request, after: int = 0):
        store.get_project(project_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "事件游标无效") from None
        if cursor < 0:
            raise HTTPException(422, "事件游标无效")

        async def stream():
            nonlocal cursor
            heartbeat_at = asyncio.get_running_loop().time()
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                batch = store.events_after(project_id, cursor)
                if batch["events"]:
                    cursor = batch["cursor"]
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps(batch, ensure_ascii=False)}\n\n"
                    heartbeat_at = asyncio.get_running_loop().time()
                elif asyncio.get_running_loop().time() - heartbeat_at >= 10:
                    yield ": heartbeat\n\n"
                    heartbeat_at = asyncio.get_running_loop().time()
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/projects/{project_id}/plan")
    def plan(project_id: str):
        require_planner()
        idle(project_id)
        detail = store.project_detail(project_id)
        if not any(a["kind"] == "product" and not a["metadata"].get("generated") for a in detail["assets"]):
            raise HTTPException(422, "请先上传真实商品图")
        return {"job": public(store.create_job(project_id, "plan", detail["project"]["revision"]))}

    @app.patch("/api/projects/{project_id}/plan")
    def edit_plan(project_id: str, body: PlanInput):
        idle(project_id)
        if store.pending_gate(project_id):
            raise HTTPException(409, "请先完成当前问题或创意选择")
        detail = store.project_detail(project_id)
        ids = {a["id"] for a in detail["assets"]}
        if any(not set(s.reference_asset_ids).issubset(ids) for s in body.plan.segments):
            raise HTTPException(422, "方案引用了不存在的素材")
        return public(store.update_plan(project_id, body.plan.model_dump(), expected_revision=body.accepted_revision))

    @app.post("/api/projects/{project_id}/generate")
    def generate(project_id: str, body: GenerateInput):
        require_video()
        detail = store.project_detail(project_id)
        if not any(a["kind"] == "product" and not a["metadata"].get("generated") for a in detail["assets"]):
            raise HTTPException(422, "请先上传真实商品图")
        # Repeated click returns the same durable run, including completed and ambiguous submissions.
        return {"job": public(store.create_job(project_id, "generate", body.accepted_revision, payload={"pause_for_creative": body.pause_for_creative}))}

    @app.post("/api/projects/{project_id}/continuity")
    def continuity(project_id: str, body: ContinuityInput):
        require_video()
        return {"job": public(store.create_continuity_upgrade(project_id, body.accepted_revision,
                                                              body.source_job_id, body.request_id))}

    @app.post("/api/projects/{project_id}/retry")
    def retry(project_id: str, body: RetryInput):
        job = store.get_job(body.job_id)
        if job["project_id"] != project_id:
            raise HTTPException(404, "任务不存在")
        if body.retry_failed_segments:
            require_video()
            return {"job": public(store.create_failed_video_retry(project_id, job["id"]))}
        if job["status"] == "submitted_unknown":
            raise HTTPException(409, "平台接单状态未知，不能自动重新提交；请核对平台任务以避免重复扣费")
        if job["status"] not in ("failed", "partial"):
            raise HTTPException(409, "当前任务无需恢复")
        if (job.get("progress") or {}).get("error_code") == "voice_too_long":
            raise HTTPException(422, "配音文案过长，请先精简文案后重新确认；直接恢复同样文案不能解决时长问题，已保存的图片会保留")
        if store.get_project(project_id)["revision"] != job["revision"]:
            raise HTTPException(409, "这是旧版本任务，请使用当前方案")
        store.assert_job_current(job["id"])
        if job["kind"] == "plan":
            require_planner()
            return {"job": public(store.retry_planning_job(project_id, job["id"]))}
        if job["kind"] == "generate":
            recovery = store.video_retry_state(job["id"])
            if recovery["uncertain_segment_ids"]:
                raise HTTPException(409, "存在受理状态未知的片段，请先核对原任务")
            if recovery["failed_segment_ids"]:
                if not recovery["pending_segment_ids"]:
                    raise HTTPException(409, "原任务已有明确失败片段，请选择重试失败片段；恢复任务不会重新提交")
                payload = {**job["payload"], "resume_known_tasks_only": True}
                job = store.update_job(job["id"], payload=payload)
        idle(project_id)
        return {"job": public(store.update_job(job["id"], status="queued", error=None))}

    @app.post("/api/projects/{project_id}/resume-planning")
    def resume_planning(project_id: str):
        require_planner()
        store.get_project(project_id)
        idle(project_id)
        return {"job": public(store.resume_saved_planning(project_id))}

    @app.post("/api/projects/{project_id}/cancel")
    def cancel(project_id: str):
        store.get_project(project_id)
        store.request_cancel(project_id)
        return {"ok": True}

    @app.post("/api/projects/{project_id}/revise-cta")
    def cta_retired(project_id: str):
        raise HTTPException(410, "独立结尾文字功能已移除")

    @app.api_route("/media/{asset_id}", methods=["GET", "HEAD"])
    def media(asset_id: str, download: bool = False):
        asset = store.get_asset(asset_id)
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to(settings.media_root.resolve()) or not path.is_file():
            raise HTTPException(404, "媒体文件不存在")
        return FileResponse(path, media_type=asset["mime"], filename=attachment_name(asset, path) if download else None,
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/settings/credentials")
    def save_credentials(body: CredentialsInput, request: Request):
        if not secrets.compare_digest(request.headers.get("x-setup-token", ""), csrf_token):
            raise HTTPException(403, "请从本地连接设置页面保存")
        if store.active_jobs():
            raise HTTPException(409, "请等待正在执行的任务完成后更改连接")
        for value in (body.deepseek_api_key, body.tudou_api_key):
            if value and (len(value.strip()) < 12 or any(c.isspace() for c in value)):
                raise HTTPException(422, "密钥格式无效")
        env_path = ROOT / ".env"
        if not env_path.exists():
            fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        for field, env_name in (("deepseek_api_key", "DEEPSEEK_API_KEY"), ("tudou_api_key", "TUDOU_API_KEY")):
            value = getattr(body, field).strip()
            if value:
                set_key(str(env_path), env_name, value)
                setattr(settings, field, value)
        os.chmod(env_path, 0o600)
        return {"ok": True, "configured": {"planner": bool(planner_api_key(settings)), "video": bool(settings.tudou_api_key)}}

    @app.get("/setup", response_class=HTMLResponse)
    def setup():
        template = (ROOT / "app" / "setup.html").read_text(encoding="utf-8")
        return (template.replace("__SETUP_TOKEN__", csrf_token)
                .replace("__DEEPSEEK_HIDDEN__", "hidden" if planner_provider(settings) == "tudou" else "")
                .replace("__PLANNER_LABEL__", html.escape(planner_label(settings))))

    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/", response_class=HTMLResponse)
        def not_built():
            return "<h1>映作后端已启动</h1><p>请先构建 frontend。<a href='/setup'>连接设置</a></p>"
    return app


app = create_app()
