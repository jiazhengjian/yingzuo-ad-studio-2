"""Fresh span visibility verdicts cannot turn character dialogue into VO."""
from copy import deepcopy

import pytest

from app.av_review import AVSpanCheck, review_speech_requirements, validate_av_review
from app.plan_review import lower_failed_plan_verdicts, validate_plan_review
from app.progressive_planning import compile_director
from app.providers.gateway import ProviderError
from app.schemas import Plan
from test_progressive_planning import (
    accepted_review, chosen_strategy, director_output, forbid_real_network,
    progressive_gateway,
)
from test_providers import settings, source, install_transport


def compiled_plan(source, mode="character_dialogue"):
    strategy = chosen_strategy(source, mode)
    draft = director_output(strategy)
    for shot in draft["shots"]:
        shot["description"] = "办公者脸部及同一瓶身可见，办公者自然开口介绍。"
    return compile_director(draft, strategy, 20, "continuity_v1", [source["id"]])[0]


@pytest.mark.parametrize("visibility", ["off_screen", "unclear", "not_applicable"])
def test_explicit_character_visibility_failure_lowers_ready_even_if_note_calls_it_voiceover(source, visibility):
    plan = compiled_plan(source)
    plan["segments"][0]["shots"][0]["description"] = "只拍同一瓶身特写，办公者在画外说话。"
    # The role ID is present, but it cannot prove the role is visible in frame.
    assert plan["segments"][0]["shots"][0]["character_ids"] == ["char_worker"]
    review = accepted_review(plan)
    review["alignment"]["spans"][0].update(speaker_visibility=visibility,
        note="画外音配合同一商品特写，台词内容相容。")
    before = deepcopy(review)
    lowered = lower_failed_plan_verdicts(review, plan)
    assert review == before
    assert lowered["ready"] is False and lowered["alignment"]["ready"] is False
    assert lowered["alignment"]["spans"][0]["passed"] is False
    assert lowered["alignment"]["spans"][1:] == before["alignment"]["spans"][1:]
    assert lower_failed_plan_verdicts(lowered, plan) == lowered
    assert validate_plan_review(lowered, plan).ready is False
    with pytest.raises(ValueError, match="说话人不可见"):
        validate_plan_review(review, plan)


@pytest.mark.parametrize("mode", ["character_dialogue", "voiceover"])
@pytest.mark.parametrize("invalid", ["missing", None, "guessed", True])
def test_fresh_review_missing_null_or_invalid_visibility_is_rejected_without_filling(source, mode, invalid):
    plan = compiled_plan(source, mode)
    review = accepted_review(plan)
    first = review["alignment"]["spans"][0]
    if invalid == "missing":
        first.pop("speaker_visibility")
    else:
        first["speaker_visibility"] = invalid
    before = deepcopy(review)
    assert lower_failed_plan_verdicts(review, plan) == before
    with pytest.raises(ValueError):
        validate_plan_review(review, plan)
    assert review == before


def test_bound_historical_review_without_visibility_is_readable_without_fabricated_verdicts(source):
    plan = compiled_plan(source)
    bound = validate_plan_review(accepted_review(plan), plan).model_dump()
    for check in bound["alignment"]["spans"]:
        check.pop("speaker_visibility")
    before = deepcopy(bound)
    stored = Plan.model_validate({**plan, "plan_review": bound})
    assert stored.plan_review == before
    assert validate_plan_review(bound, plan).model_dump() == before
    assert lower_failed_plan_verdicts(bound, plan) == before
    # Legacy AV validation also retains the missing field, never synthesizes it.
    assert validate_av_review(bound["alignment"], plan).model_dump() == bound["alignment"]
    assert "speaker_visibility" not in AVSpanCheck.model_validate(bound["alignment"]["spans"][0]).model_dump()
    with pytest.raises(ValueError, match="逐个语音时段"):
        validate_plan_review(bound, plan, require_speaker_visibility=True)


@pytest.mark.parametrize("visibility", ["off_screen", "unclear", "not_applicable"])
def test_confirmed_voiceover_does_not_require_on_screen_speaker(source, visibility):
    plan = compiled_plan(source, "voiceover")
    review = accepted_review(plan)
    for check in review["alignment"]["spans"]:
        check["speaker_visibility"] = visibility
    assert lower_failed_plan_verdicts(review, plan) == review
    assert validate_plan_review(review, plan).ready is True
    assert review_speech_requirements(plan)["requires_visible_speaking"] is False


def test_silent_plan_has_no_invented_visibility_items(source):
    plan = compiled_plan(source, "none")
    review = accepted_review(plan)
    assert review["alignment"]["spans"] == []
    assert validate_plan_review(review, plan).ready is True
    assert review_speech_requirements(plan)["speech_mode"] == "none"


def test_progressive_offscreen_dialogue_repairs_actual_shot_then_fully_reviews_again(progressive_gateway, source):
    state = progressive_gateway
    state.strategy = chosen_strategy(source, "character_dialogue")
    state.first_director_mutation = lambda draft: draft["shots"][0].update(description="商品特写，办公者在画外说话。")
    state.first_review_mutation = lambda review: review["alignment"]["spans"][0].update(
        speaker_visibility="off_screen", note="商品特写配合画外音，仍将模型总评标为通过。")
    state.refinement_mutation = lambda draft: draft["shots"][0].update(description="中景保留办公者脸部与同一瓶身，办公者面部可见并开口说话。")
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    first_review, last_review = state.contexts[2], state.contexts[4]
    assert first_review["speech_requirements"]["speech_mode"] == "character_dialogue"
    assert first_review["speech_requirements"]["speaker_id"] == "char_worker"
    assert first_review["speech_requirements"]["requires_visible_speaking"] is True
    assert first_review["speech_requirements"] == last_review["speech_requirements"]
    feedback = state.contexts[3]["review_feedback"]
    assert feedback["ready"] is False and feedback["alignment"]["spans"][0]["passed"] is False
    assert state.contexts[3]["repair_scope"]["beat_ids"] == ["b0"]
    assert "面部可见" in last_review["final_plan"]["segments"][0]["shots"][0]["description"]
    assert plan["plan_review"]["ready"] is True
    assert plan["plan_review"]["alignment"]["spans"][0]["speaker_visibility"] == "visible_speaking"


@pytest.mark.parametrize("forged_old_binding", [False, True])
def test_progressive_missing_visibility_is_invalid_even_with_copied_historical_binding(progressive_gateway, source, forged_old_binding):
    state = progressive_gateway
    def incomplete(review):
        if forged_old_binding:
            plan = compile_director(director_output(state.strategy), state.strategy, state.duration,
                                    "continuity_v1", [source["id"]])[0]
            bound = validate_plan_review(review, plan).model_dump()
            review.update({key: bound[key] for key in ("reviewed_plan_sha256", "reviewed_reference_asset_ids")})
        review["alignment"]["spans"][0].pop("speaker_visibility")
    state.first_review_mutation = state.second_review_mutation = incomplete
    with pytest.raises(ProviderError, match="完整检查尚未完成"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_review"]
