"""Class-(b) planner faults finish inside the job. The merchant is not asked to retry."""
import asyncio
import json

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
