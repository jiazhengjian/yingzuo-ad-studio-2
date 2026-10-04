"""Offline conversation/gate contracts. No provider or model requests are made."""
import asyncio
import copy
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from app.config import Settings
from app.db import StaleRevision, Store
from app.main import create_app
from app.schemas import AgentDecision, Answer


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_root=tmp_path / "data", deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY")
    app = create_app(settings, run_worker=False)
    client = TestClient(app)
    # These downstream gate tests start after the merchant has delegated the
    # advertising form. Unselected-form intake is covered separately.
    p = client.post("/api/projects", json={"brief": "为我的透明玻璃瓶做商品广告", "ad_form": "auto"}).json()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    a = client.post(f"/api/projects/{p['id']}/assets", files={"file": ("商品.png", buf.getvalue(), "image/png")}).json()
    return client, app, p["id"], a


def post_turn(env, **overrides):
    c, app, pid, _ = env
    body = {"client_turn_id": "turn-1", "expected_context_version": app.state.store.get_project(pid)["context_version"]}
    body.update(overrides)
    return c.post(f"/api/projects/{pid}/turns", json=body)


def questions():
    return [{"id": "channel", "prompt": "主要用在哪里？", "options": [{"id": "shop", "label": "商品详情页"}, {"id": "feed", "label": "信息流广告"}], "allow_custom": True, "allow_skip": True}]


def clarify(env):
    result = post_turn(env)
    assert result.status_code == 200
    store = env[1].state.store
    job = result.json()["job"]
    assert store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "clarify", "message": "确认一个信息后继续。", "questions": questions()})
    return store.pending_gate(env[2]), job


def plan_for(asset_id):
    return {"title": "商品广告", "script": "透明瓶身，光影之间看见细节。了解这款商品。", "cta": "了解详情", "language": "zh-CN", "aspect_ratio": "9:16", "facts": {"name": "玻璃瓶", "claims": []}, "audio_strategy": "voiceover", "segments": [
        {"id": f"seg_0{i + 1}", "duration_seconds": 10, "purpose": "展示商品", "prompt": "保持商品真实外观，自然柔光，稳定慢推。", "shots": [{"start": 0, "end": 10, "description": "缓慢展示"}], "reference_asset_ids": [asset_id]} for i in range(2)]}


def ready_plan(env):
    _, app, pid, asset = env
    job = post_turn(env).json()["job"]
    store = app.state.store
    store.claim_job(job["id"])
    plan = plan_for(asset["id"])
    options = [{"id": f"choice_{i}", "label": f"开场{i}", "description": f"以第{i}种构图开场", "segment": copy.deepcopy(plan["segments"][0])} for i in (1, 2)]
    store.finish_decision(job["id"], {"kind": "plan", "message": "方案已准备好。", "plan": plan, "creative_options": options})
    return store.get_project(pid), options


def creative_gate(env):
    c, app, pid, _ = env
    p, alternatives = ready_plan(env)
    result = c.post(f"/api/projects/{pid}/generate", json={"accepted_revision": p["revision"]})
    assert result.status_code == 200
    job = result.json()["job"]
    store = app.state.store
    assert store.claim_job(job["id"])
    # A real local scene fixture satisfies the persisted-image gate; no provider call.
    scene_path = store.media_root / "TEST_SCENE.png"
    Image.new("RGB", (8, 8), "gray").save(scene_path)
    scene = store.add_asset(pid, "scene", "TEST scene", scene_path, "image/png", metadata={"generated": True})
    payload = store.get_job(job["id"])["payload"]
    payload.update(audio_asset_id="test_audio", design_asset_ids=[scene["id"]])
    store.update_job(job["id"], payload=payload)
    opts = [{"id": "current", "label": "保持原开场", "description": "当前计划的开场", "segment": p["plan"]["segments"][0]}, *alternatives]
    gate = store.create_creative_gate(job["id"], opts, "请选择开场")
    return p, job, gate


@pytest.mark.parametrize("scene_state", ["missing", "product_only", "deleted", "changed"])
def test_image_gate_requires_a_saved_unchanged_scene(env, scene_state):
    _, app, pid, original = env
    project, _ = ready_plan(env)
    store = app.state.store
    job = store.create_job(pid, "generate", project["revision"], {"pause_for_creative": True})
    assert store.claim_job(job["id"])
    payload = job["payload"]
    payload["design_asset_ids"] = []
    if scene_state == "product_only":
        payload["design_asset_ids"] = [original["id"]]
    elif scene_state in ("deleted", "changed"):
        path = store.media_root / "TEST_INVALID_SCENE.png"
        Image.new("RGB", (8, 8), "gray").save(path)
        scene = store.add_asset(pid, "scene", "TEST scene", path, "image/png", metadata={"generated": True})
        payload["design_asset_ids"] = [scene["id"]]
        if scene_state == "deleted":
            path.unlink()
        else:
            path.write_bytes(b"TEST_CHANGED_FILE")
    store.update_job(job["id"], payload=payload)
    options = [{"id": "current", "label": "图片没问题，生成视频", "description": "使用原图与场景图",
                "segment": project["plan"]["segments"][0]}]
    with pytest.raises(ValueError, match="场景图"):
        store.create_creative_gate(job["id"], options, "确认图片")
    assert store.pending_gate(pid) is None
    assert store.get_job(job["id"])["status"] == "running"


def test_voice_too_long_retry_is_rejected_without_mutation(env):
    c, app, pid, _ = env
    project, _ = ready_plan(env)
    store = app.state.store
    job = store.create_job(pid, "generate", project["revision"])
    store.update_job(job["id"], status="failed", error="配音文案过长", progress={"error_code": "voice_too_long", "retryable": False})
    before = store.project_detail(pid)
    response = c.post(f"/api/projects/{pid}/retry", json={"job_id": job["id"]})
    assert response.status_code == 422 and "精简文案" in response.json()["detail"]
    assert store.project_detail(pid) == before


def test_decision_and_answer_mutual_exclusivity():
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({"kind": "reply", "message": "您好", "questions": questions()})
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({"kind": "clarify", "message": "确认一下", "questions": questions(), "reasoning": "not public"})
    with pytest.raises(ValidationError):
        Answer.model_validate({"question_id": "channel", "option_id": "shop", "text": "同时填文字"})
    with pytest.raises(ValidationError):
        Answer.model_validate({"question_id": "channel"})


def test_new_project_without_images_uses_real_turn_and_cannot_generate_video(tmp_path):
    app = create_app(Settings(data_root=tmp_path / "data", deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY"), run_worker=False)
    c = TestClient(app)
    created = c.post("/api/projects", json={"brief": "我想给我的新商品做广告"})
    assert created.status_code == 201
    p = created.json()
    detail = c.get(f"/api/projects/{p['id']}").json()
    assert len(detail["messages"]) == 1
    assert detail["messages"][0]["role"] == "user"
    assert detail["messages"][0]["content"] == "我想给我的新商品做广告"
    assert detail["assets"] == []
    assert c.post(f"/api/projects/{p['id']}/plan", json={}).status_code == 422
    turn = c.post(f"/api/projects/{p['id']}/turns", json={"client_turn_id": "no-image-turn", "expected_context_version": p["context_version"]})
    assert turn.status_code == 200 and turn.json()["job"]["status"] == "queued"
    assert turn.json()["job"]["kind"] == "plan"
    frozen = app.state.store.get_job(turn.json()["job"]["id"])["payload"]["frozen"]
    assert frozen["assets"] == [] and len(frozen["messages"]) == 1
    # A saved plan object still cannot bypass the API's real-product-image prerequisite.
    app.state.store.update_plan(p["id"], plan_for("missing-test-product"))
    generated = c.post(f"/api/projects/{p['id']}/generate", json={"accepted_revision": 1})
    assert generated.status_code == 422 and "商品图" in generated.json()["detail"]
    assert not any(j["kind"] == "generate" for j in app.state.store.project_detail(p["id"])["jobs"])


def test_turn_atomic_snapshot_and_dedupe_before_version_and_idle(env):
    c, app, pid, asset = env
    before = app.state.store.get_project(pid)["context_version"]
    body = {"client_turn_id": "stable-id", "expected_context_version": before, "content": "突出瓶身设计", "reference_asset_ids": [asset["id"]]}
    first = post_turn(env, **body)
    assert first.status_code == 200
    second = post_turn(env, **body)
    assert second.status_code == 200 and second.json()["job"]["id"] == first.json()["job"]["id"]
    detail = app.state.store.project_detail(pid)
    assert detail["project"]["context_version"] == before + 1
    assert len(detail["jobs"]) == 1
    assert sum("突出瓶身设计" in m["content"] for m in detail["messages"]) == 1
    payload = detail["jobs"][0]["payload"]
    assert payload["conversation"] and payload["frozen"]["assets"][0]["id"] == asset["id"]
    assert payload["context_version"] == before + 1
    public = c.get(f"/api/projects/{pid}").text
    assert "frozen" not in public and str(app.state.settings.data_root) not in public
    assert post_turn(env, client_turn_id="other").status_code == 409


def test_clarification_releases_slot_and_answer_resumes_once(env):
    c, app, pid, _ = env
    gate, old_job = clarify(env)
    store = app.state.store
    assert store.get_job(old_job["id"])["status"] == "awaiting_input"
    assert not store.active_jobs()
    assert c.post(f"/api/projects/{pid}/plan", json={}).status_code == 409
    context = store.get_project(pid)["context_version"]
    body = {"client_turn_id": "answer-1", "expected_context_version": context, "reply_to_gate_id": gate["id"], "answers": [{"question_id": "channel", "option_id": "shop"}]}
    response = post_turn(env, **body)
    assert response.status_code == 200
    same = post_turn(env, **body)
    assert same.json()["job"]["id"] == response.json()["job"]["id"]
    detail = store.project_detail(pid)
    assert detail["gates"][0]["status"] == "answered"
    job = store.get_job(response.json()["job"]["id"])
    assert job["payload"]["frozen"]["gates"][0]["answers"][0]["option_id"] == "shop"
    assert "商品详情页" in job["payload"]["frozen"]["messages"][-1]["content"]
    assert store.claim_job(job["id"])


def test_invalid_and_stale_gate_answers_do_not_mutate_context(env):
    _, app, pid, _ = env
    gate, _ = clarify(env)
    before = app.state.store.get_project(pid)["context_version"]
    bad = post_turn(env, client_turn_id="bad", reply_to_gate_id=gate["id"], answers=[{"question_id": "channel", "option_id": "missing"}])
    assert bad.status_code == 422
    assert app.state.store.get_project(pid)["context_version"] == before
    stale = post_turn(env, client_turn_id="stale", expected_context_version=before - 1, content="新的要求")
    assert stale.status_code == 409
    assert app.state.store.pending_gate(pid)["id"] == gate["id"]


def test_free_text_supersedes_gate_and_keeps_question_context(env):
    _, app, pid, _ = env
    gate, old_job = clarify(env)
    result = post_turn(env, client_turn_id="new-direction", content="先告诉我能做哪些广告")
    assert result.status_code == 200
    store = app.state.store
    assert not store.pending_gate(pid)
    assert store.get_job(old_job["id"])["status"] == "canceled"
    frozen = store.get_job(result.json()["job"]["id"])["payload"]["frozen"]
    assert frozen["gates"][0]["id"] == gate["id"]
    assert frozen["gates"][0]["status"] == "superseded"


def test_late_decision_rejected_after_context_change_or_cancel(env):
    _, app, pid, _ = env
    store = app.state.store
    job = post_turn(env).json()["job"]
    store.claim_job(job["id"])
    store.add_message(pid, "user", "另一条需求")
    with pytest.raises(StaleRevision):
        store.finish_decision(job["id"], {"kind": "reply", "message": "旧答复"})
    assert not any(m["content"] == "旧答复" for m in store.project_detail(pid)["messages"])
    store.request_cancel(pid)
    with pytest.raises(StaleRevision):
        store.finish_decision(job["id"], {"kind": "clarify", "message": "旧问题", "questions": questions()})


def test_plan_completion_is_atomic_and_marks_real_creative_candidates(env):
    c, app, pid, _ = env
    p, _ = ready_plan(env)
    detail = c.get(f"/api/projects/{pid}").json()
    assert p["revision"] == 1 and p["plan_context_version"] == p["context_version"]
    assert detail["jobs"][0]["progress"]["creative_options_ready"] is True
    assert detail["messages"][-1]["content"] == "方案已准备好。"
    assert detail["events"][-1]["kind"] == "terminal"
    app.state.store.add_message(pid, "user", "需要更新文案")
    assert c.post(f"/api/projects/{pid}/generate", json={"accepted_revision": 1}).status_code == 409


@pytest.mark.parametrize("previous_outcome", ["failed", "clarify"])
def test_meta_reply_cannot_mark_unresolved_requirements_as_covered(env, previous_outcome):
    c, app, pid, _ = env
    p, _ = ready_plan(env)
    store = app.state.store
    old_plan_context = p["plan_context_version"]
    changing = post_turn(env, client_turn_id="new-requirement", content="改成冷色背景并重新策划").json()["job"]
    assert store.claim_job(changing["id"])
    if previous_outcome == "failed":
        store.update_job(changing["id"], status="failed")
    else:
        store.finish_decision(changing["id"], {"kind": "clarify", "message": "确认用途", "questions": questions()})
    meta = post_turn(env, client_turn_id="meta-question", content="你能生成多少秒？").json()["job"]
    assert store.claim_job(meta["id"])
    store.finish_decision(meta["id"], {"kind": "reply", "message": "当前可以制作20秒广告。"})
    now = store.get_project(pid)
    assert now["plan_context_version"] == old_plan_context < now["context_version"]
    assert c.post(f"/api/projects/{pid}/generate", json={"accepted_revision": now["revision"]}).status_code == 409


def test_meta_reply_keeps_previously_current_plan_usable(env):
    _, app, pid, _ = env
    ready_plan(env)
    store = app.state.store
    meta = post_turn(env, client_turn_id="meta-only", content="当前成片是什么比例？").json()["job"]
    assert store.claim_job(meta["id"])
    store.finish_decision(meta["id"], {"kind": "reply", "message": "当前是9:16竖屏。"})
    p = store.get_project(pid)
    assert p["plan_context_version"] == p["context_version"]
    generated = store.create_job(pid, "generate", p["revision"], {"pause_for_creative": True})
    assert len(generated["payload"]["creative_options"]) == 2


def test_creative_confirm_reuses_job_and_saved_assets_no_submission(env):
    c, app, pid, _ = env
    p, job, gate = creative_gate(env)
    store = app.state.store
    assert not store.active_jobs() and store.get_job(job["id"])["submission_count"] == 0
    body = {"gate_id": gate["id"], "accepted_revision": p["revision"], "option_id": "choice_1", "client_turn_id": "confirm-1"}
    result = c.post(f"/api/projects/{pid}/creative-confirm", json=body)
    assert result.status_code == 200
    assert result.json()["job"]["id"] == job["id"]
    same = c.post(f"/api/projects/{pid}/creative-confirm", json=body)
    assert same.status_code == 200 and same.json()["job"]["id"] == job["id"]
    resumed = store.get_job(job["id"])
    assert resumed["revision"] == p["revision"] + 1 and resumed["submission_count"] == 0
    assert resumed["started_at"] is None
    assert store.claim_job(job["id"])
    assert store.get_job(job["id"])["started_at"] is not None
    assert resumed["payload"]["creative_confirmed"] is True
    assert resumed["payload"]["audio_asset_id"] == "test_audio"
    scene_id = next(a["id"] for a in store.project_detail(pid)["assets"] if a["kind"] == "scene")
    assert resumed["payload"]["design_asset_ids"] == [scene_id]
    assert scene_id in resumed["payload"]["plan"]["segments"][0]["reference_asset_ids"]
    assert scene_id not in store.get_project(pid)["plan"]["segments"][0]["reference_asset_ids"]


def test_creative_note_queues_planner_not_video_and_free_text_retires_gate(env):
    c, app, pid, _ = env
    p, job, gate = creative_gate(env)
    result = c.post(f"/api/projects/{pid}/creative-confirm", json={"gate_id": gate["id"], "accepted_revision": p["revision"], "option_id": "current", "note": "背景换成木质桌面", "client_turn_id": "note-1"})
    assert result.status_code == 200
    assert result.json()["job"]["kind"] == "plan"
    old = app.state.store.get_job(job["id"])
    assert old["status"] == "canceled" and old["submission_count"] == 0
    new = app.state.store.get_job(result.json()["job"]["id"])
    assert new["payload"]["frozen"]["gates"][0]["note"] == "背景换成木质桌面"


def test_free_text_supersedes_pending_creative_gate(env):
    _, app, pid, _ = env
    _, old_job, gate = creative_gate(env)
    result = post_turn(env, client_turn_id="change-creative", content="请重新想开场")
    assert result.status_code == 200 and result.json()["job"]["kind"] == "plan"
    assert app.state.store.get_job(old_job["id"])["status"] == "canceled"
    assert app.state.store.project_detail(pid)["gates"][0]["status"] == "superseded"
    assert not app.state.store.pending_gate(pid)


@pytest.mark.parametrize("kind", ["clarification", "creative"])
def test_upload_keeps_waiting_gate_and_answer_reads_new_product(env, kind):
    c, app, pid, original = env
    store = app.state.store
    if kind == "clarification":
        gate, waiting = clarify(env)
    else:
        _, waiting, gate = creative_gate(env)
    before = store.get_project(pid)
    prior_assets = store.project_detail(pid)["assets"]
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, "PNG")
    response = c.post(f"/api/projects/{pid}/assets", files={"file": ("补充商品.png", buffer.getvalue(), "image/png")})
    assert response.status_code == 201
    added = response.json()
    after = store.get_project(pid)
    assert after["context_version"] == before["context_version"] + 1
    assert after["current_render_id"] == before["current_render_id"]
    assert after["plan"] == before["plan"]
    assert after["status"] == before["status"]
    kept = store.pending_gate(pid)
    assert kept["id"] == gate["id"] and kept["status"] == "pending"
    assert kept["context_version"] == after["context_version"]
    waiting_job = store.get_job(waiting["id"])
    assert waiting_job["status"] == "awaiting_input" and waiting_job["submission_count"] == 0
    assert waiting_job["payload"]["context_version"] == after["context_version"]
    detail = store.project_detail(pid)
    assert detail["gates"][0]["status"] == "pending"
    assert {a["id"] for a in prior_assets}.issubset({a["id"] for a in detail["assets"]})
    assert any(e["job_id"] == waiting["id"] and e["kind"] == "gate" and "仍然有效" in e["label"] for e in detail["events"])
    if kind == "clarification":
        answer = post_turn(env, client_turn_id="with-new-image", reply_to_gate_id=gate["id"], answers=[{"question_id": "channel", "option_id": "shop"}])
        assert answer.status_code == 200
        frozen = store.get_job(answer.json()["job"]["id"])["payload"]["frozen"]
        assert {original["id"], added["id"]}.issubset({a["id"] for a in frozen["assets"]})
    else:
        confirmed = c.post(f"/api/projects/{pid}/creative-confirm", json={"gate_id": gate["id"], "accepted_revision": after["revision"], "option_id": "current", "client_turn_id": "keep-creative"})
        assert confirmed.status_code == 200
        resumed = store.get_job(waiting["id"])
        assert resumed["id"] == confirmed.json()["job"]["id"]
        assert resumed["status"] == "queued" and resumed["submission_count"] == 0
        assert added["id"] not in resumed["payload"].get("design_asset_ids", [])


def test_events_order_resume_and_identical_progress_has_no_fake_steps(env):
    c, app, pid, _ = env
    store = app.state.store
    job = post_turn(env).json()["job"]
    store.claim_job(job["id"])
    store.update_job(job["id"], phase="understanding")
    cursor = store.events_after(pid)["cursor"]
    store.update_job(job["id"], phase="understanding", progress={"poll": 1})
    assert store.events_after(pid, cursor)["events"] == []
    store.update_job(job["id"], phase="deciding")
    batch = c.get(f"/api/projects/{pid}/events?after={cursor}").json()
    assert len(batch["events"]) == 1 and batch["cursor"] > cursor
    assert c.get(f"/api/projects/{pid}/events?after={batch['cursor']}").json()["events"] == []
    route = next(r for r in app.routes if getattr(r, "path", "") == "/api/projects/{project_id}/events/stream")

    class Request:
        headers = {"last-event-id": str(cursor)}

        async def is_disconnected(self):
            return False

    async def inspect_stream():
        response = await route.endpoint(pid, Request(), after=0)
        stream = response.body_iterator
        assert "retry:" in await anext(stream)
        chunk = await anext(stream)
        assert f"id: {batch['cursor']}" in chunk and "event: update" in chunk
        data = json.loads(chunk.split("data: ", 1)[1].strip())
        assert data == batch
        await stream.aclose()

    asyncio.run(inspect_stream())


def test_old_database_migrates_without_losing_project(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,title TEXT,brief TEXT,route TEXT,status TEXT,revision INTEGER,plan TEXT,current_render_id TEXT,created_at REAL,updated_at REAL)")
        c.execute("INSERT INTO projects VALUES('old','旧项目','旧需求','product','draft',0,NULL,NULL,1,1)")
    store = Store(path, tmp_path / "media")
    p = store.get_project("old")
    assert p["context_version"] == p["plan_context_version"] == 0
    assert p["duration_seconds"] == 20
    assert store.project_detail("old")["gates"] == []
    assert store.create_project("新项目", "product")["context_version"] == 0


def legacy_finished_render(env):
    """Store-only fixture, deliberately never decoded or presented as generated media."""
    _, app, pid, original = env
    store = app.state.store
    plan = plan_for(original["id"])
    plan["cta"] = "历史版本的结尾字"
    p = store.update_plan(pid, plan)
    path = store.media_root / "EXPLICIT_TEST_RENDER_FIXTURE.mp4"
    path.write_bytes(b"EXPLICIT_TEST_RENDER_FIXTURE")
    asset = store.add_asset(pid, "render", "TEST ONLY", path, "video/mp4")
    old_job = store.create_job(pid, "generate", p["revision"])
    render = store.publish_render(old_job["id"], asset["id"], {"fixture": True, "cta": "历史版本的结尾字"})
    return store.get_project(pid), render


def test_recompose_snapshots_current_render_and_removes_only_new_plan_cta(env):
    _, app, pid, _ = env
    store = app.state.store
    p, render = legacy_finished_render(env)
    before = store.project_detail(pid)
    job = store.create_job(pid, "recompose", p["revision"], {"base_render_id": render["id"]})
    assert job["kind"] == "recompose" and job["status"] == "queued"
    assert job["revision"] == p["revision"] + 1 and job["submission_count"] == 0
    assert job["payload"]["base_render_id"] == render["id"]
    assert job["payload"]["context_version"] == p["context_version"]
    assert "cta" not in job["payload"]["plan"]
    current = store.get_project(pid)
    assert "cta" not in current["plan"]
    assert current["current_render_id"] == render["id"]
    assert current["plan_context_version"] == current["context_version"]
    after = store.project_detail(pid)
    assert after["assets"] == before["assets"] and after["renders"] == before["renders"]
    assert store.get_render(render["id"])["manifest"]["cta"] == "历史版本的结尾字"
    with store._connection() as c:
        old = json.loads(c.execute("SELECT plan FROM revisions WHERE project_id=? AND revision=?", (pid, p["revision"])).fetchone()["plan"])
        assert old["cta"] == "历史版本的结尾字"


@pytest.mark.parametrize("blocker", ["stale_revision", "wrong_render", "stale_context", "active"])
def test_recompose_rejects_stale_or_busy_project_atomically(env, blocker):
    _, app, pid, _ = env
    store = app.state.store
    p, render = legacy_finished_render(env)
    revision = p["revision"]
    base = render["id"]
    if blocker == "stale_revision":
        revision -= 1
    elif blocker == "wrong_render":
        base = "not-the-current-render"
    elif blocker == "stale_context":
        store.add_message(pid, "user", "未处理的新要求")
    else:
        store.create_job(pid, "plan", revision)
    before = store.project_detail(pid)
    with pytest.raises(StaleRevision):
        store.create_job(pid, "recompose", revision, {"base_render_id": base})
    assert store.project_detail(pid) == before


def duration_plan(asset_id, total):
    plan = plan_for(asset_id)
    template = plan["segments"][0]
    plan["segments"] = []
    remaining = total
    while remaining:
        seconds = min(10, remaining)
        segment = copy.deepcopy(template)
        segment.update(id=f"seg_{len(plan['segments']) + 1:02d}", duration_seconds=seconds,
                       shots=[{"start": 0, "end": seconds, "description": "展示真实商品"}])
        plan["segments"].append(segment)
        remaining -= seconds
    return plan


def duration_project(tmp_path, seconds):
    app = create_app(Settings(data_root=tmp_path / "data", deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY", max_video_submissions=6), run_worker=False)
    c = TestClient(app)
    response = c.post("/api/projects", json={"brief": "展示这款商品的真实外观", "duration_seconds": seconds})
    assert response.status_code == 201
    p = response.json()
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(data, "PNG")
    asset = c.post(f"/api/projects/{p['id']}/assets", files={"file": ("TEST_ONLY.png", data.getvalue(), "image/png")}).json()
    return c, app, p["id"], asset


@pytest.mark.parametrize("seconds,expected", [(10, [10]), (15, [10, 5]), (60, [10, 10, 10, 10, 10, 10])])
def test_selected_duration_persists_snapshots_and_caps_actual_submissions(tmp_path, seconds, expected):
    c, app, pid, asset = duration_project(tmp_path, seconds)
    store = app.state.store
    p = store.get_project(pid)
    assert p["duration_seconds"] == seconds
    turn = c.post(f"/api/projects/{pid}/turns", json={"client_turn_id": "duration-turn", "expected_context_version": p["context_version"]}).json()["job"]
    payload = store.get_job(turn["id"])["payload"]
    assert payload["duration_seconds"] == payload["frozen"]["project"]["duration_seconds"] == seconds
    store.claim_job(turn["id"])
    store.finish_decision(turn["id"], {"kind": "reply", "message": "已收到商品信息。"})
    plan = duration_plan(asset["id"], seconds)
    assert [s["duration_seconds"] for s in plan["segments"]] == expected
    saved = store.update_plan(pid, plan)
    patched = c.patch(f"/api/projects/{pid}/plan", json={"plan": plan, "accepted_revision": saved["revision"]})
    assert patched.status_code == 200
    response = c.post(f"/api/projects/{pid}/generate", json={"accepted_revision": patched.json()["revision"]})
    assert response.status_code == 200
    job = store.get_job(response.json()["job"]["id"])
    assert job["payload"]["video_submission_budget"] == len(expected)
    assert store.claim_job(job["id"])
    for segment in plan["segments"]:
        assert store.reserve_submission(job["id"], f"video:{segment['id']}", {"explicit_test": True}, 6)
    with pytest.raises(ValueError, match="额度"):
        store.reserve_submission(job["id"], "video:extra", {"explicit_test": True}, 6)
    assert store.get_job(job["id"])["submission_count"] == len(expected)
    assert store.reserve_submission(job["id"], f"video:{plan['segments'][0]['id']}", {}, 6) is False


def test_duration_defaults_and_health_range(tmp_path):
    app = create_app(Settings(data_root=tmp_path / "data"), run_worker=False)
    c = TestClient(app)
    p = c.post("/api/projects", json={"brief": "默认时长商品广告"}).json()
    assert p["duration_seconds"] == 20
    assert app.state.store.get_project(p["id"])["duration_seconds"] == 20
    capabilities = c.get("/api/health").json()["capabilities"]
    assert capabilities["duration_min_seconds"] == 10
    assert capabilities["duration_max_seconds"] == 60
    assert capabilities["default_duration_seconds"] == 20


@pytest.mark.parametrize("invalid", [9, 61, 0, -1, 10.5, "15", True, None])
def test_duration_outside_integer_range_is_rejected_without_creating_project(tmp_path, invalid):
    app = create_app(Settings(data_root=tmp_path / "data"), run_worker=False)
    c = TestClient(app)
    response = c.post("/api/projects", json={"brief": "无效时长", "duration_seconds": invalid})
    assert response.status_code == 422
    assert app.state.store.list_projects() == []
    with pytest.raises(ValueError):
        app.state.store.create_project("无效时长", "product", duration_seconds=invalid)


def test_plan_duration_mismatch_cannot_be_saved_or_finish_decision(tmp_path):
    c, app, pid, asset = duration_project(tmp_path, 15)
    store = app.state.store
    correct = duration_plan(asset["id"], 15)
    p = store.update_plan(pid, correct)
    wrong = duration_plan(asset["id"], 20)
    before = store.project_detail(pid)
    rejected = c.patch(f"/api/projects/{pid}/plan", json={"plan": wrong, "accepted_revision": p["revision"]})
    assert rejected.status_code == 422
    with pytest.raises(ValueError, match="15秒"):
        store.update_plan(pid, wrong)
    assert store.project_detail(pid) == before
    malformed = copy.deepcopy(correct)
    malformed["segments"].reverse()
    with pytest.raises(ValueError, match="15秒"):
        store.update_plan(pid, malformed)
    turn = c.post(f"/api/projects/{pid}/turns", json={"client_turn_id": "bad-model-duration", "expected_context_version": p["context_version"], "content": "更新方案"}).json()["job"]
    store.claim_job(turn["id"])
    before_finish = store.project_detail(pid)
    options = [{"id": f"choice_{i}", "label": "不同构图", "description": "保留真实外观", "segment": copy.deepcopy(wrong["segments"][0])} for i in (1, 2)]
    with pytest.raises(ValueError, match="15秒"):
        store.finish_decision(turn["id"], {"kind": "plan", "message": "方案", "plan": wrong, "creative_options": options})
    assert store.project_detail(pid) == before_finish


def test_store_rejects_new_cta_tasks_and_recompose_has_no_public_route(env):
    c, app, pid, _ = env
    store = app.state.store
    p, _ = legacy_finished_render(env)
    before = store.project_detail(pid)
    with pytest.raises(ValueError):
        store.create_job(pid, "cta", p["revision"], {"text": "不应接受"})
    assert store.project_detail(pid) == before
    assert not any(getattr(route, "path", "").endswith("/recompose") for route in app.routes)
    assert c.post(f"/api/projects/{pid}/recompose", json={}).status_code in (404, 405)
    assert store.project_detail(pid) == before
