"""Plan-level same-speaker continuation; no supplier or generated-film claims."""
from copy import deepcopy

import pytest

from app.av_review import review_speech_requirements
from app.plan_review import lower_failed_plan_verdicts, plan_review_schema, validate_plan_review
from app.production import ProductionPlan, production_authoring_schema, required_designs, validate_production_plan
from app.progressive_planning import compile_director
from app.schemas import Plan
from app.strategy import StrategyRecord
from app.video_prompt import compile_grouped_video
from test_progressive_planning import accepted_review, chosen_strategy, director_output, forbid_real_network
from test_providers import source


def continuation_plan(source, *, split=True):
    raw = chosen_strategy(source, "character_dialogue").model_dump()
    selected = next(d for d in raw["directions"] if d["id"] == raw["selected_direction_id"])
    selected["production"]["speech_delivery"] = "on_and_off_screen"
    for person in selected["production"]["characters"]:
        person["appearance"] = "face_visible"
    strategy = StrategyRecord.model_validate(raw)
    draft = director_output(strategy)
    for shot in draft["shots"]:
        shot["description"] = "办公者脸部与嘴部可见，手持同一瓶身，面向观众自然开口。"
    plan = compile_director(draft, strategy, 20, "continuity_v1", [source["id"]])[0]
    if split:
        segment = plan["segments"][0]
        shot, span = segment["shots"][0], segment["speech_spans"][0]
        cut = (span["start"] + span["end"]) / 2
        detail = deepcopy(shot)
        detail.update(id="product_detail", start=cut, character_ids=[], description="只拍同一瓶身特写，讲解者不入画。")
        detail["continuity"].update(entry_transition="cut", entry_reason="近看同一瓶身轮廓。")
        shot["end"] = cut
        segment["shots"].insert(1, detail)
        span["shot_ids"] = [shot["id"], detail["id"]]
        span["anchors"][0]["shot_ids"] = list(span["shot_ids"])
    return Plan.model_validate(plan).model_dump()


def continuation_review(plan):
    review = accepted_review(plan)
    segments = {segment["id"]: segment for segment in plan["segments"]}
    for check in review["alignment"]["spans"]:
        segment = segments[check["segment_id"]]
        span = next(item for item in segment["speech_spans"] if item["id"] == check["span_id"])
        shots = {shot["id"]: shot for shot in segment["shots"]}
        checks = [{"shot_id": shot_id,
            "visibility": "visible_speaking" if plan["production"]["speaker_id"] in shots[shot_id].get("character_ids", []) else "off_screen",
            "note": "明确离线夹具：脸部开口与商品画外声音按实际相交镜头区分。"} for shot_id in span["shot_ids"]]
        values = {item["visibility"] for item in checks}
        check.update(shot_visibility=checks, speaker_visibility="mixed" if len(values) > 1 else next(iter(values)))
    return review


def test_legacy_production_serialization_is_unchanged_and_new_authors_specify_appearance(source):
    production = chosen_strategy(source, "character_dialogue").directions[0].production.model_dump()
    assert "speech_delivery" not in production
    assert all("appearance" not in person for person in production["characters"])
    assert ProductionPlan.model_validate(production).model_dump() == production
    schema = production_authoring_schema(Plan.model_json_schema())
    character = schema["$defs"]["CharacterSpec"]
    assert "appearance" in character["required"]
    assert character["properties"]["appearance"]["enum"] == ["face_visible", "partial_only"]


@pytest.mark.parametrize("mode", ["voiceover", "none"])
def test_continuation_is_never_an_alias_for_voiceover_or_silence(source, mode):
    production = continuation_plan(source)["production"]
    production.update(speech_mode=mode, speaker_id="", voice_description="" if mode == "none" else "普通话")
    with pytest.raises(ValueError, match="只适用于"):
        ProductionPlan.model_validate(production)


def test_partial_only_role_is_a_real_cast_member_but_cannot_be_the_visible_speaker(source):
    production = continuation_plan(source)["production"]
    production["characters"][0]["appearance"] = "partial_only"
    with pytest.raises(ValueError, match="局部出镜"):
        ProductionPlan.model_validate(production)
    production.update(speech_mode="voiceover", speaker_id="", speech_delivery=None)
    parsed = ProductionPlan.model_validate(production).model_dump()
    assert parsed["characters"][0]["appearance"] == "partial_only"
    assert len(required_designs({"production": parsed})) == 2


def test_mixed_sentence_has_complete_per_shot_checks_and_keeps_one_speaker(source):
    plan = continuation_plan(source)
    before = deepcopy(plan)
    review = continuation_review(plan)
    assert review["alignment"]["spans"][0]["speaker_visibility"] == "mixed"
    assert validate_plan_review(review, plan).ready is True
    assert lower_failed_plan_verdicts(review, plan) == review
    assert plan == before
    assert {span["speaker_id"] for segment in plan["segments"] for span in segment["speech_spans"]} == {"char_worker"}
    assert review_speech_requirements(plan)["requires_visible_anchor"] is True
    schema = plan_review_schema(plan)
    assert "shot_visibility" in schema["$defs"]["AVSpanCheck"]["required"]
    assert "shot_visibility" not in plan_review_schema()["$defs"]["AVSpanCheck"]["required"]


@pytest.mark.parametrize("problem", ["missing", "null", "empty", "duplicate", "foreign", "omitted", "wrong_order", "forged_summary"])
def test_missing_or_forged_per_shot_evidence_cannot_pass(source, problem):
    plan = continuation_plan(source)
    review = continuation_review(plan)
    check = review["alignment"]["spans"][0]
    if problem == "missing":
        check.pop("shot_visibility")
    elif problem == "null":
        check["shot_visibility"] = None
    elif problem == "empty":
        check["shot_visibility"] = []
    elif problem == "duplicate":
        check["shot_visibility"].append(deepcopy(check["shot_visibility"][0]))
    elif problem == "foreign":
        check["shot_visibility"][0]["shot_id"] = "foreign"
    elif problem == "omitted":
        check["shot_visibility"].pop()
    elif problem == "wrong_order":
        check["shot_visibility"].reverse()
    else:
        check["speaker_visibility"] = "visible_speaking"
    before = deepcopy(review)
    with pytest.raises(ValueError):
        validate_plan_review(lower_failed_plan_verdicts(review, plan), plan)
    assert review == before


def test_unclear_window_lowers_verdict_without_inventing_an_on_screen_speaker(source):
    plan = continuation_plan(source)
    review = continuation_review(plan)
    check = review["alignment"]["spans"][0]
    check["shot_visibility"][1]["visibility"] = "unclear"
    check["speaker_visibility"] = "unclear"
    before = deepcopy(review)
    lowered = lower_failed_plan_verdicts(review, plan)
    assert lowered["ready"] is False and lowered["alignment"]["spans"][0]["passed"] is False
    assert lowered["alignment"]["spans"][0]["shot_visibility"] == check["shot_visibility"]
    assert validate_plan_review(lowered, plan).ready is False
    assert review == before


def test_whole_film_offscreen_cannot_pass_or_count_a_silent_face_as_speaking(source):
    plan = continuation_plan(source)
    review = continuation_review(plan)
    for check in review["alignment"]["spans"]:
        check["speaker_visibility"] = "off_screen"
        for shot in check["shot_visibility"]:
            shot["visibility"] = "off_screen"
    with pytest.raises(ValueError, match="至少须有一处"):
        validate_plan_review(review, plan)
    before = deepcopy(review)
    lowered = lower_failed_plan_verdicts(review, plan)
    assert lowered["ready"] is False
    assert lowered["alignment"]["ad_expression_passed"] is False
    assert lowered["alignment"]["spans"] == review["alignment"]["spans"]
    assert validate_plan_review(lowered, plan).ready is False
    assert review == before


@pytest.mark.parametrize("problem", ["missing_list", "null_list", "unknown_shot", "missing_span", "unknown_span", "missing_note"])
def test_incomplete_offscreen_checks_do_not_invent_a_missing_anchor_verdict(source, problem):
    plan = continuation_plan(source)
    review = continuation_review(plan)
    for check in review["alignment"]["spans"]:
        check["speaker_visibility"] = "off_screen"
        for shot in check["shot_visibility"]:
            shot["visibility"] = "off_screen"
    check = review["alignment"]["spans"][0]
    if problem == "missing_list":
        check.pop("shot_visibility")
    elif problem == "null_list":
        check["shot_visibility"] = None
    elif problem == "unknown_shot":
        check["shot_visibility"][0]["shot_id"] = "foreign"
    elif problem == "missing_span":
        review["alignment"]["spans"].pop()
    elif problem == "unknown_span":
        check["span_id"] = "foreign"
    else:
        check["shot_visibility"][0].pop("note")
    before = deepcopy(review)
    assert lower_failed_plan_verdicts(review, plan) == before
    with pytest.raises(ValueError):
        validate_plan_review(review, plan)


def test_visible_claim_cannot_count_a_different_role_or_missing_role(source):
    plan = continuation_plan(source)
    review = continuation_review(plan)
    plan["segments"][0]["shots"][0]["character_ids"] = []
    with pytest.raises(ValueError, match="必须包含"):
        validate_plan_review(review, plan)


def test_compiler_cannot_upgrade_selected_legacy_contract_or_change_speaker(source):
    plan = continuation_plan(source)
    selected = next(d for d in plan["strategy"]["directions"] if d["id"] == plan["strategy"]["selected_direction_id"])
    selected["production"].pop("speech_delivery")
    with pytest.raises(ValueError, match="已选方向"):
        validate_production_plan(plan)
    plan = continuation_plan(source)
    plan["segments"][0]["speech_spans"][0]["speaker_id"] = "char_other"
    with pytest.raises(ValueError, match="说话人"):
        validate_production_plan(plan)


@pytest.mark.parametrize("delivery", [None, "visible_only"])
def test_legacy_visible_only_contract_still_rejects_mixed_or_offscreen(source, delivery):
    plan = continuation_plan(source, split=False)
    selected = next(d for d in plan["strategy"]["directions"] if d["id"] == plan["strategy"]["selected_direction_id"])
    plan["production"]["speech_delivery"] = selected["production"]["speech_delivery"] = delivery
    review = accepted_review(plan)
    review["alignment"]["spans"][0]["speaker_visibility"] = "mixed"
    lowered = lower_failed_plan_verdicts(review, plan)
    assert validate_plan_review(lowered, plan).ready is False
    plan["segments"][0]["shots"][0]["character_ids"] = []
    with pytest.raises(ValueError, match="说话窗口"):
        validate_production_plan(plan)


def test_video_prompt_preserves_continuation_without_adding_faces_or_second_voice(source):
    plan = continuation_plan(source)
    segment = deepcopy(plan["segments"][0])
    segment["production"] = deepcopy(plan["production"])
    segment["reference_roles"] = [{"index": 1, "role": "product"}, {"index": 2, "role": "scene"},
        {"index": 3, "role": "character", "character_id": "char_worker"}]
    segment["generation_strategy"] = "continuity_v1"
    before = deepcopy(segment)
    prompt = compile_grouped_video(segment)
    assert "同一人物画外延续" in prompt
    assert "本镜说话角色不入画" in prompt
    assert "不补人物或嘴部" in prompt
    assert "不能换成另一位旁白" in prompt
    assert "这是执行要求，不是已验证的声纹" in prompt
    assert segment == before
    segment["production"].update(speech_mode="voiceover", speech_delivery=None, speaker_id="")
    segment["production"]["characters"][0]["appearance"] = "partial_only"
    partial_prompt = compile_grouped_video(segment)
    assert "仅局部出镜、不增加露脸" in partial_prompt
    assert "本镜说话角色不入画" not in partial_prompt
