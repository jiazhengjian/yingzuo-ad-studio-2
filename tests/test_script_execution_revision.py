"""An isolated action-capacity verdict consumes the existing single return."""
import pytest

from app.ad_script import AdBeat
from app.db import StaleRevision
from app.providers.gateway import ProviderError
from test_script_capacity_revision import capacity_gateway
from test_providers import settings, source, strategy_stage, install_transport


NOTE = "取放后还要等待人物取食，当前画面动作负担没有留够辨认时间。"


def fail_action_capacity(review):
    review["ready"] = False
    review["execution_check"] = {"action_capacity": False, "state_continuity": True, "note": NOTE}


@pytest.mark.parametrize("visual_seconds", [8, 12])
@pytest.mark.parametrize("enum_error", [False, True])
def test_execution_capacity_failure_reuses_review_with_original_note_and_true_budget(capacity_gateway, visual_seconds, enum_error):
    capacity_gateway.reviewed_visual_seconds = visual_seconds
    capacity_gateway.draft_enum_error = enum_error
    capacity_gateway.first_review_result_mutation = fail_action_capacity
    result = capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review", "storyboard", "alignment_review"]
    assert capacity_gateway.phases.count("refining_script") == 1
    repair = capacity_gateway.contexts[2]
    feedback = repair["review_feedback"]
    assert feedback["kind"] == "script_capacity"
    assert feedback["execution_check"] == capacity_gateway.first_review["execution_check"]
    assert feedback["execution_check"]["note"] == NOTE
    assert feedback["capacity"]["total_minimum_seconds"] == visual_seconds * 2
    assert feedback["capacity"]["overrun_seconds"] == max(0, visual_seconds * 2 - 20)
    assert "draft_validation_issues" not in repair and "draft_storyboard" not in repair
    assert [AdBeat.model_validate(beat) for beat in repair["draft_script"]["beats"]] == [
        AdBeat.model_validate(beat) for beat in capacity_gateway.first_review["script"]["beats"]]
    assert result["ad_script"] == result["script_review"]["script"]
    assert result["script_review"]["ready"] is True
    assert len(result["ad_script"]["beats"]) == 1
    assert result["strategy"] == capacity_gateway.strategy.model_dump()
    records = capacity_gateway.records()
    assert len(records) == 1 and records[0]["raw_output"] == capacity_gateway.first_review
    assert records[0]["raw_output"]["ready"] is False


@pytest.mark.parametrize("first_failure", ["execution", "numeric"])
def test_second_execution_capacity_failure_stops_without_a_third_review(capacity_gateway, first_failure):
    if first_failure == "execution": capacity_gateway.first_review_result_mutation = fail_action_capacity
    capacity_gateway.second_review_result_mutation = fail_action_capacity
    with pytest.raises(ProviderError, match="已重排一次脚本与时间，动作容量审核仍未通过"):
        capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review"]
    assert len(capacity_gateway.records()) == 2


def test_execution_failure_then_numeric_failure_share_one_return(capacity_gateway):
    capacity_gateway.first_review_result_mutation = fail_action_capacity
    capacity_gateway.repair = False
    with pytest.raises(ProviderError, match="已重排一次脚本与时间.*24秒.*20秒"):
        capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review"]


@pytest.mark.parametrize("failure", ["capacity", "cut"])
def test_execution_return_leaves_no_extra_final_av_revision(capacity_gateway, failure):
    capacity_gateway.first_review_result_mutation = fail_action_capacity
    capacity_gateway.av_failure = failure
    with pytest.raises(ProviderError, match="已重排一次脚本与时间"):
        capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review", "storyboard", "alignment_review"]
    assert "refining_storyboard" not in capacity_gateway.phases


@pytest.mark.parametrize("invalid", ["unknown_fact", "missing_selected", "missing_timing", "root_identity"])
def test_execution_failure_cannot_bypass_script_authority_guards(capacity_gateway, invalid):
    def mutate(script):
        if invalid == "unknown_fact": script["beats"][0]["evidence_ids"] = ["invented"]
        elif invalid == "missing_selected":
            for beat in script["beats"]: beat["evidence_ids"] = []
        elif invalid == "missing_timing": script["beats"][0].pop("timing")
        else: script["production"] = {"speech_mode": "none"}
    capacity_gateway.first_review_mutation = mutate
    capacity_gateway.first_review_result_mutation = fail_action_capacity
    with pytest.raises(ProviderError): capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review"]


@pytest.mark.parametrize("invalid", ["state_failed", "content_failed", "fake_bool", "inconsistent_ready", "missing_execution"])
def test_only_structurally_valid_isolated_capacity_failure_can_return(capacity_gateway, invalid):
    def mutate(review):
        fail_action_capacity(review)
        if invalid == "state_failed": review["execution_check"]["state_continuity"] = False
        elif invalid == "content_failed": review["checks"][0]["passed"] = False
        elif invalid == "fake_bool": review["execution_check"]["action_capacity"] = "false"
        elif invalid == "inconsistent_ready": review["ready"] = True
        else: review.pop("execution_check")
    capacity_gateway.first_review_result_mutation = mutate
    with pytest.raises(ProviderError): capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review"]


def test_legacy_execution_capacity_failure_retains_immediate_stop(capacity_gateway):
    capacity_gateway.new_flow = False
    capacity_gateway.first_review_result_mutation = fail_action_capacity
    with pytest.raises(ProviderError, match="脚本的动作容量仍不合理"):
        capacity_gateway.run()
    assert capacity_gateway.calls == ["strategy", "ad_script", "script_review"]


def test_cancellation_is_checked_before_execution_capacity_return(capacity_gateway):
    capacity_gateway.first_review_result_mutation = fail_action_capacity
    def progress(phase):
        if phase == "refining_script": raise StaleRevision("TEST canceled")
    with pytest.raises(StaleRevision): capacity_gateway.run(progress)
    assert capacity_gateway.calls == ["ad_script", "script_review"]
