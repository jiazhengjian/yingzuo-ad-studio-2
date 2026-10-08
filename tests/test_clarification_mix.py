"""A clarification that also writes a direction keeps the questions.

Fake planner replies only. No text, image, or video model is called.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.ad_form import (clarification_questions_cover, confirmation_message,
                         missing_clarification_ids, response_carries_direction)
from app.audience import audience_question
from app.highlight import highlight_question
from app.planning import StrategyDecision
from app.providers.gateway import Gateway
from test_conversation_api import env
from test_direction_selection import (approved_direction_review, candidates_for,
                                      concept_pool_for, isolated_skill_loader)


HEADPHONE = "给降噪耳机拍一条人物口播广告，看完让观众到店看看。"
PITCH = "UNSHOWN_DIRECTION_PITCH"
BANNED = ("请修改", "没通过", "硬门", "不是最好")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("clarification tests must not contact a model or media provider")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    for method in ("create_designs", "submit_video", "synthesize_voice", "_planner_json"):
        monkeypatch.setattr(Gateway, method, denied)


def _project(brief=HEADPHONE):
    return {"ad_form": "presenter_pitch", "cta_target": "store", "brief": brief}


def _questions():
    return [highlight_question("降噪耳机"), audience_question("降噪耳机")]


def _offer(message, questions, *, label=PITCH):
    return {
        "kind": "clarify",
        "message": message,
        "questions": questions,
        "directions": [{"id": "pitch", "label": label, "hook": "对着镜头把整条广告讲完"}],
        "concept_pool": [{"id": "pitch", "label": label}],
        "recommended_direction_id": "pitch",
        "brief": {"product_name": label, "scenario": label},
    }


def test_headphone_pitch_is_missing_only_the_selling_point_and_audience():
    project = _project()
    assert missing_clarification_ids(project) == ["highlight", "audience"]
    asked = {"kind": "clarify", "questions": _questions()}
    assert clarification_questions_cover(asked, ["highlight", "audience"]) is True
    assert response_carries_direction(_offer("先确认两件事。", _questions())) is True
    specs = {"kind": "clarify", "questions": [
        {"id": "product_info", "prompt": "几个、多大、什么包装？", "options": []},
        audience_question("降噪耳机")]}
    assert clarification_questions_cover(specs, ["highlight", "audience"]) is False
    delegated = _project(HEADPHONE + "你来定。")
    assert missing_clarification_ids(delegated) == []


def test_clarify_schema_still_rejects_a_direction_field():
    with pytest.raises(ValidationError, match="回复和澄清不包含完整创意候选"):
        StrategyDecision.model_validate({
            "kind": "clarify",
            "message": "先确认两件事。",
            "questions": _questions(),
            "recommended_direction_id": PITCH,
        })


def _open(env, brief):
    client, app, _, original = env
    created = client.post("/api/projects", json={"brief": brief}).json()
    pid = created["id"]
    source = Path(app.state.store.get_asset(original["id"])["path"]).read_bytes()
    asset = client.post(f"/api/projects/{pid}/assets", files={
        "file": ("耳机.png", source, "image/png")}).json()
    project = app.state.store.get_project(pid)
    job = client.post(f"/api/projects/{pid}/turns", json={
        "client_turn_id": "open",
        "expected_context_version": project["context_version"],
        "content": brief,
    }).json()["job"]
    frozen = app.state.store.get_job(job["id"])["payload"]["frozen"]
    return client, app, pid, asset, job, frozen


def _run(env, monkeypatch, brief, replies):
    client, app, pid, asset, job, frozen = _open(env, brief)
    calls = []
    pending = list(replies)

    async def fake(self, system, data, originals, max_tokens=5500):
        calls.append({"stage": data.get("stage"), "task_kind": data.get("task_kind"),
                      "repair_instruction": data.get("repair_instruction")})
        if data.get("stage") == "direction_review":
            return approved_direction_review(data["directions"])
        if not pending:
            pytest.fail(f"unexpected extra model call: {data.get('stage')}")
        return deepcopy(pending.pop(0))

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    result = asyncio.run(Gateway(app.state.settings).decide(
        frozen["project"], frozen["assets"], frozen["messages"], frozen["gates"], None))
    assert app.state.store.claim_job(job["id"])
    app.state.store.finish_decision(job["id"], result)
    return result, calls, app.state.store, pid, asset


def _visible(store, pid):
    detail = store.project_detail(pid)
    gate = store.pending_gate(pid)
    messages = [item["content"] for item in detail["messages"] if item["role"] == "assistant"]
    return gate, messages


def _assert_plain_card(result, store, pid, *hidden):
    gate, messages = _visible(store, pid)
    blob = json.dumps({"result": result, "gate": gate, "messages": messages}, ensure_ascii=False)
    for token in (*hidden, *BANNED):
        assert token not in blob
    assert result["kind"] == "clarify"
    assert gate["kind"] == "clarification"
    assert [item["id"] for item in gate["questions"]] == ["highlight", "audience"]
    assert gate["message"] == confirmation_message(gate["questions"])
    assert "最想让观众记住" in gate["questions"][0]["prompt"]
    assert "拍给谁看" in gate["questions"][1]["prompt"]
    assert "方向" not in gate["message"]
    return gate


def test_mixed_questions_and_direction_keep_the_card_without_retry(env, monkeypatch, isolated_skill_loader):
    mixed = _offer(f"{PITCH}：地铁里对着镜头把降噪讲完。", _questions())
    result, calls, store, pid, _asset = _run(env, monkeypatch, HEADPHONE, [mixed])
    project = store.get_project(pid)
    assert project["ad_form"] == "presenter_pitch"
    assert project["cta_target"] == "store"
    assert [item["stage"] for item in calls] == ["strategy"]
    assert calls[0]["task_kind"] is None
    _assert_plain_card(result, store, pid, PITCH)
    assert result.get("direction_candidates") is None
    assert store.pending_gate(pid)["kind"] == "clarification"


def test_a_plain_reply_is_not_turned_into_questions(env, monkeypatch, isolated_skill_loader):
    message = "想先聊聊这只耳机怎么用。"
    result, calls, store, pid, _asset = _run(env, monkeypatch, HEADPHONE, [
        {"kind": "reply", "message": message}])
    assert [item["stage"] for item in calls] == ["strategy"]
    assert result["kind"] == "reply" and result["message"] == message
    assert store.pending_gate(pid) is None
    detail = store.project_detail(pid)
    assert any(item["role"] == "assistant" and item["content"] == message for item in detail["messages"])


def test_complete_questions_without_a_direction_stay_one_call(env, monkeypatch, isolated_skill_loader):
    message = "先确认卖点和拍给谁。"
    result, calls, store, pid, _asset = _run(env, monkeypatch, HEADPHONE, [{
        "kind": "clarify", "message": message, "questions": _questions()}])
    assert [item["stage"] for item in calls] == ["strategy"]
    gate, messages = _visible(store, pid)
    assert [item["id"] for item in gate["questions"]] == ["highlight", "audience"]
    assert result["message"] == message
    assert messages == [message]
    assert result.get("direction_candidates") is None


def test_incomplete_questions_retry_once_then_hide_the_direction(env, monkeypatch, isolated_skill_loader):
    first = {
        "kind": "directions",
        "message": f"{PITCH}_ONE：三条口播已经写好。",
        "directions": [{"id": "pitch", "label": f"{PITCH}_ONE"}],
        "concept_pool": [{"id": "pitch"}],
        "recommended_direction_id": "pitch",
    }
    second = _offer(f"{PITCH}_TWO：还是把整条广告写完了。", [highlight_question("降噪耳机")], label=f"{PITCH}_TWO")
    result, calls, store, pid, _asset = _run(env, monkeypatch, HEADPHONE, [first, second])
    assert [item["stage"] for item in calls] == ["strategy", "strategy"]
    assert calls[1]["task_kind"] == "strategy_response_repair"
    assert "还缺的问题" in calls[1]["repair_instruction"]
    assert "directions" in calls[1]["repair_instruction"]
    _assert_plain_card(result, store, pid, f"{PITCH}_ONE", f"{PITCH}_TWO")
    assert result.get("direction_candidates") is None


def test_retry_that_asks_every_question_drops_a_mixed_direction(env, monkeypatch, isolated_skill_loader):
    first = {"kind": "clarify", "message": "只问卖点。", "questions": [highlight_question("降噪耳机")]}
    second = _offer(f"{PITCH}：问完了顺便把方向也写上。", _questions())
    result, calls, store, pid, _asset = _run(env, monkeypatch, HEADPHONE, [first, second])
    assert [item["stage"] for item in calls] == ["strategy", "strategy"]
    assert calls[1]["task_kind"] == "strategy_response_repair"
    _assert_plain_card(result, store, pid, PITCH)


def test_known_selling_point_and_audience_still_offer_directions_in_one_call(env, monkeypatch, isolated_skill_loader):
    brief = "给降噪耳机拍一条人物口播广告，主打安静，上班族通勤，看完让观众到店看看。"
    client, app, pid, asset, _job, _frozen = _open(env, brief)
    project = app.state.store.get_project(pid)
    assert project["ad_form"] == "presenter_pitch"
    assert project["cta_target"] == "store"
    assert missing_clarification_ids(project) == []
    candidate = candidates_for((client, app, pid, asset))
    offer = {
        "kind": "directions",
        "message": "请比较三个讲解方向",
        "brief": candidate.brief.model_dump(),
        "directions": [direction.model_dump() for direction in candidate.directions],
        "concept_pool": concept_pool_for(candidate.directions),
        "recommended_direction_id": candidate.recommended_direction_id,
    }
    calls = []

    async def fake(self, system, data, originals, max_tokens=5500):
        calls.append(data.get("stage"))
        if data.get("stage") == "direction_review":
            return approved_direction_review(data["directions"])
        assert data.get("task_kind") is None
        return deepcopy(offer)

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    frozen = app.state.store.get_job(_job["id"])["payload"]["frozen"]
    result = asyncio.run(Gateway(app.state.settings).decide(
        frozen["project"], frozen["assets"], frozen["messages"], frozen["gates"], None))
    assert calls == ["strategy", "direction_review"]
    assert result["kind"] == "directions"
    assert len(result["direction_candidates"]["directions"]) == 3
    assert app.state.store.claim_job(_job["id"])
    app.state.store.finish_decision(_job["id"], result)
    assert app.state.store.pending_gate(pid)["kind"] == "direction"
