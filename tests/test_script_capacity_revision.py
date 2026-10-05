"""Shared numeric action capacity consumes the same single semantic return."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.ad_script import script_context
from app.beat_timing import beat_minimum_ticks, script_capacity_budget
from app.db import StaleRevision
from app.providers.gateway import Gateway, ProviderError
from app.strategy import build_strategy, select_rules
from ad_script_fixtures import CRITERIA, execution_check, with_action_timing
from test_ad_script import script, strategy
from test_direction_selection import candidates_for, compiled_from_reservations, directed_review
from test_providers import settings, source, strategy_stage, install_transport, chat_output, request_context


def test_context_reports_exact_shared_joint_capacity_before_review(script, strategy):
    value = with_action_timing(script)
    value["beats"][0]["timing"].update(visual_seconds=12.01, speech_overlap=False)
    value["beats"][1]["timing"]["visual_seconds"] = 12
    context = script_context(strategy, 20, script=value, generation_strategy="continuity_v1")
    capacity = script_capacity_budget(value, 20)
    assert context["draft_capacity"] == capacity
    assert context["draft_beat_timing"] == capacity["beats"]
    assert capacity["total_minimum_seconds"] == sum(beat_minimum_ticks(beat) for beat in value["beats"]) / 10
    assert capacity["overrun_seconds"] == pytest.approx(capacity["total_minimum_seconds"] - 20)
    assert capacity["target_seconds"] == 20 and context["schedule_feasible"] is False


@pytest.fixture
def capacity_gateway(settings, source, strategy_stage, install_transport, tmp_path):
    settings.data_root = tmp_path / "data"
    settings.direction_selection_enabled = True
    settings.planning_generation_strategy = "continuity_v1"
    candidates = candidates_for((None, None, None, source))
    strategy = build_strategy(candidates.brief, candidates.directions, "direction_1", select_rules(None), merchant_choice=True)
    state = SimpleNamespace(calls=[], contexts=[], phases=[], new_flow=True, repair=True,
        av_failure=None, first_review_mutation=None, second_review_mutation=None, compiled=None, first_review=None,
        first_review_result_mutation=None, second_review_result_mutation=None, reviewed_visual_seconds=12,
        draft_enum_error=False)

    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        _, context = request_context(request)
        stage = context["stage"]
        state.calls.append(stage)
        state.contexts.append(deepcopy(context))
        if stage == "strategy":
            assert not state.new_flow
            return chat_output(strategy_stage)
        if stage == "ad_script":
            evidence = context["selected_direction"]["evidence_ids"]
            result = {"core_message": context["selected_direction"]["angle"], "audience_takeaway": "了解商品与桌面外观的关系",
                "product_role": "同一商品参与桌面搭配", "skill_version": context["runtime_skill"]["version"],
                "beats": [{"id": f"beat_{index}", "purpose": "理解商品与桌面的搭配", "message": "外观与搭配关系",
                    "visual_role": "将同一瓶子放在桌角，停留辨认轮廓", "narration": line, "evidence_ids": evidence}
                    for index, line in enumerate(("看看这瓶身。", "桌角，留给喜欢的样子。"))]}
            result = with_action_timing(result)
            for beat in result["beats"]: beat["timing"]["visual_seconds"] = 8
            if state.draft_enum_error: result["beats"][0]["boundary_before"] = "ellipsis"
            if not state.new_flow: result["evidence_ids"] = evidence
            return chat_output(result)
        if stage == "script_review":
            script = deepcopy(context["draft_script"])
            second = state.calls.count(stage) == 2
            if not second:
                # The initial draft fit; the reviewed output, despite its true
                # execution verdict, now exceeds the joint capacity.
                if state.draft_enum_error:
                    assert "draft_validation_issues" in context and "draft_capacity" not in context
                    script["beats"][0]["boundary_before"] = "complete"
                else:
                    assert context["draft_capacity"]["total_minimum_seconds"] == 16
                for beat in script["beats"]: beat["timing"]["visual_seconds"] = state.reviewed_visual_seconds
                if state.first_review_mutation: state.first_review_mutation(script)
            else:
                feedback = context["review_feedback"]
                assert feedback["kind"] == "script_capacity"
                assert feedback["capacity"] == context["draft_capacity"] == script_capacity_budget(script, 20)
                assert feedback["capacity"]["total_minimum_seconds"] == state.reviewed_visual_seconds * 2
                assert feedback["capacity"]["target_seconds"] == 20
                assert feedback["capacity"]["overrun_seconds"] == max(0, state.reviewed_visual_seconds * 2 - 20)
                assert context["draft_beat_timing"] == feedback["capacity"]["beats"]
                assert context["schedule_feasible"] is (state.reviewed_visual_seconds * 2 <= 20)
                assert "draft_storyboard" not in context
                assert "draft_validation_issues" not in context
                if state.repair:
                    # Explicit offline writer output removes redundant actions;
                    # production never silently lowers an estimate or deletes text.
                    script["beats"] = [script["beats"][0]]
                    beat = script["beats"][0]
                    beat["visual_role"] = "删除重复取放，只保留瓶子在桌角的一次轻转与辨认"
                    beat["timing"].update(visual_seconds=5, action_plan="合并两次重复取放，只轻转同一瓶子后停留辨认，画外音同步")
                if state.second_review_mutation: state.second_review_mutation(script)
            result = {"script": script, "ready": True,
                "checks": [{"criterion": key, "passed": True, "note": "离线内容审核夹具"} for key in CRITERIA]}
            if state.new_flow: result["execution_check"] = execution_check()
            result_mutation = state.second_review_result_mutation if second else state.first_review_result_mutation
            if result_mutation: result_mutation(result)
            if not second: state.first_review = deepcopy(result)
            return chat_output(result)
        if stage == "storyboard":
            state.compiled = compiled_from_reservations(context)
            return chat_output(state.compiled)
        assert stage == "alignment_review"
        result = directed_review(state.compiled)
        if state.av_failure:
            result["ready"] = False
            result["shots"][0].update(passed=False, note="离线最终声画审核拒绝")
            result["shots"][0]["pacing"].update(
                action_timing_passed=state.av_failure != "capacity",
                cut_necessity_passed=state.av_failure != "cut")
        return chat_output(result)

    install_transport(handle)
    def run(progress=None):
        def report(phase):
            state.phases.append(phase)
            if progress: progress(phase)
        async def execute():
            settings.direction_selection_enabled = state.new_flow
            gateway = Gateway(settings)
            project = {"brief": "蓝瓶商品", "duration_seconds": 20}
            if state.new_flow:
                return (await gateway.decide(project, [source], [], [], None,
                    selected_strategy=strategy.model_dump(), on_progress=report))["plan"]
            return await gateway.plan(project, [source], [], on_progress=report)
        return asyncio.run(execute())
    state.run = run
    state.strategy = strategy
    state.records = lambda: [json.loads(path.read_text()) for path in (settings.data_root / "diagnostics").glob("*.json")]
    return state


def test_numeric_overload_reuses_review_once_then_finishes_in_five_calls(capacity_gateway):
    result = capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review", "storyboard", "alignment_review"]
    assert capacity_gateway.phases.count("refining_script") == 1
    assert "refining_storyboard" not in capacity_gateway.phases
    assert len(result["ad_script"]["beats"]) == 1
    assert result["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 5
    assert result["strategy"] == capacity_gateway.strategy.model_dump()
    selected = next(d for d in capacity_gateway.strategy.directions if d.id == capacity_gateway.strategy.selected_direction_id)
    assert result["production"] == selected.production.model_dump()
    records = capacity_gateway.records()
    assert len(records) == 1 and records[0]["stage"] == "script_review"
    assert records[0]["raw_output"] == capacity_gateway.first_review


def test_second_numeric_failure_stops_with_real_seconds_and_no_storyboard(capacity_gateway):
    capacity_gateway.repair = False
    with pytest.raises(ProviderError, match="已重排一次脚本与时间.*24秒.*20秒"):
        capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review"]
    assert len(capacity_gateway.records()) == 2


@pytest.mark.parametrize("failure", ["capacity", "cut"])
def test_early_capacity_return_consumes_the_same_final_av_revision_budget(capacity_gateway, failure):
    capacity_gateway.av_failure = failure
    with pytest.raises(ProviderError, match="已重排一次脚本与时间"):
        capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review", "storyboard", "alignment_review"]
    assert "refining_storyboard" not in capacity_gateway.phases


@pytest.mark.parametrize("invalid", ["unknown_fact", "missing_timing"])
def test_numeric_overload_does_not_bypass_review_authority_guards(capacity_gateway, invalid):
    def mutate(script):
        if invalid == "unknown_fact": script["beats"][0]["evidence_ids"] = ["invented"]
        else: script["beats"][0].pop("timing")
    capacity_gateway.first_review_mutation = mutate
    with pytest.raises(ProviderError): capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review"]


def test_extra_production_on_the_reviewed_script_is_dropped(capacity_gateway):
    capacity_gateway.first_review_mutation = lambda script: script.update(production={"speech_mode": "none"})
    result = capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review", "storyboard", "alignment_review"]
    selected = next(d for d in capacity_gateway.strategy.directions
                    if d.id == capacity_gateway.strategy.selected_direction_id)
    assert result["production"] == selected.production.model_dump()
    assert "production" not in result["ad_script"]


def test_second_review_cannot_add_unknown_facts(capacity_gateway):
    capacity_gateway.second_review_mutation = lambda script: script["beats"][0].update(evidence_ids=["invented"])
    with pytest.raises(ProviderError): capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review"]


def test_cancellation_is_checked_before_the_additional_review(capacity_gateway):
    def progress(phase):
        if phase == "refining_script": raise StaleRevision("TEST canceled")
    with pytest.raises(StaleRevision): capacity_gateway.run(progress)
    assert capacity_gateway.calls == ["ad_script", "script_review"]


def test_legacy_capacity_failure_does_not_add_a_review(capacity_gateway):
    capacity_gateway.new_flow = False
    with pytest.raises(ProviderError): capacity_gateway.run()
    assert capacity_gateway.calls == ["strategy", "ad_script", "script_review"]
