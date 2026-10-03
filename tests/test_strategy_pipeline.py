"""Strategy persistence and execution gates using explicit offline planner results."""
import asyncio
import copy
import json
import httpx
import pytest

from app.db import Store
from app.pipeline import Pipeline
from app.providers.gateway import Gateway
from app.schemas import Plan
from ad_script_fixtures import stage_outputs
from test_conversation_pipeline import setup, fake_media, OfflineGateway, execute, turn, native_decision


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Offline pipeline test attempted real HTTP instead of its explicit supplier fixture")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


def fixture_outputs(project, asset, decision):
    brief = {"product_name": "蓝色玻璃瓶", "category": "general", "audience": "关注外观的顾客",
             "scenario": "商品展示", "facts": [{"id": "f1", "text": "蓝色玻璃瓶", "source_kind": "merchant", "source_id": "brief"}],
             "unknowns": ["功效未确认"], "assumptions": ["采用外观展示"]}
    direction = {"id": "appearance", "rule_id": "appearance_reveal", "label": "从轮廓看细节",
                 "audience": brief["audience"], "scenario": brief["scenario"], "angle": "外观展示",
                 "hook": "轮廓先行", "development": "展现瓶身细节", "payoff": "回到商品全貌",
                 "evidence_ids": ["f1"], "reason": "资料支持外观展示", "production_notes": ["不演示功效"]}
    strategy = {"kind": "plan", "message": "建议从轮廓进入细节。", "brief": brief,
                "directions": [direction], "selected_direction_id": "appearance"}
    plan = native_decision(decision)["plan"]
    for segment in plan["segments"]:
        for shot in segment["shots"]:
            shot.update(purpose="展示商品外观", evidence_ids=["f1"])
    return strategy, plan


def test_strategy_persists_through_restart_edit_and_image_confirmation(setup, fake_media, monkeypatch):
    store, project, asset, decision, settings = setup
    strategy, plan = fixture_outputs(project, asset, decision)
    outputs = stage_outputs(strategy, plan)
    gateway = Gateway(settings)
    calls = []
    async def response(system, context, sources, max_tokens=7000):
        calls.append(context["stage"])
        return copy.deepcopy(outputs[context["stage"]])
    monkeypatch.setattr(gateway, "_planner_json", response)
    result = execute(store, settings, gateway, turn(store, project["id"], "strategy"))
    assert result["status"] == "ready", result.get("error")
    assert calls == ["strategy", "ad_script", "script_review", "storyboard", "alignment_review"] and result["submission_count"] == 0
    assert result["payload"]["decision"]["planning_brief"] == strategy["brief"]
    assert store.pending_gate(project["id"]) is None  # No new strategy-selection gate.
    reopened = Store(store.db_path, store.media_root)
    saved = reopened.get_project(project["id"])
    assert saved["plan"]["strategy"]["selected_direction_id"] == "appearance"
    assert saved["plan"]["strategy"]["rules_used"][0]["sources"]
    labels = [e["label"] for e in reopened.events_after(project["id"])["events"]]
    assert "正在安排画面和台词" in labels
    edited = copy.deepcopy(saved["plan"])
    edited["title"] = "新的方案标题"
    updated = reopened.update_plan(project["id"], Plan.model_validate(edited).model_dump())
    assert updated["plan"]["strategy"] == saved["plan"]["strategy"]
    media_gateway = OfflineGateway([])
    generation = reopened.create_job(project["id"], "generate", updated["revision"], {"pause_for_creative": True})
    waiting = execute(reopened, settings, media_gateway, generation)
    assert waiting["status"] == "awaiting_input", waiting.get("error")
    gate = reopened.pending_gate(project["id"], "creative")
    assert len(gate["options"]) == 1 and gate["options"][0]["id"] == "current"
    assert media_gateway.design_calls == 1 and not media_gateway.submissions and media_gateway.voice_calls == 0
    queued = reopened.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "approve-images"})
    assert reopened.get_project(project["id"])["plan"]["strategy"] == saved["plan"]["strategy"]
    assert queued["status"] == "queued" and queued["submission_count"] == 0


@pytest.mark.parametrize("change,expected", [("cancel", "canceled"), ("new_context", "needs_review")])
def test_stale_or_canceled_strategy_does_not_start_storyboard_request(setup, monkeypatch, change, expected):
    store, project, asset, decision, settings = setup
    strategy, plan = fixture_outputs(project, asset, decision)
    outputs = stage_outputs(strategy, plan)
    gateway = Gateway(settings)
    calls = []
    async def response(system, context, sources, max_tokens=7000):
        calls.append(context["stage"])
        if change == "cancel":
            store.request_cancel(project["id"])
        else:
            store.add_message(project["id"], "user", "需求已变，请等一下")
        return copy.deepcopy(strategy)
    monkeypatch.setattr(gateway, "_planner_json", response)
    result = execute(store, settings, gateway, turn(store, project["id"], "changing"))
    assert result["status"] == expected, result.get("error")
    assert calls == ["strategy"]
    assert store.get_project(project["id"])["plan"] is None and not store.pending_gate(project["id"])


def test_change_direction_can_rewrite_narration_and_preserves_previous_revision(setup, monkeypatch):
    store, project, asset, decision, settings = setup
    old = native_decision(decision)["plan"]
    saved = store.update_plan(project["id"], old)
    strategy, plan = fixture_outputs(project, asset, decision)
    outputs = stage_outputs(strategy, plan)
    strategy["directions"][0].update(id="context", rule_id="everyday_context", label="桌边一刻",
                                     hook="从桌边使用情境切入", development="展示桌面中的商品", payoff="留在日常场景")
    strategy["selected_direction_id"] = "context"
    for segment in plan["segments"]:
        for shot in segment["shots"]:
            shot["narration"] = "看看桌边这一抹蓝。" if shot["start"] == 0 else ""
        segment["narration"] = "".join(s["narration"] for s in segment["shots"])
    plan["script"] = "".join(s["narration"] for s in plan["segments"])
    outputs = stage_outputs(strategy, plan)
    gateway = Gateway(settings)
    contexts = []
    async def response(system, context, sources, max_tokens=7000):
        contexts.append(context)
        return copy.deepcopy(outputs[context["stage"]])
    monkeypatch.setattr(gateway, "_planner_json", response)
    result = execute(store, settings, gateway, turn(store, project["id"], "change", "换成日常场景，从桌边切入"))
    assert result["status"] == "ready", result.get("error")
    new = store.get_project(project["id"])
    assert new["revision"] == saved["revision"] + 1 and new["plan"]["script"] != old["script"]
    assert contexts[0]["current_plan"]["script"] == old["script"]
    assert "桌边" in contexts[0]["conversation"][-1]["content"]
    assert contexts[1]["selected_direction"]["id"] == "context"
    assert "directions" not in contexts[1]["creative_strategy"]
    with store._connection() as connection:
        previous = connection.execute("SELECT plan FROM revisions WHERE project_id=? AND revision=?", (project["id"], saved["revision"])).fetchone()
    assert old["script"] in previous[0]


def test_legacy_planning_entry_also_stops_before_second_request_on_cancel(setup, monkeypatch):
    store, project, asset, decision, settings = setup
    strategy, _ = fixture_outputs(project, asset, decision)
    gateway = Gateway(settings)
    calls = []
    async def response(system, context, sources, max_tokens=7000):
        calls.append(context["stage"])
        store.request_cancel(project["id"])
        return copy.deepcopy(strategy)
    monkeypatch.setattr(gateway, "_planner_json", response)
    job = store.create_job(project["id"], "plan", project["revision"], {})
    result = execute(store, settings, gateway, job)
    assert result["status"] == "canceled" and calls == ["strategy"]
    assert store.get_project(project["id"])["plan"] is None


def test_legacy_planning_passes_answered_gates_for_evidence_filtering(setup, monkeypatch):
    from test_conversation_pipeline import CLARIFY
    store, project, asset, decision, settings = setup
    execute(store, settings, OfflineGateway([CLARIFY]), turn(store, project["id"], "ask"))
    gate = store.pending_gate(project["id"])
    answered = turn(store, project["id"], "answer", reply_to_gate_id=gate["id"], answers=[{"question_id": "channel", "option_id": "douyin"}])
    execute(store, settings, OfflineGateway([{"kind": "reply", "message": "已收到。"}]), answered)
    strategy, plan = fixture_outputs(project, asset, decision)
    outputs = stage_outputs(strategy, plan)
    gateway = Gateway(settings)
    contexts = []
    async def response(system, context, sources, max_tokens=7000):
        contexts.append(context)
        return copy.deepcopy(outputs[context["stage"]])
    monkeypatch.setattr(gateway, "_planner_json", response)
    p = store.get_project(project["id"])
    result = execute(store, settings, gateway, store.create_job(project["id"], "plan", p["revision"], {}))
    assert result["status"] == "ready", result.get("error")
    assert contexts[0]["gates"][-1]["status"] == "answered"
    assert "主要在哪里" not in str(contexts[0]["evidence_sources"])
    assert "抖音商品页" in str(contexts[0]["evidence_sources"])


@pytest.mark.parametrize("change,expected", [("cancel", "canceled"), ("new_context", "needs_review")])
@pytest.mark.parametrize("stop_after", ["ad_script", "script_review", "storyboard", "alignment_review"])
def test_stale_or_canceled_stage_stops_before_next_request_or_save(setup, monkeypatch, change, expected, stop_after):
    store, project, asset, decision, settings = setup
    strategy, plan = fixture_outputs(project, asset, decision)
    outputs = stage_outputs(strategy, plan)
    gateway = Gateway(settings)
    calls = []
    async def response(system, context, sources, max_tokens=7000):
        calls.append(context["stage"])
        if context["stage"] == stop_after:
            if change == "cancel":
                store.request_cancel(project["id"])
            else:
                store.add_message(project["id"], "user", "不要继续，需求已更新")
        return copy.deepcopy(outputs[context["stage"]])
    monkeypatch.setattr(gateway, "_planner_json", response)
    result = execute(store, settings, gateway, turn(store, project["id"], "changing-before-save"))
    assert result["status"] == expected, result.get("error")
    order = list(outputs)
    assert calls == order[:order.index(stop_after) + 1]
    assert store.get_project(project["id"])["plan"] is None and not store.pending_gate(project["id"])
    assert result["submission_count"] == 0


@pytest.mark.parametrize("failed_stage", ["script_review", "alignment_review"])
def test_failed_review_keeps_previous_plan_and_never_calls_media(setup, monkeypatch, failed_stage):
    store, project, asset, decision, settings = setup
    previous = store.update_plan(project["id"], native_decision(decision)["plan"])
    strategy, plan = fixture_outputs(project, asset, decision)
    outputs = stage_outputs(strategy, plan)
    outputs[failed_stage]["ready"] = False
    if failed_stage == "script_review":
        outputs[failed_stage]["checks"][0]["passed"] = False
    else:
        outputs[failed_stage]["spans"][0]["passed"] = False
    gateway = Gateway(settings)
    calls = []
    async def response(system, context, sources, max_tokens=7000):
        calls.append(context["stage"])
        if failed_stage == "script_review":
            assert context["stage"] != "storyboard"
        return copy.deepcopy(outputs[context["stage"]])
    monkeypatch.setattr(gateway, "_planner_json", response)
    result = execute(store, settings, gateway, turn(store, project["id"], "review-failed"))
    assert result["status"] == "failed", result.get("error")
    saved = store.get_project(project["id"])
    assert saved["plan"] == previous["plan"] and saved["revision"] == previous["revision"]
    expected_stages = list(outputs)
    assert result["submission_count"] == 0 and calls == expected_stages[:expected_stages.index(failed_stage) + 1]


def test_provider_reasoning_is_not_persisted_in_project_job_or_events(setup, monkeypatch):
    store, project, asset, decision, settings = setup
    strategy, plan = fixture_outputs(project, asset, decision)
    outputs = stage_outputs(strategy, plan)
    private_marker = "TEST_PRIVATE_REASONING_MUST_NOT_BE_SAVED"
    calls = []
    client = httpx.AsyncClient
    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        payload = json.loads(request.content)
        content = payload["messages"][1]["content"]
        context = json.loads(content if isinstance(content, str) else content[0]["text"])
        calls.append(context["stage"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(outputs[context["stage"]]), "reasoning_content": private_marker}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: client(*a, transport=httpx.MockTransport(handle), **kw))
    result = execute(store, settings, Gateway(settings), turn(store, project["id"], "private-reasoning"))
    assert result["status"] == "ready", result.get("error")
    assert calls == ["strategy", "ad_script", "script_review", "storyboard", "alignment_review"]
    assert result["submission_count"] == 0
    assert private_marker not in json.dumps(result)
    assert private_marker not in json.dumps(store.project_detail(project["id"]))
    assert private_marker not in json.dumps(store.events_after(project["id"]))
    with store._connection() as connection:
        assert private_marker not in "\n".join(connection.iterdump())
