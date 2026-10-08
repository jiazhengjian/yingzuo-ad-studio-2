"""New wire records server version metadata without weakening content guards."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.ad_script import AD_EXPRESSION_VERSION, derive_script_evidence, validate_script
from app.providers.gateway import ProviderError, reviewable_draft_enum_issues
from app import runtime_skills
from app.runtime_skills import skill_trace, shared_reference_trace
from ad_script_fixtures import with_action_timing
from test_ad_script import script, strategy
from test_draft_enum_review import enum_gateway
from test_script_capacity_revision import capacity_gateway
from test_providers import settings, source, strategy_stage, install_transport, assert_skill_wire_matches_source


def replace_echo(raw, echo):
    if echo == "missing":
        raw.pop("skill_version", None)
    else:
        raw["skill_version"] = deepcopy(echo)


@pytest.mark.parametrize("echo", ["missing", "ad_expression_1.3.0", None, {"version": "model-owned"}])
def test_explicit_server_version_is_recorded_without_mutating_original_content(script, strategy, echo):
    replace_echo(script, echo)
    before, facts_before = deepcopy(script), strategy.model_dump()
    derived = derive_script_evidence(script, strategy, server_skill_version=AD_EXPRESSION_VERSION)
    assert validate_script(derived, strategy, 20).skill_version == AD_EXPRESSION_VERSION
    assert {key: value for key, value in derived.items() if key != "skill_version"} == {
        key: value for key, value in before.items() if key != "skill_version"}
    assert script == before and strategy.model_dump() == facts_before


def test_default_derivation_preserves_historical_version_and_current_guard(script, strategy):
    script["skill_version"] = "historical-version"
    derived = derive_script_evidence(script, strategy)
    assert derived["skill_version"] == "historical-version"
    with pytest.raises(ValueError, match="当前广告表达规则版本"):
        validate_script(derived, strategy, 20)
    assert validate_script(derived, strategy, 20, require_current_version=False).skill_version == "historical-version"
    script.pop("skill_version")
    derived = derive_script_evidence(script, strategy)
    assert "skill_version" not in derived
    with pytest.raises(ValidationError):
        validate_script(derived, strategy, 20, require_current_version=False)


def test_injected_version_is_still_checked_instead_of_bypassing_the_guard(script, strategy):
    derived = derive_script_evidence(script, strategy, server_skill_version="server-config-mismatch")
    assert derived["skill_version"] == "server-config-mismatch"
    with pytest.raises(ValueError, match="当前广告表达规则版本"):
        validate_script(derived, strategy, 20, final=False)


@pytest.mark.parametrize("references", [["invented"], ["layers", "layers"]])
def test_version_recording_cannot_hide_invalid_beat_facts(script, strategy, references):
    script["beats"][0]["evidence_ids"] = references
    script.pop("skill_version")
    with pytest.raises(ValueError):
        derive_script_evidence(script, strategy, server_skill_version=AD_EXPRESSION_VERSION)


def test_enum_check_copy_uses_explicit_server_version_without_rewriting_the_draft(script, strategy):
    raw = with_action_timing(script)
    raw["skill_version"] = "ad_expression_1.3.0"
    raw["beats"][0]["boundary_before"] = "ellipsis"
    before = deepcopy(raw)
    issues = reviewable_draft_enum_issues(raw, strategy, 20, "continuity_v1",
                                        server_skill_version=AD_EXPRESSION_VERSION)
    assert [issue["loc"] for issue in issues] == [["beats", 0, "boundary_before"]]
    assert raw == before


@pytest.mark.parametrize("enum_field", [None, "boundary_before", "time_transition"])
@pytest.mark.parametrize("echo", ["missing", "ad_expression_1.3.0"])
def test_new_flow_draft_and_review_use_runtime_version_without_more_requests(enum_gateway, enum_field, echo, monkeypatch):
    service_contexts = {}
    load = runtime_skills.stage_skill_context

    def capture(stage, mode=None, **kwargs):
        loaded = load(stage, mode, **kwargs)
        if stage in service_contexts:
            assert service_contexts[stage] == loaded
        service_contexts[stage] = deepcopy(loaded)
        return loaded

    monkeypatch.setattr(runtime_skills, "stage_skill_context", capture)
    enum_gateway.enum_field = enum_field
    enum_gateway.initial_mutation = lambda raw: replace_echo(raw, echo)
    enum_gateway.review_mutation = lambda raw: replace_echo(raw, echo)
    result = enum_gateway.run()
    assert enum_gateway.calls == ["ad_script", "script_review", "storyboard", "alignment_review"]
    assert result["ad_script"]["skill_version"] == result["script_review"]["script"]["skill_version"] == AD_EXPRESSION_VERSION
    review_input = enum_gateway.contexts[1]
    assert "skill_version" not in review_input["draft_script"]
    assert review_input["runtime_skill"]["version"] == AD_EXPRESSION_VERSION
    ordered_contexts = [service_contexts[sent["stage_skills"]["stage"]] for sent in enum_gateway.contexts]
    for sent, loaded in zip(enum_gateway.contexts, ordered_contexts, strict=True):
        assert_skill_wire_matches_source(sent["stage_skills"], loaded)
    from app.planner_recovery import SKILL_TRACE_LIMIT, bound_server_list
    expected_trace = bound_server_list(
        [trace for context in ordered_contexts
         for trace in [*skill_trace(context), *shared_reference_trace(context)]],
        SKILL_TRACE_LIMIT)
    assert result["skill_trace"] == expected_trace
    assert all(trace["version"] and trace["content_sha256"] for trace in result["skill_trace"])
    assert all(trace["source_content_sha256"] for trace in result["skill_trace"]
               if trace["execution_kind"] != "shared_reference")
    if echo == "missing":
        assert "skill_version" not in enum_gateway.raw
    else:
        assert enum_gateway.raw["skill_version"] == echo


@pytest.mark.parametrize("echo", ["missing", "ad_expression_1.3.0"])
def test_legacy_gateway_still_rejects_missing_or_wrong_model_version(enum_gateway, echo):
    enum_gateway.new_flow = False
    enum_gateway.enum_field = None
    enum_gateway.initial_mutation = lambda raw: replace_echo(raw, echo)
    with pytest.raises(ProviderError):
        enum_gateway.run()
    assert enum_gateway.calls == ["strategy", "ad_script"]


def test_version_echo_does_not_hide_capacity_failure_or_rewrite_raw_diagnostic(capacity_gateway):
    capacity_gateway.first_review_mutation = lambda raw: replace_echo(raw, "ad_expression_1.3.0")
    capacity_gateway.second_review_mutation = lambda raw: replace_echo(raw, "another-model-echo")
    result = capacity_gateway.run()
    assert capacity_gateway.calls == ["ad_script", "script_review", "script_review", "storyboard", "alignment_review"]
    assert result["ad_script"]["skill_version"] == AD_EXPRESSION_VERSION
    records = capacity_gateway.records()
    assert len(records) == 1 and records[0]["raw_output"] == capacity_gateway.first_review
    assert records[0]["raw_output"]["script"]["skill_version"] == "ad_expression_1.3.0"
    for context in capacity_gateway.contexts:
        if context["stage"] == "script_review":
            assert "skill_version" not in context["draft_script"]
