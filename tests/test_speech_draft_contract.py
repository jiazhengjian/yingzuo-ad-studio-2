"""A whole-copy draft keeps source identity without inventing timed beats."""
from copy import deepcopy

import pytest

from app.speech_draft import speech_draft_context, validate_speech_draft
from app.strategy import StrategyRecord
from app.runtime_skills import stage_skill_context
from test_progressive_planning import chosen_strategy
from test_providers import source


def product_strategy(source):
    value = chosen_strategy(source).model_dump()
    value["brief"]["ad_form"] = "brand_tvc"
    selected = next(d for d in value["directions"] if d["id"] == value["selected_direction_id"])
    selected["film_concept"] = {"structure": "product_visual", "key_visual": "蓝色瓶身从局部逐渐显现为整只商品。",
        "memory_anchor": "蓝色瓶身的完整轮廓。", "sound_role": "留白后以画外音回应桌面布置。"}
    return StrategyRecord.model_validate(value)


def test_copy_brief_preserves_sources_and_complete_director_handoff(source):
    strategy = product_strategy(source)
    original = strategy.model_dump()
    conversation = [{"role": "user", "content": "保留台词“桌角添一点蓝。”，不要把它改成别的。"}]
    before = deepcopy(conversation)
    locked = ["桌角添一点蓝。"]
    context = speech_draft_context(strategy, 20, "蓝瓶广告", "继续", conversation, locked)
    assert strategy.model_dump() == original and conversation == before
    assert context["source_context"]["strategy"] == original
    assert context["source_context"]["fixed_speech"] == locked
    for fact in strategy.brief.facts:
        assert fact.text in context["task"] and fact.id in context["task"]
    assert conversation[0]["content"] in context["task"]
    assert locked[0] in context["task"]
    selected = next(d for d in strategy.directions if d.id == strategy.selected_direction_id)
    assert selected.film_concept.sound_role in context["task"]
    assert set(context["schema"]["properties"]) == {"narration", "hook_options", "closing_options"}
    assert context["schema"]["required"] == ["narration"]
    assert context["schema"]["properties"]["narration"]["maxLength"] == 60
    assert context["schema"]["properties"]["hook_options"]["maxItems"] == 2
    assert "beats" not in context and "draft_capacity" not in context
    context["source_context"]["conversation"][0]["content"] = "mutated output"
    assert conversation == before


@pytest.mark.parametrize("raw", [{}, {"narration": None}, {"narration": ""},
    {"narration": "  "}, {"narration": 12}, {"narration": "字" * 501},
    {"narration": "桌角添一点蓝。", "beats": []}])
def test_invalid_speech_cannot_become_a_plan(raw):
    with pytest.raises(ValueError):
        validate_speech_draft(raw)


def test_draft_validation_never_splits_or_fills_authored_copy():
    raw = {"narration": "桌角添一点蓝。\n让花有个位置。"}
    assert validate_speech_draft(raw).model_dump() == raw


@pytest.mark.parametrize("duration", [True, 9, 61, 20.5])
def test_draft_cannot_invent_out_of_contract_duration(source, duration):
    with pytest.raises(ValueError):
        speech_draft_context(product_strategy(source), duration, "", "", [], [])


def test_other_speech_modes_require_existing_authoring_path(source):
    strategy = product_strategy(source).model_dump()
    selected = next(d for d in strategy["directions"] if d["id"] == strategy["selected_direction_id"])
    selected["production"].update(speech_mode="none", voice_description="")
    with pytest.raises(ValueError):
        speech_draft_context(strategy, 20, "", "", [], [])


def test_copy_skill_is_not_a_registered_stage():
    with pytest.raises(ValueError, match="尚未绑定"):
        stage_skill_context("speech_draft")
    for stage in ("strategy", "ad_script", "storyboard", "plan_review"):
        assert "ads-video-voiceover-draft" not in {s["id"] for s in stage_skill_context(stage)["skills"]}
