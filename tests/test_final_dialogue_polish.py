"""One final dialogue-only repair still needs a complete independent review."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.planner_runtime import planner_wire_context
from app.planning_session import PlanningSession, PlanningSessionError
from app.progressive_planning import (
    DIALOGUE_POLISH_SYSTEM, ProgressivePlanningError, compose_dialogue_polish,
    dialogue_narration_limits, dialogue_polish_schema, progressive_plan,
)
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import source


class SimulatedShutdown(BaseException):
    pass


@pytest.fixture
def scenario(source):
    state = SimpleNamespace(strategy=chosen_strategy(source), calls=[], actual_calls=[], failures=[],
        review_failures=["shot", "natural", "natural", None], polish_mutation=None,
        initial_brief="商品展示", checkpoint=None, interrupt_final_review=False, saved=[])
    state.draft = director_output(state.strategy)
    # Silent beats must not be added to the narrow response's identity set.
    state.draft["ad_script"]["beats"][1]["narration"] = ""

    def reject(review, kind):
        if kind is None:
            return review
        review["ready"] = False
        if kind == "shot":
            review["alignment"]["ready"] = False
            review["alignment"]["shots"][0]["passed"] = False
        else:
            for check in review["checks"]:
                if check["criterion"] == "natural_speech":
                    check["passed"] = False
            if kind == "another_content":
                review["checks"][0]["passed"] = False
            elif kind in ("action_capacity", "state_continuity"):
                review["execution_check"][kind] = False
            elif kind == "alignment":
                review["alignment"]["ready"] = False
                review["alignment"]["ad_expression_passed"] = False
            elif kind == "span":
                review["alignment"]["ready"] = False
                review["alignment"]["spans"][0]["passed"] = False
            elif kind == "natural_and_shot":
                review["alignment"]["ready"] = False
                review["alignment"]["shots"][0]["passed"] = False
        return review

    async def execute(system, context):
        state.actual_calls.append(deepcopy(context))
        stage = context["stage"]
        if stage == "storyboard":
            return deepcopy(state.draft)
        if context.get("task_kind") == "final_dialogue_polish":
            assert system == DIALOGUE_POLISH_SYSTEM
            result = {"narrations": [{"beat_id": beat["id"],
                "narration": "这轮廓。" if beat["id"] == "b0" else beat["narration"]}
                for beat in context["draft_storyboard"]["ad_script"]["beats"] if beat["narration"]]}
            if state.polish_mutation:
                state.polish_mutation(result)
            return result
        if stage == "plan_refinement":
            result = deepcopy(context["draft_storyboard"])
            shot = next(s for s in result["shots"] if s["id"] in context["repair_scope"]["shot_ids"])
            shot["description"] += f"第{context['repair_attempt']}次调整，保留静置辨认。"
            return result
        assert stage == "plan_review"
        number = sum(call["stage"] == "plan_review" for call in state.calls)
        if number == 4 and state.interrupt_final_review:
            state.interrupt_final_review = False
            raise SimulatedShutdown()
        return reject(accepted_review(context["final_plan"]), state.review_failures[number - 1])

    def run(*, checkpointed=False):
        state.calls = []

        def save(value):
            state.checkpoint = deepcopy(value)
            state.saved.append(deepcopy(value))

        session = PlanningSession(state.checkpoint, save, {"test": "dialogue_polish"},
                                  retry_delay=0) if checkpointed else None

        async def stage_call(system, context, assets):
            state.calls.append(deepcopy(context))
            if session:
                return await session.call(context["stage"],
                    {"system": system, "context": planner_wire_context(context)},
                    lambda: execute(system, context))
            return await execute(system, context)

        return asyncio.run(progressive_plan(stage_call=stage_call, on_progress=None,
            record_failure=lambda plan: state.failures.append(deepcopy(plan)),
            strategy=state.strategy, draft=state.draft["ad_script"], draft_issues=[], duration=20,
            generation_strategy="continuity_v1", assets=[source], conversation=[],
            merchant_request="改自然些", current_plan=None, initial_brief=state.initial_brief))

    state.run = run
    state.polishes = lambda: [c for c in state.calls if c.get("task_kind") == "final_dialogue_polish"]
    return state


def test_only_natural_speech_after_two_repairs_gets_one_polish_and_full_recheck(scenario):
    plan = scenario.run()
    assert [c["stage"] for c in scenario.calls] == ["storyboard", "plan_review", "plan_refinement",
        "plan_review", "plan_refinement", "plan_review", "plan_refinement", "plan_review"]
    assert len(scenario.polishes()) == 1
    polish = scenario.polishes()[0]
    assert polish["repair_attempt"] == polish["repair_attempt_limit"] == 2
    before = deepcopy(polish["draft_storyboard"]["ad_script"])
    after = deepcopy(plan["ad_script"])
    for a, b in zip(before["beats"], after["beats"]):
        a.pop("narration")
        b.pop("narration")
    assert before == after
    assert plan["ad_script"]["beats"][1]["narration"] == ""
    assert plan["visual_direction"] == polish["draft_storyboard"]["visual_direction"]
    assert plan["script"].startswith("这轮廓。")
    final_review = scenario.calls[-1]
    assert final_review["final_plan"]["script"] == plan["script"]
    assert final_review["execution_windows"] and final_review["transition_boundaries"]
    assert len(final_review["final_plan"]["segments"][0]["shots"]) == 3
    assert plan["plan_review"]["ready"] is True


@pytest.mark.parametrize("failure", ["another_content", "action_capacity", "state_continuity",
                                      "alignment", "span", "natural_and_shot", "shot"])
def test_other_failures_after_two_repairs_do_not_qualify(scenario, failure):
    scenario.review_failures[2] = failure
    with pytest.raises(ProgressivePlanningError):
        scenario.run()
    assert scenario.polishes() == []
    assert len(scenario.calls) == 6


def test_repeated_natural_speech_before_two_repairs_keeps_existing_early_stop(scenario):
    scenario.review_failures = ["natural", "natural"]
    with pytest.raises(ProgressivePlanningError, match="未改善"):
        scenario.run()
    assert scenario.polishes() == []
    assert len(scenario.calls) == 4


@pytest.mark.parametrize("last_failure", ["natural", "state_continuity", "shot"])
def test_failed_full_recheck_never_repeats_polish_or_normal_repair(scenario, last_failure):
    scenario.review_failures[-1] = last_failure
    # A shorter replacement is still subject to every final check.
    scenario.polish_mutation = lambda value: value["narrations"][0].update(narration="看看。")
    with pytest.raises(ProgressivePlanningError, match="单次台词收尾后"):
        scenario.run()
    assert len(scenario.polishes()) == 1
    assert len(scenario.calls) == 8


def test_fixed_speech_remains_protected_by_compile(scenario):
    scenario.initial_brief = "台词逐字保留“看轮廓。”"
    with pytest.raises(ProgressivePlanningError) as error:
        scenario.run()
    assert "用户固定台词未完整保留" in error.value.issue_summary
    assert len(scenario.polishes()) == 1
    assert scenario.calls[-1]["task_kind"] == "final_dialogue_polish"


def test_fixed_speech_can_be_returned_unchanged_while_other_line_is_polished(scenario):
    scenario.initial_brief = "台词逐字保留“看轮廓。”"
    def keep_locked(value):
        value["narrations"][0]["narration"] = "看轮廓。"
        value["narrations"][1]["narration"] = "留这里。"
    scenario.polish_mutation = keep_locked
    plan = scenario.run()
    assert plan["script"] == "看轮廓。留这里。"
    assert plan["plan_review"]["ready"]


def test_unchanged_polish_stops_without_reviewing_the_same_failed_draft_again(scenario):
    original = {beat["id"]: beat["narration"] for beat in scenario.draft["ad_script"]["beats"]}
    def unchanged(value):
        for line in value["narrations"]:
            line["narration"] = original[line["beat_id"]]
    scenario.polish_mutation = unchanged
    with pytest.raises(ProgressivePlanningError, match="没有产生实际文字变化"):
        scenario.run()
    assert len(scenario.polishes()) == 1
    assert len(scenario.calls) == 7


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(shots=[]),
    lambda value: value["narrations"][0].update(visual_seconds=0.1),
    lambda value: value["narrations"][0].update(beat_id="foreign"),
    lambda value: value["narrations"][0].update(beat_id="b1"),
    lambda value: value["narrations"].pop(),
    lambda value: value["narrations"].append(deepcopy(value["narrations"][0])),
    lambda value: value["narrations"][0].update(narration=""),
    lambda value: value["narrations"][0].update(narration=" \n\t"),
    lambda value: value["narrations"][0].update(narration="长" * 30),
])
def test_narrow_response_rejects_extra_fields_and_wrong_spoken_beat_coverage(scenario, mutation):
    scenario.polish_mutation = mutation
    with pytest.raises(ProgressivePlanningError, match="仅修改现有台词"):
        scenario.run()
    assert len(scenario.polishes()) == 1
    assert len(scenario.calls) == 7


def test_polish_schema_and_composition_preserve_every_non_dialogue_field(scenario):
    before = deepcopy(scenario.draft)
    limits = dialogue_narration_limits({"segments": [{"id": "seg_1", "speech_spans": [
        {"beat_id": "b0", "start": 0, "end": 3}, {"beat_id": "b2", "start": 3, "end": 6}]}]})
    schema = dialogue_polish_schema(before, limits)
    assert set(schema["properties"]) == {"narrations"}
    assert schema["additionalProperties"] is False
    items = schema["properties"]["narrations"]["items"]["oneOf"]
    assert all(item["additionalProperties"] is False for item in items)
    assert [item["properties"]["beat_id"]["const"] for item in items] == ["b0", "b2"]
    value = {"narrations": [{"beat_id": "b2", "narration": "留在这里。"},
                             {"beat_id": "b0", "narration": "看看这处轮廓。"}]}
    revised = compose_dialogue_polish(value, before, limits)
    assert scenario.draft == before
    for old, new in zip(before["ad_script"]["beats"], revised["ad_script"]["beats"]):
        new["narration"] = old["narration"]
    assert revised == before


@pytest.mark.parametrize(("start", "end", "seconds", "characters"), [
    (7.7, 14.0, 6.3, 18),
    (6.4, 16.4, 10.0, 29),  # Binary subtraction gives slightly less than ten.
    (0, 10.1, 10.1, 29),
    (1.7, 3.1, 1.4, 4),
])
def test_narration_limits_use_exact_actual_window_and_floor_without_mutation(start, end, seconds, characters):
    plan = {"segments": [{"id": "seg_1", "speech_spans": [
        {"beat_id": "b0", "start": start, "end": end}]}]}
    original = deepcopy(plan)
    assert dialogue_narration_limits(plan) == [{"beat_id": "b0", "segment_id": "seg_1",
        "start": start, "end": end, "available_seconds": seconds, "max_characters": characters}]
    assert plan == original


def test_polish_receives_per_beat_limits_and_keeps_existing_windows(scenario):
    plan = scenario.run()
    request = scenario.polishes()[0]
    assert "speech_budget" not in request
    assert {limit["beat_id"]: limit["max_characters"] for limit in request["narration_limits"]} == {"b0": 4, "b2": 4}
    items = request["schema"]["properties"]["narrations"]["items"]["oneOf"]
    assert {item["properties"]["beat_id"]["const"]: item["properties"]["narration"]["maxLength"]
            for item in items} == {"b0": 4, "b2": 4}
    for before, after in zip(request["final_plan"]["segments"], plan["segments"]):
        assert [(s["id"], s["start"], s["end"]) for s in before["shots"]] == [
            (s["id"], s["start"], s["end"]) for s in after["shots"]]
        assert [(s["beat_id"], s["start"], s["end"]) for s in before["speech_spans"]] == [
            (s["beat_id"], s["start"], s["end"]) for s in after["speech_spans"]]


def test_copy_within_global_cap_but_over_actual_beat_limit_stops(scenario):
    scenario.polish_mutation = lambda value: value["narrations"][0].update(narration="看这轮廓。")
    with pytest.raises(ProgressivePlanningError, match="逐拍字数预算"):
        scenario.run()
    assert len(scenario.polishes()) == 1
    assert len(scenario.calls) == 7


def test_resume_after_polish_replays_it_once_and_rechecks_the_full_plan(scenario):
    scenario.interrupt_final_review = True
    with pytest.raises(SimulatedShutdown):
        scenario.run(checkpointed=True)
    assert scenario.checkpoint["request_count"] == 8
    plan = scenario.run(checkpointed=True)
    assert plan["plan_review"]["ready"]
    assert scenario.checkpoint["request_count"] == 9
    assert sum(c.get("task_kind") == "final_dialogue_polish" for c in scenario.actual_calls) == 1
    assert len(scenario.polishes()) == 1


def test_resumed_final_review_remains_under_existing_request_cap(scenario):
    scenario.interrupt_final_review = True
    with pytest.raises(SimulatedShutdown):
        scenario.run(checkpointed=True)
    scenario.checkpoint["request_count"] = 12
    calls = len(scenario.actual_calls)
    with pytest.raises(PlanningSessionError, match="处理次数上限"):
        scenario.run(checkpointed=True)
    assert len(scenario.actual_calls) == calls
    assert sum(c.get("task_kind") == "final_dialogue_polish" for c in scenario.actual_calls) == 1
