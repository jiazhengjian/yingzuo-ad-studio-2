"""Saying 再改一下 reopens the original clarification. Fake replies only."""
import asyncio
import json

import httpx
import pytest

from app.ad_form import (ad_form_question, confirmation_message, is_which_part_menu,
                         requests_clarification_revision)
from app.audience import audience_question
from app.cta import cta_question
from app.highlight import highlight_question
from app.providers.gateway import Gateway
from test_conversation_api import env, post_turn
from test_direction_selection import isolated_skill_loader


BANNED = ("请修改", "没通过", "硬门", "哪一部分", "这次想改的是哪一部分")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("clarification revision must not contact a model or media provider")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.mark.parametrize("text,expected", [
    ("再改一下", True),
    ("我想改一下", True),
    ("我想改个内容", True),
    ("改一下内容", True),
    ("刚才的选择再改一下", True),
    ("嗯，再改一下", True),
    ("改一下台词", False),
    ("改一下口播广告里的台词", False),
    ("改一下方案", False),
    ("再改一下时长", False),
    ("换个方向", False),
    ("我想改广告倾向，想做口播广告", False),
    ("把时长改成30秒", False),
])
def test_only_a_clarification_edit_reopens_the_card(text, expected):
    assert requests_clarification_revision(text) is expected


def test_which_part_menu_is_recognized_and_the_original_card_is_not():
    menu = {"kind": "clarify", "message": "这次想改的是哪一部分？", "questions": [{
        "id": "part", "prompt": "这次想改的是哪一部分？",
        "options": [
            {"id": "form", "label": "换表达方式"},
            {"id": "point", "label": "换想被记住的点"},
            {"id": "who", "label": "换拍给谁看"},
            {"id": "where", "label": "换看完后的去向"},
            {"id": "none", "label": "以上都不对"},
        ]}]}
    assert is_which_part_menu(menu) is True
    original = {"kind": "clarify", "message": confirmation_message([
        ad_form_question(), highlight_question("杯子"), audience_question("杯子"), cta_question()]),
        "questions": [ad_form_question(), highlight_question("杯子"), audience_question("杯子"), cta_question()]}
    assert is_which_part_menu(original) is False


def _card_questions():
    return [ad_form_question(), highlight_question("杯子"), audience_question("杯子"), cta_question()]


def _prepare(env, answers):
    client, app, pid, _ = env
    store = app.state.store
    with store._connection(True) as connection:
        connection.execute("UPDATE projects SET ad_form=NULL, cta_target=NULL, brief=? WHERE id=?",
                           ("给杯子做广告", pid))
    opening = post_turn(env, client_turn_id="open", content="给杯子做广告")
    assert opening.status_code == 200
    assert store.claim_job(opening.json()["job"]["id"])
    store.finish_decision(opening.json()["job"]["id"], {
        "kind": "clarify", "message": "先确认这几项。", "questions": _card_questions()})
    gate = store.pending_gate(pid)
    current = store.get_project(pid)
    answered = client.post(f"/api/projects/{pid}/turns", json={
        "client_turn_id": "answer",
        "expected_context_version": current["context_version"],
        "reply_to_gate_id": gate["id"],
        "answers": answers,
    })
    assert answered.status_code == 200, answered.text
    assert store.claim_job(answered.json()["job"]["id"])
    store.finish_decision(answered.json()["job"]["id"], {"kind": "reply", "message": "已记下。"})
    return client, app, pid, store


def _revision_turn(env, store, pid, content):
    client = env[0]
    current = store.get_project(pid)
    response = client.post(f"/api/projects/{pid}/turns", json={
        "client_turn_id": "revise-" + content,
        "expected_context_version": current["context_version"],
        "content": content,
    })
    assert response.status_code == 200, response.text
    job = store.get_job(response.json()["job"]["id"])
    return job, job["payload"]["frozen"]


def _assert_plain(blob):
    for token in BANNED:
        assert token not in blob


def test_again_reopens_the_original_card_without_a_model(env, monkeypatch):
    _client, app, pid, store = _prepare(env, [
        {"question_id": "ad_form", "option_id": "brand_tvc"},
        {"question_id": "highlight", "text": "瓶身透亮"},
        {"question_id": "audience", "text": "上班族通勤"},
        {"question_id": "cta_target", "option_id": "store"},
    ])
    job, frozen = _revision_turn(env, store, pid, "再改一下")

    async def fake(*args, **kwargs):
        pytest.fail("再改一下 must not call a model")

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    result = asyncio.run(Gateway(app.state.settings).decide(
        frozen["project"], frozen["assets"], frozen["messages"], frozen["gates"], None))
    assert [item["id"] for item in result["questions"]] == ["ad_form", "highlight", "audience", "cta_target"]
    assert result["message"] == confirmation_message(result["questions"])
    by_id = {item["question_id"]: item for item in result["prefilled_answers"]}
    assert by_id["ad_form"]["option_id"] == "brand_tvc"
    assert by_id["highlight"]["text"] == "瓶身透亮"
    assert by_id["audience"]["text"] == "上班族通勤"
    assert by_id["cta_target"]["option_id"] == "store"
    assert store.claim_job(job["id"])
    store.finish_decision(job["id"], result)
    gate = store.pending_gate(pid)
    saved = {item["question_id"]: item for item in gate["answers"]}
    assert saved["highlight"]["text"] == "瓶身透亮"
    assert saved["ad_form"]["option_id"] == "brand_tvc"
    _assert_plain(json.dumps({"result": result, "gate": gate}, ensure_ascii=False))


def test_story_reopen_still_omits_the_destination(env, monkeypatch):
    _client, app, pid, store = _prepare(env, [
        {"question_id": "ad_form", "option_id": "scene_story"},
        {"question_id": "highlight", "text": "窗边的瓶子"},
        {"question_id": "audience", "text": "自己用"},
        {"question_id": "cta_target", "option_id": "store"},
    ])
    _job, frozen = _revision_turn(env, store, pid, "我想改个内容")

    async def fake(*args, **kwargs):
        pytest.fail("改内容 must not call a model")

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    result = asyncio.run(Gateway(app.state.settings).decide(
        frozen["project"], frozen["assets"], frozen["messages"], frozen["gates"], None))
    assert [item["id"] for item in result["questions"]] == ["ad_form", "highlight", "audience"]
    assert "cta_target" not in {item["question_id"] for item in result["prefilled_answers"]}
    assert result["prefilled_answers"][0]["option_id"] == "scene_story"
    _assert_plain(json.dumps(result, ensure_ascii=False))


def test_which_part_reply_is_replaced_with_the_saved_card(env, monkeypatch, isolated_skill_loader):
    _client, app, pid, store = _prepare(env, [
        {"question_id": "ad_form", "option_id": "presenter_pitch"},
        {"question_id": "highlight", "text": "手感"},
        {"question_id": "audience", "text": "上班族"},
        {"question_id": "cta_target", "option_id": "message"},
    ])
    job, frozen = _revision_turn(env, store, pid, "这些回答我想调整一下")
    calls = []

    async def fake(self, system, data, originals, max_tokens=5500):
        calls.append(data.get("stage"))
        return {"kind": "clarify", "message": "这次想改的是哪一部分？", "questions": [{
            "id": "part", "prompt": "这次想改的是哪一部分？",
            "options": [
                {"id": "form", "label": "换表达方式"},
                {"id": "point", "label": "换想被记住的点"},
                {"id": "who", "label": "换拍给谁看"},
                {"id": "where", "label": "换看完后的去向"},
            ]}]}

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    result = asyncio.run(Gateway(app.state.settings).decide(
        frozen["project"], frozen["assets"], frozen["messages"], frozen["gates"], None))
    assert calls == ["strategy"]
    assert [item["id"] for item in result["questions"]] == ["ad_form", "highlight", "audience", "cta_target"]
    assert {item["question_id"]: item.get("option_id") or item.get("text") for item in result["prefilled_answers"]} == {
        "ad_form": "presenter_pitch", "highlight": "手感", "audience": "上班族", "cta_target": "message"}
    assert store.claim_job(job["id"])
    store.finish_decision(job["id"], result)
    _assert_plain(json.dumps(store.pending_gate(pid), ensure_ascii=False))


def test_a_line_edit_is_not_turned_back_into_the_clarification(env, monkeypatch, isolated_skill_loader):
    _client, app, pid, store = _prepare(env, [
        {"question_id": "ad_form", "option_id": "brand_tvc"},
        {"question_id": "highlight", "text": "瓶身透亮"},
        {"question_id": "audience", "text": "上班族通勤"},
        {"question_id": "cta_target", "option_id": "store"},
    ])
    _job, frozen = _revision_turn(env, store, pid, "改一下台词")
    calls = []

    async def fake(self, system, data, originals, max_tokens=5500):
        calls.append(data.get("stage"))
        return {"kind": "reply", "message": "台词可以再收短一点。"}

    monkeypatch.setattr(Gateway, "_planner_json", fake)
    result = asyncio.run(Gateway(app.state.settings).decide(
        frozen["project"], frozen["assets"], frozen["messages"], frozen["gates"], None))
    assert calls == ["strategy"]
    assert result["kind"] == "reply"
    assert result["message"] == "台词可以再收短一点。"
    assert not result.get("questions")
