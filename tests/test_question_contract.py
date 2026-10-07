"""Free-form answers use the composer; options must be actual choices."""
import pytest

from app.schemas import Plan, Question


def test_question_filters_typing_actions_without_inventing_answers():
    q = Question.model_validate({"id": "facts", "prompt": "你希望突出什么？", "options": [
        {"id": "looks", "label": "只展示图片里可见的外观"},
        {"id": "custom", "label": "我来补充可核实的卖点（自填）"},
        {"id": "ai", "label": "让AI决定展示方式"},
    ]})
    assert [o.id for o in q.options] == ["looks", "ai"]
    assert q.allow_custom
    only_text = q.model_copy(update={"options": []})
    assert Question.model_validate(only_text.model_dump()).options == []


def test_legitimate_choices_about_diy_products_are_not_removed():
    q = Question.model_validate({"id": "people", "prompt": "目标人群？", "options": [
        {"id": "diy", "label": "喜欢自己做饭的人"}, {"id": "gift", "label": "送礼人群"},
    ]})
    assert len(q.options) == 2


def test_long_script_must_be_repaired_before_plan_can_be_accepted():
    value = {"title": "商品展示", "script": "长" * 59, "facts": {"name": "杯子", "claims": []}, "segments": [
        {"id": f"s{i}", "duration_seconds": 10, "purpose": "展示", "prompt": "慢慢展示商品", "shots": [
            {"start": 0, "end": 10, "description": f"商品特写第{i + 1}处"}], "reference_asset_ids": ["test-original"]} for i in range(2)]}
    with pytest.raises(ValueError, match="朗读长度"):
        Plan.model_validate(value)
    repair_candidate = Plan.model_validate(value, context={"allow_script_repair": True})
    value["script"] = "近看杯身细节，光影随着角度变化。"
    assert Plan.model_validate(value).script == value["script"]
    assert repair_candidate.script == "长" * 59


def shot_bound_plan():
    words = ["杯身轮廓很简洁。", "近看表面的纹理。", ""]
    return {"title": "商品展示", "audio_strategy": "native", "script": "".join(words),
        "facts": {"name": "杯子", "claims": []}, "segments": [{"id": "s1", "duration_seconds": 10,
        "purpose": "展示外观", "prompt": "忠实商品外观", "reference_asset_ids": ["original"],
        "narration": "".join(words), "shots": [
            {"start": 0, "end": 4, "description": "完整杯身", "narration": words[0]},
            {"start": 4, "end": 8, "description": "杯面纹理近景", "narration": words[1]},
            {"start": 8, "end": 10, "description": "自然收束", "narration": words[2]}]}]}


def test_native_words_are_bound_to_their_shot_time_not_only_total_duration():
    value = shot_bound_plan()
    assert Plan.model_validate(value, context={"require_shot_narration": True})
    value["segments"][0]["shots"][0]["narration"] = value["script"]
    value["segments"][0]["shots"][1]["narration"] = ""
    # Total and segment length still fit, but the first four seconds do not.
    with pytest.raises(ValueError, match="镜头台词超过"):
        Plan.model_validate(value)


def test_native_shot_edit_cannot_leave_stale_segment_or_full_script():
    value = shot_bound_plan()
    value["segments"][0]["shots"][0]["narration"] = "看看杯身。"
    with pytest.raises(ValueError, match="对应镜头台词一致"):
        Plan.model_validate(value)


def test_historical_native_plan_remains_readable_but_new_planning_requires_shots():
    value = shot_bound_plan()
    for shot in value["segments"][0]["shots"]:
        shot.pop("narration")
    assert Plan.model_validate(value).script == value["script"]
    with pytest.raises(ValueError, match="对应镜头台词一致"):
        Plan.model_validate(value, context={"require_shot_narration": True})
