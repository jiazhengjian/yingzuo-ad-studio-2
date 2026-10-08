"""Only progressive initial copy may defer its line-length error to the director."""
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from app.ad_script import AdBeat
from app.providers.gateway import ProviderError
from test_draft_enum_review import enum_gateway
from test_progressive_planning import progressive_gateway, reject_first_shot
from test_providers import settings, source, strategy_stage, install_transport


LONG_LINE = "给办公桌留一处喜欢的风景，这只瓶子的轮廓让忙碌的时刻也多一点从容。"
SHORT_LINE = "给桌面留一处喜欢的风景。"


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Draft narration tests must not contact a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


def test_original_long_draft_reaches_existing_director_then_full_independent_review(progressive_gateway):
    state = progressive_gateway
    originals = []
    def draft(value):
        value["beats"][0]["narration"] = LONG_LINE
        originals.append(deepcopy(value))
    state.draft_mutation = draft
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(narration=SHORT_LINE)
    plan = state.run()
    assert len(LONG_LINE) > 29
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    director = state.contexts[1]
    assert director["draft_script"] == originals[0]
    assert director["draft_validation_issues"] == [{"loc": ["beats", 0, "narration"],
        "type": "string_too_long", "max_length": 29, "actual_length": len(LONG_LINE)}]
    assert "校验占位" not in json.dumps([state.contexts, plan], ensure_ascii=False)
    assert plan["ad_script"]["beats"][0]["narration"] == SHORT_LINE
    assert state.contexts[-1]["final_plan"]["ad_script"]["beats"][0]["narration"] == SHORT_LINE
    assert plan["plan_review"]["ready"] is True
    # The stored beat ceiling is wider than this untyped brief. The profile cap
    # still rejects the long line; only the progressive draft may defer it.
    assert len(LONG_LINE) <= AdBeat.model_json_schema()["properties"]["narration"]["maxLength"]
    with pytest.raises(ValidationError):
        AdBeat.model_validate({**originals[0]["beats"][0], "narration": "字" * 81})


@pytest.mark.parametrize("stage", ["storyboard", "plan_refinement"])
def test_long_narration_in_director_or_refinement_cannot_reach_delivery(progressive_gateway, stage):
    state = progressive_gateway
    state.draft_mutation = lambda value: value["beats"][0].update(narration=LONG_LINE)
    if stage == "storyboard":
        state.first_director_mutation = lambda value: value["ad_script"]["beats"][0].update(narration=LONG_LINE)
    else:
        state.first_review_mutation = reject_first_shot
        state.refinement_mutation = lambda value: value["ad_script"]["beats"][0].update(narration=LONG_LINE)
    if stage == "storyboard":
        with pytest.raises(ProviderError, match="这次没生成出来"):
            state.run()
        assert state.calls[0] == "ad_script" and "storyboard" in state.calls
        assert "plan_review" not in state.calls
        return
    plan = state.run()
    assert LONG_LINE not in json.dumps(plan, ensure_ascii=False)
    assert plan["segments"]
    assert state.calls[0] == "ad_script" and "storyboard" in state.calls
    assert len(state.calls) <= 8


@pytest.mark.parametrize("invalid", ["unknown_fact", "duplicate_fact", "duplicate_beat", "missing_timing",
                                      "root_extra", "beat_extra", "other_long_field"])
def test_long_draft_cannot_hide_other_shape_or_fact_errors(progressive_gateway, invalid):
    state = progressive_gateway
    def mutate(value):
        value["beats"][0]["narration"] = LONG_LINE
        if invalid == "unknown_fact":
            value["beats"][0]["evidence_ids"] = ["invented"]
        elif invalid == "duplicate_fact":
            value["beats"][0]["evidence_ids"] = ["appearance", "appearance"]
        elif invalid == "duplicate_beat":
            value["beats"][1]["id"] = value["beats"][0]["id"]
        elif invalid == "missing_timing":
            value["beats"][1].pop("timing")
        elif invalid == "root_extra":
            value["inferred_fact"] = "unsupported"
        elif invalid == "beat_extra":
            value["beats"][1]["inferred_fact"] = "unsupported"
        else:
            value["beats"][1]["purpose"] = "字" * 1201
    state.draft_mutation = mutate
    if invalid in ("duplicate_fact", "beat_extra", "other_long_field"):
        plan = state.run()
        dumped = json.dumps(plan, ensure_ascii=False)
        assert "unsupported" not in dumped and "字" * 120 not in dumped and LONG_LINE not in dumped
        assert plan["segments"] and state.calls[0] == "ad_script" and "storyboard" in state.calls
        assert len(state.calls) <= 6
        return
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script"]


def test_long_draft_can_report_both_known_enum_and_length_errors(progressive_gateway):
    state = progressive_gateway
    def mutate(value):
        value["beats"][0].update(narration=LONG_LINE, boundary_before="ellipsis")
    state.draft_mutation = mutate
    state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    issues = state.contexts[1]["draft_validation_issues"]
    assert {issue["type"] for issue in issues} == {"string_too_long", "literal_error"}
    assert state.contexts[1]["draft_script"]["beats"][0]["narration"] == LONG_LINE
    assert state.contexts[1]["draft_script"]["beats"][0]["boundary_before"] == "ellipsis"


@pytest.mark.parametrize("new_flow", [False, True])
def test_nonprogressive_flows_keep_rejecting_long_initial_lines(enum_gateway, new_flow):
    state = enum_gateway
    state.new_flow = new_flow
    state.enum_field = None
    state.initial_mutation = lambda value: value["beats"][0].update(narration=LONG_LINE)
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == (["ad_script"] if new_flow else ["strategy", "ad_script"])
