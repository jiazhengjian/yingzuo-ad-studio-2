"""Merchant-facing follow-ups. Planner replies stay local; no paid image or video call."""
import io

import httpx
import pytest
from PIL import Image

from app.ad_form import asks_product_name, intake_strategy_response, pack_clarification_questions
from app.db import Store
from app.design import character_design_request
from app.production import required_designs
from app.story_logic import strip_speech_ordinals


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("These tests must not contact a supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


def test_product_name_is_not_asked():
    assert asks_product_name({"prompt": "这款香水的产品名称是什么?"})
    assert not asks_product_name({"prompt": "最想让观众记住的一点是什么?"})
    project = {"ad_form": "brand_tvc", "cta_target": "ai_decide", "brief": "香水"}
    packed = pack_clarification_questions([
        {"id": "product_name", "prompt": "这款香水的产品名称是什么?", "allow_skip": False},
        {"id": "scent", "prompt": "最想让观众记住的一点是什么?"},
    ], project)
    assert all(not asks_product_name(item) for item in packed)
    result = intake_strategy_response(
        {"kind": "clarify", "message": "先确认", "questions": [
            {"id": "q_name", "prompt": "这款香水的产品名称是什么?", "allow_custom": True, "allow_skip": False},
        ]}, project)
    assert result["kind"] == "clarify"
    assert result["questions"]
    assert all("名称" not in item["prompt"] for item in result["questions"])


def test_storyboard_ordinal_is_not_the_spoken_line():
    plan = {"script": "玫瑰清香", "segments": [{
        "prompt": "0–2秒：画外女声旁白说第三句",
        "purpose": "她开口说第三句",
        "narration": "玫瑰清香",
        "shots": [{"description": "画外女声旁白说第三句", "purpose": "心里想第三句",
                   "continuity": {"start_state": "她开口说第三句"}}],
        "speech_spans": [{"text": "玫瑰清香", "anchors": [{"visual_evidence": "旁白说第三句", "text": "玫瑰清香"}]}],
    }]}
    assert strip_speech_ordinals(plan) is True
    shot = plan["segments"][0]["shots"][0]
    assert "第三句" not in shot["description"]
    assert "第三句" not in shot["purpose"]
    assert "第三句" not in shot["continuity"]["start_state"]
    assert "第三句" not in plan["segments"][0]["prompt"]
    assert "第三句" not in plan["segments"][0]["speech_spans"][0]["anchors"][0]["visual_evidence"]
    assert plan["segments"][0]["speech_spans"][0]["text"] == "玫瑰清香"
    assert plan["segments"][0]["narration"] == "玫瑰清香"
    assert plan["script"] == "玫瑰清香"
    assert "旁白说" in shot["description"]


def test_states_stay_on_the_character_sheet():
    person = {"id": "girl", "name": "女生", "description": "灰色卫衣，中长发"}
    plan = {"production": {"characters": [person], "speaker_id": "girl"}, "segments": [{"shots": [
        {"id": "s0", "character_ids": ["girl"], "description": "头发是湿的。",
         "continuity": {"start_state": "头发是湿的", "end_state": "头发是湿的"}},
        {"id": "s1", "character_ids": ["girl"], "description": "头发已经干了。",
         "continuity": {"start_state": "头发已经干了", "end_state": "头发已经干了"}},
    ]}], "state_changes": [{
        "subject_label": "头发", "owner": "girl", "start": "湿", "result": "干",
        "start_shot": "s0", "result_shot": "s1",
    }]}
    designs = required_designs(plan)
    assert [item["kind"] for item in designs] == ["scene", "character"]
    assert all(item["kind"] != "start_frame" for item in designs)
    request = character_design_request({}, plan, person)
    assert "不要另出状态参考图" in request["prompt"]
    assert "起始状态：湿" in request["prompt"]
    assert "结果状态：干" in request["prompt"]
    assert "一张角色参考可以有多个状态" in request["prompt"]


def test_attached_image_can_continue_without_text(tmp_path):
    store = Store(tmp_path / "state.sqlite3", tmp_path / "media")
    project = store.create_project("一瓶香水", "product", ad_form="brand_tvc")
    pid = project["id"]
    job = store.create_turn(pid, {"client_turn_id": "t1", "expected_context_version": project["context_version"],
                                  "content": "拍一条香水广告"})
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "clarify", "message": "先确认卖点。", "questions": [
        {"id": "highlight", "prompt": "最想让观众记住的一点是什么?", "allow_custom": True, "allow_skip": True},
    ]})
    gate = store.pending_gate(pid)
    version = store.get_project(pid)["context_version"]
    with pytest.raises(ValueError, match="请回答问题或补充你的需求"):
        store.create_turn(pid, {"client_turn_id": "empty", "expected_context_version": version, "content": ""})
    folder = tmp_path / "media" / pid
    folder.mkdir(parents=True)
    path = folder / "bottle.png"
    Image.new("RGBA", (4, 4), (255, 255, 255, 0)).save(path, "PNG")
    store.add_asset(pid, "product", "bottle.png", path, "image/png", metadata={"generated": False})
    created = store.create_turn(pid, {
        "client_turn_id": "image-only",
        "expected_context_version": store.get_project(pid)["context_version"],
        "content": "",
    })
    assert created["status"] == "queued"
    assert created["id"] != job["id"]
    pending = store.pending_gate(pid)
    assert pending is None or pending["id"] != gate["id"]
    assert store.get_project(pid)["plan"] is None
