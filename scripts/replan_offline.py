#!/usr/bin/env python3
"""Re-plan a saved job without writing the database or calling image or video APIs.

The selected direction stays as saved. Only the script, storyboard, local
rewrites and plan review run again. Use a fake stage_call in tests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def _mime_for(path):
    return _MIME.get(Path(str(path)).suffix.lower(), "image/png")


MEDIA_METHODS = (
    "submit_video", "create_designs", "upload_media", "_submit",
    "_original_reference_url", "generate_image", "generate_video",
)


def stub_media(gateway):
    """Replace paid media methods. A call is recorded and then refused."""
    called = []

    def make(name):
        def wrapped(*args, **kwargs):
            called.append(name)
            raise RuntimeError("离线重排不会生成或上传图片和视频")
        return wrapped

    for name in MEDIA_METHODS:
        if hasattr(gateway, name):
            setattr(gateway, name, make(name))
    gateway._offline_media_calls = called
    return gateway


def load_saved_job(db_path, job_id):
    """Read one job, its project, messages and assets. The database stays read-only."""
    path = Path(db_path).resolve()
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise SystemExit("找不到这个策划任务")
        payload = json.loads(job["payload"] or "{}")
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else None
        if plan is None:
            decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
            plan = decision.get("plan") if isinstance(decision.get("plan"), dict) else {}
        if not isinstance(plan.get("strategy"), dict):
            raise SystemExit("这个任务里没有已保存的方向")
        project = connection.execute("SELECT * FROM projects WHERE id=?", (job["project_id"],)).fetchone()
        if project is None:
            raise SystemExit("找不到这个任务对应的项目")
        assets = []
        for row in connection.execute(
                "SELECT * FROM assets WHERE project_id=? ORDER BY created_at, id", (job["project_id"],)):
            metadata = row["metadata"]
            if isinstance(metadata, str) and metadata:
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            keys = set(row.keys())
            mime = row["mime"] if "mime" in keys and row["mime"] else _mime_for(row["path"])
            assets.append({
                "id": row["id"], "kind": row["kind"], "name": row["name"], "path": row["path"],
                "mime": mime, "metadata": metadata if isinstance(metadata, dict) else {},
            })
        messages = [{"role": row["role"], "content": row["content"]} for row in connection.execute(
            "SELECT role, content FROM messages WHERE project_id=? ORDER BY created_at, id", (job["project_id"],))]
        duration = project["duration_seconds"] if "duration_seconds" in project.keys() else None
        has_cta_column = "cta_target" in project.keys()
        return {
            "job_id": job["id"],
            "project_id": project["id"],
            "brief": project["brief"],
            "cta_column": has_cta_column,
            "cta_target": project["cta_target"] if has_cta_column else None,
            "duration_seconds": duration if isinstance(duration, int) and duration > 0 else 20,
            "plan": plan,
            "assets": assets,
            "messages": messages,
        }
    finally:
        connection.close()


def planner_product_images(assets, limit=3):
    """Original product photos only. Generated scenes and character sheets stay out."""
    chosen = []
    for asset in assets or []:
        if asset.get("kind") != "product":
            continue
        metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
        if metadata.get("generated"):
            continue
        chosen.append(asset)
        if len(chosen) >= limit:
            break
    return chosen


def _order_assets(assets, production):
    characters = (production or {}).get("characters") or []
    order = {person.get("id"): index for index, person in enumerate(characters) if isinstance(person, dict)}

    def key(asset):
        kind = asset.get("kind")
        rank = 0 if kind == "product" else 1 if kind == "scene" else 2
        return (rank, order.get((asset.get("metadata") or {}).get("character_id"), len(order)))

    return sorted([asset for asset in assets if asset.get("kind") in ("product", "scene", "character")], key=key)


def _script_lines(plan, strategy=None):
    from app.line_quality import grouped_line_flags

    flags = grouped_line_flags(plan.get("ad_script") or {}, strategy, plan=plan) if strategy is not None else {}
    rows = []
    for beat in (plan.get("ad_script") or {}).get("beats") or []:
        windows = []
        for segment in plan.get("segments") or []:
            for span in segment.get("speech_spans") or []:
                if span.get("beat_id") == beat.get("id"):
                    windows.append(f"{span['start']:g}–{span['end']:g}秒")
            if not any(span.get("beat_id") == beat.get("id") for span in segment.get("speech_spans") or []):
                for shot in segment.get("shots") or []:
                    covered = shot.get("beat_ids") or ([shot["beat_id"]] if shot.get("beat_id") else [])
                    if beat.get("id") in covered:
                        windows.append(f"{shot['start']:g}–{shot['end']:g}秒")
        rows.append({
            "beat_id": beat.get("id"),
            "time": "、".join(dict.fromkeys(windows)),
            "text": beat.get("narration") or "",
            "flags": list(flags.get(beat.get("id")) or []),
        })
    return rows


def _shots(plan):
    rows = []
    for segment in plan.get("segments") or []:
        for shot in segment.get("shots") or []:
            continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
            rows.append({
                "id": shot.get("id"),
                "start": shot.get("start"),
                "end": shot.get("end"),
                "product_visible": shot.get("product_visible"),
                "character_ids": list(shot.get("character_ids") or []),
                "description": shot.get("description") or "",
                "camera_move": continuity.get("camera_move") or "",
            })
    return rows


def _one_sheet_each(assets):
    """A character can have two saved sheets. The video request keeps one."""
    chosen = {}
    others = []
    for asset in assets:
        if asset.get("kind") != "character":
            others.append(asset)
            continue
        character_id = (asset.get("metadata") or {}).get("character_id")
        layout = (asset.get("metadata") or {}).get("sheet_layout")
        rank = 2 if layout == "horizontal_16x9" else 1
        current = chosen.get(character_id)
        if current is None or rank >= current[0]:
            chosen[character_id] = (rank, asset)
    return others + [item[1] for item in chosen.values()]


def _compiled_prompt(plan, assets):
    from app.pipeline import video_submission_spec
    from app.video_prompt import compiled_video_request
    segments = plan.get("segments") or []
    if not segments:
        return "", ["没有可编译的视频段"]
    segment = segments[-1]
    ordered = _one_sheet_each(_order_assets(assets, plan.get("production")))
    notes = []
    if segment.get("reference_roles") is None and ordered:
        notes.append("新方案还没绑定参考图，已按商品、场景、角色的保存顺序补上。")
    spec = video_submission_spec(plan, segment, ordered or None)
    prompt = compiled_video_request(spec)["prompt"]
    return prompt, notes


async def replan_offline(*, job_id, db_path, stage_call, gateway=None, skip_review=False):
    """Return the new script, shots, review and compiled prompt. Nothing is written back."""
    from app.progressive_planning import progressive_plan
    from app.strategy import StrategyRecord

    if gateway is not None:
        stub_media(gateway)
    saved = load_saved_job(db_path, job_id)
    plan = saved["plan"]
    strategy = StrategyRecord.model_validate(plan["strategy"])
    from app.cta import project_cta_mode
    cta_project = {"brief": saved.get("brief")}
    if saved.get("cta_column"):
        cta_project["cta_target"] = saved.get("cta_target")
    cta_mode = project_cta_mode(cta_project)

    async def production_stage(system, data, assets, max_tokens=7000):
        if isinstance(data, dict):
            data.setdefault("ad_form", strategy.brief.ad_form)
            if cta_mode is not None:
                data["cta_target"] = cta_mode
            from app.planner_stage import enrich_planner_stage
            enrich_planner_stage(data)
        prepared = []
        for asset in assets or []:
            item = dict(asset)
            if not item.get("mime"):
                item["mime"] = _mime_for(item.get("path") or item.get("name") or "")
            prepared.append(item)
        return await stage_call(system, data, planner_product_images(prepared), max_tokens=max_tokens)

    def record_failure(candidate):
        if gateway is None:
            return
        from app.providers.gateway import save_failed_planner_output
        if isinstance(candidate, dict) and candidate.get("nonblocking"):
            raw_output = {
                "unparsed_content": candidate.get("message") or "",
                "issue_summary": candidate.get("issues") or [],
                "error_code": candidate.get("error_code"),
            }
        elif candidate is None:
            raw_output = {"unparsed_content": "", "issue_summary": ["没有留下模型原文"], "error_code": "replan_offline"}
        else:
            raw_output = candidate
        path = save_failed_planner_output(gateway.settings, {
            "stage": "replan_offline",
            "model": gateway.settings.planner_model,
            "raw_output": raw_output,
        })
        directory = gateway.settings.data_root / "diagnostics"
        print(f"诊断目录：{directory}")
        if path is not None:
            print(f"诊断文件：{path}")

    result = await progressive_plan(
        stage_call=production_stage,
        on_progress=None,
        record_failure=record_failure,
        strategy=strategy,
        draft=plan.get("ad_script"),
        draft_issues=[],
        duration=saved["duration_seconds"],
        generation_strategy=plan.get("generation_strategy") or "continuity_v1",
        assets=saved["assets"],
        conversation=saved["messages"],
        merchant_request=saved["brief"],
        current_plan=None,
        skip_review=skip_review,
        cta_target=cta_mode,
    )
    prompt, notes = _compiled_prompt(result, saved["assets"])
    review = result.get("plan_review") or {}
    return {
        "job_id": job_id,
        "direction_id": strategy.selected_direction_id,
        "lines": _script_lines(result, strategy),
        "shots": _shots(result),
        "review": {
            "ready": review.get("ready"),
            "skipped": bool(skip_review),
            "suggestions": review.get("suggestions") or [],
        },
        "prompt": prompt,
        "notes": notes,
        "media_calls": list(getattr(gateway, "_offline_media_calls", [])),
    }


def _print_report(report):
    print(f"任务 {report['job_id']}，方向 {report['direction_id']}，没有写回数据库。")
    print("台词：")
    for line in report["lines"]:
        flagged = "、".join(line.get("flags") or []) or "未触发"
        print(f"  {line['beat_id']} {line['time']} {line['text']} 检查：{flagged}")
    print("镜头：")
    for shot in report["shots"]:
        visible = "商品在画面里" if shot["product_visible"] else "商品不在画面里" if shot["product_visible"] is False else "未标商品"
        print(f"  {shot['id']} {shot['start']}–{shot['end']}秒 {visible} 角色={','.join(shot['character_ids']) or '无'}")
        print(f"    {shot['description']}")
        if shot["camera_move"]:
            print(f"    运镜：{shot['camera_move']}")
    review = report["review"]
    if review["skipped"]:
        print("审核：这次跳过了。")
    else:
        print("审核：" + ("可以通过" if review["ready"] else "还有问题"))
        for item in review["suggestions"]:
            print(f"  建议：{item.get('note', '')}")
    for note in report["notes"]:
        print(note)
    print("视频提示词：")
    print(report["prompt"])


def _live_stage_call():
    from app.config import Settings
    from app.providers.gateway import Gateway
    settings = Settings.from_env()
    settings.data_root = Path(tempfile.mkdtemp(prefix="replan-offline-"))
    gateway = stub_media(Gateway(settings))

    async def stage_call(system, data, assets, max_tokens=7000):
        return await gateway._planner_json(system, data, assets, max_tokens=max_tokens)

    return gateway, stage_call


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线重排一个已保存的策划任务，不写数据库，不提交图片或视频。")
    parser.add_argument("--job", required=True)
    parser.add_argument("--db", default="data/studio.sqlite3")
    parser.add_argument("--skip-review", action="store_true")
    parser.add_argument("--json", dest="json_path", default="")
    args = parser.parse_args(argv)
    gateway, stage_call = _live_stage_call()
    print(f"诊断目录：{gateway.settings.data_root / 'diagnostics'}")
    try:
        report = asyncio.run(replan_offline(
            job_id=args.job, db_path=args.db, stage_call=stage_call, gateway=gateway,
            skip_review=args.skip_review))
    except Exception as exc:
        summary = getattr(exc, "issue_summary", None) or []
        code = getattr(exc, "error_code", None)
        print(f"离线重排没有完成。{exc}")
        if code:
            print(f"错误代码：{code}")
        if summary:
            print("问题：" + "；".join(str(item) for item in summary))
        return 1
    _print_report(report)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
