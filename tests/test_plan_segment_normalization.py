"""Final plan validation must keep a presenter film that already passed review.

The attached job payload was not on disk. This fixture follows the same failure:
a presenter_pitch plan compiled at 4.8 characters/second, then rejected when each
segment was checked again at the historical 2.9 rate. The public error hid the
pydantic message. Global shot clocks and beat membership are normalized here.
"""
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import Store
from app.pipeline import PlanStructureError, validate_plan
from app.planning_session import fingerprint
from app.schemas import Plan, Segment
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
    with pytest.raises(ValidationError, match="超过对应朗读时段"):
        Segment.model_validate(segment)
    accepted = validate_plan(plan, 10)
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
