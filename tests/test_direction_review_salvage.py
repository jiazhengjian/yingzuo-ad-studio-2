"""A ready direction review must not fail the job, and diagnostics name the check."""
import json
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError
import pytest

from app.direction_review import review_failure_reasons, salvage_direction_review, validate_direction_review
from app.providers.gateway import save_failed_planner_output
from app.strategy import CreativeDirection, ProductBrief, build_direction_candidates, select_rules
from test_conversation_api import env
from test_direction_selection import candidates_for


FIXTURE = Path(__file__).parent / "fixtures" / "direction_review_fail_1315.json"


def _scene_story_original(raw):
    """The strategy step already stored the concept fields this review omitted."""
    hooks = ("awkward_moment", "situational_conflict", "arresting_line")
    worlds = ("identity", "sensory", "functional")
    insights = (
        "唱完生日歌，谁先切第一刀让人卡住。",
        "想先自己看一眼，又怕拆开就没了惊喜。",
        "最后一块想留给自己，手已经伸过来了。",
    )
    ideas = ("谁来切第一刀", "先看顶上再切开", "最后一块留给自己")
    directions = []
    for index, item in enumerate(raw["directions"]):
        copied = deepcopy(item)
        copied.update(hook_type=hooks[index], brand_world=worlds[index],
                      consumer_insight=insights[index], big_idea=ideas[index])
        directions.append(CreativeDirection.model_validate(copied))
    brief = ProductBrief(
        product_name="巧克力蛋糕", category="food", audience="年轻女生，生日送礼", scenario="生日聚会",
        ad_form="scene_story",
        creative_intent={"use_context": "年轻女生生日送礼", "setting_requirement": "",
                         "reference_intent": "", "structure_preference": "auto"},
        facts=[
            {"id": "fact_choco", "text": "巧克力口感丝滑", "source_kind": "merchant", "source_id": "msg_1"},
            {"id": "fact_audience", "text": "年轻女生，生日送礼", "source_kind": "merchant", "source_id": "msg_1"},
            {"id": "fact_appearance", "text": "一块多层巧克力蛋糕切块，深色淋面和车厘子", "source_kind": "image", "source_id": "asset_1"},
        ])
    return build_direction_candidates(brief, directions, directions[0].id, select_rules(None))


def test_saved_scene_story_review_is_rejected_only_for_omitted_idea_fields():
    raw = json.loads(FIXTURE.read_text())["raw_output"]
    original = _scene_story_original(raw)
    with pytest.raises(ValueError, match="不能改写创意机制") as caught:
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    assert review_failure_reasons(caught.value) == ["idea_field"]


def test_ready_scene_story_review_keeps_text_fixes_and_canonical_fields():
    raw = json.loads(FIXTURE.read_text())["raw_output"]
    original = _scene_story_original(raw)
    review, reasons = salvage_direction_review(raw, original, select_rules(None), product_reference_count=1)
    assert review.ready is True
    assert reasons == ["idea_field"]
    assert [direction.hook_type for direction in review.directions] == [
        direction.hook_type for direction in original.directions]
    assert [direction.brand_world for direction in review.directions] == [
        direction.brand_world for direction in original.directions]
    assert review.directions[0].story_summary == raw["directions"][0]["story_summary"]
    assert review.directions[0].big_idea == original.directions[0].big_idea
    assert "这巧克力口感真丝滑" in review.directions[0].story_summary


def test_unusable_revision_falls_back_to_prereview_directions_with_safe_text(env):
    original = candidates_for(env)
    raw = {
        "ready": True, "distinct_directions_passed": True,
        "summary": "审核通过，只改了开场那一句。",
        "image_observation_corrections": [],
        "checks": [{"direction_id": direction.id, "physical_plausibility_passed": True,
            "product_role_passed": True, "attraction_passed": True, "claim_support_passed": True,
            "expression_form_passed": True, "film_concept_passed": True,
            "note": "开场改成观众听得懂的一句。", "creative_tension": "medium", "hook_strength": "medium"}
            for direction in original.directions],
        "directions": [direction.model_dump() for direction in original.directions],
    }
    for direction in raw["directions"]:
        direction.pop("hook_type", None)
        direction.pop("brand_world", None)
        direction.pop("consumer_insight", None)
        direction.pop("big_idea", None)
        direction["evidence_ids"] = ["missing_fact"]
        direction["angle"] = "先看清瓶子的实际轮廓，再决定要不要把它留在桌上。"
    review, reasons = salvage_direction_review(raw, original, select_rules(None), product_reference_count=1)
    assert review.ready is True
    assert "idea_field" in reasons
    assert [direction.evidence_ids for direction in review.directions] == [
        direction.evidence_ids for direction in original.directions]
    assert review.directions[0].angle == raw["directions"][0]["angle"]
    assert review.directions[0].hook_type == original.directions[0].hook_type


def test_failed_review_is_not_rewritten_into_a_pass(env):
    original = candidates_for(env)
    raw = {
        "ready": False, "distinct_directions_passed": False,
        "summary": "有一条方向仍把外观当成口感证据。",
        "checks": [{"direction_id": direction.id, "physical_plausibility_passed": True,
            "product_role_passed": True, "attraction_passed": index != 0, "claim_support_passed": True,
            "expression_form_passed": True, "film_concept_passed": True, "note": "第一条仍越界。",
            "creative_tension": "low", "hook_strength": "low"}
            for index, direction in enumerate(original.directions)],
        "directions": [direction.model_dump() for direction in original.directions],
    }
    review, reasons = salvage_direction_review(raw, original, select_rules(None), product_reference_count=1)
    assert review.ready is False and reasons == []
    broken = deepcopy(raw)
    broken["directions"][0].pop("hook_type")
    with pytest.raises((ValueError, ValidationError)):
        salvage_direction_review(broken, original, select_rules(None), product_reference_count=1)


def test_failure_diagnostic_records_routed_model_and_reason_codes(tmp_path):
    settings = type("Settings", (), {})()
    settings.planner_model = "gpt-6-sol"
    settings.deepseek_api_key = ""
    settings.tudou_api_key = ""
    settings.data_root = tmp_path
    save_failed_planner_output(settings, {
        "stage": "direction_review", "model": "deepseek-flash",
        "raw_output": {"ready": True},
        "validation_reasons": ["idea_field", "不要写商家原话", "hook_type"],
    })
    saved = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text())
    assert saved["model"] == "deepseek-flash"
    assert saved["validation_reasons"] == ["idea_field", "hook_type"]
    assert "gpt-6-sol" not in json.dumps(saved)
    assert "商家原话" not in json.dumps(saved)
