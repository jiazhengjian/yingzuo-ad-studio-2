"""Stage-one intake persistence and provenance; all model calls are replaced."""
import asyncio
from copy import deepcopy
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ad_form import (AD_FORM_LABELS, ad_form_question, explicit_ad_form,
                         intake_strategy_response, unresolved_ad_form_change,
                         guard_ad_form_confirmation)
from app.config import Settings
from app.db import Store, StaleRevision
from app.main import create_app
from app.planning import evidence_sources
from app.providers.gateway import Gateway
from app.strategy import ProductBrief, validate_brief
from test_conversation_api import env, post_turn, ready_plan
from test_direction_selection import candidates_for, isolated_skill_loader, approved_direction_review, concept_pool_for


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Intake tests must not contact any real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.sqlite3", tmp_path / "media")


def turn(store, pid, content="", **extra):
    p = store.get_project(pid)
    return store.create_turn(pid, {"client_turn_id": "t-" + str(p["context_version"]),
        "expected_context_version": p["context_version"], "content": content, **extra})


def complete_reply(store, job):
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "reply", "message": "已记录。"})


@pytest.mark.parametrize("text,expected", [
    ("改成 TVC 广告大片", "brand_tvc"), ("我想做一个吹风机的TVC广告", "brand_tvc"),
    ("为吹风机拍一条TVC广告大片", "brand_tvc"), ("改成人物口播广告", "presenter_pitch"),
    ("广告倾向：剧情／场景广告", "scene_story"), ("根据商品推荐", "auto"),
    ("不要改成人物口播广告", None), ("TVC还是人物口播广告？", None),
    ("我看到的TVC广告", None), ("比如做TVC广告", None),
    ("我不想做TVC广告", None), ("如果拍TVC广告", None),
    ("我选TVC广告，改成人物口播广告", None), ("你好", None),
    ("如果预算允许，做TVC广告大片", None), ("做TVC广告大片，还是人物口播广告", None),
    ("我想拍TVC广告大片，但现在不要改", None),
    ("我想换个剧情广告", "scene_story"),
    ("我想换个口播广告", "presenter_pitch"),
    ("换一种人物讲解广告", "presenter_pitch"),
    ("我想改广告倾向，想做口播广告", "presenter_pitch"),
    ("广告倾向改一下，做产品展示广告", "brand_tvc"),
    ("把广告形式变更为口播广告", "presenter_pitch"),
    ("从TVC广告改成剧情广告", "scene_story"),
    ("把现在的产品展示广告换成剧情广告", "scene_story"),
    ("我想改成既剧情又口播广告", None),
    ("我想换成产品展示和人物讲解广告", None),
    ("人物讲解广告（口播）", "presenter_pitch"),
    ("换个剧情广告是不是更好", None),
    ("不要换成剧情广告", None),
    ("如果改成口播广告会怎样？", None),
    ("改一下口播广告里的台词", None),
])
def test_text_choices_are_explicit_not_keyword_mentions(text, expected):
    assert explicit_ad_form(text) == expected


@pytest.mark.parametrize("text,required", [
    ("我想换广告倾向", True), ("广告倾向改一下", True),
    ("换一种广告类型，具体还没决定", True),
    ("我想改成既剧情又口播的广告", True),
    ("我想改成既剧情又口播广告", True),
    ("我想换成产品展示和人物讲解广告", True),
    ("请换成采访式广告", True),
    ("我想换个剧情广告", False),
    ("产品展示广告和口播广告哪个好？", False),
    ("如果改成口播广告会怎样？", False),
    ("不要换成剧情广告", False), ("广告倾向先不要改", False),
    ("口播广告里的人物台词改一下", False),
    ("改一下口播广告里的台词", False),
    ("换个口播广告的演员", False),
    ("换一下产品展示广告的图片", False),
    ("改一下那个广告，不改倾向", False),
    ("改一下剧情", False), ("换个创意方向", False),
])
def test_uncertain_form_change_is_not_an_ordinary_content_edit(text, required):
    assert unresolved_ad_form_change(text) is required


@pytest.mark.parametrize("message,clarify", [
    ("这一版按剧情／场景的方向来做。", True),
    ("已改为人物讲解广告（口播）。", True),
    ("已为你改成剧情广告。", True),
    ("本次已切换为剧情广告。", True),
    ("我们已将广告倾向改为剧情广告。", True),
    ("接下来按口播广告来做。", True),
    ("已改成产品展示广告。", False),
    ("我推荐人物讲解广告，它适合演示产品。", False),
    ("如果改成口播广告，人物会面向观众。", False),
    ("例如输入“已改为口播广告”这句话。", False),
    ("没有改成口播广告，仍按当前类型。", False),
])
def test_only_contradictory_switch_announcements_are_guarded(message, clarify):
    raw = {"kind": "reply", "message": message}
    result = guard_ad_form_confirmation(raw, {"ad_form": "brand_tvc"})
    assert (result["kind"] == "clarify") is clarify
    if clarify:
        assert result["questions"] == [ad_form_question()]
        assert "产品展示广告" in result["message"]
    else:
        assert result == raw
    assert guard_ad_form_confirmation(raw, {"ad_form": "auto"}) == raw
    assert guard_ad_form_confirmation(raw, {}) == raw


def test_model_can_clarify_an_unrecognized_change_without_reasking_a_saved_choice():
    raw = {"kind": "clarify", "message": "需要确认更换广告倾向后的主要表达方式。",
           "questions": [{**ad_form_question(), "prompt": "模型自写的问题"}]}
    result = intake_strategy_response(raw, {"ad_form": "brand_tvc"})
    assert result["questions"] == [ad_form_question()]
    assert "当前保存" in result["message"]
    with pytest.raises(ValueError, match="不应重复提问"):
        intake_strategy_response({**raw, "message": "你想选哪种表达？"}, {"ad_form": "brand_tvc"})


def test_structured_and_text_preference_conflict_is_rejected_atomically(store):
    p = store.create_project("商品", "product", ad_form="brand_tvc")
    with pytest.raises(ValueError, match="本次文字要求不一致"):
        turn(store, p["id"], "改成人物讲解广告", ad_form="scene_story")
    assert store.get_project(p["id"]) == p
    assert not store.project_detail(p["id"])["messages"]


def test_uncertain_change_keeps_results_and_needs_no_model_then_clears_on_answer(env, monkeypatch):
    _, app, pid, _ = env
    store = app.state.store
    saved, _ = ready_plan(env)
    with store._connection(True) as c:
        c.execute("UPDATE projects SET ad_form='brand_tvc',current_render_id='old-render' WHERE id=?", (pid,))
    job = turn(store, pid, "我想换广告倾向")
    frozen = job["payload"]["frozen"]
    assert frozen["project"]["ad_form_confirmation_required"] is True
    assert frozen["project"]["ad_form"] == "brand_tvc"
    assert job["payload"]["base_plan_current"] is False
    async def forbidden(*args, **kwargs):
        pytest.fail("Unresolved preference uses a local clarification, no planner call")
    monkeypatch.setattr(Gateway, "_planner_json", forbidden)
    app.state.settings.deepseek_api_key = ""
    result = asyncio.run(Gateway(app.state.settings).decide(frozen["project"], frozen["assets"],
        frozen["messages"], frozen["gates"], frozen["project"]["plan"]))
    assert result["kind"] == "clarify" and result["questions"] == [ad_form_question()]
    store.claim_job(job["id"])
    # Final persistence defends the same boundary even for another provider.
    store.finish_decision(job["id"], {"kind": "reply", "message": "已改为口播广告。"})
    detail = store.project_detail(pid)
    assert detail["messages"][-1]["content"] == result["message"]
    assert detail["project"]["plan"] == saved["plan"]
    assert detail["project"]["current_render_id"] == "old-render"
    assert "ad_form_confirmation_required" not in detail["project"]
    gate = store.pending_gate(pid)
    next_job = turn(store, pid, reply_to_gate_id=gate["id"],
        answers=[{"question_id": "ad_form", "option_id": "presenter_pitch"}])
    next_project = next_job["payload"]["frozen"]["project"]
    assert next_project["ad_form"] == "presenter_pitch"
    assert "ad_form_confirmation_required" not in next_project
    assert next_project["plan"] == saved["plan"]
    complete_reply(store, next_job)
    assert store.pending_gate(pid) is None


def test_contradictory_reply_cannot_restore_old_direction_offer(env):
    from test_direction_selection import offer
    _, app, pid, _ = env
    store = app.state.store
    with store._connection(True) as c:
        c.execute("UPDATE projects SET ad_form='brand_tvc' WHERE id=?", (pid,))
    offer(env)
    old_gate = store.pending_gate(pid)
    job = turn(store, pid, "说明一下")
    assert job["payload"].get("pending_direction_candidates")
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "reply", "message": "这一版按剧情／场景的方向来做。"})
    assert store.pending_gate(pid)["kind"] == "clarification"
    assert store.pending_gate(pid)["questions"] == [ad_form_question()]
    assert store.get_project(pid)["ad_form"] == "brand_tvc"
    assert next(g for g in store.project_detail(pid)["gates"] if g["id"] == old_gate["id"])["status"] == "superseded"


def test_real_acceptance_sequence_through_api_frozen_gateway_and_reopened_store(env, monkeypatch, isolated_skill_loader):
    client, app, pid, _ = env
    store = app.state.store
    with store._connection(True) as c:
        c.execute("UPDATE projects SET ad_form=NULL WHERE id=?", (pid,))
    opening = post_turn(env, client_turn_id="opening").json()["job"]
    store.claim_job(opening["id"])
    store.finish_decision(opening["id"], {"kind": "clarify", "message": "确认主要表达", "questions": [ad_form_question()]})
    first_gate = store.pending_gate(pid)
    inputs = [
        ({"reply_to_gate_id": first_gate["id"], "answers": [{"question_id": "ad_form", "option_id": "brand_tvc"}]}, "brand_tvc"),
        ({"content": "我想换个剧情广告"}, "scene_story"),
        ({"content": "我想改广告倾向，想做口播广告"}, "presenter_pitch"),
    ]
    seen = []
    async def fake(self, system, data, sources, **kwargs):
        seen.append((data["stage"], data["ad_form"]))
        if data["stage"] == "direction_review":
            return approved_direction_review(data["directions"])
        candidate = candidates_for(env)
        brief = candidate.brief.model_dump()
        brief.pop("ad_form", None)
        return {"kind": "directions", "message": "请选择一个方向。", "brief": brief,
                "directions": [d.model_dump() for d in candidate.directions],
                "concept_pool": concept_pool_for(candidate.directions)}
    monkeypatch.setattr(Gateway, "_planner_json", fake)
    previous_gate = None
    for index, (fields, expected) in enumerate(inputs):
        current = client.get(f"/api/projects/{pid}").json()["project"]
        body = {"client_turn_id": f"acceptance-{index}", "expected_context_version": current["context_version"], **fields}
        response = client.post(f"/api/projects/{pid}/turns", json=body)
        assert response.status_code == 200
        job = response.json()["job"]
        # Idempotent resubmission does not create another model job.
        assert client.post(f"/api/projects/{pid}/turns", json=body).json()["job"]["id"] == job["id"]
        job = store.get_job(job["id"])
        frozen = job["payload"]["frozen"]
        assert frozen["project"]["ad_form"] == expected
        assert "pending_direction_candidates" not in job["payload"]
        result = asyncio.run(Gateway(app.state.settings).decide(frozen["project"], frozen["assets"],
            frozen["messages"], frozen["gates"], None))
        assert result["direction_candidates"]["brief"]["ad_form"] == expected
        assert seen[-2:] == [("strategy", expected), ("direction_review", expected)]
        store.claim_job(job["id"])
        store.finish_decision(job["id"], result)
        reopened = Store(store.db_path, store.media_root)
        assert reopened.get_project(pid)["ad_form"] == expected
        assert client.get(f"/api/projects/{pid}").json()["project"]["ad_form"] == expected
        if previous_gate:
            assert next(g for g in reopened.project_detail(pid)["gates"] if g["id"] == previous_gate)["status"] == "superseded"
        previous_gate = reopened.pending_gate(pid)["id"]


def test_database_upgrade_does_not_infer_form_or_rewrite_old_plan(tmp_path):
    db = tmp_path / "old.sqlite3"
    saved = '{"title":"historical","opaque":"keep-exact-bytes"}'
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT,brief TEXT,route TEXT,status TEXT,revision INTEGER,plan TEXT,current_render_id TEXT,created_at REAL,updated_at REAL)")
        c.execute("INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)", ("old", "old", "TVC广告大片", "product", "ready", 1, saved, "old-render", 1, 1))
    reopened = Store(db, tmp_path / "media")
    p = reopened.get_project("old")
    assert p["ad_form"] is None and p["ad_form_updated_context_version"] == 0
    assert p["current_render_id"] == "old-render"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT plan FROM projects").fetchone()[0] == saved


@pytest.mark.parametrize("form", list(AD_FORM_LABELS))
def test_api_create_exposes_saved_form_without_switching_production_route(tmp_path, form):
    app = create_app(Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY"), run_worker=False)
    client = TestClient(app)
    response = client.post("/api/projects", json={"brief": "给杯子做广告", "ad_form": form})
    assert response.status_code == 201
    p = response.json()
    assert p["ad_form"] == form and p["route"] == "product"
    assert client.get(f"/api/projects/{p['id']}").json()["project"]["ad_form"] == form
    assert client.post("/api/projects", json={"brief": "给杯子做广告", "ad_form": "invented"}).status_code == 422


@pytest.mark.parametrize("answer,expected", [
    *[({"option_id": value}, value) for value in AD_FORM_LABELS],
    ({"skipped": True}, "auto"), ({"text": "改成 TVC 广告大片"}, "brand_tvc"),
    ({"text": "TVC还是口播我还没决定"}, None),
])
def test_answer_survives_restart_and_is_frozen_before_planner_runs(store, answer, expected):
    p = store.create_project("给杯子做广告", "product")
    job = turn(store, p["id"])
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "clarify", "message": "确认表达倾向", "questions": [ad_form_question()]})
    gate = store.pending_gate(p["id"])
    next_job = turn(store, p["id"], reply_to_gate_id=gate["id"],
        answers=[{"question_id": "ad_form", **answer}])
    reopened = Store(store.db_path, store.media_root)
    current = reopened.get_project(p["id"])
    assert current["ad_form"] == expected
    assert current["context_version"] == 2
    assert reopened.get_job(next_job["id"])["payload"]["frozen"]["project"]["ad_form"] == expected
    assert len(reopened.project_detail(p["id"])["jobs"]) == 2


def test_preference_change_is_versioned_without_touching_old_plan_or_render(env):
    _, app, pid, _ = env
    store = app.state.store
    saved, _ = ready_plan(env)
    old_plan = deepcopy(saved["plan"])
    old_version = saved["context_version"]
    with store._connection(True) as c:
        c.execute("UPDATE projects SET current_render_id='old-render' WHERE id=?", (pid,))
    job = turn(store, pid, "改成 TVC 广告大片")
    current = store.get_project(pid)
    assert current["ad_form"] == "brand_tvc"
    assert current["context_version"] == old_version + 1
    assert current["plan_context_version"] < current["context_version"]
    assert current["plan"] == old_plan and current["current_render_id"] == "old-render"
    assert job["payload"]["base_plan_current"] is False
    assert job["payload"]["frozen"]["project"]["ad_form"] == "brand_tvc"
    with pytest.raises(StaleRevision):
        store.create_turn(pid, {"client_turn_id": "stale", "expected_context_version": old_version,
                               "ad_form": "scene_story"})
    assert store.get_project(pid)["ad_form"] == "brand_tvc"


def test_explicit_preference_is_not_changed_by_mention_or_null(store):
    p = store.create_project("给杯子做广告", "product", ad_form="scene_story")
    for content in ("不要改成人物口播广告", "TVC还是口播广告好？", "把文字缩短一点"):
        job = turn(store, p["id"], content, ad_form=None)
        assert store.get_project(p["id"])["ad_form"] == "scene_story"
        complete_reply(store, job)
    job = turn(store, p["id"], ad_form="auto")
    assert store.get_project(p["id"])["ad_form"] == "auto"
    assert job["payload"]["frozen"]["messages"][-1]["content"] == "广告倾向：根据商品推荐"


def test_intake_merges_canonical_question_but_never_inserts_it_into_greeting():
    project = {"ad_form": None}
    greeting = {"kind": "reply", "message": "你好"}
    assert intake_strategy_response(greeting, project) == greeting
    raw = {"kind": "clarify", "message": "确认信息", "questions": [
        {"id": f"q{i}", "prompt": str(i), "options": []} for i in range(3)]}
    result = intake_strategy_response(raw, project)
    assert len(result["questions"]) == 3 and result["questions"][0] == ad_form_question()
    assert len(raw["questions"]) == 3  # no mutation of model/raw checkpoint
    assert intake_strategy_response({"kind": "directions"}, project)["kind"] == "clarify"
    already = {"kind": "clarify", "questions": [ad_form_question(), raw["questions"][0]]}
    assert [q["id"] for q in intake_strategy_response(already, {"ad_form": "auto"})["questions"]] == ["q0"]


def test_brief_preference_is_inherited_and_model_cannot_change_or_invent_it():
    brief = {"product_name": "杯子", "category": "general"}
    raw = {"kind": "reply", "message": "已记录", "brief": brief}
    result = intake_strategy_response(raw, {"ad_form": "scene_story"})
    assert result["brief"]["ad_form"] == "scene_story" and "ad_form" not in brief
    with pytest.raises(ValueError, match="不能擅自"):
        intake_strategy_response({**raw, "brief": {**brief, "ad_form": "brand_tvc"}}, {"ad_form": "scene_story"})
    with pytest.raises(ValueError, match="不能擅自"):
        intake_strategy_response({**raw, "brief": {**brief, "ad_form": "brand_tvc"}}, {"ad_form": None})
    assert "ad_form" not in ProductBrief.model_validate(brief).model_dump()


@pytest.mark.parametrize("label", ["TVC 广告大片", "产品展示广告"])
def test_preference_and_skipped_facts_are_not_merchant_evidence(label):
    q = ad_form_question()
    q["options"][0]["label"] = label
    facts = {"id": "facts", "prompt": "是否有快干依据？", "options": []}
    answers = [{"question_id": "ad_form", "option_id": "brand_tvc"}, {"question_id": "facts", "skipped": True}]
    content = q["prompt"] + "：" + label + "\n是否有快干依据？：暂不确定，请使用谨慎表达"
    gate = {"kind": "clarification", "status": "answered", "questions": [q, facts], "answers": answers}
    registry = evidence_sources({"brief": "给吹风机做广告"}, [{"id": "m1", "role": "user", "content": content}], [gate])
    assert registry["m1"] == ""
    for claim in ("TVC 广告大片", "快干", "暂不确定"):
        brief = {"product_name": "吹风机", "category": "appliance", "unknowns": ["快干效果待确认"],
                 "facts": [{"id": "f", "text": claim, "source_kind": "merchant", "source_id": "m1"}]}
        with pytest.raises(ValueError):
            validate_brief(brief, registry, set())
    original = "已确认三档风速。功率不能确定。"
    mixed = "改成 TVC 广告大片。" + original
    registry = evidence_sources({"brief": mixed, "ad_form": "brand_tvc"}, [{"id": "m2", "role": "user", "content": mixed}])
    assert original in registry["m2"] and "TVC" not in registry["m2"]
    valid = {"product_name": "吹风机", "category": "appliance", "unknowns": ["功率"],
             "facts": [{"id": "f", "text": "已确认三档风速", "source_kind": "merchant", "source_id": "m2"}]}
    assert validate_brief(valid, registry, set()).facts[0].text == "已确认三档风速"


def test_gateway_intake_requests_only_reply_or_clarify_until_form_is_chosen(env, monkeypatch, isolated_skill_loader):
    _, app, _, original = env
    app.state.settings.direction_selection_enabled = True
    p = {**app.state.store.get_project(env[2]), "ad_form": None}
    contexts = []
    async def fake(self, system, data, sources, **kwargs):
        contexts.append(deepcopy(data))
        return {"kind": "clarify", "message": "确认信息", "questions": [
            {"id": "facts", "prompt": "有哪些已知特点？", "options": [], "allow_skip": True}]}
    monkeypatch.setattr(Gateway, "_planner_json", fake)
    result = asyncio.run(Gateway(app.state.settings).decide(p, [app.state.store.get_asset(original["id"])], [], [], None))
    assert result["kind"] == "clarify" and [q["id"] for q in result["questions"]] == ["ad_form", "cta_target", "facts"]
    assert len(contexts) == 1
    assert contexts[0]["allowed_decisions"] == ["reply", "clarify"]
    assert contexts[0]["ad_form_question"] == ad_form_question()
    assert contexts[0]["ad_form"] is None


def test_changing_form_releases_old_selected_direction(env, monkeypatch, isolated_skill_loader):
    _, app, pid, original = env
    app.state.settings.direction_selection_enabled = True
    candidate = candidates_for(env)
    candidate.brief.ad_form = "scene_story"
    strategy = candidate.select(candidate.directions[0].id).model_dump()
    project = {**app.state.store.get_project(pid), "ad_form": "brand_tvc", "ad_form_updated_context_version": 5}
    contexts = []
    async def fake(self, system, data, sources, **kwargs):
        contexts.append(deepcopy(data))
        return {"kind": "reply", "message": "按新的广告倾向继续。"}
    monkeypatch.setattr(Gateway, "_planner_json", fake)
    old_gate = {"kind": "direction", "status": "answered", "context_version": 3,
        "selected_option_id": candidate.directions[0].id, "options": [d.model_dump() for d in candidate.directions]}
    asyncio.run(Gateway(app.state.settings).decide(project, [app.state.store.get_asset(original["id"])], [],
                                                  [old_gate], {"strategy": strategy}))
    assert contexts[0]["existing_direction"] is None
    assert contexts[0]["direction_selection_required"] is True
    assert "directions" in contexts[0]["allowed_decisions"] and "plan" not in contexts[0]["allowed_decisions"]
    assert contexts[0]["ad_form"] == "brand_tvc"


@pytest.mark.parametrize("questions", [None, "invalid", ["bad"], [None]])
def test_malformed_intake_questions_remain_repairable(questions):
    with pytest.raises(ValueError, match="对象列表"):
        intake_strategy_response({"kind": "clarify", "questions": questions}, {"ad_form": None})


@pytest.mark.parametrize("text,cleared", [("不要TVC广告大片了", True),
    ("TVC广告大片不要了", True), ("我不想拍TVC广告了", True),
    ("如果不做TVC会怎样？", False), ("不喜欢片中红色", False)])
def test_explicit_withdrawal_requests_new_choice_without_guessing(store, text, cleared):
    p = store.create_project("给杯子做广告", "product", ad_form="brand_tvc")
    job = turn(store, p["id"], text)
    current = store.get_project(p["id"])
    assert current["ad_form"] == (None if cleared else "brand_tvc")
    assert job["payload"]["frozen"]["messages"][-1]["content"] == text
    if cleared:
        assert current["ad_form_updated_context_version"] == current["context_version"]
        result = intake_strategy_response({"kind": "directions"}, current)
        assert result["kind"] == "clarify"


def test_ad_form_filter_preserves_product_attributes_and_historical_sources():
    text = "给这款双档位吹风机拍一条TVC广告大片"
    registry = evidence_sources({"brief": text, "ad_form": "brand_tvc"}, [])
    brief = {"product_name": "吹风机", "category": "appliance", "facts": [
        {"id": "f", "text": "双档位", "source_kind": "merchant", "source_id": "brief"}]}
    assert validate_brief(brief, registry, set()).facts[0].text == "双档位"
    assert "TVC" not in registry["brief"]
    assert evidence_sources({"brief": text}, [])["brief"] == text


@pytest.mark.parametrize("case", json.loads((Path(__file__).parent / "fixtures/intake_acceptance/intake_cases.json").read_text())["cases"], ids=lambda c: c["id"])
def test_authored_intake_cases_preference_persistence_only(store, case):
    # This executes only the stated preference result, not the semantic/fact
    # expectations in these human acceptance cases or model quality.
    if not case.get("user_turns"):
        pytest.skip("UI card cases are exercised through the gate tests")
    p = store.create_project("入口离线测试商品", "product", ad_form=case["initial_ad_form"])
    for text in case["user_turns"]:
        job = turn(store, p["id"], text)
        complete_reply(store, job)
    assert store.get_project(p["id"])["ad_form"] == case["expected_ad_form"]


def test_legacy_intake_preserves_question_flow_but_inherits_explicit_form():
    raw = {"kind": "clarify", "message": "一个问题", "questions": [
        {"id": "facts", "prompt": "已知卖点？", "options": []}],
        "brief": {"product_name": "杯子", "category": "general"}}
    result = intake_strategy_response(raw, {"ad_form": "brand_tvc"}, ask_preference=False)
    assert result["questions"] == raw["questions"]
    assert result["brief"]["ad_form"] == "brand_tvc"
    assert intake_strategy_response(raw, {"ad_form": None}, ask_preference=False) == raw


@pytest.mark.parametrize("form", list(AD_FORM_LABELS))
def test_actual_gateway_candidate_and_selected_job_inherit_preference(env, monkeypatch, isolated_skill_loader, form):
    _, app, pid, original = env
    store = app.state.store
    app.state.settings.direction_selection_enabled = True
    job = turn(store, pid, ad_form=form)
    candidate = candidates_for(env)
    raw_brief = candidate.brief.model_dump()
    raw_brief.pop("ad_form", None)  # the server injects an omitted owned field
    contexts = []
    async def fake(self, system, data, sources, **kwargs):
        contexts.append(deepcopy(data))
        if data["stage"] == "strategy":
            return {"kind": "directions", "message": "请选择", "brief": raw_brief,
                    "directions": [d.model_dump() for d in candidate.directions],
                    "concept_pool": concept_pool_for(candidate.directions)}
        assert data["stage"] == "direction_review"
        return approved_direction_review(data["directions"])
    monkeypatch.setattr(Gateway, "_planner_json", fake)
    frozen = job["payload"]["frozen"]
    result = asyncio.run(Gateway(app.state.settings).decide(frozen["project"], frozen["assets"],
        frozen["messages"], frozen["gates"], None))
    assert result["direction_candidates"]["brief"]["ad_form"] == form
    assert all(c["ad_form"] == form for c in contexts)
    store.claim_job(job["id"])
    store.finish_decision(job["id"], result)
    gate = store.pending_gate(pid)
    following = store.select_direction(pid, {"client_turn_id": "chosen", "gate_id": gate["id"],
        "direction_id": candidate.directions[0].id, "expected_context_version": gate["context_version"],
        "accepted_revision": gate["revision"]})
    assert following["payload"]["selected_strategy"]["brief"]["ad_form"] == form
    assert following["payload"]["frozen"]["project"]["ad_form"] == form


def test_conflicting_card_choice_and_withdrawal_is_atomic(store):
    p = store.create_project("给杯子做广告", "product", ad_form="brand_tvc")
    job = turn(store, p["id"])
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "clarify", "message": "选择", "questions": [ad_form_question()]})
    gate = store.pending_gate(p["id"])
    before = store.get_project(p["id"])
    with pytest.raises(ValueError, match="撤回要求不一致"):
        turn(store, p["id"], "不要TVC广告大片了", reply_to_gate_id=gate["id"],
             answers=[{"question_id": "ad_form", "option_id": "brand_tvc"}])
    assert store.get_project(p["id"])["context_version"] == before["context_version"]
    assert store.pending_gate(p["id"])["status"] == "pending"


@pytest.mark.parametrize("answer,expected_form,retained,forbidden", [
    ({"text": "做TVC广告大片，产品支持三档风速。"}, "brand_tvc", "产品支持三档风速", "TVC"),
    ({"text": "做TVC广告大片。产品并不支持快干。"}, "brand_tvc", "产品并不支持快干", "TVC"),
    ({"text": "给双档位吹风机做TVC广告大片"}, "brand_tvc", "双档位", "TVC"),
    ({"text": "TVC 广告大片"}, "brand_tvc", "", "TVC"),
    ({"text": "做TVC广告大片"}, "brand_tvc", "", "TVC"),
    ({"text": "我想做20秒TVC广告大片"}, "brand_tvc", "", "TVC"),
    ({"option_id": "brand_tvc"}, "brand_tvc", "", "TVC"),
    ({"text": "想做定格动画，产品支持三档风速"}, None, "", "三档风速"),
])
def test_free_ad_form_answer_retains_separate_merchant_source_through_real_gate(store, answer, expected_form, retained, forbidden):
    p = store.create_project("给吹风机做广告", "product")
    job = turn(store, p["id"])
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "clarify", "message": "确认表达倾向", "questions": [ad_form_question()]})
    gate = store.pending_gate(p["id"])
    following = turn(store, p["id"], reply_to_gate_id=gate["id"],
        answers=[{"question_id": "ad_form", **answer}])
    frozen = following["payload"]["frozen"]
    assert frozen["project"]["ad_form"] == expected_form
    registry = evidence_sources(frozen["project"], frozen["messages"], frozen["gates"])
    message = frozen["messages"][-1]
    assert answer.get("text", AD_FORM_LABELS["brand_tvc"]) in message["content"]
    source = registry[message["id"]]
    assert forbidden not in source
    if not retained:
        assert source == ""
        return
    assert retained in source
    brief = {"product_name": "吹风机", "category": "appliance", "facts": [
        {"id": "f", "text": retained, "source_kind": "merchant", "source_id": message["id"]}]}
    assert validate_brief(brief, registry, set()).facts[0].text == retained
    if "并不" in retained:
        brief["facts"][0]["text"] = "支持快干"
        with pytest.raises(ValueError, match="否定或不确定限定"):
            validate_brief(brief, registry, set())
