"""Conservative new-wire failures retain strict models and one repair budget."""
import asyncio
from copy import deepcopy
import json
import math

import pytest
from pydantic import ValidationError

from app.av_review import AVReview, lower_failed_av_verdicts, validate_av_review
from app.providers.gateway import Gateway, ProviderError
from test_av_review import plan as av_plan, review as av_review
from test_av_refinement import harness, NORMAL, REFINED
from test_continuity_planning import compiled_from_reservations, directed_review
from test_providers import settings, source, plan, strategy_stage, install_transport, request_context, chat_output
from ad_script_fixtures import script_stages


@pytest.fixture
def complete_review(av_plan, av_review):
    raw = deepcopy(av_review)
    raw["shots"] = [{"segment_id": segment.id, "shot_id": shot.id, "passed": True,
        "note": "按实际动作审核", "pacing": {"action_seconds_required": 2,
            "action_timing_passed": True, "cut_necessity_passed": True, "note": "动作和停留估计两秒"}}
        for segment in av_plan.segments for shot in segment.shots]
    return raw


@pytest.mark.parametrize("field", ["action_timing_passed", "cut_necessity_passed"])
def test_explicit_pacing_failure_only_lowers_parents_and_preserves_raw(av_plan, complete_review, field):
    raw = deepcopy(complete_review)
    raw["shots"][0]["pacing"][field] = False
    before = deepcopy(raw)
    with pytest.raises(ValidationError): AVReview.model_validate(raw)
    lowered = lower_failed_av_verdicts(raw, av_plan)
    expected = deepcopy(raw)
    expected["shots"][0]["passed"] = expected["ready"] = False
    assert lowered == expected and raw == before
    assert not validate_av_review(lowered, av_plan, require_pacing=True).ready
    assert lower_failed_av_verdicts(lowered, av_plan) == lowered


@pytest.mark.parametrize("failure", ["ad", "span", "shot"])
def test_top_level_cannot_pass_over_an_explicit_child_failure(av_plan, complete_review, failure):
    if failure == "ad": complete_review["ad_expression_passed"] = False
    elif failure == "span": complete_review["spans"][0]["passed"] = False
    else: complete_review["shots"][0]["passed"] = False
    result = lower_failed_av_verdicts(complete_review, av_plan)
    assert result == {**complete_review, "ready": False}
    assert not validate_av_review(result, av_plan, require_pacing=True).ready


@pytest.mark.parametrize("seconds,passed", [(10, True), (10.00000001, True), (10.1, False)])
def test_existing_numeric_timing_guard_lowers_only_known_overruns(av_plan, complete_review, seconds, passed):
    complete_review["shots"][0]["pacing"]["action_seconds_required"] = seconds
    before = deepcopy(complete_review)
    result = lower_failed_av_verdicts(complete_review, av_plan.model_dump())
    assert result["shots"][0]["pacing"]["action_seconds_required"] == seconds
    assert result["shots"][0]["pacing"]["action_timing_passed"] is passed
    assert result["shots"][0]["passed"] is passed and result["ready"] is passed
    assert result["shots"][1] == before["shots"][1]  # Same shot ID in another segment is separate.
    assert complete_review == before
    assert validate_av_review(result, av_plan, require_pacing=True).ready is passed


def test_false_verdict_is_never_upgraded_even_when_children_pass(av_plan, complete_review):
    complete_review["ready"] = False
    assert lower_failed_av_verdicts(complete_review, av_plan) == complete_review
    with pytest.raises(ValidationError): AVReview.model_validate(lower_failed_av_verdicts(complete_review, av_plan))


@pytest.mark.parametrize("invalid", ["string_bool", "numeric_bool", "missing_passed", "missing_pacing_check",
    "empty_note", "negative_seconds", "infinite_seconds", "string_seconds", "unknown_shot", "unknown_span", "extra_field"])
def test_lowering_does_not_repair_other_invalid_fields_or_identities(av_plan, complete_review, invalid):
    shot = complete_review["shots"][0]
    shot["pacing"]["cut_necessity_passed"] = False
    if invalid == "string_bool": shot["pacing"]["action_timing_passed"] = "false"
    elif invalid == "numeric_bool": shot["pacing"]["action_timing_passed"] = 0
    elif invalid == "missing_passed": shot.pop("passed")
    elif invalid == "missing_pacing_check": shot["pacing"].pop("action_timing_passed")
    elif invalid == "empty_note": shot["note"] = " "
    elif invalid == "negative_seconds": shot["pacing"]["action_seconds_required"] = -1
    elif invalid == "infinite_seconds": shot["pacing"]["action_seconds_required"] = math.inf
    elif invalid == "string_seconds": shot["pacing"]["action_seconds_required"] = "12"
    elif invalid == "unknown_shot": shot["shot_id"] = "unknown"
    elif invalid == "unknown_span": complete_review["spans"][0]["span_id"] = "unknown"
    else: shot["changed_fact"] = "unsupported"
    before = deepcopy(complete_review)
    result = lower_failed_av_verdicts(complete_review, av_plan)
    assert complete_review == before
    assert result["shots"][0]["pacing"] == before["shots"][0]["pacing"]
    with pytest.raises(ValueError): validate_av_review(result, av_plan, require_pacing=True)


@pytest.mark.parametrize("false_like", ["false", 0, None])
def test_nonboolean_child_values_never_create_a_rejection(av_plan, complete_review, false_like):
    complete_review["shots"][0]["pacing"]["action_timing_passed"] = false_like
    assert lower_failed_av_verdicts(complete_review, av_plan) == complete_review
    with pytest.raises(ValidationError): AVReview.model_validate(complete_review)


def install_contradictory_response(monkeypatch, *, numeric=False, malformed=False):
    """Alter mocked provider replies at the real stage boundary, never network."""
    original = Gateway._planner_json
    emitted = []
    async def planner(self, system, data, originals, **kwargs):
        result = await original(self, system, data, originals, **kwargs)
        if data["stage"] == "alignment_review":
            result = deepcopy(result)
            if not emitted and numeric:
                shot = data["segments"][0]["shots"][0]
                result["shots"][0]["pacing"].update(action_timing_passed=True,
                    action_seconds_required=shot["end"] - shot["start"] + .1)
            if not result["ready"] or (not emitted and numeric):
                result["ready"] = True
                result["shots"][0]["passed"] = True
            if malformed and not emitted:
                result["shots"][0]["pacing"]["action_timing_passed"] = "false"
            emitted.append(result)
        return result
    monkeypatch.setattr(Gateway, "_planner_json", planner)
    return emitted


@pytest.mark.parametrize("kind", ["action", "cut", "numeric"])
def test_new_gateway_routes_lowered_failure_into_its_existing_single_revision(harness, monkeypatch, kind):
    harness.capacity = kind == "action"
    if kind == "numeric": harness.first_pass = True
    emitted = install_contradictory_response(monkeypatch, numeric=kind == "numeric")
    result = harness.run()["plan"]
    assert harness.calls == (REFINED if kind == "cut" else NORMAL + ["script_review", "storyboard", "alignment_review"])
    assert result["alignment_review"]["ready"] is True
    assert emitted[0]["ready"] is True and emitted[0]["shots"][0]["passed"] is True
    records = harness.records()
    assert len(records) == 1 and records[0]["raw_output"] == emitted[0]
    revised = next(context for context in harness.contexts if "review_feedback" in context)
    feedback = revised["review_feedback"]
    assert feedback["ready"] is False and feedback["shots"][0]["passed"] is False
    assert feedback["shots"][0]["pacing"]["action_seconds_required"] == emitted[0]["shots"][0]["pacing"]["action_seconds_required"]
    assert feedback["shots"][0]["note"] == emitted[0]["shots"][0]["note"]


def test_second_contradictory_failure_cannot_start_another_revision(harness, monkeypatch):
    harness.capacity = True
    harness.second_pass = False
    emitted = install_contradictory_response(monkeypatch)
    with pytest.raises(ProviderError, match="已重排一次脚本与时间"):
        harness.run()
    assert harness.calls == NORMAL + ["script_review", "storyboard", "alignment_review"]
    assert len(emitted) == 2 and all(raw["ready"] for raw in emitted)
    assert sorted(json.dumps(record["raw_output"], sort_keys=True) for record in harness.records()) == sorted(
        json.dumps(raw, sort_keys=True) for raw in emitted)


def test_new_gateway_still_rejects_malformed_booleans_without_semantic_retry(harness, monkeypatch):
    harness.capacity = True
    install_contradictory_response(monkeypatch, malformed=True)
    with pytest.raises(ProviderError): harness.run()
    assert harness.calls == NORMAL
    assert "refining_script" not in harness.phases and "refining_storyboard" not in harness.phases


def test_legacy_gateway_keeps_raw_contradictions_strict(settings, source, plan, strategy_stage, install_transport):
    settings.planning_generation_strategy = "continuity_v1"
    script, review, _ = script_stages(strategy_stage, plan)
    calls, compiled = [], None
    def handle(request):
        nonlocal compiled
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        if stage == "strategy": return chat_output(strategy_stage)
        if stage == "ad_script": return chat_output(script)
        if stage == "script_review": return chat_output(review)
        if stage == "storyboard":
            compiled = compiled_from_reservations(context)
            return chat_output(compiled)
        result = directed_review(compiled)
        for check in result["shots"]:
            check["pacing"] = {"action_seconds_required": 1, "action_timing_passed": True,
                "cut_necessity_passed": True, "note": "离线动作估计"}
        result["shots"][0]["pacing"]["action_timing_passed"] = False
        return chat_output(result)
    install_transport(handle)
    with pytest.raises(ProviderError):
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品", "duration_seconds": 20}, [source], []))
    assert calls == ["strategy", *NORMAL]
