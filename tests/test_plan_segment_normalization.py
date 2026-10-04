"""Final plan validation must keep a presenter film that already passed review.

The attached job payloads were not on disk. These fixtures follow the same
failures: a presenter_pitch plan compiled at 4.8 characters/second, then rejected
when a segment or creative option was checked again at the historical 2.9 rate.
Global shot clocks and beat membership are normalized here.
"""
import asyncio
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import Store
from app.pipeline import Pipeline, PlanStructureError, merchant_facing_error, validate_plan
from app.planning_session import fingerprint
from app.schemas import CreativeOption, Plan, Segment
from test_pipeline import PNG
from test_speech_spans import speech_plan_data


def _presenter_plan():
    plan = speech_plan_data()
    line = "字" * 20 + "。"
    segment = plan["segments"][0]
    span = segment["speech_spans"][0]
    span["text"] = segment["narration"] = plan["script"] = line
    span["anchors"] = [{"text": line, "start": 0, "end": 7, "shot_ids": ["desk", "detail"],
                        "visual_evidence": "办公桌上的蛋糕建立下午茶选择"}]
    plan["ad_script"]["beats"][0]["narration"] = line
    plan["script_review"]["script"] = deepcopy(plan["ad_script"])
    plan["strategy"]["brief"]["ad_form"] = "presenter_pitch"
    return Plan.model_validate(plan, context={"require_shot_narration": True}).model_dump()


def test_presenter_line_is_not_rejected_as_a_broken_timeline():
    plan = _presenter_plan()
    segment = plan["segments"][0]
    assert segment["speech_characters_per_tenth"] == 48
    bare = deepcopy(segment)
    bare.pop("speech_characters_per_tenth")
    with pytest.raises(ValidationError, match="超过对应朗读时段"):
        Segment.model_validate(bare)
    assert Segment.model_validate(segment).speech_spans[0].text == "字" * 20 + "。"
    stored = deepcopy(plan)
    stored["segments"][0].pop("speech_characters_per_tenth")
    accepted = validate_plan(stored, 10)
    assert accepted["segments"][0]["speech_characters_per_tenth"] == 48
    assert accepted["segments"][0]["speech_spans"][0]["text"] == "字" * 20 + "。"
    assert accepted["segments"][0]["shots"][0]["start"] == 0
    assert accepted["segments"][0]["shots"][-1]["end"] == 10


def test_global_shot_clock_is_rebased_onto_the_segment():
    plan = Plan.model_validate(speech_plan_data(), context={"require_shot_narration": True}).model_dump()
    segment = plan["segments"][0]
    for shot in segment["shots"]:
        shot["start"] += 10
        shot["end"] += 10
    for span in segment["speech_spans"]:
        span["start"] += 10
        span["end"] += 10
        for anchor in span["anchors"]:
            anchor["start"] += 10
            anchor["end"] += 10
    with pytest.raises(ValidationError, match="镜头时段必须从0连续排列"):
        Segment.model_validate(segment)
    accepted = validate_plan(plan, 10)
    shots = accepted["segments"][0]["shots"]
    assert [shot["start"] for shot in shots] == [0, 3, 7]
    assert shots[-1]["end"] == 10
    assert accepted["segments"][0]["speech_spans"][0]["start"] == 0
    assert accepted["segments"][0]["speech_spans"][0]["end"] == 7


def test_gaps_and_beat_membership_are_snapped_without_a_new_model_call():
    plan = _presenter_plan()
    segment = plan["segments"][0]
    segment["shots"][1]["start"] = 3.2
    segment["shots"][2]["end"] = 9.4
    segment["shots"][0]["beat_ids"] = ["choice"]
    segment["shots"][0]["beat_id"] = "ending"
    accepted = validate_plan(plan, 10)
    shots = accepted["segments"][0]["shots"]
    assert shots[0]["start"] == 0
    assert shots[0]["beat_id"] == "choice"
    assert shots[0]["beat_ids"] == ["choice"]
    assert shots[1]["start"] == 3
    assert shots[-1]["end"] == 10


def test_unfixed_segment_failure_writes_the_pydantic_message(tmp_path):
    plan = _presenter_plan()
    plan["segments"][0]["shots"][1]["id"] = "desk"
    settings = Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY")
    with pytest.raises(PlanStructureError, match="视频段镜头时轴或素材结构无效") as error:
        validate_plan(plan, 10, settings=settings)
    assert error.value.retryable is True
    assert error.value.retry_action == "retry_planning"
    assert any("镜头ID" in item["msg"] for item in error.value.validation_errors)
    saved = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text())
    assert saved["stage"] == "plan_validation"
    assert saved["validation_errors"][0]["loc"][:2] == ["segments", 0]
    assert saved["validation_errors"][0]["type"] == "value_error"
    assert "镜头ID" in saved["validation_errors"][0]["msg"]
    assert "字" * 20 not in next((tmp_path / "diagnostics").glob("*.json")).read_text()


def test_resume_replays_the_saved_storyboard_instead_of_starting_over(tmp_path):
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    project = store.create_project("转得这么快", "product", duration_seconds=10)
    job = store.create_turn(project["id"], {
        "client_turn_id": "plan-1", "expected_context_version": 0, "content": "口播"})
    checkpoint = {"version": 1, "scope": fingerprint({"project": project["id"]}),
        "request_count": 3, "transient_retries": 0, "empty_retries": 0, "manual_retries": 0,
        "entries": [
            {"stage": "ad_script", "input_hash": "script", "state": "ready", "attempts": 1,
             "result": {"kept": "script"}},
            {"stage": "storyboard", "input_hash": "board", "state": "ready", "attempts": 1,
             "result": {"kept": "storyboard"}},
            {"stage": "plan_review", "input_hash": "review", "state": "ready", "attempts": 1,
             "result": {"ready": True}}]}
    store.update_job(job["id"], status="failed", error="视频段镜头时轴或素材结构无效",
        payload={**job["payload"], "planning_checkpoint": checkpoint},
        progress={"error_code": "planning_invalid_response", "retryable": False, "retry_action": "edit_plan"})
    store.set_project_status(project["id"], job["revision"], "failed")
    retried = store.retry_planning_job(project["id"], job["id"])
    restored = retried["payload"]["planning_checkpoint"]
    assert retried["status"] == "queued"
    assert [entry["stage"] for entry in restored["entries"]] == ["ad_script", "storyboard", "plan_review"]
    assert all(entry["state"] == "ready" for entry in restored["entries"])
    assert restored["entries"][1]["result"] == {"kept": "storyboard"}
    assert retried["payload"]["plan_structure_replayed"] is True
    assert any(event["label"] == "策划已重新排队，将继续未完成的步骤"
               for event in store.project_detail(project["id"])["events"])


def _creative_option(segment):
    return {"id": "current", "label": "图片没问题，生成视频",
            "description": "使用已确认的商品原图与参考图片制作当前方案", "segment": segment}


def test_creative_option_uses_the_segment_rate_and_hides_pydantic_text(tmp_path):
    plan = _presenter_plan()
    segment = deepcopy(plan["segments"][0])
    segment.pop("speech_characters_per_tenth")
    with pytest.raises(ValidationError, match="超过对应朗读时段") as error:
        CreativeOption.model_validate(_creative_option(segment))
    shown = str(error.value)
    assert "validation error" in shown.lower() and "CreativeOption" in shown
    assert "validation error" not in merchant_facing_error(error.value).lower()
    assert "CreativeOption" not in merchant_facing_error(error.value)
    assert merchant_facing_error(error.value) == "台词和时长没有对上，已保留图片。请点继续制作。"
    stamped = deepcopy(plan["segments"][0])
    assert CreativeOption.model_validate(_creative_option(stamped)).segment.speech_characters_per_tenth == 48
    settings = Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY")
    from app.providers.gateway import save_failed_planner_output
    from app.pipeline import _validation_details
    save_failed_planner_output(settings, {"stage": "generate_validation", "raw_output": {"check": "model"},
        "validation_errors": _validation_details(error.value)})
    saved = next((tmp_path / "diagnostics").glob("*.json")).read_text()
    assert "generate_validation" in saved and "speech_spans" in saved
    assert "字" * 20 not in saved and "validation errors for" not in saved


class _HostSheet:
    """Records which character sheets a resume asks to redraw."""

    def __init__(self):
        self.calls = []

    async def create_designs(self, project, assets, plan, folder, cancelled=None, replace_keys=None):
        self.calls.append(list(replace_keys or []))
        path = folder / "host-horizontal.png"
        path.write_bytes(PNG + b"horizontal")
        from app.production import CHARACTER_SHEET_TEMPLATE
        return [{"kind": "character", "name": "讲解人 · 四视图", "source_path": path,
                 "metadata": {"character_id": "char_host", "character_name": "讲解人",
                              "view_layout": "four_view", "sheet_layout": "horizontal_16x9",
                              "template_version": CHARACTER_SHEET_TEMPLATE}}]


def _saved_presenter_job(tmp_path, sheet_metadata):
    """uploads/job_36fe_generate_fail.json was not in the workspace.

    The fixture is that failure: presenter speech that fits 4.8 characters/second,
    with a scene and a character sheet already saved.
    """
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    project = store.create_project("转得这么快", "product", duration_seconds=10, ad_form="presenter_pitch")
    product_path = store.media_root / "product.png"
    product_path.write_bytes(PNG)
    product = store.add_asset(project["id"], "product", "原图", product_path, "image/png")
    plan = _presenter_plan()
    plan["segments"][0].pop("speech_characters_per_tenth")
    plan["production"] = {"version": "1.0.0",
        "product_constraints": [{"text": "多层切面", "evidence_ids": ["layers"]}],
        "characters": [{"id": "char_host", "name": "讲解人",
                        "description": "创作设定：成年女性，齐肩深色直发，浅灰色家居长袖上衣"}],
        "speech_mode": "voiceover", "speaker_id": "", "voice_description": "自然普通话"}
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"], {"pause_for_creative": True})
    scene_path = store.media_root / "scene.png"
    sheet_path = store.media_root / "host.png"
    scene_path.write_bytes(PNG)
    sheet_path.write_bytes(PNG + b"sheet")
    scene = store.add_asset(project["id"], "scene", "场景参考图", scene_path, "image/png",
                            metadata={"generated": True})
    sheet = store.add_asset(project["id"], "character", "讲解人 · 四视图", sheet_path, "image/png",
                            metadata=sheet_metadata)
    plan["segments"][0]["reference_asset_ids"] = [product["id"], scene["id"], sheet["id"]]
    payload = job["payload"]
    payload["design_asset_ids"] = [scene["id"], sheet["id"]]
    payload["plan"] = plan
    store.update_job(job["id"], status="failed", phase="creating_designs",
                     error="4 validation errors for CreativeOption\nsegment.speech_spans.0\n  Value error, 旁白台词超过对应朗读时段的长度",
                     payload=payload)
    return store, project, plan, scene, sheet, job


def _production_settings(tmp_path):
    return Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY",
                    ffmpeg_path="TEST_ONLY", max_video_submissions=1, max_active_runs=1,
                    poll_seconds=0.01, provider_timeout_seconds=1, tts_voice="TEST_ONLY",
                    video_model="TEST_ONLY")


@pytest.mark.parametrize("sheet", ["legacy", "layout_only", "current"])
def test_continue_production_refreshes_only_an_unconfirmed_stale_character_sheet(tmp_path, sheet):
    from app.production import CHARACTER_SHEET_TEMPLATE
    metadata = {"generated": True, "character_id": "char_host", "character_name": "讲解人", "view_layout": "four_view"}
    if sheet != "legacy":
        metadata["sheet_layout"] = "horizontal_16x9"
    if sheet == "current":
        metadata["template_version"] = CHARACTER_SHEET_TEMPLATE
    store, project, _, scene, old_sheet, job = _saved_presenter_job(tmp_path, metadata)
    retried = store.update_job(job["id"], status="queued", error=None, phase="queued")
    gateway = _HostSheet()
    result = asyncio.run(Pipeline(store, gateway, _production_settings(tmp_path)).execute(retried["id"]))
    assert result["status"] == "awaiting_input", result.get("error")
    assert result["phase"] == "awaiting_creative"
    assert result.get("error") in (None, "")
    assert store.pending_gate(project["id"], "creative")["options"][0]["segment"]["speech_characters_per_tenth"] == 48
    assert store.get_asset(scene["id"])["path"] == scene["path"]
    assert store.get_asset(old_sheet["id"])["path"] == old_sheet["path"]
    ids = result["payload"]["design_asset_ids"]
    refs = result["payload"]["plan"]["segments"][0]["reference_asset_ids"]
    if sheet == "current":
        assert gateway.calls == []
        assert ids == [scene["id"], old_sheet["id"]]
        assert old_sheet["id"] in refs
        return
    assert gateway.calls == [["character_char_host"]]
    assert scene["id"] in ids and old_sheet["id"] not in ids
    assert old_sheet["id"] not in refs and scene["id"] in refs
    refreshed = store.get_asset(next(item for item in ids if item != scene["id"]))
    assert refreshed["metadata"]["sheet_layout"] == "horizontal_16x9"
    assert refreshed["metadata"]["template_version"] == CHARACTER_SHEET_TEMPLATE
    assert any(event["label"] == "角色参考已按当前横版模板重新生成，场景图仍用已保存的"
               for event in store.events_after(project["id"])["events"])


@pytest.mark.parametrize("hold", ["confirmed", "submitted", "frozen_video"])
def test_confirmed_or_submitted_jobs_keep_the_saved_character_sheet(tmp_path, hold):
    store, project, plan, scene, sheet, job = _saved_presenter_job(tmp_path, {
        "generated": True, "character_id": "char_host", "character_name": "讲解人", "view_layout": "four_view"})
    payload = store.get_job(job["id"])["payload"]
    if hold == "confirmed":
        payload["creative_confirmed"] = True
    elif hold == "frozen_video":
        payload["frozen_video_requests"] = {"seg": {"model": "TEST_ONLY"}}
    store.update_job(job["id"], payload=payload)
    if hold == "submitted":
        with store._connection(True) as connection:
            connection.execute("UPDATE jobs SET submission_count=1 WHERE id=?", (job["id"],))
    gateway = _HostSheet()
    resumed = asyncio.run(Pipeline(store, gateway, None)._designs(store.get_job(job["id"]), payload["plan"]))
    assert gateway.calls == []
    assert store.get_job(job["id"])["payload"]["design_asset_ids"] == [scene["id"], sheet["id"]]
    assert sheet["id"] in resumed["segments"][0]["reference_asset_ids"]
    assert store.get_asset(scene["id"])["path"] == scene["path"]
    assert store.get_asset(sheet["id"])["path"] == sheet["path"]
