"""Offline bounded-repair regressions; no real planner or generation calls."""
import asyncio
from copy import deepcopy
import json
import traceback

import httpx
import pytest
from pydantic import ValidationError

from ad_script_fixtures import alignment_review, stage_outputs
from test_providers import (settings, source, plan, strategy_stage, install_transport,
                            request_context, assert_redacted)
from app.db import StaleRevision
from app.providers import ProviderError
from app.providers.gateway import Gateway
from app.schemas import Plan
from app.storyboard_repair import repairable_timing_issues


PRIVATE_REASONING = "PRIVATE_PROVIDER_REASONING_MUST_NEVER_ENTER_REPAIR"
NORMAL_STAGES = ["strategy", "ad_script", "script_review", "storyboard"]


def response(value):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(value), "reasoning_content": PRIVATE_REASONING}}]})


@pytest.fixture
def repair_outputs(plan, strategy_stage):
    original = deepcopy(plan)
    segment = original["segments"][0]
    segment["narration"] = "选只蓝瓶放在桌上，日常也能看见喜欢的颜色。"
    segment["shots"] = [deepcopy(segment["shots"][0])]
    segment["shots"][0].update(end=10, narration=segment["narration"])
    outputs = stage_outputs(strategy_stage, original)
    valid = outputs["storyboard"]
    segment = valid["segments"][0]
    first = segment["shots"][0]
    second = deepcopy(first)
    first.update(end=2.6, description="蓝瓶在桌面的全景")
    second.update(id=first["id"] + "_detail", start=2.6, description="桌上的蓝瓶颜色近景")
    segment["shots"] = [first, second]
    span = segment["speech_spans"][0]
    span["shot_ids"] = [first["id"], second["id"]]
    assert span["start"] < 2.6 and span["end"] >= 6.8
    phrase = "选只蓝瓶放在桌上，"
    assert span["text"].startswith(phrase)
    span["anchors"] = [
        {"text": phrase, "start": span["start"], "end": 2.6,
         "shot_ids": [first["id"]], "visual_evidence": "桌上的蓝瓶全景"},
        {"text": span["text"][len(phrase):], "start": 2.6, "end": 6.8,
         "shot_ids": [second["id"]], "visual_evidence": "蓝瓶的颜色近景"},
    ]
    Plan.model_validate(valid)
    outputs["storyboard_repair"] = deepcopy(valid)
    outputs["alignment_review"] = alignment_review(valid)
    bad = deepcopy(valid)
    bad["segments"][0]["shots"][0]["end"] = 4.5
    bad["segments"][0]["shots"][1]["start"] = 4.5
    outputs["storyboard"] = bad
    return outputs


def _run(settings, source, on_progress=None):
    return asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], [], on_progress=on_progress))


def _install(install_transport, outputs, calls, contexts=None):
    def handle(request):
        payload, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        assert PRIVATE_REASONING not in json.dumps(payload)
        if contexts is not None:
            contexts[stage] = deepcopy(context)
        return response(outputs[stage])
    install_transport(handle)


def test_phrase_before_its_shot_gets_one_timing_repair_and_final_review(
        settings, source, repair_outputs, install_transport):
    calls, phases, contexts = [], [], {}
    original_bad = deepcopy(repair_outputs["storyboard"])
    _install(install_transport, repair_outputs, calls, contexts)
    result = _run(settings, source, phases.append)
    assert calls == NORMAL_STAGES + ["storyboard_repair", "alignment_review"]
    assert phases.count("repairing_storyboard") == 1
    assert phases.index("repairing_storyboard") < phases.index("checking_alignment")
    repair = contexts["storyboard_repair"]
    assert repair["draft_storyboard"] == original_bad
    assert repair["approved_ad_script"] == repair_outputs["script_review"]["script"]
    assert repair["expression_schedule"] == contexts["storyboard"]["expression_schedule"]
    assert repair["validation_issues"] == [{"field": "segments.0", "code": "speech_coverage",
        "message": "旁白及锚点时段须由对应镜头覆盖"}]
    # The repaired camera cut supports the unchanged phrase from its first frame.
    segment = result["segments"][0]
    assert segment["shots"][1]["start"] == 2.6
    assert segment["speech_spans"] == repair_outputs["storyboard_repair"]["segments"][0]["speech_spans"]
    assert result["ad_script"] == repair_outputs["script_review"]["script"]
    assert PRIVATE_REASONING not in json.dumps(result)
    assert repair_outputs["storyboard"] == original_bad


def test_still_invalid_repair_stops_after_one_attempt(settings, source, repair_outputs, install_transport):
    repair_outputs["storyboard_repair"] = deepcopy(repair_outputs["storyboard"])
    calls = []
    _install(install_transport, repair_outputs, calls)
    with pytest.raises(ProviderError) as error:
        _run(settings, source)
    assert_redacted(error.value)
    assert PRIVATE_REASONING not in "".join(traceback.format_exception(error.value))
    assert calls == NORMAL_STAGES + ["storyboard_repair"]


def test_cancel_before_repair_does_not_submit_another_request(settings, source, repair_outputs, install_transport):
    calls = []
    _install(install_transport, repair_outputs, calls)
    def progress(phase):
        if phase == "repairing_storyboard":
            raise StaleRevision("已停止当前规划")
    with pytest.raises(StaleRevision):
        _run(settings, source, progress)
    assert calls == NORMAL_STAGES


@pytest.mark.parametrize("change", ["copy", "speech_window", "asset", "duration"])
def test_repair_cannot_change_approved_copy_or_fixed_contract(
        settings, source, repair_outputs, install_transport, change):
    repaired = repair_outputs["storyboard_repair"]
    segment = repaired["segments"][0]
    span = segment["speech_spans"][0]
    if change == "copy":
        span["text"] = span["text"].replace("颜色", "色彩")
        span["anchors"][-1]["text"] = span["anchors"][-1]["text"].replace("颜色", "色彩")
    elif change == "speech_window":
        span["end"] += 0.1
    elif change == "asset":
        segment["reference_asset_ids"] = ["invented-product"]
    else:
        segment["duration_seconds"] = 9
        segment["shots"][-1]["end"] = 9
    calls = []
    _install(install_transport, repair_outputs, calls)
    with pytest.raises(ProviderError) as error:
        _run(settings, source)
    assert_redacted(error.value)
    assert calls == NORMAL_STAGES + ["storyboard_repair"]


def test_repaired_timing_does_not_bypass_independent_semantic_review(
        settings, source, repair_outputs, install_transport):
    verdict = repair_outputs["alignment_review"]
    verdict.update(ready=False, ad_expression_passed=False)
    verdict["spans"][0].update(passed=False, note="画面尚不能支持对应商品表达")
    calls = []
    _install(install_transport, repair_outputs, calls)
    with pytest.raises(ProviderError, match="对应检查"):
        _run(settings, source)
    assert calls == NORMAL_STAGES + ["storyboard_repair", "alignment_review"]


@pytest.mark.parametrize("failed_stage", ["storyboard", "storyboard_repair"])
@pytest.mark.parametrize("failure", ["timeout", "bad_json"])
def test_transport_retries_current_stage_only_while_invalid_json_stops(
        settings, source, repair_outputs, install_transport, failed_stage, failure):
    calls = []
    def handle(request):
        _, context = request_context(request)
        stage = context["stage"]
        calls.append(stage)
        if stage == failed_stage:
            if failure == "timeout":
                raise httpx.ReadTimeout(PRIVATE_REASONING, request=request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": "not JSON " + PRIVATE_REASONING, "reasoning_content": PRIVATE_REASONING}}]})
        return response(repair_outputs[stage])
    install_transport(handle)
    with pytest.raises(ProviderError) as error:
        _run(settings, source)
    assert_redacted(error.value)
    assert PRIVATE_REASONING not in "".join(traceback.format_exception(error.value))
    expected = NORMAL_STAGES + (["storyboard_repair"] if failed_stage == "storyboard_repair" else [])
    assert calls == expected + ([failed_stage] * 2 if failure == "timeout" else [])


@pytest.mark.parametrize("invalid", ["title", "fact", "speech", "asset", "duration", "mixed"])
def test_non_timing_failures_never_get_a_paid_repair(
        settings, source, repair_outputs, install_transport, invalid):
    bad = deepcopy(repair_outputs["storyboard_repair"])
    if invalid == "mixed":
        bad = deepcopy(repair_outputs["storyboard"])
        bad["title"] = ""
    elif invalid == "title":
        bad["title"] = ""
    elif invalid == "fact":
        bad["segments"][0]["shots"][0]["evidence_ids"] = ["unknown-fact"]
    elif invalid == "speech":
        span = bad["segments"][0]["speech_spans"][0]
        span["text"] = span["text"].replace("颜色", "色彩")
        span["anchors"][-1]["text"] = span["anchors"][-1]["text"].replace("颜色", "色彩")
    elif invalid == "asset":
        bad["segments"][0]["reference_asset_ids"] = ["invented-product"]
    elif invalid == "duration":
        bad["segments"][0]["duration_seconds"] = 9
    repair_outputs["storyboard"] = bad
    calls = []
    _install(install_transport, repair_outputs, calls)
    with pytest.raises(ProviderError):
        _run(settings, source)
    assert calls == NORMAL_STAGES


def test_repair_hint_excludes_model_input_and_unknown_errors(repair_outputs):
    bad = deepcopy(repair_outputs["storyboard"])
    bad["segments"][0]["shots"][0]["description"] = PRIVATE_REASONING
    with pytest.raises(ValidationError) as error:
        Plan.model_validate(bad)
    hints = repairable_timing_issues(error.value)
    assert hints and PRIVATE_REASONING not in json.dumps(hints)
    bad["title"] = ""
    with pytest.raises(ValidationError) as mixed_error:
        Plan.model_validate(bad)
    assert repairable_timing_issues(mixed_error.value) == []
