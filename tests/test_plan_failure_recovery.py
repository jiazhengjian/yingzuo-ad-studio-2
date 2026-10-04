"""Schema, revision and merchant-facing failures stay inside the planning job."""
import asyncio
import json
import sqlite3

import pytest

from app.ad_form import infer_ad_form, needs_ad_form_choice, plain_merchant_text
from app.db import Store
from app.line_quality import offending_lines
from app.planning import ending_hold_seconds, expression_schedule
from app.planning_session import fingerprint
from app.progressive_planning import compile_director, fallback_director, progressive_plan
from app.shot_craft import _clauses, shot_prompt_lines, shot_suggestions
from app.strategy import StrategyRecord
from app.video_prompt import compile_grouped_video
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import source
from test_video_prompt_lipsync import _segment


def test_missing_start_state_and_empty_extras_compile(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["shots"][0]["continuity"].pop("start_state")
    raw["shots"][0]["continuity"]["sound_extra"] = None
    raw["shots"][0]["continuity"]["start_state_note"] = ""
    raw["shots"][2]["continuity"]["entry_transition"] = "cut"
    raw["shots"][2]["continuity"].pop("start_state")
    raw["shots"][2]["sound_extra"] = {}
    before = json.loads(json.dumps(raw))
    plan, _, _ = compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])
    assert raw == before
    shots = plan["segments"][0]["shots"]
    assert shots[0]["continuity"]["start_state"].strip()
    assert shots[2]["continuity"]["entry_transition"] == "cut"
    assert shots[2]["continuity"]["start_state"].strip()
    assert "sound_extra" not in shots[0]["continuity"]
    assert "start_state_note" not in shots[0]["continuity"]


def test_nonempty_unknown_field_is_still_rejected(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["shots"][0]["continuity"]["sound_extra"] = "另加环境声"
    with pytest.raises(ValueError):
        compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])


def test_schema_failure_repairs_then_regenerates_and_still_returns_a_plan(source):
    strategy = chosen_strategy(source)
    calls = []

    async def stage_call(system, data, assets, max_tokens=7000):
        kind = data.get("task_kind") or data.get("stage")
        calls.append(kind)
        if data.get("stage") == "storyboard_repair" or kind == "storyboard_regeneration":
            return {"sound_extra": None, "start_state_note": ""}
        if data.get("stage") == "storyboard":
            return {"title": "坏稿", "shots": [{"continuity": {
                "entry_transition": "cut", "sound_extra": None, "start_state_note": ""}}]}
        return {}

    plan = asyncio.run(progressive_plan(
        stage_call=stage_call, on_progress=lambda _phase: None, record_failure=lambda _item: None,
        strategy=strategy, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
        assets=[source], conversation=[], merchant_request="看清瓶子", current_plan=None, skip_review=True))
    assert "storyboard_repair" in calls
    assert "storyboard_regeneration" in calls
    assert plan["segments"] and plan["segments"][0]["shots"]


def test_sparse_revision_falls_back_to_the_previous_plan(source):
    strategy = chosen_strategy(source)
    formats = []

    async def stage_call(system, data, assets, max_tokens=7000):
        if data.get("stage") == "plan_review":
            review = accepted_review(data["final_plan"])
            review["ready"] = False
            review["checks"][0]["passed"] = False
            review["checks"][0]["note"] = "首镜需要再看一眼商品。"
            return review
        if data.get("stage") == "plan_refinement":
            formats.append(data.get("refinement_output_format"))
            return {"patch": []}
        if data.get("task_kind") in ("line_jargon_rewrite", "shot_craft_rewrite"):
            return {}
        value = director_output(strategy)
        value["video_flow"] = "先看见瓶子，再停住。"
        return value

    plan = asyncio.run(progressive_plan(
        stage_call=stage_call, on_progress=lambda _phase: None, record_failure=lambda _item: None,
        strategy=strategy, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
        assets=[source], conversation=[], merchant_request="看清瓶子", current_plan=None))
    assert formats and formats[0] in ("sparse", "sparse_or_full")
    assert None in formats
    assert plan["title"]


def test_structure_failure_resume_reruns_storyboard(tmp_path):
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    project = store.create_project("蛋糕广告", "product")
    job = store.create_turn(project["id"], {
        "client_turn_id": "plan-1", "expected_context_version": 0, "content": "优化广告方案"})
    checkpoint = {"version": 1, "scope": fingerprint({"project": project["id"]}),
        "request_count": 4, "transient_retries": 0, "empty_retries": 0, "manual_retries": 0,
        "entries": [
            {"stage": "strategy", "input_hash": fingerprint("strategy"), "state": "ready",
             "attempts": 1, "result": {"completed": "direction"}},
            {"stage": "ad_script", "input_hash": fingerprint("script"), "state": "ready",
             "attempts": 1, "result": {"completed": "script"}},
            {"stage": "storyboard", "input_hash": fingerprint("storyboard"), "state": "terminal",
             "attempts": 1, "failure": {"message": "结构不完整", "error_code": "planning_content_failed",
                                        "retryable": False, "retry_action": "edit_plan"}}]}
    store.update_job(job["id"], status="failed",
        error="本次方案结构不完整，暂未采用。已有方案和素材已保留，尚未开始新的图片或视频生成",
        payload={**job["payload"], "planning_checkpoint": checkpoint},
        progress={"error_code": "planning_content_failed", "retryable": False, "retry_action": "edit_plan"})
    store.set_project_status(project["id"], job["revision"], "failed")
    retried = store.retry_planning_job(project["id"], job["id"])
    stages = [entry["stage"] for entry in retried["payload"]["planning_checkpoint"]["entries"]]
    assert retried["status"] == "queued"
    assert stages == ["strategy", "ad_script"]
    assert retried["payload"]["planning_checkpoint"]["manual_retries"] == 1
    assert retried["payload"]["planning_checkpoint"]["entries"][0]["state"] == "ready"


def _form_strategy(source, form):
    strategy = chosen_strategy(source)
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = form
    return StrategyRecord.model_validate(raw)


def test_product_freeze_reserves_two_silent_seconds():
    from types import SimpleNamespace
    from app.planning import _cap_ending_speech

    beat = SimpleNamespace(id="b0", narration="看" * 20 + "。", visual_role="商品", timing=None)
    script = SimpleNamespace(beats=[beat])
    schedule = expression_schedule(script, [10], beat_groups=[[0]], ad_form="scene_story")
    spoken = [group for group in schedule[-1]["groups"] if group.get("text")]
    assert spoken[-1]["speech_end"] <= 8 + 1e-6
    segment = {"duration_seconds": 20, "groups": [
        {"text": "先看这个。", "start": 0, "end": 16, "speech_start": 0.2, "speech_end": 4},
        {"text": "再看一眼。", "start": 16, "end": 20, "speech_start": 16.2, "speech_end": 19.7},
    ]}
    _cap_ending_speech(segment, ending_hold_seconds(script, "brand_tvc"))
    assert segment["groups"][-1]["speech_end"] <= 18 + 1e-6


def test_presenter_cta_keeps_one_and_a_half_seconds_after_the_line():
    from app.planning import _cap_ending_speech

    segment = {"duration_seconds": 20, "groups": [
        {"text": "先看这个。", "start": 0, "end": 16, "speech_start": 0.2, "speech_end": 4},
        {"text": "点下面链接看看。", "start": 16, "end": 20, "speech_start": 16.2, "speech_end": 19.7},
    ]}
    _cap_ending_speech(segment, 1.5)
    assert 18 < segment["groups"][-1]["speech_end"] <= 18.5 + 1e-6


def test_hand_closeup_does_not_ask_for_lip_sync():
    segment = _segment()
    segment["shots"][1] = {
        "id": "s4", "start": 3, "end": 6, "description": "手部特写，她的手托着吹风机。",
        "character_ids": ["char_host"], "product_visible": True,
        "continuity": {"start_state": "手托着吹风机", "allowed_changes": "手保持握住",
                       "end_state": "手仍托着吹风机", "camera_move": "手部特写，机位固定",
                       "entry_transition": "cut", "entry_reason": "看清商品"},
    }
    segment["speech_spans"][0]["shot_ids"] = ["s0", "s4"]
    prompt = compile_grouped_video(segment)
    hand = prompt.split("s4 |", 1)[1].split("台词时间", 1)[0]
    assert "面向镜头说话，嘴型与台词同步" not in hand
    assert "画外接着说" in hand
    assert "面向镜头说话，嘴型与台词同步" in prompt.split("s0 |", 1)[1].split("s4 |", 1)[0]


def test_quoted_period_does_not_split_the_action():
    description = "她说“好。再来”然后拿起商品。"
    assert _clauses(description) == ["她说“好。再来”然后拿起商品"]
    lines = shot_prompt_lines(
        {"id": "s1", "start": 0, "end": 8, "description": description}, hold_tail=False)
    assert any("好。再来" in line for line in lines)


def test_cast_line_is_not_repeated_and_mouth_starts_open():
    segment = _segment()
    segment["production"]["characters"][0]["description"] = "短发，日常大陆长相"
    segment["shots"][0]["continuity"] = {
        "start_state": "嘴未张开，看着镜头", "allowed_changes": "开口说话",
        "end_state": "已经开口", "camera_move": "近景，机位固定",
        "entry_transition": "opening", "entry_reason": "开口",
    }
    segment["shots"][1]["character_ids"] = ["char_host"]
    segment["shots"][1]["description"] = "她看着镜头，继续拿着吹风机。"
    segment["shots"][1]["continuity"] = {
        "start_state": "嘴未张开，手持商品", "allowed_changes": "继续说",
        "end_state": "已经开口", "camera_move": "近景，机位固定",
        "entry_transition": "continuous", "entry_reason": "接着说",
    }
    prompt = compile_grouped_video(segment)
    assert prompt.count("日常大陆长相") == 1
    assert "。。" not in prompt
    assert "嘴未张开" not in prompt
    assert "已经开口" in prompt


def test_hand_note_is_once_and_only_when_a_hand_holds_the_product(source):
    strategy = _form_strategy(source, "brand_tvc")

    def plan(shots):
        return {"video_flow": "先看商品再停住", "segments": [{"shots": shots}]}

    quiet = plan([
        {"id": "a", "start": 0, "end": 4, "description": "产品在台面上，光线稳定。", "product_visible": True,
         "continuity": {"camera_move": "中景，镜头缓慢推进"}},
        {"id": "b", "start": 4, "end": 8, "description": "产品特写，机位停住。", "product_visible": True,
         "continuity": {"camera_move": "特写，机位停住"}},
    ])
    assert not any("手和商品动作多" in item["note"] for item in shot_suggestions(quiet, strategy))
    busy = plan([
        {"id": "a", "start": 0, "end": 4, "description": "手握着商品快速翻转。", "product_visible": True,
         "continuity": {"camera_move": "近景，镜头固定"}},
        {"id": "b", "start": 4, "end": 8, "description": "两手交接商品。", "product_visible": True,
         "continuity": {"camera_move": "近景，机位停住"}},
    ])
    notes = [item["note"] for item in shot_suggestions(busy, strategy) if "手和商品动作多" in item["note"]]
    assert len(notes) == 1


def test_brief_keywords_choose_an_ad_type(tmp_path):
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    project = store.create_project("给吹风机拍一条口播", "product")
    assert project["ad_form"] == "presenter_pitch"
    assert needs_ad_form_choice(project) is False
    assert infer_ad_form("讲一个故事") == "scene_story"
    assert infer_ad_form("做成品牌片") == "brand_tvc"
    assert infer_ad_form("拍一条剧情短视频") == "scene_story"
    assert infer_ad_form("做一个小剧情") == "scene_story"
    assert infer_ad_form("做成产品展示") == "brand_tvc"
    assert infer_ad_form("不出镜来拍") == "brand_tvc"
    assert infer_ad_form("口播还是剧情") is None
    assert infer_ad_form("不要口播") is None
    assert infer_ad_form("改一下口播广告里的台词") is None
    assert infer_ad_form("我想做定格动画的广告，但不一定有剧情。") is None
    assert plain_merchant_text("保存值为空的偏好") == "还没选定广告类型"
    assert plain_merchant_text("还差一项保存的偏好") == "还需要先选定广告类型"


def test_line_craft_flags_repeat_voice_action_and_unanswered_question(source):
    story = _form_strategy(source, "scene_story")
    brand = _form_strategy(source, "brand_tvc")

    def names(script, strategy):
        found = []
        for item in offending_lines(script, strategy=strategy):
            found.extend(item["offenses"])
        return found

    assert "相邻台词重复" in names({"beats": [
        {"id": "a", "narration": "你听。"}, {"id": "b", "narration": "你听这声音。"}]}, story)
    assert "人称不统一" in names({"beats": [
        {"id": "a", "narration": "她走过窗边。"}, {"id": "b", "narration": "就坐这儿吧。"}]}, story)
    assert "只描述画面" in names({"beats": [
        {"id": "a", "narration": "先看这个。"}, {"id": "b", "narration": "叉子切下第一块。"}]}, story)
    assert "开场问题没有下文" in names({"beats": [
        {"id": "a", "narration": "要不要坐这儿？"}, {"id": "b", "narration": "还是换一处？"}]}, story)
    answered = names({"beats": [
        {"id": "a", "narration": "要不要坐这儿？"}, {"id": "b", "narration": "就坐这儿吧。"}]}, story)
    assert "开场问题没有下文" not in answered
    hidden = {"video_flow": "先给处境", "segments": [{"shots": [
        {"id": "a", "start": 0, "end": 2, "beat_id": "b0", "description": "只有房间。", "product_visible": False,
         "continuity": {"camera_move": "中景，镜头固定"}},
        {"id": "b", "start": 2, "end": 6, "beat_id": "b1", "description": "商品放在桌上。", "product_visible": True,
         "continuity": {"camera_move": "近景，机位停住"}},
    ]}]}
    from app.shot_craft import craft_refine_request
    brand_request = craft_refine_request(hidden, brand)
    story_request = craft_refine_request(hidden, story)
    assert brand_request is not None and "1.5" in brand_request["feedback"]["issue"]
    assert story_request is None or "1.5" not in story_request["feedback"]["issue"]


def test_replan_reads_decision_plan_without_writing_diagnostics(tmp_path, source, monkeypatch):
    from scripts.replan_offline import load_saved_job, replan_offline

    strategy = _form_strategy(source, "presenter_pitch")
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    selected = next(item for item in strategy.directions if item.id == strategy.selected_direction_id)
    character_id = selected.production.characters[0].id
    payload = {"decision": {"kind": "plan", "plan": {
        "strategy": strategy.model_dump(),
        "ad_script": director_output(strategy)["ad_script"],
        "generation_strategy": "continuity_v1"}}}
    product = tmp_path / "product.png"
    product.write_bytes(b"png")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "INSERT INTO projects(id,title,brief,route,status,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("project_real", "吹风机", "口播介绍吹风机", "studio", "ready", 1, 1, 1))
        connection.execute(
            "INSERT INTO assets(id,project_id,kind,name,path,mime,revision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("product_1", "project_real", "product", "商品", str(product), None, 1, "{}", 1))
        for index, layout in enumerate(("grid_2x2", "horizontal_16x9"), start=1):
            connection.execute(
                "INSERT INTO assets(id,project_id,kind,name,path,mime,revision,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"char_{index}", "project_real", "character", "主讲", str(tmp_path / f"sheet{index}.png"), None, 1,
                 json.dumps({"character_id": character_id, "character_name": "主讲", "sheet_layout": layout}), index + 1))
        connection.execute(
            "INSERT INTO jobs(id,project_id,revision,kind,status,phase,progress,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("job_real", "project_real", 1, "plan", "ready", "ready", "{}", json.dumps(payload), 1, 1))
    saved = load_saved_job(store.db_path, "job_real")
    assert saved["plan"]["strategy"]["brief"]["ad_form"] == "presenter_pitch"
    assert saved["assets"][0]["mime"] == "image/png"
    seen = {}

    async def stage_call(system, data, assets, max_tokens=7000):
        if data.get("stage") == "storyboard" and not data.get("task_kind"):
            seen["ending"] = data.get("ending_policy")
            seen["speech"] = dict(data.get("speech_budget") or {})
            seen["skills"] = bool(data.get("stage_skills"))
            seen["mime"] = [asset.get("mime") for asset in assets]
            value = director_output(strategy)
            value["video_flow"] = "先看见商品，再停住。"
            for shot in value["shots"]:
                shot["product_visible"] = True
                shot["continuity"]["camera_move"] = "中景，机位固定"
            return value
        if data.get("stage") == "plan_review":
            return accepted_review(data["final_plan"])
        return {}

    before = sqlite3.connect(store.db_path).execute("SELECT payload FROM jobs WHERE id='job_real'").fetchone()[0]
    report = asyncio.run(replan_offline(
        job_id="job_real", db_path=store.db_path, stage_call=stage_call, gateway=None, skip_review=False))
    after = sqlite3.connect(store.db_path).execute("SELECT payload FROM jobs WHERE id='job_real'").fetchone()[0]
    assert after == before
    assert seen["ending"]["form"] == "presenter_pitch"
    assert seen["speech"].get("characters_per_second")
    assert seen["skills"] is True
    assert seen["mime"] and all(item == "image/png" for item in seen["mime"])
    assert "角色参考须对应已确认的唯一角色" not in report["prompt"]
    assert report["prompt"].count("四视图") == 1
    assert not (tmp_path / "data" / "diagnostics").exists()


def test_line_focus_stays_a_suggestion(source):
    from app.ad_form_playbook import review_finding_disposition
    from app.ad_script import CHECK_CRITERIA
    from app.line_quality import with_line_focus_suggestions
    from app.plan_review import validate_plan_review

    assert len(CHECK_CRITERIA) == 6
    assert "line_focus" not in CHECK_CRITERIA
    assert review_finding_disposition("line_focus") == "suggestion"
    strategy = _form_strategy(source, "presenter_pitch")
    raw = director_output(strategy)
    raw["ad_script"]["beats"][1]["narration"] = "风大，这几下就吹开了。"
    plan, _, _ = compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])
    review = validate_plan_review(
        accepted_review(plan), plan, require_speaker_visibility=True,
        require_temporal_omission=True, require_temporal_omission_evidence=True,
        require_visual_execution=True, require_review_policy=True)
    attached = with_line_focus_suggestions(review, plan, strategy)
    assert attached.ready is True
    assert any(item.kind == "line_focus" and item.note.startswith("台词是否对焦商品、前后连贯。")
               for item in attached.suggestions)
    assert attached.reviewed_plan_sha256 == review.reviewed_plan_sha256


def test_closing_line_keeps_its_budget_before_the_hold():
    from types import SimpleNamespace
    from app.planning import _cap_ending_speech
    from app.schemas import shot_character_limit

    presenter = "价格399。想要的话，点下面链接看看。"
    story = "三百九十九，点下面看看。"
    for text, form in ((presenter, "presenter_pitch"), (story, "scene_story")):
        beats = [
            SimpleNamespace(id="b0", narration="先看这个。", visual_role="商品", timing=None),
            SimpleNamespace(id="b1", narration=text, visual_role="商品", timing=None),
        ]
        script = SimpleNamespace(beats=beats)
        schedule = expression_schedule(script, [20], beat_groups=[[0, 1]], ad_form=form)
        last = [group for group in schedule[-1]["groups"] if group.get("text")][-1]
        assert last["speech_end"] <= 18 + 1e-6
        assert len(last["text"]) <= shot_character_limit(last["speech_start"], last["speech_end"], 29)


def test_speech_window_overflow_is_a_capacity_error():
    from types import SimpleNamespace
    from pydantic import ValidationError
    from app.progressive_planning import _is_capacity_error
    from app.schemas import SpeechSpan

    script = SimpleNamespace(beats=[SimpleNamespace(
        id="b0", narration="看" * 28 + "。", visual_role="商品", timing=None)])
    with pytest.raises(ValueError, match="最后一句需在"):
        expression_schedule(script, [10], beat_groups=[[0]], ad_form="brand_tvc")
    try:
        expression_schedule(script, [10], beat_groups=[[0]], ad_form="brand_tvc")
    except ValueError as error:
        assert _is_capacity_error(error)
    try:
        SpeechSpan.model_validate({
            "id": "speech_1", "beat_id": "b0", "text": "价格399。想要的话，点下面链接看看。",
            "start": 17, "end": 20, "shot_ids": ["s0"],
            "anchors": [{"text": "价格399。想要的话，点下面链接看看。", "start": 17, "end": 20,
                         "shot_ids": ["s0"], "visual_evidence": "商品停住"}],
        })
    except ValidationError as error:
        assert _is_capacity_error(error)
        assert "旁白台词超过对应朗读时段的长度" in str(error)


def test_long_closing_lines_still_produce_a_plan(source):
    cases = (
        ("presenter_pitch", "价格399。想要的话，点下面链接看看。"),
        ("scene_story", "三百九十九，点下面看看。"),
    )
    for form, line in cases:
        record = _form_strategy(source, form)

        async def stage_call(system, data, assets, max_tokens=7000, line=line, record=record):
            if data.get("stage") == "plan_review":
                return accepted_review(data["final_plan"])
            if data.get("stage") == "plan_refinement":
                value = data["draft_storyboard"]
                value["ad_script"]["beats"][-1]["narration"] = "先看到这里。"
                return value
            if data.get("task_kind") in ("line_jargon_rewrite", "shot_craft_rewrite"):
                return {}
            value = director_output(record)
            value["ad_script"]["beats"][-1]["narration"] = line
            value["video_flow"] = "先看见商品，再停住。"
            for shot in value["shots"]:
                shot["product_visible"] = True
                shot["continuity"]["camera_move"] = "中景，机位固定"
            return value

        plan = asyncio.run(progressive_plan(
            stage_call=stage_call, on_progress=lambda _phase: None, record_failure=lambda _item: None,
            strategy=record, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
            assets=[source], conversation=[], merchant_request="看清商品", current_plan=None,
            cta_target="none"))
        assert plan["segments"]
        spoken = plan["segments"][-1]["speech_spans"]
        assert spoken
        assert spoken[-1]["end"] <= plan["segments"][-1]["duration_seconds"] - 2 + 1e-6
        assert "399" not in spoken[-1]["text"] and "三百九十九" not in spoken[-1]["text"]
        assert "点下面" not in spoken[-1]["text"] and "链接" not in spoken[-1]["text"]


def test_fallback_director_compiles(source):
    strategy = chosen_strategy(source)
    plan, _, _ = compile_director(fallback_director(strategy), strategy, 20, "continuity_v1", [source["id"]])
    assert plan["segments"]
