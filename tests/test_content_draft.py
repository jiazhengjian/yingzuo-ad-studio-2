"""Opt-in content drafting keeps final planning, evidence and replay guards."""
import asyncio
from copy import deepcopy

import pytest

from app.config import Settings
from app.content_draft import ContentDraft, content_draft_schema, validate_content_draft
from app.planning_session import PlanningSessionError
from app.providers.gateway import Gateway, ProviderError
from app.schemas import Plan
from test_planner_transport import isolated_environment
from test_progressive_planning import (
    chosen_strategy, director_output, forbid_real_network, install_transport,
    progressive_gateway, reject_first_shot, settings, source,
)


def lean_response(value):
    beats = [{key: deepcopy(beat[key]) for key in ("id", "narration", "visual_role", "evidence_ids")}
             for beat in value["beats"]]
    value.clear()
    value["beats"] = beats


@pytest.fixture
def content_gateway(progressive_gateway, settings):
    settings.content_draft_enabled = True
    progressive_gateway.draft_mutation = lean_response
    return progressive_gateway


@pytest.fixture(autouse=True)
def no_media(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Content draft tests must not submit, upload or generate media")
    for name in ("create_designs", "submit_video", "poll_video", "download_video",
                 "synthesize_voice", "_submit", "_uploaded_reference_url"):
        monkeypatch.setattr(Gateway, name, denied)


def test_content_draft_keeps_authored_data_without_invented_rationale_or_timing(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)["ad_script"]
    lean_response(raw)
    raw["beats"][1].update(narration="", evidence_ids=[])
    before = deepcopy(raw)
    checked = validate_content_draft(raw, strategy)
    assert checked.model_dump() == before == raw
    schema = content_draft_schema(strategy)
    assert set(schema["properties"]) == {"beats", "key_line_options"}
    assert schema["required"] == ["beats"]
    beat = schema["$defs"]["ContentBeat"]
    assert set(beat["required"]) == {"id", "narration", "visual_role", "evidence_ids"}
    assert beat["properties"]["narration"]["maxLength"] == 29
    assert set(beat["properties"]["evidence_ids"]["items"]["enum"]) == {f.id for f in strategy.brief.facts}


@pytest.mark.parametrize("defect", ["empty", "too_many", "duplicate_id", "missing_narration",
    "blank_narration", "oversized_narration", "wrong_narration_type", "unknown_fact", "duplicate_fact", "timing", "purpose"])
def test_malformed_content_cannot_gain_missing_fields_or_fabricated_facts(source, defect):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)["ad_script"]
    lean_response(raw)
    if defect == "empty":
        raw["beats"] = []
    elif defect == "too_many":
        raw["beats"] = [{**deepcopy(raw["beats"][0]), "id": f"b{i}"} for i in range(13)]
    elif defect == "duplicate_id":
        raw["beats"][1]["id"] = raw["beats"][0]["id"]
    elif defect == "missing_narration":
        raw["beats"][0].pop("narration")
    elif defect == "blank_narration":
        raw["beats"][0]["narration"] = "  "
    elif defect == "oversized_narration":
        raw["beats"][0]["narration"] = "字" * 501
    elif defect == "wrong_narration_type":
        raw["beats"][0]["narration"] = 123
    elif defect == "unknown_fact":
        raw["beats"][0]["evidence_ids"] = ["invented_performance"]
    elif defect == "duplicate_fact":
        raw["beats"][0]["evidence_ids"] *= 2
    else:
        raw["beats"][0][defect] = "不能混入旧输出合同"
    before = deepcopy(raw)
    with pytest.raises(ValueError):
        validate_content_draft(raw, strategy)
    assert raw == before


def test_enabled_content_draft_is_only_a_first_stage_before_complete_plan_review(content_gateway):
    state = content_gateway
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    content, director, review = state.contexts
    assert content["draft_format"] == director["draft_format"] == "content_only"
    assert set(content["schema"]["properties"]) == {"beats", "key_line_options"}
    assert content["schema"]["required"] == ["beats"]
    assert not {"runtime_skill", "creative_strategy", "ad_expression_skill", "generation_strategy"}.intersection(content)
    assert not {"draft_capacity", "draft_capacity_basis"}.intersection(director)
    assert ContentDraft.model_validate(director["draft_script"]).model_dump() == director["draft_script"]
    selected = next(d for d in state.strategy.directions if d.id == state.strategy.selected_direction_id)
    assert content["selected_direction"] == director["selected_direction"]
    for key in ("id", "rule_id", "evidence_ids", "production", "story_summary", "film_concept",
                "attraction_mechanism", "product_expression"):
        assert content["selected_direction"][key] == selected.model_dump()[key]
    assert content["brief"]["facts"] == director["brief"]["facts"] == [f.model_dump() for f in state.strategy.brief.facts]
    assert plan["strategy"] == state.strategy.model_dump()
    assert any(item["stage"] == "content_draft" for item in plan["skill_trace"])
    assert review["execution_windows"]
    assert Plan.model_validate(plan).plan_review["ready"] is True
    assert all(beat["timing"] and beat["purpose"] and beat["message"] for beat in plan["ad_script"]["beats"])


def test_invalid_content_fact_stops_before_directing(content_gateway):
    state = content_gateway
    def invent(value):
        lean_response(value)
        value["beats"][0]["evidence_ids"] = ["invented_fact"]
    state.draft_mutation = invent
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script"]


def test_bounded_long_draft_reaches_director_without_relaxing_final_or_wire_limits(content_gateway):
    state = content_gateway
    sentence = "这句超过初稿建议长度的完整原话需要交给分镜协调，初稿校验不应提前截断这句话。"
    assert 29 < len(sentence) <= 500
    def long_draft(value):
        lean_response(value)
        value["beats"][0]["narration"] = sentence
    state.draft_mutation = long_draft
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert state.contexts[0]["schema"]["$defs"]["ContentBeat"]["properties"]["narration"]["maxLength"] == 29
    assert state.contexts[1]["draft_script"]["beats"][0]["narration"] == sentence
    assert plan["ad_script"]["beats"][0]["narration"] == "看轮廓。"
    assert all(len(beat["narration"]) <= 29 for beat in plan["ad_script"]["beats"])
    assert Plan.model_validate(plan).plan_review["ready"]


@pytest.mark.parametrize("narration", ["字" * 501, 123])
def test_unbounded_or_wrong_type_draft_stops_before_director(content_gateway, narration):
    state = content_gateway
    def invalid(value):
        lean_response(value)
        value["beats"][0]["narration"] = narration
    state.draft_mutation = invalid
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script"]


def test_long_fixed_original_must_not_be_silently_truncated_by_director(content_gateway):
    state = content_gateway
    sentence = "这句超过初稿建议长度的完整原话必须保留，分镜不能为了符合字段上限而悄悄删改。"
    assert len(sentence) > 29
    state.brief = f"保留台词“{sentence}”，继续安排。"
    def locked_draft(value):
        lean_response(value)
        value["beats"][0]["narration"] = sentence
    state.draft_mutation = locked_draft
    # The mock director returns shorter, otherwise valid speech. It cannot
    # silently substitute that speech for the merchant's fixed original.
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script", "storyboard"]
    assert state.contexts[1]["draft_script"]["beats"][0]["narration"] == sentence
    assert state.contexts[0]["fixed_speech"] == state.contexts[1]["fixed_speech"] == [sentence]


@pytest.mark.parametrize("guard", ["fixed_speech", "silent_mode", "selected_evidence"])
def test_lean_draft_cannot_bypass_final_director_guards(content_gateway, source, guard):
    state = content_gateway
    if guard == "fixed_speech":
        state.brief = "保留台词“看轮廓。”，继续安排。"
        state.first_director_mutation = lambda data: data["ad_script"]["beats"][0].update(narration="换一句话。")
    elif guard == "silent_mode":
        state.strategy = chosen_strategy(source, "none")
        state.first_director_mutation = lambda data: data["ad_script"]["beats"][0].update(narration="多说一句。")
    else:
        def remove_evidence(data):
            for beat in data["ad_script"]["beats"]:
                beat["evidence_ids"] = []
            for shot in data["shots"]:
                shot["evidence_ids"] = []
        state.first_director_mutation = remove_evidence
    if guard == "selected_evidence":
        with pytest.raises(ProviderError, match="结构不完整"):
            state.run()
        assert state.calls[0:2] == ["ad_script", "storyboard"]
        assert "plan_review" not in state.calls
        return
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script", "storyboard"]
    if guard == "fixed_speech":
        assert state.contexts[0]["fixed_speech"] == state.contexts[1]["fixed_speech"] == ["看轮廓。"]


def test_lean_draft_still_revises_rejections_and_independently_reviews_again(content_gateway):
    state = content_gateway
    state.first_review_mutation = reject_first_shot
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    assert state.contexts[3]["review_feedback"]["ready"] is False
    assert state.contexts[3]["repair_scope"]["shot_ids"] == ["shot_0"]
    assert state.contexts[3]["draft_format"] == "content_only"
    assert plan["plan_review"]["ready"]


def test_default_disabled_preserves_prior_three_stage_contract(progressive_gateway, settings):
    assert Settings().content_draft_enabled is False
    settings.content_draft_enabled = False
    state = progressive_gateway
    state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert "draft_format" not in state.contexts[0]
    assert state.contexts[1]["draft_capacity"]
    assert state.contexts[1]["draft_script"]["beats"][0]["timing"]


@pytest.mark.parametrize("joint", [False, True])
def test_replay_uses_effective_content_route_and_joint_takes_precedence(content_gateway, settings, source, joint):
    state = content_gateway
    settings.joint_planning_enabled = joint
    checkpoint = None
    def save(value):
        nonlocal checkpoint
        checkpoint = deepcopy(value)
    def run(saved=None):
        return asyncio.run(Gateway(settings).decide(
            {"brief": state.brief, "duration_seconds": state.duration}, [source],
            state.conversation, [], state.current_plan, selected_strategy=state.strategy.model_dump(),
            planning_checkpoint=saved, on_checkpoint=save))
    first = run()
    frozen = deepcopy(checkpoint)
    expected_calls = ["storyboard", "plan_review"] if joint else ["ad_script", "storyboard", "plan_review"]
    assert state.calls == expected_calls
    assert run(checkpoint)["plan"] == first["plan"]
    assert state.calls == expected_calls
    settings.content_draft_enabled = False
    if joint:
        assert run(checkpoint)["plan"] == first["plan"]
        assert "draft_format" not in state.contexts[0]
    else:
        with pytest.raises(PlanningSessionError):
            run(checkpoint)
    assert state.calls == expected_calls
    assert checkpoint == frozen


@pytest.mark.parametrize("raw,enabled", [(None, False), (" true ", True), ("FALSE", False)])
def test_content_draft_configuration_is_explicit_and_default_off(isolated_environment, monkeypatch, raw, enabled):
    monkeypatch.delenv("CONTENT_DRAFT_ENABLED", raising=False)
    if raw is not None:
        monkeypatch.setenv("CONTENT_DRAFT_ENABLED", raw)
    assert Settings.from_env().content_draft_enabled is enabled


def test_invalid_content_configuration_fails_before_any_provider(isolated_environment, monkeypatch):
    monkeypatch.setenv("CONTENT_DRAFT_ENABLED", "automatic")
    with pytest.raises(ValueError, match="CONTENT_DRAFT_ENABLED"):
        Settings.from_env()
