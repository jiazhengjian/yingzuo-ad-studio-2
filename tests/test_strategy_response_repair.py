"""One strategy correction through real persistence and mocked planner HTTP."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.db import Store
from app.pipeline import Pipeline
from app.providers.gateway import Gateway, ProviderError
from test_conversation_api import env, post_turn
from test_direction_selection import candidates_for, approved_direction_review, isolated_skill_loader
from test_providers import chat_output, request_context


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Strategy correction tests must not contact real network or media providers")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    for method in ("create_designs", "submit_video", "synthesize_voice"):
        monkeypatch.setattr(Gateway, method, denied)


@pytest.fixture
def repair_case(env, monkeypatch, isolated_skill_loader):
    client, app, _, original = env
    settings = app.state.settings
    settings.direction_selection_enabled = settings.progressive_planning_enabled = True
    settings.planning_generation_strategy = "continuity_v1"
    settings.planner_retry_delay_seconds = 0
    # A new project and the same generated eight-pixel fixture, never user data.
    pid = client.post("/api/projects", json={"brief": "我想为吹风机做一条商品广告", "ad_form": "auto"}).json()["id"]
    source_path = app.state.store.get_asset(original["id"])["path"]
    asset = client.post(f"/api/projects/{pid}/assets", files={
        "file": ("test-appliance.png", Path(source_path).read_bytes(), "image/png")}).json()
    local_env = client, app, pid, asset
    candidate = candidates_for(local_env)
    valid = {"kind": "directions", "message": "请比较三个外观故事", "brief": candidate.brief.model_dump(),
        "directions": [direction.model_dump() for direction in candidate.directions],
        "recommended_direction_id": candidate.recommended_direction_id}
    valid = json.loads(json.dumps(valid, ensure_ascii=False).replace("瓶身", "机身").replace("瓶子", "吹风机"))
    valid["brief"]["category"] = "appliance"
    bad = deepcopy(valid)
    bad["directions"][2].update(rule_id="sensory_detail", label="UNSHOWN_INVALID_DIRECTION")
    state = SimpleNamespace(client=client, app=app, pid=pid, asset=asset, store=app.state.store,
        settings=settings, valid=valid, first=bad, corrected=deepcopy(valid), contexts=[], bodies=[],
        interrupt_stage=None, interrupt_always=False, interrupted=False, stop_before_repair=None)
    job = post_turn(local_env, content="仅按可见外观策划，受众和情境允许保守设定，不猜测功能。").json()["job"]
    state.job = job
    state.before = state.store.project_detail(pid)

    def respond(request):
        _, context = request_context(request)
        state.contexts.append(deepcopy(context))
        state.bodies.append(json.loads(request.content))
        stage = context["stage"]
        step = context.get("task_kind", stage)
        if step == state.interrupt_stage and (state.interrupt_always or not state.interrupted):
            state.interrupted = True
            raise asyncio.CancelledError()
        if stage == "strategy":
            if context.get("task_kind") == "strategy_response_repair":
                return chat_output(deepcopy(state.corrected))
            if state.stop_before_repair == "cancel":
                state.store.request_cancel(pid)
            elif state.stop_before_repair == "context_changed":
                state.store.add_message(pid, "user", "改为只展示外观，先不要继续旧需求。")
            return chat_output(deepcopy(state.first))
        assert stage == "direction_review", f"Unexpected stage: {stage}"
        return chat_output(approved_direction_review(context["directions"]))

    original_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs:
        original_client(*args, transport=httpx.MockTransport(respond), **kwargs))
    state.execute = lambda store=None: asyncio.run(
        Pipeline(store or state.store, Gateway(settings), settings).execute(job["id"]))
    state.stages = lambda: [context["stage"] for context in state.contexts]
    return state


def assert_no_plan_or_media(case, result):
    detail = case.store.project_detail(case.pid)
    assert detail["project"]["plan"] is None
    assert detail["project"]["revision"] == 0
    assert detail["assets"] == case.before["assets"]
    assert detail["renders"] == case.before["renders"]
    assert result["submission_count"] == 0
    assert "UNSHOWN_INVALID_DIRECTION" not in case.client.get(f"/api/projects/{case.pid}").text


def parsed_request(body):
    # The checkpoint canonicalizes object keys, so its replay may serialize the
    # previous response in a different key order without changing any input.
    result = deepcopy(body)
    result["messages"][1]["content"][0]["text"] = json.loads(result["messages"][1]["content"][0]["text"])
    return result


@pytest.mark.parametrize("first_error", ["category_rule", "schema"])
def test_invalid_appliance_response_can_be_corrected_but_still_requires_direction_review(repair_case, first_error):
    case = repair_case
    if first_error == "schema":
        case.first = deepcopy(case.valid)
        case.first["directions"][2]["unexpected_field"] = "not part of direction schema"
    result = case.execute()
    assert result["status"] == "awaiting_input", result.get("error")
    assert case.stages() == ["strategy", "strategy", "direction_review"]
    correction = case.contexts[1]
    assert correction["task_kind"] == "strategy_response_repair"
    assert correction["previous_response"] == case.first
    assert correction["validation_feedback"]
    assert correction["allowed_decisions"] == case.contexts[0]["allowed_decisions"]
    assert correction["assets"] == case.contexts[0]["assets"]
    assert case.contexts[2]["directions"] == case.valid["directions"]
    saved = result["payload"]["direction_candidates"]
    assert saved["direction_review"]["ready"] is True
    assert saved["brief"]["category"] == "appliance"
    assert {d["rule_id"] for d in saved["directions"]} == {"appearance_reveal", "everyday_context"}
    assert "selected_direction_id" not in saved
    assert case.store.pending_gate(case.pid)["kind"] == "direction"
    assert_no_plan_or_media(case, result)


def test_all_bad_citations_and_quotes_reach_the_single_correction(repair_case):
    case = repair_case
    case.first = deepcopy(case.valid)
    start = len(case.first["brief"]["facts"])
    case.first["brief"]["facts"] += [
        {"id": "bad_source", "source_kind": "merchant", "source_id": "mistyped_message_id", "text": "吹风机"},
        {"id": "bad_source_and_quote", "source_kind": "merchant", "source_id": "mistyped_message_id", "text": "独有自动修复功效"},
    ]
    result = case.execute()
    assert result["status"] == "awaiting_input", result.get("error")
    assert case.stages() == ["strategy", "strategy", "direction_review"]
    issues = case.contexts[1]["validation_feedback"]
    locations = {tuple(issue["loc"]) for issue in issues}
    assert ("brief", "facts", start, "source_id") in locations
    assert ("brief", "facts", start + 1, "source_id") in locations
    assert ("brief", "facts", start + 1, "text") in locations
    assert result["payload"]["direction_candidates"]["brief"]["facts"] == case.valid["brief"]["facts"]
    assert_no_plan_or_media(case, result)


def test_invalid_response_can_return_to_clarification_without_planning_or_media(repair_case):
    case = repair_case
    case.corrected = {"kind": "clarify", "message": "请先确认商品信息。", "questions": [
        {"id": "benefit", "prompt": "你确认想表达哪个商品特点？", "options": [], "allow_custom": True}]}
    result = case.execute()
    assert result["status"] == "awaiting_input"
    assert case.stages() == ["strategy", "strategy"]
    gate = case.store.pending_gate(case.pid)
    assert gate["kind"] == "clarification" and gate["questions"][0]["id"] == "benefit"
    assert "direction_candidates" not in result["payload"]
    assert_no_plan_or_media(case, result)


@pytest.mark.parametrize("second_error", ["same_category_rule", "foreign_source", "missing_prerequisite"])
def test_second_invalid_response_stops_without_saving_offer_and_revalidates_sources(repair_case, second_error):
    case = repair_case
    if second_error == "same_category_rule":
        case.corrected = deepcopy(case.first)
    elif second_error == "foreign_source":
        case.corrected["brief"]["facts"][0]["source_id"] = "foreign-image-not-uploaded"
    else:
        case.corrected["directions"][2]["rule_id"] = "operation_walkthrough"
    result = case.execute()
    assert result["status"] == "failed"
    assert case.stages() == ["strategy", "strategy"]
    assert case.store.pending_gate(case.pid) is None
    assert "direction_candidates" not in result["payload"]
    assert len(result["payload"]["planning_checkpoint"]["entries"]) == 2
    assert case.execute()["status"] == "failed"
    assert case.stages() == ["strategy", "strategy"]
    assert_no_plan_or_media(case, result)


@pytest.mark.parametrize("change", ["direction_id", "rule_id"])
def test_strategy_correction_cannot_replace_an_existing_merchant_selection(env, monkeypatch, isolated_skill_loader, change):
    _, app, pid, asset = env
    app.state.settings.direction_selection_enabled = True
    candidate = candidates_for(env)
    current = {"strategy": candidate.select("direction_1").model_dump()}
    original = deepcopy(current)
    selected = candidate.directions[1].model_dump()
    selected[change] = "direction_2" if change == "direction_id" else "appearance_reveal"
    if change == "direction_id":
        selected["id"] = selected.pop("direction_id")
    response = {"kind": "plan", "message": "修改当前方向", "brief": candidate.brief.model_dump(),
        "directions": [selected], "selected_direction_id": selected["id"]}
    contexts = []
    async def planned(self, system, context, originals, max_tokens=7000):
        contexts.append(deepcopy(context))
        assert context["stage"] == "strategy"
        return deepcopy(response)
    monkeypatch.setattr(Gateway, "_planner_json", planned)
    with pytest.raises(ProviderError):
        asyncio.run(Gateway(app.state.settings).decide(app.state.store.get_project(pid), [asset],
            [{"role": "user", "content": "把当前方向的台词改自然些"}], [], current))
    assert current == original
    assert len(contexts) == 2 and contexts[1]["task_kind"] == "strategy_response_repair"
    assert contexts[1]["existing_direction"]["id"] == "direction_1"
    assert contexts[1]["existing_direction"]["rule_id"] == "everyday_context"


@pytest.mark.parametrize(("stop", "status"), [("cancel", "canceled"), ("context_changed", "needs_review")])
def test_cancellation_or_context_change_prevents_correction_request(repair_case, stop, status):
    case = repair_case
    case.stop_before_repair = stop
    result = case.execute()
    assert result["status"] == status
    assert case.stages() == ["strategy"]
    assert case.store.pending_gate(case.pid) is None
    assert_no_plan_or_media(case, result)


def test_restart_replays_both_completed_strategy_responses_before_review(repair_case):
    case = repair_case
    case.interrupt_stage = "direction_review"
    with pytest.raises(asyncio.CancelledError):
        case.execute()
    saved = case.store.get_job(case.job["id"])["payload"]["planning_checkpoint"]
    assert [entry["state"] for entry in saved["entries"]] == ["ready", "ready", "running"]
    assert saved["request_count"] == 3
    restored = Store(case.store.db_path, case.store.media_root)
    restored.recover_jobs()
    result = case.execute(restored)
    assert result["status"] == "awaiting_input"
    assert case.stages() == ["strategy", "strategy", "direction_review", "direction_review"]
    checkpoint = result["payload"]["planning_checkpoint"]
    assert checkpoint["request_count"] == 4 and checkpoint["transient_retries"] == 1
    assert case.bodies[2] == case.bodies[3]
    assert_no_plan_or_media(case, result)


def test_repeated_shutdowns_in_same_correction_do_not_reset_retry_allowance(repair_case):
    case = repair_case
    case.interrupt_stage = "strategy_response_repair"
    case.interrupt_always = True
    for _ in range(3):
        with pytest.raises(asyncio.CancelledError):
            case.execute(Store(case.store.db_path, case.store.media_root))
    result = case.execute(Store(case.store.db_path, case.store.media_root))
    assert result["status"] == "failed"
    assert len(case.contexts) == 4
    assert sum(c.get("task_kind") == "strategy_response_repair" for c in case.contexts) == 3
    checkpoint = result["payload"]["planning_checkpoint"]
    assert len(checkpoint["entries"]) == 2
    assert checkpoint["request_count"] == 4 and checkpoint["transient_retries"] == 2
    assert parsed_request(case.bodies[1]) == parsed_request(case.bodies[2]) == parsed_request(case.bodies[3])
    assert_no_plan_or_media(case, result)


def test_restart_cannot_refresh_the_shared_total_request_budget(repair_case):
    case = repair_case
    case.interrupt_stage = "strategy_response_repair"
    with pytest.raises(asyncio.CancelledError):
        case.execute()
    payload = case.store.get_job(case.job["id"])["payload"]
    payload["planning_checkpoint"]["request_count"] = 12
    case.store.update_job(case.job["id"], payload=payload)
    restored = Store(case.store.db_path, case.store.media_root)
    restored.recover_jobs()
    result = case.execute(restored)
    assert result["status"] == "failed"
    assert result["progress"]["retryable"] is False
    assert len(case.contexts) == 2
    assert result["payload"]["planning_checkpoint"]["request_count"] == 12
    assert_no_plan_or_media(case, result)
