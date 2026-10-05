"""The opt-in voiceover experiment preserves the three-stage production guards."""
import asyncio
from copy import deepcopy

import pytest

from app.config import Settings
from app.planner_runtime import planner_model_context, planner_wire_context
from app.planning_session import fingerprint
from app.providers.gateway import Gateway, ProviderError
from app.schemas import Plan
from app.speech_draft import SPEECH_DRAFT_VERSION
from app.strategy import StrategyRecord
from ad_script_fixtures import stage_outputs
from test_content_draft import lean_response
from test_planner_transport import isolated_environment
from test_progressive_planning import (
    forbid_real_network, install_transport, progressive_gateway, reject_first_shot,
    settings, source,
)
from test_providers import chat_output, plan, request_context, strategy_stage


@pytest.fixture(autouse=True)
def no_media(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Speech draft routing must not generate or submit media")
    for name in ("create_designs", "submit_video", "poll_video", "download_video",
                 "synthesize_voice", "_submit", "_uploaded_reference_url"):
        monkeypatch.setattr(Gateway, name, denied)


@pytest.fixture
def speech_gateway(progressive_gateway, settings):
    state = progressive_gateway
    settings.speech_draft_enabled = True
    raw = state.strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    selected = next(d for d in raw["directions"] if d["id"] == raw["selected_direction_id"])
    selected["film_concept"]["structure"] = "product_visual"
    state.strategy = StrategyRecord.model_validate(raw)

    def draft(value):
        if state.contexts[-1].get("draft_format") == "speech_only":
            value.clear()
            value["narration"] = "给忙碌的桌角，留一点自己的喜欢。"
        elif state.contexts[-1].get("draft_format") == "content_only":
            lean_response(value)
    state.draft_mutation = draft
    return state


def test_speech_draft_replaces_first_call_without_inventing_beats(speech_gateway):
    state = speech_gateway
    before = state.strategy.model_dump()
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    content, director, review = state.contexts
    assert content["draft_format"] == director["draft_format"] == "speech_only"
    assert content["speech_draft_version"] == director["speech_draft_version"] == SPEECH_DRAFT_VERSION
    assert set(content["schema"]["properties"]) == {"narration", "hook_options", "closing_options"}
    assert content["schema"]["required"] == ["narration"]
    assert content["stage_skills"]["stage"] == "speech_draft"
    assert "source_context" not in content
    assert director["draft_speech"] == {"narration": "给忙碌的桌角，留一点自己的喜欢。"}
    assert not {"draft_script", "draft_capacity", "draft_capacity_basis"}.intersection(director)
    assert director["brief"]["facts"] == before["brief"]["facts"]
    selected = next(d for d in before["directions"] if d["id"] == before["selected_direction_id"])
    for field in ("production", "film_concept", "story_summary", "product_expression", "evidence_ids"):
        assert director["selected_direction"][field] == selected[field]
    assert plan["strategy"] == before == state.strategy.model_dump()
    assert Plan.model_validate(plan).plan_review["ready"]
    assert "draft_speech" not in review
    assert any(trace["stage"] == "speech_draft" for trace in plan["skill_trace"])


@pytest.mark.parametrize("excluded", ["off", "content", "joint", "scene", "presenter",
    "auto", "missing_form", "dialogue", "silent", "lifestyle", "missing_concept"])
def test_noneligible_paths_keep_existing_contracts(speech_gateway, settings, excluded):
    state = speech_gateway
    raw = state.strategy.model_dump()
    selected = next(d for d in raw["directions"] if d["id"] == raw["selected_direction_id"])
    if excluded == "off":
        settings.speech_draft_enabled = False
    elif excluded == "content":
        settings.content_draft_enabled = True
    elif excluded == "joint":
        settings.joint_planning_enabled = True
    elif excluded in ("scene", "presenter", "auto", "missing_form"):
        raw["brief"]["ad_form"] = {"scene": "scene_story", "presenter": "presenter_pitch",
            "auto": "auto", "missing_form": None}[excluded]
    elif excluded in ("dialogue", "silent"):
        selected["production"].update(speech_mode="character_dialogue" if excluded == "dialogue" else "none",
            speaker_id="char_worker" if excluded == "dialogue" else "",
            voice_description="自然介绍。" if excluded == "dialogue" else "")
    elif excluded == "lifestyle":
        selected["film_concept"]["structure"] = "lifestyle"
    else:
        selected.pop("film_concept")
    state.strategy = StrategyRecord.model_validate(raw)
    state.run()
    assert all(context.get("draft_format") != "speech_only" for context in state.contexts)
    assert not any("draft_speech" in context for context in state.contexts)
    expected = ["storyboard", "plan_review"] if excluded == "joint" else ["ad_script", "storyboard", "plan_review"]
    assert state.calls == expected
    if excluded == "content":
        assert state.contexts[0]["draft_format"] == "content_only"


@pytest.mark.parametrize("bad", [{}, {"narration": " "}, {"narration": 4}, {"narration": "字" * 501}])
def test_invalid_speech_output_stops_before_director(speech_gateway, bad):
    state = speech_gateway
    def invalid(value):
        value.clear()
        value.update(deepcopy(bad))
    state.draft_mutation = invalid
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script"]


def test_extra_speech_field_is_dropped_before_the_director(speech_gateway):
    state = speech_gateway
    def invalid(value):
        value.clear()
        value.update({"narration": "有效话语。", "beats": []})
    state.draft_mutation = invalid
    state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert state.contexts[1]["draft_speech"] == {"narration": "有效话语。"}


def test_fixed_speech_remains_guarded_after_new_handoff(speech_gateway):
    state = speech_gateway
    line = "这一句必须原样保留。"
    state.brief = f"固定台词“{line}”，继续安排。"
    state.draft_mutation = lambda value: (value.clear(), value.update(narration=line))
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script", "storyboard"]
    assert line in state.contexts[0]["task"]
    assert state.contexts[1]["fixed_speech"] == [line]


def test_new_draft_does_not_turn_unlocked_copy_into_merchant_lock(speech_gateway):
    state = speech_gateway
    plan = state.run()
    assert state.contexts[1]["fixed_speech"] == []
    assert "".join(beat["narration"] for beat in plan["ad_script"]["beats"]) == "看轮廓。看桌角。留一处。"


def test_speech_route_does_not_allow_director_to_drop_selected_evidence(speech_gateway):
    state = speech_gateway
    def drop(value):
        for beat in value["ad_script"]["beats"]:
            beat["evidence_ids"] = []
        for shot in value["shots"]:
            shot["evidence_ids"] = []
    state.first_director_mutation = drop
    plan = state.run()
    used = {fact_id for beat in plan["ad_script"]["beats"] for fact_id in beat["evidence_ids"]}
    selected = next(item for item in state.strategy.directions if item.id == state.strategy.selected_direction_id)
    assert set(selected.evidence_ids).issubset(used)
    assert plan["segments"]
    assert state.calls[0:2] == ["ad_script", "storyboard"]
    assert len(state.calls) <= 6


def test_nonprogressive_initial_authoring_keeps_timed_contract(speech_gateway, settings, monkeypatch):
    state = speech_gateway
    settings.progressive_planning_enabled = False
    observed = []
    class Captured(BaseException):
        pass
    async def capture(self, system, context, originals, **kwargs):
        observed.append(deepcopy(context))
        raise Captured()
    monkeypatch.setattr(Gateway, "_planner_json", capture)
    with pytest.raises(Captured):
        state.run()
    assert len(observed) == 1
    assert observed[0]["stage"] == "ad_script"
    assert "draft_format" not in observed[0]
    assert "beats" in observed[0]["schema"]["properties"]


def test_nonchoice_legacy_ignores_experimental_flag(settings, source, plan, strategy_stage, install_transport):
    settings.speech_draft_enabled = True
    outputs = stage_outputs(strategy_stage, plan)
    observed = []
    def handle(request):
        _, context = request_context(request)
        observed.append(context)
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    delivered = asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    content = next(context for context in observed if context["stage"] == "ad_script")
    assert "draft_format" not in content
    assert "stage_skills" not in content
    assert "beats" in content["schema"]["properties"]
    assert delivered["script"] == plan["script"]


def test_refinement_uses_current_storyboard_without_stale_speech(speech_gateway):
    state = speech_gateway
    state.first_review_mutation = reject_first_shot
    state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    refinement = state.contexts[3]
    assert "draft_storyboard" in refinement
    assert "draft_speech" not in refinement
    assert "draft_script" not in refinement
    assert "fixed_speech" in refinement


def test_source_projection_preserves_signed_inputs_and_other_stages():
    source = {"strategy": {"selected_direction": "original"}, "fixed_speech": ["必须保留。"]}
    context = {"stage": "ad_script", "draft_format": "speech_only",
        "speech_draft_version": SPEECH_DRAFT_VERSION, "source_context": source, "task": "创作输入"}
    before = deepcopy(context)
    assert planner_wire_context(context)["source_context"] == source
    assert "source_context" not in planner_model_context(context)
    changed = deepcopy(context)
    changed["source_context"]["strategy"]["selected_direction"] = "changed"
    assert fingerprint(planner_wire_context(context)) != fingerprint(planner_wire_context(changed))
    for stage in ("storyboard", "plan_review", "strategy"):
        assert planner_model_context({**context, "stage": stage})["source_context"] == source
    assert planner_model_context({**context, "draft_format": "content_only"})["source_context"] == source
    assert context == before


@pytest.mark.parametrize("change", ["version", "mode"])
def test_checkpoint_cannot_replay_other_speech_version_or_mode(speech_gateway, settings, source, monkeypatch, change):
    state = speech_gateway
    checkpoint = {}
    def save(value):
        checkpoint.clear()
        checkpoint.update(deepcopy(value))
    def run(saved=None):
        return asyncio.run(Gateway(settings).decide(
            {"brief": state.brief, "duration_seconds": 20}, [source], [], [], None,
            selected_strategy=state.strategy.model_dump(), planning_checkpoint=saved,
            on_checkpoint=save))
    first = run()
    frozen = deepcopy(checkpoint)
    assert run(checkpoint) == first
    assert len(state.calls) == 3
    if change == "version":
        monkeypatch.setattr("app.providers.gateway.SPEECH_DRAFT_VERSION", "new-version")
    else:
        settings.speech_draft_enabled = False
    result = run(checkpoint)
    assert result["plan"]["plan_review"]["ready"]
    assert len(state.calls) > 3
    assert checkpoint != frozen


@pytest.mark.parametrize("raw,enabled", [(None, False), (" true ", True), ("FALSE", False)])
def test_speech_configuration_is_explicit_and_default_off(isolated_environment, monkeypatch, raw, enabled):
    monkeypatch.delenv("SPEECH_DRAFT_ENABLED", raising=False)
    if raw is not None:
        monkeypatch.setenv("SPEECH_DRAFT_ENABLED", raw)
    assert Settings().speech_draft_enabled is False
    assert Settings.from_env().speech_draft_enabled is enabled


def test_invalid_speech_configuration_fails_without_provider(isolated_environment, monkeypatch):
    monkeypatch.setenv("SPEECH_DRAFT_ENABLED", "automatic")
    with pytest.raises(ValueError, match="SPEECH_DRAFT_ENABLED"):
        Settings.from_env()
