"""Class-(b) planner faults finish inside the job. The merchant is not asked to retry."""
import asyncio
import json

import pytest

from app.plan_review import validate_plan_review
from app.planner_recovery import RECOVERY_PATHS
from app.progressive_planning import compile_director, progressive_plan
from app.providers.gateway import _decode_planner_json
from app.strategy import StrategyRecord
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import source


def test_recovery_table_covers_merchant_and_system_paths():
    assert {item["klass"] for item in RECOVERY_PATHS} == {"a", "b"}
    assert any(item["path"] == "fixed_speech" and item["klass"] == "a" for item in RECOVERY_PATHS)
    system = {item["path"] for item in RECOVERY_PATHS if item["klass"] == "b"}
    assert {"broken_json", "wrapped_envelope", "citation_mismatch", "ending_hold",
            "continuity", "transport", "review_not_converged"} <= system


def test_each_system_fault_still_returns_a_plan(source):
    strategy = chosen_strategy(source)

    async def stage_call(system, data, assets, max_tokens=7000):
        stage = data.get("stage")
        if stage == "plan_review":
            review = accepted_review(data["final_plan"])
            review["ready"] = False
            review["alignment"]["ready"] = False
            review["checks"][0]["passed"] = False
            review["checks"][0]["note"] = "前后状态衔接仍需调整"
            review["execution_check"]["state_continuity"] = False
            wrapped = {"content": review}
            if data.get("task_kind") == "plan_review_response_repair":
                review["suggestions"] = [{
                    "kind": "shot_craft", "target": "/segments/0/shots/1/description",
                    "quote": review["alignment"]["shots"][0]["note"] if False else "办公桌角",
                    "note": "可以再近一点"}]
                review["policy_version"] = 1
                return {"content": review, "checks": [{"film_concept_passed_note_placeholder": "x"}]}
            return wrapped
        if stage == "plan_refinement":
            return {"patch": [], "unexpected_override": "多出来的字段"}
        if data.get("task_kind") in ("line_jargon_rewrite", "shot_craft_rewrite"):
            return {}
        value = director_output(strategy)
        value["shots"][1]["continuity"]["end_state"] = "风已关掉"
        value["shots"][1]["description"] = "她还拿着吹风机，风开着。"
        value["shots"][2]["continuity"]["start_state"] = "风已关掉"
        value["shots"][0]["continuity"]["sound_extra"] = "另加一句"
        return value

    plan = asyncio.run(progressive_plan(
        stage_call=stage_call, on_progress=lambda _phase: None, record_failure=lambda _item: None,
        strategy=strategy, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
        assets=[source], conversation=[], merchant_request="看清瓶子", current_plan=None))
    assert plan["segments"]
    assert plan["segments"][0]["shots"]
    ends = [shot["continuity"].get("end_state", "") for shot in plan["segments"][0]["shots"]]
    assert all("关掉" not in end for end in ends)


def test_bad_citation_does_not_void_a_passed_review(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    plan, _, _ = compile_director(raw, strategy, 20, "continuity_v1", [source["id"]], cta_target="none")
    review = accepted_review(plan)
    review["ready"] = True
    review["policy_version"] = 1
    description = plan["segments"][0]["shots"][0]["description"]
    review["suggestions"] = [{
        "kind": "shot_craft",
        "target": "/segments/0/shots/1/description",
        "quote": description[:8],
        "note": "这句在前一镜。",
    }]
    shot = review["alignment"]["shots"][0]
    shot["visual_execution"] = {"verdict": "clear", "source": "description",
                                "quote": "这段引文不在这一镜", "explanation": "引文对不上这一镜。"}
    checked = validate_plan_review({"content": review}, plan, require_review_policy=True,
                                   require_speaker_visibility=True, require_temporal_omission=True,
                                   require_temporal_omission_evidence=True, require_visual_execution=True)
    assert checked.ready is True
    assert checked.suggestions
    assert checked.suggestions[0].quote in description


def test_truncated_json_and_content_envelope_parse():
    parsed = _decode_planner_json('{"kind": "plan", "message": "好"')
    assert parsed["kind"] == "plan"
    wrapped = _decode_planner_json(json.dumps({"content": {"ready": True, "checks": []}}))
    assert wrapped["ready"] is True


def test_brief_destination_is_spoken_when_the_column_is_missing(source):
    from app.cta import project_cta_mode
    assert project_cta_mode({"brief": "引导大家点下方链接下单"}) == "link"
    strategy = chosen_strategy(source)
    raw_strategy = strategy.model_dump()
    fact = raw_strategy["brief"]["facts"][0]
    extra = dict(fact)
    extra["id"] = "f_cta"
    extra["text"] = "引导大家点下方链接下单"
    raw_strategy["brief"]["facts"].append(extra)
    direction = next(item for item in raw_strategy["directions"]
                     if item["id"] == raw_strategy["selected_direction_id"])
    direction["evidence_ids"] = list(dict.fromkeys([*direction["evidence_ids"], "f_cta"]))
    bound = StrategyRecord.model_validate(raw_strategy)
    raw = director_output(bound)
    plan, _, _ = compile_director(raw, bound, 20, "continuity_v1", [source["id"]], cta_target="link")
    assert "点下面链接" in plan["segments"][-1]["speech_spans"][-1]["text"]
    assert "f_cta" in plan["ad_script"]["beats"][-1]["evidence_ids"]
    assert plan["segments"][-1]["speech_spans"][-1]["end"] <= 18 + 1e-6


def test_registered_json_prompts_name_json_and_the_client_appends_it():
    import re
    from app import (ad_script, av_review, content_draft, direction_review, line_quality, planning,
                     progressive_planning, shot_craft, speech_draft)
    from app.planner_recovery import ensure_json_prompt, text_mentions_json
    from app.planner_transport import PlannerRoute
    from app.providers.gateway import Gateway
    modules = (ad_script, av_review, content_draft, direction_review, line_quality, planning,
               progressive_planning, shot_craft, speech_draft)
    found = []
    for module in modules:
        for name, value in vars(module).items():
            if name.endswith("_SYSTEM") and isinstance(value, str):
                found.append(f"{module.__name__}.{name}")
                assert text_mentions_json(value), name
    assert "app.line_quality.LINE_REWRITE_SYSTEM" in found
    assert "app.shot_craft.SHOT_REWRITE_SYSTEM" in found
    assert "app.planning.FILM_CONCEPT_SYSTEM" in found
    bare = ensure_json_prompt("只输出对象。", {"stage": "ad_script"})
    assert "json" in bare.lower() and bare.endswith("请只输出 JSON 对象。")
    assert ensure_json_prompt("只输出 JSON。") == "只输出 JSON。"
    gateway = Gateway(type("Settings", (), {"planner_model": "deepseek-flash"})())
    route = PlannerRoute("deepseek", "deepseek-flash", None, True)
    payload, _ = gateway._planner_payload("只输出对象。", {"stage": "ad_script"}, [], 100, route, stream=False)
    assert payload["response_format"] == {"type": "json_object"}
    assert text_mentions_json(payload["messages"][0]["content"], payload["messages"][1]["content"])
    assert re.search(r"json", payload["messages"][0]["content"], re.IGNORECASE)


def test_rewrite_rejection_keeps_the_original_line_and_reaches_creative(source, tmp_path):
    """A 400 on the optional line rewrite must not stop before reference images."""
    import asyncio
    from app.config import Settings
    from app.db import Store
    from app.pipeline import Pipeline
    from app.production import CHARACTER_SHEET_TEMPLATE, required_designs
    from app.providers.gateway import PlannerInvalidResponse
    from app.strategy import StrategyRecord
    from test_pipeline import PNG

    strategy = chosen_strategy(source, "character_dialogue")
    raw_strategy = strategy.model_dump()
    raw_strategy["brief"]["ad_form"] = "presenter_pitch"
    strategy = StrategyRecord.model_validate(raw_strategy)
    original = "关注秀发护理。"
    calls = []

    async def stage_call(system, data, assets, max_tokens=7000):
        calls.append(data.get("task_kind") or data.get("stage"))
        if data.get("task_kind") in ("line_jargon_rewrite", "shot_craft_rewrite"):
            raise PlannerInvalidResponse("策划服务未接受本次请求；已有成果仍保留。")
        if data.get("stage") == "plan_review":
            return accepted_review(data["final_plan"])
        if data.get("stage") == "plan_refinement":
            return data.get("draft_storyboard") or {}
        value = director_output(strategy)
        value["ad_script"]["beats"][0]["narration"] = original
        value["video_flow"] = "先看见商品，再停住。"
        for shot in value["shots"]:
            shot["product_visible"] = True
            shot["continuity"]["camera_move"] = "中景，机位固定"
        return value

    plan = asyncio.run(progressive_plan(
        stage_call=stage_call, on_progress=lambda _phase: None, record_failure=lambda _item: None,
        strategy=strategy, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
        assets=[source], conversation=[], merchant_request="看清瓶子", current_plan=None,
        cta_target="none"))
    assert "line_jargon_rewrite" in calls
    assert original in plan["script"]
    assert plan["segments"]
    store = Store(tmp_path / "studio.sqlite3", tmp_path / "media")
    project = store.create_project("吹风机", "product", duration_seconds=20, ad_form="presenter_pitch")
    product_path = store.media_root / "product.png"
    product_path.write_bytes(PNG)
    product = store.add_asset(project["id"], "product", "原图", product_path, "image/png")
    for segment in plan["segments"]:
        segment["reference_asset_ids"] = [product["id"]]
    # The offline review was bound to the fixture image. Picture confirmation
    # checks the saved product asset, not that fixture id.
    plan.pop("plan_review", None)
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"], {"pause_for_creative": True})

    class Designs:
        async def create_designs(self, project, assets, plan, folder, cancelled=None, replace_keys=None):
            saved = []
            for item in required_designs(plan):
                path = folder / f"{item['key']}.png"
                path.write_bytes(PNG)
                metadata = {"generated": True}
                if item["kind"] == "character":
                    metadata.update(character_id=item["character_id"], view_layout="four_view",
                                    sheet_layout="horizontal_16x9", template_version=CHARACTER_SHEET_TEMPLATE)
                saved.append({"kind": item["kind"], "name": item["name"], "source_path": path, "metadata": metadata})
            return saved

    settings = Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY",
                        ffmpeg_path="TEST_ONLY", max_video_submissions=1, max_active_runs=1,
                        poll_seconds=0.01, provider_timeout_seconds=1, tts_voice="TEST_ONLY",
                        video_model="TEST_ONLY")
    result = asyncio.run(Pipeline(store, Designs(), settings).execute(job["id"]))
    assert result["phase"] == "awaiting_creative", result.get("error")
    assert original in result["payload"]["plan"]["script"]


def test_skeleton_is_not_chosen_when_a_real_candidate_exists(source):
    from app.progressive_planning import choose_accepted_plan, compile_director, fallback_director, is_placeholder_plan
    strategy = chosen_strategy(source)
    real_raw = director_output(strategy)
    real_raw["ad_script"]["beats"][0]["narration"] = "瓶身轮廓在桌角。"
    real_raw["video_flow"] = "先看见商品，再停住。"
    for shot in real_raw["shots"]:
        shot["product_visible"] = True
        shot["continuity"]["camera_move"] = "中景，机位固定"
    real, _, _ = compile_director(real_raw, strategy, 20, "continuity_v1", [source["id"]], cta_target="none")
    review = {"ready": False, "checks": [{"passed": True}, {"passed": False}]}
    real["plan_review"] = review
    skeleton, _, _ = compile_director(fallback_director(strategy), strategy, 20, "continuity_v1",
                                      [source["id"]], cta_target="none")
    assert is_placeholder_plan(skeleton)
    chosen = choose_accepted_plan(
        [{"plan": skeleton, "review": {"ready": False, "checks": [{"passed": False}]}},
         {"plan": real, "review": review}],
        skeleton)
    assert chosen is real
    assert "瓶身轮廓在桌角" in chosen["script"]
    assert not is_placeholder_plan(chosen)
    assert choose_accepted_plan([], skeleton) is skeleton


def test_failed_storyboard_is_not_replaced_by_a_template(source):
    from app.progressive_planning import ProgressivePlanningError
    from app.providers.gateway import PlannerInvalidResponse
    strategy = chosen_strategy(source)
    draft = director_output(strategy)["ad_script"]
    draft["beats"][0]["narration"] = "瓶身轮廓在桌角。"
    notes = []

    async def fail(system, data, assets, max_tokens=7000):
        raise PlannerInvalidResponse("策划服务未接受本次请求；已有成果仍保留。")

    with pytest.raises(ProgressivePlanningError, match="占位稿不是完成的方案"):
        asyncio.run(progressive_plan(
            stage_call=fail, on_progress=lambda _phase: None, record_failure=notes.append,
            strategy=strategy, draft=draft, draft_issues=[], duration=20, generation_strategy="continuity_v1",
            assets=[source], conversation=[], merchant_request="看清瓶子", current_plan=None,
            cta_target="none"))
    assert any(isinstance(item, dict) and item.get("error_code") == "fallback_skeleton" for item in notes)
    assert not any("瓶身轮廓在桌角" in json.dumps(item, ensure_ascii=False) for item in notes if isinstance(item, dict) and item.get("error_code") == "fallback_skeleton")
    empty_notes = []
    with pytest.raises(ProgressivePlanningError, match="占位稿不是完成的方案"):
        asyncio.run(progressive_plan(
            stage_call=fail, on_progress=lambda _phase: None, record_failure=empty_notes.append,
            strategy=strategy, draft=None, draft_issues=[], duration=20, generation_strategy="continuity_v1",
            assets=[source], conversation=[], merchant_request="看清瓶子", current_plan=None,
            cta_target="none"))
    assert any(isinstance(item, dict) and item.get("error_code") == "fallback_skeleton" for item in empty_notes)
    assert sum(1 for item in empty_notes if isinstance(item, dict) and item.get("issues") == ["storyboard"]) >= 3
