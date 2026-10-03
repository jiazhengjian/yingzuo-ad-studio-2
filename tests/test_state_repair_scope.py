"""State repairs can change a neighboring action without unlocking the story."""
from copy import deepcopy

import pytest

from app.progressive_planning import repair_scope, validate_local_refinement


def draft_with_handoff(count=7):
    state = "商品放在桌上，接收者手中已有道具"
    return {
        "title": "桌边分享",
        "visual_direction": {"scene": "同一张桌边"},
        "ad_script": {
            "core_message": "分享同一商品",
            "audience_takeaway": "记住分享时刻",
            "product_role": "商品供两人共同使用",
            "beats": [{
                "id": f"b{i}", "narration": "", "visual_role": "接收者拿着道具观察商品",
                "timing": {"visual_seconds": 2, "speech_overlap": True,
                    "action_plan": "接收者拿着道具观察商品", "start_state": state,
                    "end_state": state, "time_transition": "continuous"},
            } for i in range(count)],
        },
        "shots": [{
            "id": f"s{i}", "coverage": [{"beat_id": f"b{i}", "weight": 1}],
            "description": "接收者接过道具并握住，观察商品。",
            "continuity": {"start_state": state, "end_state": state,
                "allowed_changes": "接收者握住道具，商品保持原样",
                "camera_move": "固定双人近景", "entry_reason": "承接道具交接",
                "entry_transition": "opening" if i == 0 else "continuous"},
        } for i in range(count)],
    }


def rejected_review(draft, failed_shot="s3", *, state_continuity=False):
    return {
        "checks": [{"passed": True}],
        "execution_check": {"action_capacity": True, "state_continuity": state_continuity},
        "alignment": {"ad_expression_passed": True, "spans": [], "shots": [
            {"shot_id": shot["id"], "passed": shot["id"] != failed_shot,
             "pacing": {"action_timing_passed": True}} for shot in draft["shots"]]},
    }


def scope_for(draft, failed_shot="s3", *, state_continuity=False):
    return repair_scope(rejected_review(draft, failed_shot, state_continuity=state_continuity),
                        draft, {"segments": []})


def test_state_repair_can_remove_duplicate_action_in_neighbor_and_update_its_beat():
    before = draft_with_handoff()
    scope = scope_for(before)
    assert set(scope["shot_ids"]) == {"s2", "s3", "s4"}
    assert set(scope["beat_ids"]) == {"b2", "b3", "b4"}
    assert scope["allow_summary_update"] is False
    assert not scope.get("allow_beat_restructure", False)
    after = deepcopy(before)
    after["shots"][3]["description"] = "递出道具，接收者接稳并握住。"
    after["shots"][4]["description"] = "接收者握着道具观察商品，没有再次接过。"
    after["shots"][4]["continuity"]["entry_reason"] = "承接前镜已经完成的交接"
    after["ad_script"]["beats"][4]["visual_role"] = "接收者保持握住道具，不再重复交接"
    validate_local_refinement(before, after, scope)


@pytest.mark.parametrize("target", ["shot", "beat", "title", "visual_direction", "summary"])
def test_state_repair_keeps_distant_content_and_globals_fixed(target):
    before = draft_with_handoff()
    after = deepcopy(before)
    if target == "shot":
        after["shots"][0]["description"] = "换成另一段独立动作。"
    elif target == "beat":
        after["ad_script"]["beats"][0]["visual_role"] = "重写开场内容"
    elif target == "title":
        after["title"] = "另一故事"
    elif target == "visual_direction":
        after["visual_direction"]["scene"] = "另一个环境"
    else:
        after["ad_script"]["core_message"] = "另一个主旨"
    with pytest.raises(ValueError, match="未受影响"):
        validate_local_refinement(before, after, scope_for(before))


def test_non_state_failure_still_allows_only_neighbor_state_fields():
    before = draft_with_handoff()
    scope = scope_for(before, state_continuity=True)
    assert scope["shot_ids"] == ["s3"]
    after = deepcopy(before)
    after["shots"][4]["continuity"]["start_state"] = "接收者已经握住道具"
    validate_local_refinement(before, after, scope)
    after["shots"][4]["description"] = "接收者握着道具观察商品，没有再次接过。"
    with pytest.raises(ValueError, match="未受影响镜头"):
        validate_local_refinement(before, after, scope)


@pytest.mark.parametrize(("failed_shot", "expected"), [("s0", {"s0", "s1"}), ("s6", {"s5", "s6"})])
def test_state_repair_at_first_and_last_shot_does_not_wrap(failed_shot, expected):
    before = draft_with_handoff()
    scope = scope_for(before, failed_shot)
    assert set(scope["shot_ids"]) == expected
    after = deepcopy(before)
    neighbor = next(i for i, shot in enumerate(after["shots"])
                    if shot["id"] in expected and shot["id"] != failed_shot)
    after["shots"][neighbor]["description"] = "道具交接已完成，接收者保持握住。"
    validate_local_refinement(before, after, scope)


def test_shared_coverage_is_editable_without_recursive_neighbor_expansion():
    before = draft_with_handoff(count=8)
    for i, shot in enumerate(before["shots"][:-1]):
        shot["coverage"].append({"beat_id": f"b{i + 1}", "weight": 1})
    scope = scope_for(before)
    # Failed s3 covers b3/b4, initially affecting s2/s3/s4. Expand those
    # neighbors once; s6 shares newly added b6 but remains protected.
    assert set(scope["shot_ids"]) == {"s1", "s2", "s3", "s4", "s5"}
    assert set(scope["beat_ids"]) == {"b1", "b2", "b3", "b4", "b5", "b6"}
    after = deepcopy(before)
    after["shots"][5]["description"] = "交接结束，接收者继续握住道具。"
    after["ad_script"]["beats"][6]["visual_role"] = "在同一交接终态上继续观察"
    validate_local_refinement(before, after, scope)
    after["shots"][6]["description"] = "继续向更远处扩展改写。"
    with pytest.raises(ValueError, match="未受影响镜头"):
        validate_local_refinement(before, after, scope)
