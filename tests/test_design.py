"""Scene compilation is local and must preserve the accepted setting."""
import copy
import json

from app.design import scene_design_request


def sample_plan():
    return {"facts": {"name": "蛋糕"}, "strategy": {
        "brief": {"scenario": "办公室下午茶"}, "selected_direction_id": "office",
        "directions": [
            {"id": "office", "scenario": "办公室工位旁的茶水区", "production_notes": ["电脑背面与办公椅可辨识"]},
            {"id": "unused", "scenario": "未选的海边野餐", "production_notes": ["野餐篮"]},
        ]}, "segments": [
            {"id": "s1", "prompt": "蛋糕近景", "shots": [{"description": "办公桌前景"}]},
            {"id": "s2", "prompt": "保持同一下午窗光", "shots": [{"description": "拉远看见工位隔断"}]},
        ]}


def context(request):
    return json.loads(request["prompt"].split("已确认的场景与视觉方案：\n", 1)[1])


def test_scene_request_preserves_setting_without_copying_the_video_plan():
    plan = sample_plan()
    before = copy.deepcopy(plan)
    request = scene_design_request({"brief": "过时的原始场景"}, plan)
    data = context(request)
    assert request["scenario"] == data["confirmed_scenario"] == "办公室下午茶"
    assert data["scene_details"] == "办公室工位旁的茶水区"
    assert "production_notes" not in data and "visual_plan" not in data
    assert "拉远看见工位隔断" not in request["prompt"]
    assert "未选的海边野餐" not in request["prompt"]
    assert "过时的原始场景" not in request["prompt"]
    assert plan == before


def test_legacy_scene_request_uses_accepted_visual_context_without_each_shot():
    plan = sample_plan()
    del plan["strategy"]
    data = context(scene_design_request({"brief": "办公室下午茶广告"}, plan))
    assert data["legacy_visual_context"] == ["蛋糕近景", "保持同一下午窗光"]
    assert "visual_plan" not in data and "legacy_original_brief" not in data
    assert not data["confirmed_scenario"]


def test_scene_prompt_states_the_confirmed_light_direction():
    plan = sample_plan()
    plan["visual_direction"] = {"scene": "合住房间", "lighting": "晨光从左侧进来"}
    request = scene_design_request({}, plan)
    assert "光线方向按已确认方案执行：晨光从左侧进来" in request["prompt"]
    assert "不要改到相反的一侧" in request["prompt"]


def test_scene_request_uses_selected_setting_when_brief_has_no_scene():
    plan = sample_plan()
    plan["strategy"]["brief"]["scenario"] = ""
    assert scene_design_request({}, plan)["scenario"] == "办公室工位旁的茶水区"
