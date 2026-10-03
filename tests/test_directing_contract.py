"""Offline direction ownership and all-shot continuity contracts.

Passing these tests does not establish the semantic quality of the direction,
that frames obey it, or that separate native-audio requests share a voice.
"""
from copy import deepcopy

import pytest

from app.directing import VisualDirection, ShotContinuity, direction_context
from app.schemas import Plan
from test_speech_spans import plan_data


@pytest.fixture
def directed_plan_data(plan_data):
    value = deepcopy(plan_data)
    value["visual_direction"] = {
        "version": "1.0.0", "product_anchor_ids": ["layers"],
        "scene": "办公室下午茶桌面，远处可见办公环境",
        "lighting": "左侧窗光柔和照亮蛋糕，保持同一光向",
        "color_palette": "背景暖灰色，保留原图蛋糕颜色",
        "camera_style": "中近景建立环境，细节镜头不改变商品状态",
        "sound": "自然成年女性普通话，背景声低于旁白；音色一致性待样片验证",
    }
    for index, shot in enumerate(value["segments"][0]["shots"]):
        shot["continuity"] = {
            "start_state": "完整蛋糕切块静置于办公桌上的同一餐盘中央",
            "allowed_changes": "主体保持静止，不增减配料或改变切面层次",
            "end_state": "完整蛋糕切块静置于办公桌上的同一餐盘中央",
            "camera_move": "展示商品与桌面的关系" if index == 0 else "切至同一商品的细节景别",
            "entry_transition": "opening" if index == 0 else "continuous",
            "entry_reason": "建立下午茶商品选择" if index == 0 else "看清已有商品层次或完成展示收束",
        }
    return value


def test_historical_plan_dump_remains_without_new_keys(plan_data):
    model = Plan.model_validate(plan_data)
    before = model.model_dump()
    assert "visual_direction" not in before
    assert all("continuity" not in shot for segment in before["segments"] for shot in segment["shots"])
    assert direction_context(model) is None and direction_context(before) is None
    assert Plan.model_validate(before).model_dump() == before


def test_new_direction_and_every_shot_are_required_only_when_opted_in(plan_data, directed_plan_data):
    assert Plan.model_validate(plan_data)
    with pytest.raises(ValueError, match="统一视觉约定"):
        Plan.model_validate(plan_data, context={"require_directing_contract": True})
    result = Plan.model_validate(directed_plan_data, context={"require_directing_contract": True})
    assert result.visual_direction.version == "1.0.0"
    assert len([shot.continuity for segment in result.segments for shot in segment.shots]) == 3


def test_direction_context_resolves_only_cited_facts_and_is_defensively_new(directed_plan_data):
    directed_plan_data["strategy"]["brief"]["facts"].append({
        "id": "unused_fact", "text": "未被视觉约定引用的另一项已知外观", "source_kind": "image", "source_id": "photo"})
    directed_plan_data["facts"]["claims"].append("未被视觉约定引用的另一项已知外观")
    result = direction_context(Plan.model_validate(directed_plan_data))
    assert result == {**directed_plan_data["visual_direction"], "product_anchors": [
        {"id": "layers", "text": "多层切面", "source_kind": "image", "source_id": "photo"}]}
    result["product_anchors"][0]["text"] = "不能回写原图事实"
    result["product_anchor_ids"].clear()
    assert direction_context(directed_plan_data)["product_anchors"][0]["text"] == "多层切面"
    assert directed_plan_data["visual_direction"]["product_anchor_ids"] == ["layers"]


@pytest.mark.parametrize("invalid", ["missing_strategy", "unknown_anchor", "duplicate_anchor", "empty_anchors",
                                      "missing_one_shot", "orphan_continuity", "empty_shot_id", "duplicate_shot_id"])
def test_incomplete_or_unowned_direction_cannot_enter_plan(directed_plan_data, invalid):
    value = deepcopy(directed_plan_data)
    shots = value["segments"][0]["shots"]
    if invalid == "missing_strategy": value.pop("strategy")
    elif invalid == "unknown_anchor": value["visual_direction"]["product_anchor_ids"] = ["invented"]
    elif invalid == "duplicate_anchor": value["visual_direction"]["product_anchor_ids"] *= 2
    elif invalid == "empty_anchors": value["visual_direction"]["product_anchor_ids"] = []
    elif invalid == "missing_one_shot": shots[-1].pop("continuity")  # silent ending still needs coverage
    elif invalid == "orphan_continuity": value.pop("visual_direction")
    elif invalid == "empty_shot_id": shots[0]["id"] = ""
    else: shots[1]["id"] = shots[0]["id"]
    with pytest.raises(ValueError): Plan.model_validate(value)


@pytest.mark.parametrize("invalid", ["first_is_cut", "second_is_opening", "silent_ending_is_opening", "continuous_state_drift"])
def test_transition_structure_cannot_reset_opening_or_subject(directed_plan_data, invalid):
    value = deepcopy(directed_plan_data)
    shots = value["segments"][0]["shots"]
    if invalid == "first_is_cut": shots[0]["continuity"]["entry_transition"] = "cut"
    elif invalid == "second_is_opening": shots[1]["continuity"]["entry_transition"] = "opening"
    elif invalid == "silent_ending_is_opening": shots[-1]["continuity"]["entry_transition"] = "opening"
    else: shots[1]["continuity"]["start_state"] = "蛋糕已经切开，摆到另一张桌子上"
    with pytest.raises(ValueError): Plan.model_validate(value)


def test_camera_change_does_not_require_new_physical_subject_state(directed_plan_data):
    shot = directed_plan_data["segments"][0]["shots"][1]
    shot["continuity"]["camera_move"] = "从中景切至同一切面近景，机位保持在桌子同一侧"
    result = Plan.model_validate(directed_plan_data)
    assert result.segments[0].shots[1].continuity.start_state == result.segments[0].shots[0].continuity.end_state
    assert result.segments[0].shots[1].continuity.camera_move != result.segments[0].shots[0].continuity.camera_move


@pytest.mark.parametrize("invalid", ["unapproved_change", "not_first_shot_of_beat", "missing_script"])
def test_scene_change_is_not_an_escape_hatch_for_state_discontinuity(directed_plan_data, invalid):
    value = deepcopy(directed_plan_data)
    shots = value["segments"][0]["shots"]
    if invalid == "not_first_shot_of_beat":
        value["ad_script"]["beats"][0]["boundary_before"] = "scene_change"
        value["script_review"]["script"] = deepcopy(value["ad_script"])
        shots[1]["continuity"]["entry_transition"] = "scene_change"
    else:
        shots[-1]["continuity"]["entry_transition"] = "scene_change"
        if invalid == "missing_script":
            value.pop("ad_script")
            value.pop("script_review")
    with pytest.raises(ValueError, match="已审脚本"):
        Plan.model_validate(value)


def test_real_approved_scene_change_can_start_at_first_shot_of_new_beat(directed_plan_data):
    value = deepcopy(directed_plan_data)
    value["ad_script"]["beats"][1]["boundary_before"] = "scene_change"
    value["script_review"]["script"] = deepcopy(value["ad_script"])
    value["segments"][0]["shots"][-1]["continuity"].update(
        entry_transition="scene_change", start_state="蛋糕完整地摆在已约定的展示台上", end_state="蛋糕完整地摆在已约定的展示台上",
        entry_reason="已审脚本在此转换为独立商品收束场景")
    result = Plan.model_validate(value)
    assert result.segments[0].shots[-1].continuity.entry_transition == "scene_change"


def test_continuous_state_is_checked_across_generation_boundary(directed_plan_data):
    value = deepcopy(directed_plan_data)
    value.pop("ad_script")
    value.pop("script_review")
    second = deepcopy(value["segments"][0])
    second.update(id="seg2", narration="", speech_spans=[])
    for shot in second["shots"]:
        shot["continuity"]["entry_transition"] = "continuous"
    value["segments"].append(second)
    assert Plan.model_validate(value)
    second["shots"][0]["continuity"]["start_state"] = "蛋糕突然更换到另一个盘子"
    with pytest.raises(ValueError, match="逐字继承"):
        Plan.model_validate(value)


@pytest.mark.parametrize("field", ["scene", "lighting", "color_palette", "camera_style", "sound"])
def test_visual_direction_rejects_blank_and_unbounded_fields(directed_plan_data, field):
    value = deepcopy(directed_plan_data["visual_direction"])
    for invalid in ("  ", "字" * 801, 123, None):
        value[field] = invalid
        with pytest.raises(ValueError): VisualDirection.model_validate(value)


@pytest.mark.parametrize("field", ["start_state", "allowed_changes", "end_state", "camera_move", "entry_reason"])
def test_shot_continuity_rejects_blank_and_unbounded_fields(directed_plan_data, field):
    value = deepcopy(directed_plan_data["segments"][0]["shots"][0]["continuity"])
    for invalid in ("  ", "字" * 801, 123, None):
        value[field] = invalid
        with pytest.raises(ValueError): ShotContinuity.model_validate(value)


def test_direction_models_forbid_unowned_fields_and_versions(directed_plan_data):
    direction = deepcopy(directed_plan_data["visual_direction"])
    direction["version"] = "unreviewed-v2"
    with pytest.raises(ValueError): VisualDirection.model_validate(direction)
    direction = deepcopy(directed_plan_data["visual_direction"])
    direction["brand_claim"] = "未提供的性能"
    with pytest.raises(ValueError): VisualDirection.model_validate(direction)
    shot = deepcopy(directed_plan_data["segments"][0]["shots"][0]["continuity"])
    shot["provider_task"] = "not-owned"
    with pytest.raises(ValueError): ShotContinuity.model_validate(shot)
