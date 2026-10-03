"""Bounded final-AV feedback revisions over mocked text transport only."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.db import StaleRevision
from app.providers.gateway import Gateway, ProviderError
from app.strategy import build_strategy, select_rules
from ad_script_fixtures import CRITERIA, stage_outputs, with_action_timing, execution_check
from test_direction_selection import candidates_for, compiled_from_reservations, directed_review
from test_providers import settings, source, plan, strategy_stage, install_transport, request_context, chat_output


PRIVATE_REASONING = "TEST_HIDDEN_REASONING_NOT_FOR_REPLAY"
NORMAL = ["ad_script", "script_review", "storyboard", "alignment_review"]
REFINED = NORMAL + ["storyboard", "alignment_review"]


@pytest.fixture
def harness(settings, source, tmp_path, install_transport):
    settings.data_root = tmp_path / "data"
    settings.direction_selection_enabled = True
    settings.planning_generation_strategy = "continuity_v1"
    candidates = candidates_for((None, None, None, source))
    strategy = build_strategy(candidates.brief, candidates.directions, "direction_1", select_rules(None), merchant_choice=True)
    state = SimpleNamespace(calls=[], contexts=[], phases=[], traces=[], compiled=None,
                            first_review=None, second_review=None, first_pass=False,
                            second_pass=True, mutation=None, failure=None, capacity=False, review_mutation=None)

    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions", "No media/network destination allowed"
        payload, context = request_context(request)
        stage = context["stage"]
        assert PRIVATE_REASONING not in json.dumps(payload)
        state.calls.append(stage)
        state.contexts.append(deepcopy(context))
        if stage == "ad_script":
            result = {"core_message": "办公桌上留一个好看的位置", "audience_takeaway": "关注桌面与商品的外观搭配",
                "product_role": "主体是商品", "skill_version": context["runtime_skill"]["version"],
                "beats": [{"id": "b1", "purpose": "建立办公场景与商品选择", "message": "桌面搭配",
                    "visual_role": "办公桌上摆放同一瓶子", "narration": "给桌面，留一点自己喜欢的样子。", "evidence_ids": ["appearance"]}]}
            result = with_action_timing(result)
        elif stage == "script_review":
            script = deepcopy(context["draft_script"])
            if "review_feedback" in context:
                script["beats"][0]["narration"] = "喜欢的瓶子，留在桌角。"
                script["beats"][0]["timing"]["visual_seconds"] = 3
                if state.review_mutation:
                    state.review_mutation(script)
            result = {"script": script, "ready": True, "execution_check": execution_check(),
                "checks": [{"criterion": key, "passed": True, "note": "明确的离线验收夹具"} for key in CRITERIA]}
        elif stage == "storyboard":
            if "review_feedback" not in context:
                state.compiled = compiled_from_reservations(context)
            else:
                assert state.calls.count("storyboard") == 2
                result = deepcopy(context["draft_storyboard"])
                segment = result["segments"][0]
                shot = segment["shots"][0]
                shot["end"] = segment["duration_seconds"]
                shot["description"] = "同一瓶子留在办公桌上，镜头缓慢靠近，人物仅轻轻转向商品。"
                segment["shots"] = [shot]
                for span in segment["speech_spans"]:
                    span["shot_ids"] = [shot["id"]]
                    for anchor in span["anchors"]:
                        anchor["shot_ids"] = [shot["id"]]
                if state.mutation:
                    state.mutation(result)
                state.compiled = result
            result = state.compiled
        elif stage == "alignment_review":
            if state.failure and (state.calls.count(stage) == 1 or state.failure == "timeout"):
                if state.failure == "timeout":
                    raise httpx.ReadTimeout("private failure", request=request)
                if state.failure == "length":
                    return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
                if state.failure == "empty":
                    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
                return chat_output({"ready": False})
            if state.failure == "empty":
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
            result = directed_review(state.compiled)
            accepted = state.first_pass if state.calls.count(stage) == 1 else state.second_pass
            if not accepted:
                result["ready"] = False
                result["shots"][0].update(passed=False, note="TEST private review action needs simplification")
                result["shots"][0]["pacing"].update(action_timing_passed=not state.capacity, cut_necessity_passed=state.capacity,
                    note="TEST review: continuous observation needs a simpler action")
            if state.calls.count(stage) == 1:
                state.first_review = deepcopy(result)
            else:
                state.second_review = deepcopy(result)
        else:
            pytest.fail("Unexpected stage: " + stage)
        response = chat_output(result)
        body = response.json()
        body["choices"][0]["message"]["reasoning_content"] = PRIVATE_REASONING
        return httpx.Response(200, json=body)

    install_transport(handle)

    def run(progress=None):
        def report(phase):
            state.phases.append(phase)
            if progress:
                progress(phase)
        return asyncio.run(Gateway(settings).decide(
            {"brief": "为我的瓶子做商品广告", "duration_seconds": 20}, [source], [], [], None,
            selected_strategy=strategy.model_dump(), on_progress=report, on_skill_trace=state.traces.append))
    state.run = run
    state.strategy = strategy
    state.settings = settings
    state.records = lambda: [json.loads(path.read_text()) for path in (settings.data_root / "diagnostics").glob("*.json")]
    return state


def test_rejected_av_gets_one_refinement_then_independent_complete_review(harness):
    result = harness.run()["plan"]
    assert harness.calls == REFINED
    first, second = [context for context in harness.contexts if context["stage"] == "storyboard"]
    for key in ("approved_ad_script", "expression_schedule", "selected_direction", "production", "generation_grouping", "assets"):
        assert first[key] == second[key]
    assert second["review_feedback"] == harness.first_review
    assert second["stage_skills"]["stage"] == "legacy_storyboard"
    assert len(second["draft_storyboard"]["segments"][0]["shots"]) == 2
    assert len(result["segments"][0]["shots"]) == 1
    assert result["ad_script"] == first["approved_ad_script"]
    assert result["production"] == first["production"]
    assert result["strategy"] == harness.strategy.model_dump()
    assert result["alignment_review"] == harness.second_review
    assert harness.phases.count("refining_storyboard") == 1
    assert harness.phases.count("checking_alignment") == 2
    actual_av = [context for context in harness.contexts if context["stage"] == "alignment_review"]
    assert len(actual_av[0]["segments"][0]["shots"]) == 2
    assert len(actual_av[1]["segments"][0]["shots"]) == 1
    assert "review_feedback" not in actual_av[1]
    assert [trace["id"] for trace in harness.traces].count("ads-video-storyboard-direction") == 2
    assert [trace["id"] for trace in harness.traces].count("ads-video-plan-review") == 3
    records = harness.records()
    assert len(records) == 1
    assert records[0]["raw_output"] == harness.first_review
    assert len(records[0]["validation_candidate"]["segments"][0]["shots"]) == 2
    assert PRIVATE_REASONING not in json.dumps(records)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (harness.settings.data_root / "diagnostics").glob("*.json"))


def test_already_accepted_av_has_no_refinement(harness):
    harness.first_pass = True
    assert harness.run()["plan"]["alignment_review"]["ready"]
    assert harness.calls == NORMAL
    assert not harness.records()


def test_second_valid_rejection_stops_with_owned_category_and_both_diagnostics(harness):
    harness.second_pass = False
    with pytest.raises(ProviderError, match="已修正一次分镜，仍需调整切镜必要性") as error:
        harness.run()
    assert "TEST private" not in str(error.value)
    assert harness.calls == REFINED
    assert len(harness.records()) == 2
    assert sorted(len(record["validation_candidate"]["segments"][0]["shots"]) for record in harness.records()) == [1, 2]


@pytest.mark.parametrize("change", ["copy", "speech_window", "fact", "asset", "duration", "generation_id", "language", "format", "audio", "structure", "character"])
def test_refinement_cannot_change_fixed_inputs_or_trigger_additional_timing_repair(harness, change):
    def mutate(result):
        segment = result["segments"][0]
        span = segment["speech_spans"][0]
        if change == "copy":
            span["text"] = span["text"].replace("样子", "色彩")
            span["anchors"][0]["text"] = span["text"]
        elif change == "speech_window":
            span["end"] -= .2
            span["anchors"][0]["end"] -= .2
        elif change == "fact":
            segment["shots"][0]["evidence_ids"] = ["invented_fact"]
        elif change == "asset":
            segment["reference_asset_ids"] = ["invented_original"]
        elif change == "duration":
            segment["duration_seconds"] = 19
            segment["shots"][0]["end"] = 19
        elif change == "generation_id":
            segment["id"] = "replacement"
        elif change == "language":
            result["language"] = "en-US"
        elif change == "format":
            result["aspect_ratio"] = "16:9"
        elif change == "audio":
            result["audio_strategy"] = "voiceover"
        elif change == "character":
            segment["shots"][0]["character_ids"] = ["char_invented"]
        else:
            # A repairable timing mismatch is still rejected immediately here.
            span["anchors"][0]["start"] = span["start"] - 1
    harness.mutation = mutate
    with pytest.raises(ProviderError):
        harness.run()
    assert harness.calls == NORMAL + ["storyboard"]
    assert "repairing_storyboard" not in harness.phases


def test_refinement_cannot_overwrite_server_owned_copy_facts_cast_or_voice(harness):
    def mutate(result):
        result.update(ad_script={"core_message": "invalid override"}, facts={"name": "invented item"},
            script="invalid copy", strategy={"selected_direction_id": "different"},
            production={"characters": [], "speech_mode": "none"})
        segment = result["segments"][0]
        segment["production"] = {"speech_mode": "none"}
        segment["speech_spans"][0]["speaker_id"] = "char_invented"
    harness.mutation = mutate
    result = harness.run()["plan"]
    approved = harness.contexts[2]
    assert result["ad_script"] == approved["approved_ad_script"]
    assert result["production"] == approved["production"]
    assert result["strategy"] == harness.strategy.model_dump()
    assert result["segments"][0]["speech_spans"][0].get("speaker_id", "") == ""
    assert result["script"] == approved["approved_ad_script"]["beats"][0]["narration"]


@pytest.mark.parametrize("phase,count", [("refining_storyboard", 1), ("checking_alignment", 2)])
def test_cancellation_or_revision_change_blocks_next_request(harness, phase, count):
    seen = 0
    def progress(value):
        nonlocal seen
        if value == phase:
            seen += 1
            if seen == count:
                raise StaleRevision("TEST canceled")
    with pytest.raises(StaleRevision):
        harness.run(progress)
    assert harness.calls == (NORMAL if phase == "refining_storyboard" else NORMAL + ["storyboard"])


@pytest.mark.parametrize("failure", ["timeout", "length", "empty", "invalid_review"])
def test_transport_empty_length_or_invalid_review_never_triggers_semantic_refinement(harness, failure):
    harness.failure = failure
    with pytest.raises(ProviderError):
        harness.run()
    retries = 2 if failure == "timeout" else 1 if failure == "empty" else 0
    assert harness.calls == NORMAL + ["alignment_review"] * retries
    assert "refining_storyboard" not in harness.phases


def test_legacy_plan_still_stops_after_one_rejected_review(settings, source, plan, strategy_stage, install_transport):
    outputs = stage_outputs(strategy_stage, plan)
    outputs["alignment_review"]["ready"] = False
    outputs["alignment_review"]["spans"][0]["passed"] = False
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        assert "review_feedback" not in context
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(ProviderError, match="对应检查"):
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert calls == ["strategy", *NORMAL]


def test_refinement_may_omit_unchanged_optional_language_and_format_defaults(harness):
    def mutate(result):
        result.pop("language")
        result.pop("aspect_ratio")
    harness.mutation = mutate
    result = harness.run()["plan"]
    assert result["language"] == "zh-CN"
    assert result["aspect_ratio"] == "9:16"
    assert result["alignment_review"]["ready"]
    assert harness.calls == REFINED


def test_capacity_failure_returns_once_to_script_review_and_rebuilds_timing(harness):
    harness.capacity = True
    result = harness.run()["plan"]
    assert harness.calls == NORMAL + ["script_review", "storyboard", "alignment_review"]
    first, revised = [x for x in harness.contexts if x["stage"] == "script_review"]
    assert revised["review_feedback"] == harness.first_review
    assert revised["draft_storyboard"][0]["shots"]
    assert first["selected_direction"] == revised["selected_direction"]
    assert first["merchant_request"] == revised["merchant_request"]
    first_board, revised_board = [x for x in harness.contexts if x["stage"] == "storyboard"]
    assert first_board["expression_schedule"] != revised_board["expression_schedule"]
    assert result["ad_script"]["beats"][0]["narration"] == "喜欢的瓶子，留在桌角。"
    assert result["production"] == next(d.production.model_dump() for d in harness.strategy.directions if d.id == harness.strategy.selected_direction_id)
    assert harness.phases.count("refining_script") == 1
    assert "refining_storyboard" not in harness.phases


def test_capacity_second_failure_cannot_start_another_revision(harness):
    harness.capacity = True
    harness.second_pass = False
    with pytest.raises(ProviderError, match="已重排一次脚本与时间"):
        harness.run()
    assert harness.calls == NORMAL + ["script_review", "storyboard", "alignment_review"]
    assert len(harness.records()) == 2


def test_capacity_revision_guard_runs_before_model_request(harness):
    harness.capacity = True
    def progress(phase):
        if phase == "refining_script":
            raise StaleRevision("TEST superseded")
    with pytest.raises(StaleRevision):
        harness.run(progress)
    assert harness.calls == NORMAL


def test_capacity_revision_cannot_add_unknown_product_claim(harness):
    harness.capacity = True
    harness.review_mutation = lambda script: script["beats"][0].update(evidence_ids=["unknown_claim"])
    with pytest.raises(ProviderError):
        harness.run()
    assert harness.calls == NORMAL + ["script_review"]
