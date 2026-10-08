"""Automatic repair and retry loops stop after two rounds in each category."""
import asyncio
import json

import pytest

from app.config import Settings
from app.db import Store
from app.pipeline import (
    STRUCTURE_EXHAUSTED_CARD, Pipeline, _auto_retry_count, _structure_auto_failure,
)
from app.planner_recovery import AUTO_RETRY_CAP
from app.providers.gateway import PlannerInvalidResponse, PlannerRequestError, save_check_reasons
from app.progressive_planning import ProgressivePlanningError, progressive_plan
from test_progressive_planning import chosen_strategy, director_output
from test_providers import settings, source


def _failed_plan(tmp_path, checkpoint=True):
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    project = store.create_project("口播重试", "product", duration_seconds=15)
    job = store.create_turn(project["id"], {
        "client_turn_id": "plan-cap", "expected_context_version": 0, "content": "口播"})
    payload = dict(job["payload"])
    if checkpoint:
        payload["planning_checkpoint"] = {
            "version": 1, "scope": "scope", "request_count": 2,
            "transient_retries": 0, "empty_retries": 0, "manual_retries": 0,
            "entries": [
                {"stage": "ad_script", "input_hash": "script", "state": "ready", "attempts": 1,
                 "result": {"kept": "script"}},
                {"stage": "storyboard", "input_hash": "board", "state": "ready", "attempts": 1,
                 "result": {"kept": "storyboard"}},
            ],
        }
    store.update_job(job["id"], status="failed", payload=payload,
                     error="规划结果未通过结构校验，已有成果仍保留；尚未开始图片或视频生成",
                     progress={"error_code": "planning_invalid_response", "retryable": True,
                               "retry_action": "retry_planning"})
    store.set_project_status(project["id"], job["revision"], "failed")
    return store, project, job


def _mark_failed(store, project, job):
    store.update_job(job["id"], status="failed")
    store.set_project_status(project["id"], job["revision"], "failed")


def test_structure_schema_failure_retries_twice_then_stops(tmp_path):
    store, project, job = _failed_plan(tmp_path)
    pipeline = Pipeline(store, None, None)
    error = PlannerInvalidResponse("规划结果未通过结构校验，已有成果仍保留；尚未开始图片或视频生成")
    assert _structure_auto_failure(error)
    assert pipeline._auto_repair_plan_job(job["id"], error) is True
    once = store.get_job(job["id"])
    assert once["status"] == "queued"
    assert _auto_retry_count(once["payload"], "structure_auto_retries") == 1
    assert "storyboard" not in [entry["stage"] for entry in once["payload"]["planning_checkpoint"]["entries"]]
    _mark_failed(store, project, job)
    assert pipeline._auto_repair_plan_job(job["id"], error) is True
    twice = store.get_job(job["id"])
    assert _auto_retry_count(twice["payload"], "structure_auto_retries") == AUTO_RETRY_CAP
    _mark_failed(store, project, job)
    assert pipeline._auto_repair_plan_job(job["id"], error) is False
    stopped = store.get_job(job["id"])
    assert stopped["status"] == "failed"
    assert _auto_retry_count(stopped["payload"], "structure_auto_retries") == AUTO_RETRY_CAP
    assert STRUCTURE_EXHAUSTED_CARD == "这次没生成出来"


def test_exhausted_structure_retry_asks_to_regenerate_without_an_edit(tmp_path):
    store, project, job = _failed_plan(tmp_path)
    payload = dict(store.get_job(job["id"])["payload"])
    payload["structure_auto_retries"] = AUTO_RETRY_CAP
    store.update_job(job["id"], status="queued", error=None, payload=payload)
    store.set_project_status(project["id"], job["revision"], "queued")
    settings = Settings(data_root=tmp_path / "data", deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY")
    pipeline = Pipeline(store, None, settings)

    async def boom(_job):
        raise PlannerInvalidResponse("规划结果未通过结构校验，已有成果仍保留；尚未开始图片或视频生成")

    pipeline._plan = boom
    result = asyncio.run(pipeline.execute(job["id"]))
    assert result["status"] == "failed"
    assert result["error"] == STRUCTURE_EXHAUSTED_CARD
    assert result["progress"]["structure_retries_exhausted"] is True
    assert result["progress"]["retry_action"] == "retry_planning"
    assert "请修改" not in result["error"] and "继续处理" not in result["error"]
    regenerated = store.retry_planning_job(project["id"], job["id"])
    assert regenerated["status"] == "queued"
    assert "planning_checkpoint" not in regenerated["payload"]
    assert "structure_auto_retries" not in regenerated["payload"]


def test_content_retries_stop_at_two_and_transport_stays_failed(tmp_path):
    store, project, job = _failed_plan(tmp_path, checkpoint=False)
    pipeline = Pipeline(store, None, None)

    class Content(Exception):
        error_code = "planning_content_failed"
        retryable = False
        retry_action = None
        issue_summary = ["方案结构、商品依据或已确认内容需要调整"]

        def __str__(self):
            return "本次方案未能满足已确认要求，暂未采用"

    for _ in range(AUTO_RETRY_CAP):
        _mark_failed(store, project, job)
        assert pipeline._auto_repair_plan_job(job["id"], Content()) is True
    _mark_failed(store, project, job)
    assert pipeline._auto_repair_plan_job(job["id"], Content()) is False
    assert _auto_retry_count(store.get_job(job["id"])["payload"], "content_auto_retries") == AUTO_RETRY_CAP

    # Connection failures already retry inside the planning request. A second
    # job-level loop would keep calling the model after that cap.
    store.update_job(job["id"], payload={key: value for key, value in store.get_job(job["id"])["payload"].items()
                                         if key != "content_auto_retries"})
    _mark_failed(store, project, job)
    assert pipeline._auto_repair_plan_job(job["id"], PlannerRequestError("策划服务暂时没有连上")) is False
    stopped = store.get_job(job["id"])
    assert stopped["status"] == "failed"
    assert "transport_auto_retries" not in stopped["payload"]


def test_check_failure_writes_the_concrete_reason_not_only_raw_output(tmp_path, settings):
    settings.data_root = tmp_path / "data"
    reason = "镜头只能引用已确认的出镜角色"
    path = save_check_reasons(settings, [reason], error_code="planning_content_failed", stage_result={
        "stage": "storyboard", "raw_output": {"shots": [{"character_ids": ["missing_person"]}]}})
    saved = json.loads(path.read_text())
    assert saved["raw_output"]["shots"][0]["character_ids"] == ["missing_person"]
    assert saved["validation_errors"][0]["msg"] == reason
    assert reason in path.read_text()


def test_storyboard_fetch_stops_after_two_model_calls(source):
    strategy = chosen_strategy(source)
    calls = []

    async def stage_call(system, context, assets):
        calls.append(context["stage"])
        raise RuntimeError("分镜没有返回")

    with pytest.raises(ProgressivePlanningError):
        asyncio.run(progressive_plan(
            stage_call=stage_call, on_progress=None, record_failure=lambda _item: None,
            strategy=strategy, draft=None, draft_issues=[], duration=20,
            generation_strategy="continuity_v1", assets=[source], conversation=[],
            merchant_request="蓝瓶商品", current_plan=None))
    assert calls == ["storyboard", "storyboard"]
    assert len(calls) == AUTO_RETRY_CAP


def test_presenter_contract_failure_records_the_concrete_check(source):
    from app.strategy import StrategyRecord
    strategy = chosen_strategy(source)
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "presenter_pitch"
    strategy = StrategyRecord.model_validate(raw)
    board = director_output(strategy)
    for shot in board["shots"]:
        shot["character_ids"] = ["missing_person"]
    recorded = []

    async def stage_call(system, context, assets):
        if context["stage"] == "storyboard":
            return board
        if context["stage"] == "plan_refinement":
            return context["draft_storyboard"]
        raise AssertionError(context["stage"])

    asyncio.run(progressive_plan(
        stage_call=stage_call, on_progress=None, record_failure=recorded.append,
        strategy=strategy, draft=board["ad_script"], draft_issues=[], duration=15,
        generation_strategy="continuity_v1", assets=[source], conversation=[],
        merchant_request="口播", current_plan=None))
    reasons = [item for record in recorded if isinstance(record, dict) for item in record.get("failed_reasons") or []]
    assert any("出镜角色" in item for item in reasons)
    errors = [item for record in recorded if isinstance(record, dict) for item in record.get("validation_errors") or []]
    assert any("出镜角色" in (item.get("msg") or "") for item in errors)
    assert any(record.get("error_code") == "planning_content_failed" for record in recorded if isinstance(record, dict))


def test_storyboard_check_writes_the_reason_into_the_diagnostics_file(tmp_path, settings):
    from app.progressive_planning import storyboard_check_note
    from app.providers.gateway import save_failed_planner_output

    settings.data_root = tmp_path / "data"
    note = storyboard_check_note(ValueError("镜头只能引用已确认的出镜角色"))
    assert note["message"] == "分镜校验没有通过"
    assert note["error_code"] == "storyboard_check"
    assert note["validation_errors"][0]["loc"] == ["storyboard"]
    assert note["validation_errors"][0]["msg"] == "镜头只能引用已确认的出镜角色"
    quoted = storyboard_check_note(ValueError('引用了 "missing" 角色'))
    assert "missing" in quoted["validation_errors"][0]["msg"]
    assert '"' not in quoted["validation_errors"][0]["msg"]
    path = save_failed_planner_output(settings, {
        "stage": "storyboard", "raw_output": {"shots": [{"id": "s3"}]},
        "validation_errors": note["validation_errors"]})
    saved = json.loads(path.read_text())
    assert saved["validation_errors"][0]["msg"] == "镜头只能引用已确认的出镜角色"
    assert saved["validation_errors"][0]["type"] == "value_error"
    kept = save_failed_planner_output(settings, {
        "stage": "storyboard", "raw_output": {"shots": []},
        "validation_errors": quoted["validation_errors"]})
    assert "missing" in json.loads(kept.read_text())["validation_errors"][0]["msg"]
