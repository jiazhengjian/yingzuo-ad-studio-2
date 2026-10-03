"""Trust boundaries for merchant evidence versus rendered dialogue history."""
from app.planning import evidence_sources


def test_gate_question_is_not_merchant_evidence_and_skips_are_not_assertions():
    gates = [{"kind": "clarification", "status": "answered", "questions": [
        {"id": "where", "prompt": "这款可以杀菌的小家电投放哪里？", "options": [{"id": "douyin", "label": "抖音"}]},
        {"id": "claim", "prompt": "你确认省电吗？", "options": []}],
        "answers": [{"question_id": "where", "option_id": "douyin"}, {"question_id": "claim", "skipped": True}]}]
    messages = [{"id": "m1", "role": "user", "content": "补充：蓝色机身\n这款可以杀菌的小家电投放哪里？：抖音\n你确认省电吗？：暂不确定，请使用谨慎表达"},
                {"id": "m2", "role": "assistant", "content": "这是高效杀菌的商品"}]
    sources = evidence_sources({"brief": "小家电"}, messages, gates)
    assert sources == {"brief": "小家电", "m1": "补充：蓝色机身\n抖音"}
    assert "杀菌" not in str(sources) and "省电" not in str(sources)


def test_typed_gate_answer_and_source_id_survive_without_file_name_as_fact():
    gates = [{"kind": "clarification", "status": "answered", "questions": [
        {"id": "detail", "prompt": "还有什么资料？", "options": []}],
        "answers": [{"question_id": "detail", "text": "机身长20厘米"}]}]
    sources = evidence_sources({"brief": "广告"}, [{"role": "user", "content": "【参考素材：号称全球第一.png】还有什么资料？：机身长20厘米"}], gates)
    assert sources["message_1"] == "机身长20厘米"


def test_clarification_cannot_remove_every_answer_path():
    import pytest
    from pydantic import ValidationError
    from app.schemas import Question
    with pytest.raises(ValidationError):
        Question.model_validate({"id": "deadend", "prompt": "补充信息？", "options": [
            {"id": "custom", "label": "我来补充"}], "allow_custom": False, "allow_skip": False})


def test_copy_review_brief_retains_only_chosen_direction_evidence():
    import copy
    from app.planning import advertising_copy_brief
    visible = {"id": "layers", "text": "多层切面", "source_kind": "image", "source_id": "product_photo"}
    unused = {"id": "box", "text": "纸盒包装", "source_kind": "merchant", "source_id": "merchant_note"}
    plan = {"facts": {"name": "旧标题", "claims": ["不应通过此旧字段引入卖点"]}, "strategy": {
        "brief": {"product_name": "蛋糕切块", "audience": "办公人群", "scenario": "办公室下午茶",
                  "facts": [visible, unused], "unknowns": ["配料与口味"], "assumptions": ["下午茶为创意场景"]},
        "selected_direction_id": "afternoon",
        "directions": [{"id": "gift", "angle": "未选择的送礼角度", "evidence_ids": ["box"]},
                       {"id": "afternoon", "angle": "用蛋糕给下午茶增添仪式感", "hook": "桌面放下蛋糕",
                        "development": "用多层切面支撑外观吸引力", "payoff": "回到同一张办公桌",
                        "audience": "过时的受众", "scenario": "过时的场所", "evidence_ids": ["layers"]}]}}
    before = copy.deepcopy(plan)
    result = advertising_copy_brief(plan)
    assert result == {"product_name": "蛋糕切块", "audience": "办公人群", "scenario": "办公室下午茶",
                      "core_message": "用蛋糕给下午茶增添仪式感",
                      "story": {"hook": "桌面放下蛋糕", "development": "用多层切面支撑外观吸引力", "payoff": "回到同一张办公桌"},
                      "supported_facts": [visible], "unknowns": ["配料与口味"], "assumptions": ["下午茶为创意场景"]}
    assert plan == before


def test_legacy_copy_brief_does_not_promote_unreferenced_claims():
    from app.planning import advertising_copy_brief
    result = advertising_copy_brief({"facts": {"name": "蛋糕", "claims": ["绝对健康"]}})
    assert result["product_name"] == "蛋糕"
    assert result["supported_facts"] == [] and result["core_message"] == ""
    assert result["audience"] == "" and result["scenario"] == ""
    assert "绝对健康" not in str(result)


def scheduling_script(words):
    from app.ad_script import AdScript, AD_EXPRESSION_VERSION
    return AdScript.model_validate({"core_message": "商品在日常中的具体选择", "audience_takeaway": "了解商品外观与使用场景",
        "product_role": "商品承担内容的主体", "evidence_ids": ["fact"], "skill_version": AD_EXPRESSION_VERSION,
        "beats": [{"id": f"beat_{index}", "purpose": "表达商品与日常的关系", "message": "使观众理解商品的吸引力",
                   "visual_role": "展示商品及已确认的日常场景", "narration": word, "evidence_ids": ["fact"]}
                  for index, word in enumerate(words)]})


def assert_schedule_covers_video(schedule, words, durations):
    import math
    assert [segment["duration_seconds"] for segment in schedule] == durations
    spoken = []
    for segment, duration in zip(schedule, durations):
        shots = segment["shots"]
        assert 1 <= len(shots) <= 6
        assert shots[0]["start"] == 0 and shots[-1]["end"] == duration
        for index, shot in enumerate(shots):
            assert shot["end"] > shot["start"]
            if index:
                assert shot["start"] == shots[index - 1]["end"]
            # Acceptance property: complete reviewed speech fits actual allocated shot time.
            assert len(shot["narration"]) <= math.floor((shot["end"] - shot["start"]) * 2.9 + 1e-8)
            if shot["narration"]:
                spoken.append(shot["narration"])
    assert spoken == [word for word in words if word]


import pytest


@pytest.mark.parametrize("durations,words", [
    ([10], ["给桌面一点蓝。", "一只蓝瓶，自成风景。"]),
    ([10, 5], ["一只蓝瓶，自成风景。", "", "看看商品详情。"]),
    ([10, 10], ["下午茶，选块蛋糕。", "这层次，看着就抢眼。", "忙完这一阵。", "留几分钟给自己。"]),
    ([10] * 6, ["给桌面一点蓝。", "", "一只蓝瓶，自成风景。"]),
])
def test_schedule_places_complete_copy_once_with_contiguous_supported_timings(durations, words):
    from app.planning import storyboard_schedule
    schedule = storyboard_schedule(scheduling_script(words), durations)
    assert_schedule_covers_video(schedule, words, durations)
    assert schedule[0]["shots"][0]["narration"] == words[0]


def test_few_beats_start_with_opening_and_continue_silently_without_new_copy():
    from app.planning import storyboard_schedule
    words = ["下午茶，选块蛋糕。"]
    schedule = storyboard_schedule(scheduling_script(words), [10] * 6)
    assert_schedule_covers_video(schedule, words, [10] * 6)
    assert schedule[0]["shots"][0]["narration"] == words[0]
    assert all(not shot["narration"] for segment in schedule[1:] for shot in segment["shots"])
    assert {shot["beat_id"] for segment in schedule for shot in segment["shots"]} == {"beat_0"}


def test_impossible_display_time_stops_schedule_instead_of_squeezing_silent_beats():
    from app.planning import storyboard_schedule
    words = ["字" * 28 + "。", ""]  # speech uses the whole ten seconds; visual-only beat still needs time.
    with pytest.raises(ValueError):
        storyboard_schedule(scheduling_script(words), [10])


def test_seven_beats_cannot_be_scheduled_as_seven_shots_in_one_segment():
    from app.planning import storyboard_schedule
    words = ["蛋糕。", "", "", "", "", "", ""]
    with pytest.raises(ValueError):
        storyboard_schedule(scheduling_script(words), [10])


def test_four_reviewed_lines_do_not_overload_second_generation_segment():
    """Regression: a provider put only line one in segment one and three lines in segment two."""
    from app.planning import storyboard_schedule
    words = ["下午茶，给这块蛋糕留个位置。", "推近一点，先分给旁边。", "切面一层层，真的看得清。", "下午茶，就留在这个位置。"]
    schedule = storyboard_schedule(scheduling_script(words), [10, 10])
    assert_schedule_covers_video(schedule, words, [10, 10])
    assert [[shot["narration"] for shot in segment["shots"]] for segment in schedule] == [words[:2], words[2:]]
