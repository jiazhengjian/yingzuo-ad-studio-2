"""A missing selling point is asked directly, and a story drops any destination."""
from app.ad_form import ad_form_question, intake_strategy_response
from app.ad_form_playbook import ending_policy, resolve_playbook_form
from app.cta import apply_spoken_policy, cta_question, project_cta_mode, should_ask_cta
from app.db import Store
from app.highlight import highlight_question, should_ask_highlight
from app.planner_stage import enrich_planner_stage


def _clarify(question_id, prompt):
    return {"kind": "clarify", "message": "再确认一件事", "questions": [
        {"id": question_id, "prompt": prompt, "options": [], "allow_custom": True, "allow_skip": True}]}


def test_a_bare_cake_brief_asks_what_to_remember_instead_of_specs():
    project = {"ad_form": None, "cta_target": None, "brief": "我想做一个蛋糕广告"}
    raw = _clarify("product_info", "这次要拍的蛋糕具体是什么？比如种类、几个、尺寸或包装。")
    result = intake_strategy_response(raw, project)
    assert [item["id"] for item in result["questions"]] == ["ad_form", "highlight", "audience", "cta_target"]
    highlight = result["questions"][1]
    assert highlight["options"] == []
    assert highlight["allow_skip"] is True and highlight["allow_custom"] is True
    assert "最想让观众记住" in highlight["prompt"]
    assert "夹心" in highlight["prompt"] and "制作工艺" in highlight["prompt"]
    assert "尺寸" not in highlight["prompt"] and "包装" not in highlight["prompt"]
    audience = result["questions"][2]
    assert audience["options"] == [] and audience["allow_skip"] is True
    assert "拍给谁看" in audience["prompt"] and "情境" in audience["prompt"]
    assert "上班族" in audience["prompt"] and "下午茶" in audience["prompt"]
    assert should_ask_cta(project) is True
    assert result["questions"][3]["id"] == "cta_target"


def test_other_forms_keep_the_destination_and_a_story_card_does_not():
    brief = {"brief": "我想做一个蛋糕广告", "cta_target": None}
    for form in ("brand_tvc", "presenter_pitch", "auto"):
        result = intake_strategy_response(_clarify("audience", "给谁看？"), {**brief, "ad_form": form})
        assert [item["id"] for item in result["questions"]] == ["highlight", "audience", "cta_target"]
    story = intake_strategy_response(_clarify("audience", "给谁看？"), {**brief, "ad_form": "scene_story"})
    assert [item["id"] for item in story["questions"]] == ["highlight", "audience"]
    assert should_ask_cta({**brief, "ad_form": "scene_story", "cta_target": "link"}) is False


def test_a_stated_or_skipped_highlight_is_not_asked_again():
    stated = {"ad_form": "brand_tvc", "cta_target": "ai_decide", "brief": "这款蛋糕主打松软的夹心"}
    assert should_ask_highlight(stated) is False
    asked = intake_strategy_response(_clarify("audience", "给谁看？"), stated)
    assert all(item["id"] != "highlight" for item in asked["questions"])
    skipped = {"ad_form": "brand_tvc", "cta_target": "ai_decide", "brief": "我想做一个蛋糕广告"}
    gates = [{"kind": "clarification", "status": "answered",
              "questions": [highlight_question("蛋糕")],
              "answers": [{"question_id": "highlight", "skipped": True}]}]
    assert should_ask_highlight(skipped, gates=gates) is False
    again = intake_strategy_response(_clarify("audience", "给谁看？"), skipped, gates=gates)
    assert [item["id"] for item in again["questions"]] == ["audience"]
    answered = [{"kind": "clarification", "status": "answered",
                 "questions": [highlight_question("蛋糕")],
                 "answers": [{"question_id": "highlight", "text": "生日"}]}]
    quiet = intake_strategy_response(_clarify("audience", "给谁看？"), skipped, gates=answered)
    assert all(item["id"] != "highlight" for item in quiet["questions"])


def test_story_answer_discards_the_destination_and_a_later_story_ignores_it(tmp_path):
    store = Store(tmp_path / "state.sqlite3", tmp_path / "media")
    project = store.create_project("我想做一个蛋糕广告", "product")
    job = store.create_turn(project["id"], {
        "client_turn_id": "open", "expected_context_version": 0, "content": "我想做一个蛋糕广告"})
    store.claim_job(job["id"])
    store.finish_decision(job["id"], {"kind": "clarify", "message": "先确认这几项。", "questions": [
        ad_form_question(), cta_question(), highlight_question("我想做一个蛋糕广告")]})
    gate = store.pending_gate(project["id"])
    current = store.get_project(project["id"])
    store.create_turn(project["id"], {
        "client_turn_id": "answer", "expected_context_version": current["context_version"],
        "reply_to_gate_id": gate["id"], "answers": [
            {"question_id": "ad_form", "option_id": "scene_story"},
            {"question_id": "cta_target", "option_id": "link"},
            {"question_id": "highlight", "skipped": True},
        ]})
    saved = store.get_project(project["id"])
    assert saved["ad_form"] == "scene_story"
    assert saved["cta_target"] == "ai_decide"
    assert project_cta_mode(saved) == "none"
    assert should_ask_cta(saved) is False
    presenter = {**saved, "ad_form": "presenter_pitch"}
    assert should_ask_cta(presenter) is False
    detail = store.project_detail(project["id"])
    follow = intake_strategy_response(_clarify("audience", "给谁看？"), saved, gates=detail["gates"])
    assert all(item["id"] not in {"cta_target", "highlight"} for item in follow["questions"])
    assert project_cta_mode({"ad_form": "scene_story", "cta_target": "link", "brief": "点下面链接"}) == "none"


def test_directions_without_a_selling_point_become_a_clarification():
    brief = "我想做一个蛋糕的剧情广告，下午茶场景，女生主角。"
    project = {"ad_form": "scene_story", "cta_target": None, "brief": brief}
    offered = {"kind": "directions", "message": "三个方向", "directions": [{"id": "d1"}]}
    result = intake_strategy_response(offered, project)
    assert result["kind"] == "clarify"
    assert [item["id"] for item in result["questions"]] == ["highlight"]
    assert "最想让观众记住" in result["questions"][0]["prompt"]
    assert offered["kind"] == "directions"
    bare = {"ad_form": "scene_story", "cta_target": None, "brief": "我想做一个蛋糕的剧情广告。"}
    both = intake_strategy_response(offered, bare)
    assert [item["id"] for item in both["questions"]] == ["highlight", "audience"]
    role_only = {"ad_form": "scene_story", "cta_target": None, "brief": "我想做一个蛋糕的剧情广告，女生主角。"}
    scene_only = {"ad_form": "scene_story", "cta_target": None, "brief": "我想做一个蛋糕的剧情广告，下午茶场景。"}
    assert [item["id"] for item in intake_strategy_response(offered, role_only)["questions"]] == ["highlight"]
    assert [item["id"] for item in intake_strategy_response(offered, scene_only)["questions"]] == ["highlight"]
    skipped = [{"kind": "clarification", "status": "answered",
                "questions": [highlight_question(brief)],
                "answers": [{"question_id": "highlight", "skipped": True}]}]
    kept = intake_strategy_response(offered, project, gates=skipped)
    assert kept["kind"] == "directions"
    delegated = intake_strategy_response(offered, {**project, "brief": brief + "你来定。"})
    assert delegated["kind"] == "directions"
    resumed = intake_strategy_response(offered, project, planning_requested=True)
    assert resumed["kind"] == "directions"
    editing = intake_strategy_response(offered, {**project, "plan": {"segments": []}})
    assert editing["kind"] == "directions"
    chosen = [{"kind": "direction", "status": "answered", "selected_option_id": "d1",
               "questions": [], "answers": []}]
    assert intake_strategy_response({"kind": "plan", "message": "改一下"}, project, gates=chosen)["kind"] == "plan"


def test_recommending_a_story_later_ignores_a_saved_destination():
    strategy = {
        "brief": {"ad_form": "auto", "facts": []},
        "selected_direction_id": "d1",
        "directions": [{"id": "d1", "film_concept": {"structure": "scene_action"}}],
    }
    revised = apply_spoken_policy(
        {"beats": [{"id": "b0", "narration": "想带走，就点下面链接。"}]}, strategy, "link")
    assert "点下面" not in revised["beats"][0]["narration"]
    assert "链接" not in revised["beats"][0]["narration"]
    form = resolve_playbook_form("auto", "scene_action")
    assert form == "scene_story"
    assert ending_policy(form, "link")["cta_target"] == "none"
    data = {"stage": "ad_script", "ad_form": "auto", "cta_target": "link",
            "selected_direction": {"film_concept": {"structure": "scene_action"}}}
    enrich_planner_stage(data)
    assert data["cta_target"] == "none"
    assert data["ending_policy"]["cta_target"] == "none"
