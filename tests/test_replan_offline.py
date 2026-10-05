"""Offline re-plan reads a saved job and never calls image or video methods."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

from app.db import Store
from scripts.replan_offline import replan_offline
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import source


def test_replan_reads_a_saved_presenter_plan_without_media_calls(tmp_path, source):
    strategy = chosen_strategy(source, mode="character_dialogue")
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "presenter_pitch"
    raw["brief"]["product_name"] = "徕芬吹风机"
    from app.strategy import StrategyRecord
    strategy = StrategyRecord.model_validate(raw)
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    selected = next(item for item in strategy.directions if item.id == strategy.selected_direction_id)
    character_id = selected.production.speaker_id
    payload = {"plan": {"strategy": strategy.model_dump(),
                        "ad_script": director_output(strategy)["ad_script"],
                        "generation_strategy": "continuity_v1"}}
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "INSERT INTO projects(id,title,brief,route,status,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("project_1", "吹风机", "徕芬吹风机，早上不想吵到人", "studio", "ready", 1, 1, 1))
        connection.execute(
            "INSERT INTO assets(id,project_id,kind,name,path,mime,revision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("product_1", "project_1", "product", "商品", str(tmp_path / "product.png"), "image/png", 1, "{}", 1))
        connection.execute(
            "INSERT INTO assets(id,project_id,kind,name,path,mime,revision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("scene_1", "project_1", "scene", "场景", str(tmp_path / "scene.png"), "image/png", 1,
             json.dumps({"generated": True}), 2))
        connection.execute(
            "INSERT INTO assets(id,project_id,kind,name,path,mime,revision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("char_1", "project_1", "character", "主讲", str(tmp_path / "host.png"), "image/png", 1,
             json.dumps({"character_id": character_id, "character_name": "主讲", "sheet_layout": "horizontal_16x9"}), 3))
        connection.execute(
            "INSERT INTO jobs(id,project_id,revision,kind,status,phase,progress,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("job_plan", "project_1", 1, "plan", "ready", "ready", "{}", json.dumps(payload, ensure_ascii=False), 1, 1))
    before = json.loads(sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True).execute(
        "SELECT payload FROM jobs WHERE id='job_plan'").fetchone()[0])

    async def stage_call(system, data, assets, max_tokens=7000):
        if data.get("stage") == "plan_review":
            return accepted_review(data["final_plan"])
        if data.get("task_kind") == "line_jargon_rewrite":
            return {"lines": [{"beat_id": item["beat_id"], "narration": "吹头发的时候跟你说话，不用喊。"}
                              for item in data["lines"]]}
        assert data.get("stage") == "storyboard"
        value = director_output(strategy)
        value["video_flow"] = "先看见吹风机，再讲完停住。"
        for index, shot in enumerate(value["shots"]):
            shot["product_visible"] = True
            shot["continuity"]["camera_move"] = "中近景，镜头缓慢推进" if index < 2 else "中景，机位停住"
            if index == len(value["shots"]) - 1:
                shot["description"] += "机位停住。"
        return value

    gateway = SimpleNamespace(submit_video=lambda *args, **kwargs: None,
                              create_designs=lambda *args, **kwargs: None,
                              _submit=lambda *args, **kwargs: None)
    report = asyncio.run(replan_offline(
        job_id="job_plan", db_path=store.db_path, stage_call=stage_call, gateway=gateway))
    assert gateway._offline_media_calls == []
    assert report["lines"] and all("beat_id" in line and "text" in line for line in report["lines"])
    assert report["shots"] and report["shots"][0]["product_visible"] is True
    assert report["review"]["ready"] is True
    assert "嘴型" in report["prompt"] or "画外旁白" in report["prompt"]
    assert any("参考图" in note for note in report["notes"])
    after = json.loads(sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True).execute(
        "SELECT payload FROM jobs WHERE id='job_plan'").fetchone()[0])
    assert after == before
