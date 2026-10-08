"""The model names the next creative step. Timing, review and spend stay in code.

Fake planner replies only. No text, image, or video model is called.
"""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.ad_form import confirmation_message
from app.early_schedule import creative_steps, detach_model_schedule, schedule_from_choice, still_only_speech
from app.planning import STRATEGY_SELECTION_SYSTEM, strategy_decision_schema
from app.providers.gateway import Gateway
from app.release_gate import assert_spend_confirmed, images_blocked_reason
from test_clarification_mix import _offer, _questions, _run
from test_conversation_api import env
from test_conversation_pipeline import OfflineGateway, execute, fake_media, setup, turn
from test_direction_selection import isolated_skill_loader
from test_progressive_planning import progressive_gateway
from test_providers import install_transport, settings, source


BANNED = ("请修改", "没通过", "硬门", "不是最好")
FOUR_QUESTION_COPY = "先确认广告怎么拍、最想让观众记住的一点、拍给谁看、在什么情境下使用、看完去哪里，再继续准备方案。"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("early-step tests must not contact a model or media provider")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


def test_scheduler_follows_the_model_and_refuses_program_steps():
    historical = ("speech", "storyboard")
    assert creative_steps(schedule_from_choice("clarify"), historical) == ("clarify",)
    assert creative_steps(schedule_from_choice("directions"), historical) == ("directions",)
    assert creative_steps(schedule_from_choice("speech"), historical) == ("speech",)
    assert creative_steps(schedule_from_choice("storyboard"), historical) == ("storyboard",)
    assert creative_steps(schedule_from_choice(["storyboard", "speech"]), historical) == ("storyboard", "speech")
    assert creative_steps(schedule_from_choice(None), historical) == historical

    spent = schedule_from_choice("spend")
    assert spent.steps == () and spent.refused == "spend"
    assert creative_steps(spent, historical) == historical
    payload = {"next_step": "review", "next_steps": ["timing", "speech"], "kind": "plan"}
    mixed = detach_model_schedule(payload)
    assert mixed.refused in ("review", "timing") and mixed.steps == ("speech",)
    assert "next_step" not in payload and "next_steps" not in payload and payload["kind"] == "plan"
    schema = strategy_decision_schema(new_directions=True)
    assert schema["properties"]["next_step"]["enum"] == ["clarify", "directions", "speech", "storyboard"]
    assert "spend" not in schema["properties"]["next_step"]["enum"]
    assert "必要澄清每轮最多4题" in STRATEGY_SELECTION_SYSTEM
    assert "信息不够就只提问" in STRATEGY_SELECTION_SYSTEM
    assert "只有第二次还是只有台词，才按原来的顺序把分镜接上" in STRATEGY_SELECTION_SYSTEM
    assert "第二次写成澄清或出方向就照做" in STRATEGY_SELECTION_SYSTEM
    assert "不会替你补上" not in STRATEGY_SELECTION_SYSTEM
    assert still_only_speech(schedule_from_choice("speech"))
    assert not still_only_speech(schedule_from_choice("storyboard"))
    assert not still_only_speech(schedule_from_choice(["speech", "storyboard"]))
    assert confirmation_message([
        {"id": "ad_form"}, {"id": "highlight"}, {"id": "audience"}, {"id": "cta_target"}]) == FOUR_QUESTION_COPY


def test_model_can_choose_clarify_without_a_second_call(env, monkeypatch, isolated_skill_loader):
    mixed = _offer("先确认两件事。", _questions())
    mixed["next_step"] = "storyboard"
    mixed["next_steps"] = ["directions", "speech"]
    result, calls, store, pid, _asset = _run(env, monkeypatch, "给降噪耳机拍一条人物口播广告，看完让观众到店看看。", [mixed])
    assert [item["stage"] for item in calls] == ["strategy"]
    assert result["kind"] == "clarify"
    assert result.get("model_next_steps") is None
    assert result.get("direction_candidates") is None
    gate = store.pending_gate(pid)
    assert gate["kind"] == "clarification"
    assert gate["message"] == confirmation_message(gate["questions"])
    blob = json.dumps({"result": result, "gate": gate}, ensure_ascii=False)
    for token in BANNED:
        assert token not in blob


def test_model_can_choose_directions_and_review_stays_with_the_program(env, monkeypatch, isolated_skill_loader):
    from test_clarification_mix import _open
    from test_direction_selection import approved_direction_review, candidates_for, concept_pool_for
    client, app, pid, asset, job, _frozen = _open(
        env, "给降噪耳机拍一条人物口播广告，主打安静，上班族通勤，看完让观众到店看看。")
    candidate = candidates_for((client, app, pid, asset))
    offer = {
        "kind": "directions",
        "message": "请比较三个讲解方向",
        "brief": candidate.brief.model_dump(),
        "directions": [direction.model_dump() for direction in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions),
        "recommended_direction_id": candidate.recommended_direction_id,
        "next_step": "spend",
        "next_steps": ["speech"],
    }
    calls = []

    async def fake(self, system, data, originals, max_tokens=5500, **kwargs):
        calls.append(data.get("stage"))
        if data.get("stage") == "direction_review":
            return approved_direction_review(data["directions"])
        return deepcopy(offer)

    async def paid(self, *args, **kwargs):
        pytest.fail("direction offer must not submit image or video")

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    monkeypatch.setattr(Gateway, "create_designs", paid)
    monkeypatch.setattr(Gateway, "submit_video", paid)
    store = app.state.store
    frozen = store.get_job(job["id"])["payload"]["frozen"]
    result = asyncio.run(Gateway(app.state.settings).decide(
        frozen["project"], frozen["assets"], frozen["messages"], frozen["gates"], None))
    assert calls == ["strategy", "direction_review"]
    assert result["kind"] == "directions"
    assert result["model_next_steps"] == ["speech"]
    store.claim_job(job["id"])
    store.finish_decision(job["id"], result)
    gate = store.pending_gate(pid)
    following = store.select_direction(pid, {
        "client_turn_id": "chosen", "gate_id": gate["id"],
        "direction_id": candidate.directions[0].id,
        "expected_context_version": gate["context_version"],
        "accepted_revision": gate["revision"]})
    assert following["payload"]["model_next_step"] == ["speech"]
    assert following["submission_count"] == 0


def _continue(progressive_gateway, settings, source, choice):
    state = progressive_gateway
    state.calls.clear()
    state.contexts.clear()
    project = {"brief": state.brief, "duration_seconds": state.duration}
    return asyncio.run(Gateway(settings).decide(
        project, [source], [], [], None,
        selected_strategy=state.strategy.model_dump(),
        model_next_step=choice)), state


def test_model_can_choose_speech_or_storyboard(progressive_gateway, settings, source, monkeypatch):
    async def denied(self, *args, **kwargs):
        pytest.fail("a creative choice must not submit image or video")
    monkeypatch.setattr(Gateway, "create_designs", denied)
    monkeypatch.setattr(Gateway, "submit_video", denied)

    state = progressive_gateway
    state.follow_up_step = "speech"
    speech, state = _continue(progressive_gateway, settings, source, "speech")
    assert state.calls == ["ad_script", "strategy", "storyboard", "plan_review"]
    follow = [item for item in state.contexts if item.get("task_kind") == "next_step"]
    assert len(follow) == 1
    assert follow[0]["stage"] == "strategy"
    assert follow[0]["written"] == ["speech"]
    assert speech["kind"] == "plan"
    assert speech["plan"]["segments"]
    blob = json.dumps(speech, ensure_ascii=False)
    assert "台词先写好了" not in blob
    assert "画面还没排，现在不会出图" not in blob
    for token in BANNED:
        assert token not in speech["message"]

    board, state = _continue(progressive_gateway, settings, source, "storyboard")
    assert state.calls[0] == "storyboard"
    assert "ad_script" not in state.calls
    assert "strategy" not in state.calls
    assert "plan_review" in state.calls
    assert board["kind"] == "plan"
    assert board["plan"]["segments"]
    spoken = json.dumps(board["plan"], ensure_ascii=False)
    assert "看轮廓" in spoken and "看桌角" in spoken and "留一处" in spoken
    assert all(type(segment["duration_seconds"]) is int for segment in board["plan"]["segments"])


def test_second_answer_clarify_or_directions_is_followed(progressive_gateway, settings, source, monkeypatch):
    from test_direction_selection import candidates_for, concept_pool_for

    async def denied(self, *args, **kwargs):
        pytest.fail("a follow-up choice must not submit image or video")
    monkeypatch.setattr(Gateway, "create_designs", denied)
    monkeypatch.setattr(Gateway, "submit_video", denied)

    state = progressive_gateway
    state.follow_up_step = "clarify"
    state.reentry_reply = {"kind": "clarify", "message": "还要确认在哪用。", "questions": [{
        "id": "channel", "prompt": "主要在哪里展示这条广告？",
        "options": [{"id": "douyin", "label": "抖音商品页"}],
        "allow_custom": True, "allow_skip": True}]}
    clarified, state = _continue(progressive_gateway, settings, source, "speech")
    assert state.calls == ["ad_script", "strategy", "strategy"]
    assert [item.get("task_kind") for item in state.contexts].count("next_step") == 1
    assert clarified["kind"] == "clarify"
    assert clarified["questions"][0]["id"] == "channel"
    assert "storyboard" not in state.calls and "plan_review" not in state.calls
    blob = json.dumps(clarified, ensure_ascii=False)
    assert "台词先写好了" not in blob
    assert "画面还没排，现在不会出图" not in blob

    candidate = candidates_for((None, None, None, source))
    state.follow_up_step = "directions"
    state.reentry_reply = {
        "kind": "directions",
        "message": "请比较三个讲解方向",
        "brief": candidate.brief.model_dump(),
        "directions": [direction.model_dump() for direction in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions),
        "recommended_direction_id": candidate.recommended_direction_id,
    }
    offered, state = _continue(progressive_gateway, settings, source, "speech")
    assert state.calls[0] == "ad_script"
    assert state.calls.count("strategy") == 2
    assert "direction_review" in state.calls
    assert "storyboard" not in state.calls and "plan_review" not in state.calls
    assert [item.get("task_kind") for item in state.contexts].count("next_step") == 1
    assert offered["kind"] == "directions"
    assert len(offered["direction_candidates"]["directions"]) == 3
    blob = json.dumps(offered, ensure_ascii=False)
    assert "台词先写好了" not in blob
    assert "画面还没排，现在不会出图" not in blob


def test_model_can_choose_clarify_after_a_direction_was_selected(progressive_gateway, settings, source, monkeypatch):
    calls = []

    async def fake(self, system, data, originals, max_tokens=7000, **kwargs):
        calls.append(data.get("stage"))
        assert data.get("stage") == "strategy"
        return {"kind": "clarify", "message": "还要确认在哪用。", "questions": [{
            "id": "channel", "prompt": "主要在哪里展示这条广告？",
            "options": [{"id": "douyin", "label": "抖音商品页"}],
            "allow_custom": True, "allow_skip": True}]}

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    result, state = _continue(progressive_gateway, settings, source, "clarify")
    assert calls == ["strategy"]
    assert state.calls == []
    assert result["kind"] == "clarify"
    assert result["questions"][0]["id"] == "channel"
    for token in BANNED:
        assert token not in result["message"]


def test_program_still_times_reviews_and_refuses_spend(progressive_gateway, settings, source, monkeypatch):
    paid = []

    async def record_design(self, *args, **kwargs):
        paid.append("design")
        pytest.fail("model must not start image spend")

    async def record_video(self, *args, **kwargs):
        paid.append("video")
        pytest.fail("model must not start video spend")

    monkeypatch.setattr(Gateway, "create_designs", record_design)
    monkeypatch.setattr(Gateway, "submit_video", record_video)
    for choice in ("spend", "review", "timing"):
        result, state = _continue(progressive_gateway, settings, source, choice)
        assert state.calls[:3] == ["ad_script", "storyboard", "plan_review"]
        assert result["kind"] == "plan"
        durations = [segment["duration_seconds"] for segment in result["plan"]["segments"]]
        assert durations and all(type(item) is int for item in durations)
        assert result["plan"].get("plan_review") or result["plan"].get("review_status")
    assert paid == []
    with pytest.raises(ValueError, match="还没有确认"):
        assert_spend_confirmed(
            {"revision": 1, "payload": {"model_next_step": "spend"}},
            {"revision": 1})
    assert images_blocked_reason({"segments": []}) is not None


def test_confirmed_picture_and_video_order_is_unchanged(setup, fake_media):
    store, project, _asset, decision, settings = setup
    gateway = OfflineGateway([decision])
    execute(store, settings, gateway, turn(store, project["id"], "plan"))
    current = store.get_project(project["id"])
    job = store.create_job(current["id"], "generate", current["revision"], {
        "pause_for_creative": True, "model_next_step": "spend"})
    waiting = execute(store, settings, gateway, job)
    assert waiting["status"] == "awaiting_input"
    assert waiting["submission_count"] == 0 and not gateway.submissions
    assert gateway.calls == ["scene"]
    gate = store.pending_gate(project["id"], "creative")
    queued = store.confirm_creative(project["id"], {
        "gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "confirm"})
    result = execute(store, settings, gateway, queued)
    assert result["status"] == "ready", result.get("error")
    assert gateway.calls == ["scene", ("voice", 20), "video", "video"]
    assert len(gateway.submissions) == 2


def test_unconfirmed_plan_still_cannot_spend(setup, fake_media):
    store, project, _asset, decision, settings = setup
    gateway = OfflineGateway([])
    saved = store.update_plan(project["id"], decision["plan"])
    job = store.create_job(saved["id"], "generate", saved["revision"], {
        "pause_for_creative": True, "model_next_step": "spend"})
    payload = dict(job["payload"])
    payload.pop("merchant_confirmed_revision", None)
    payload["model_next_step"] = "spend"
    job = store.update_job(job["id"], payload=payload)
    result = execute(store, settings, gateway, job)
    assert result["status"] == "failed"
    assert "确认" in (result.get("error") or "")
    assert result["submission_count"] == 0
    assert gateway.design_calls == 0 and gateway.voice_calls == 0 and not gateway.submissions
