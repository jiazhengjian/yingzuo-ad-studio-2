"""Spoken-line checks, candidate selection and the one rewrite gate. Offline."""
import asyncio

import pytest

from app.ad_script import derive_script_evidence, script_stage_schema
from app.line_quality import (
    apply_line_rewrite, line_offenses, line_style, narration_budget_ok,
    offending_lines, prepare_script_lines, prepare_speech_draft, product_terms,
    rewrite_script_if_needed, type_line_score,
)
from app.runtime_skills import stage_skill_context
from app.strategy import StrategyRecord
from test_strategy import make_strategy


def typed(form, idea="肩线是这只瓶子的脸"):
    strategy = make_strategy()
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = form
    selected = next(item for item in raw["directions"] if item["id"] == raw["selected_direction_id"])
    selected["big_idea"] = idea
    selected["hook"] = "先看见肩线"
    return StrategyRecord.model_validate(raw)


def beat(narration, beat_id="b0", evidence=None):
    return {"id": beat_id, "purpose": "让人看见肩线", "message": "肩线可以被认出",
            "visual_role": "瓶肩停在桌角", "narration": narration,
            "evidence_ids": ["f1"] if evidence is None else evidence}


def script_of(*beats):
    return {"core_message": "记住这只瓶子的肩线", "audience_takeaway": "桌角还是这一只",
            "product_role": "肩线承担辨认", "beats": list(beats)}


@pytest.mark.parametrize("text", [
    "看轮廓。", "看桌角。", "留一处。", "蓝瓶和白桌。", "第一次。", "最后。", "第一口。",
    "就放在手边。", "你按这边。",
])
def test_ordinary_speech_is_not_planner_jargon(text):
    assert line_offenses(text) == []


@pytest.mark.parametrize(("text", "name"), [
    ("采用等离子技术，关注秀发护理。", "关注"),
    ("呈现产品质感。", "呈现"),
    ("兼顾下午茶氛围。", "兼顾"),
    ("打造分享场景。", "打造"),
    ("采用等离子技术。", "采用技术"),
    ("这是高端质感。", "高端质感"),
    ("赋能日常收纳。", "赋能"),
    ("满足你对便携的关注。", "满足你对"),
    ("今天讲解核心卖点。", "卖点"),
    ("银灰与紫红，亮相高阶审美。", "颜色罗列"),
    ("这是最好的瓶子。", "最好"),
    ("销量第一。", "第一"),
    ("可以根治。", "根治"),
    ("现在下单。", "购买引导"),
])
def test_checker_names_planner_spec_and_absolute_language(text, name):
    assert name in line_offenses(text)


def test_purchase_stays_banned_when_a_spoken_closer_is_allowed(monkeypatch):
    monkeypatch.setenv("AD_TAGLINE_ENABLED", "true")
    assert line_offenses("现在下单。", tagline_enabled=True) == ["购买引导"]
    assert line_offenses("就放在手边。", tagline_enabled=True) == []
    monkeypatch.setenv("AD_TAGLINE_ENABLED", "false")
    assert "购买引导" in line_offenses("限时优惠。", tagline_enabled=False)


def _dryer():
    strategy = typed("presenter_pitch")
    raw = strategy.model_dump()
    raw["brief"]["product_name"] = "徕芬吹风机"
    return StrategyRecord.model_validate(raw)


def test_opening_without_a_product_is_unclear_and_a_spoken_closer_can_point():
    strategy = _dryer()
    terms = product_terms(strategy)
    assert "开场指代不清" in line_offenses("转这么快，我说话你听得到吧", role="hook", product_terms_list=terms)
    assert line_offenses("吹头发的时候跟你说话，不用喊。", role="hook", product_terms_list=terms) == []
    assert line_offenses("想要的话，点下面看看。", role="closing") == []
    assert "购买引导" in line_offenses("赶紧下单")
    assert "购买引导" in line_offenses("限时优惠。", facts_text="高速马达")
    assert line_offenses("限时优惠。", facts_text="现在有限时优惠") == []
    script = script_of(beat("转这么快，我说话你听得到吧"))
    assert any("开场指代不清" in item["offenses"] for item in offending_lines(script, strategy=strategy))
    assert type_line_score("想要的话，点下面看看。", "presenter_pitch", "closing") >= type_line_score(
        "想要的话，点下面看看。", "presenter_pitch", "line") + 3


def test_scores_follow_the_ad_type():
    assert "感官" in line_style("brand_tvc")
    assert "来有回" in line_style("scene_story")
    assert "钩子" in line_style("presenter_pitch")
    assert type_line_score("肩线这一圈，是它的脸。", "brand_tvc", "hook") > type_line_score(
        "采用圆肩工艺，呈现高端质感。", "brand_tvc", "hook")
    assert type_line_score("你按这边。", "scene_story", "line") > type_line_score(
        "打造分享场景。", "scene_story", "line")
    assert type_line_score("包太满？折一下。", "presenter_pitch", "hook") > type_line_score(
        "今天讲解核心卖点。", "presenter_pitch", "hook")


def test_variant_selection_keeps_the_spoken_line_inside_the_type_budget():
    strategy = typed("brand_tvc")
    raw = script_of(
        beat("采用圆肩工艺，呈现高端质感。"),
        beat("放回桌角。", "b1"),
    )
    raw["key_line_options"] = {
        "hook": {"beat_id": "b0", "options": ["肩线这一圈，是它的脸。", "关注瓶身。"]},
        "closing": {"beat_id": "b1", "options": ["放回桌角。还是这一只。", "高端质感。"]},
    }
    chosen = prepare_script_lines(raw, strategy, 20)
    assert "key_line_options" not in chosen
    assert chosen["beats"][0]["narration"] == "肩线这一圈，是它的脸。"
    assert chosen["beats"][1]["narration"] == "放回桌角。还是这一只。"
    assert narration_budget_ok(chosen, strategy, 20)
    derived = derive_script_evidence(raw, strategy, duration_seconds=20)
    assert derived["beats"][0]["narration"] == "肩线这一圈，是它的脸。"
    assert "key_line_options" not in derived


def test_selection_keeps_locked_over_budget_and_unfinished_lines():
    strategy = typed("brand_tvc")
    locked = "采用圆肩工艺，呈现高端质感。"
    raw = script_of(beat(locked), beat("字" * 20 + "。", "b1"))
    raw["key_line_options"] = {"hook": {"beat_id": "b0", "options": [
        "肩线这一圈，是它的脸。", "肩线这一圈，"]}}
    chosen = prepare_script_lines(raw, strategy, 10, locked_lines=[locked])
    assert chosen["beats"][0]["narration"] == locked
    open_raw = script_of(beat("看。"), beat("字" * 20 + "。", "b1"))
    open_raw["key_line_options"] = {"hook": {"beat_id": "b0", "options": [
        "肩线这一圈，是它的脸。", "肩线这一圈，"]}}
    # 11 characters plus the 21-character neighbour exceed a 10-second brand budget of 30.
    assert prepare_script_lines(open_raw, strategy, 10)["beats"][0]["narration"] == "看。"
    mismatched = script_of(beat("看。"), beat("放回桌角。", "b1"))
    mismatched["key_line_options"] = {"hook": {"beat_id": "other", "options": ["肩线这一圈，是它的脸。", "再看一圈。"]}}
    assert prepare_script_lines(mismatched, strategy, 20)["beats"][0]["narration"] == "看。"


def test_one_spoken_beat_does_not_also_take_a_closing_candidate():
    strategy = typed("presenter_pitch", idea="包太满就折一下")
    raw = script_of(beat("今天讲解核心卖点。"))
    raw["key_line_options"] = {
        "hook": {"beat_id": "b0", "options": ["包太满？折一下。", "关注便携。"]},
        "closing": {"beat_id": "b0", "options": ["现在下单。", "限时优惠。"]},
    }
    chosen = prepare_script_lines(raw, strategy, 20)
    assert chosen["beats"][0]["narration"] == "包太满？折一下。"


def test_voiceover_draft_picks_hook_and_closing_inside_one_narration():
    strategy = typed("brand_tvc")
    raw = {"narration": "采用圆肩工艺，呈现高端质感。放回桌角。",
           "hook_options": ["肩线这一圈，是它的脸。", "关注瓶身。"],
           "closing_options": ["放回桌角。还是这一只。", "高端质感。"]}
    chosen = prepare_speech_draft(raw, strategy, 20)
    assert "hook_options" not in chosen and "closing_options" not in chosen
    assert chosen["narration"] == "肩线这一圈，是它的脸。放回桌角。还是这一只。"
    locked = prepare_speech_draft(
        {"narration": "采用圆肩工艺，呈现高端质感。", "hook_options": ["肩线这一圈，是它的脸。", "再看一圈。"]},
        strategy, 20, locked_lines=["采用圆肩工艺，呈现高端质感。"])
    assert locked["narration"] == "采用圆肩工艺，呈现高端质感。"


def test_brand_budget_is_tighter_than_a_spec_sheet_line():
    strategy = typed("brand_tvc")
    assert narration_budget_ok(script_of(beat("肩线这一圈，是它的脸。")), strategy, 10)
    assert not narration_budget_ok(script_of(beat("字" * 31 + "。")), strategy, 20)


def test_rewrite_accepts_only_a_complete_clean_set():
    strategy = typed("brand_tvc")
    original = script_of(beat("关注秀发护理。"))
    limits = {"b0": 30}
    assert apply_line_rewrite(original, {"lines": [{"beat_id": "b0", "narration": "美白见效。"}]},
                              limits=limits, facts_text="蓝瓶") is None
    assert apply_line_rewrite(original, {"lines": [{"beat_id": "b0", "narration": "3档风力。"}]},
                              limits=limits, facts_text="蓝瓶") is None
    assert apply_line_rewrite(original, {"lines": [{"beat_id": "b0", "narration": "呈现质感。"}]},
                              limits=limits, facts_text="蓝瓶") is None
    assert apply_line_rewrite(original, {"lines": []}, limits=limits, facts_text="蓝瓶") is None
    assert original["beats"][0]["narration"] == "关注秀发护理。"
    accepted = apply_line_rewrite(
        original, {"lines": [{"beat_id": "b0", "narration": "肩线这一圈。"}]},
        limits=limits, facts_text="蓝瓶", duration_seconds=20, strategy=strategy)
    assert accepted["beats"][0]["narration"] == "肩线这一圈。"
    too_long = apply_line_rewrite(
        original, {"lines": [{"beat_id": "b0", "narration": "肩" * 31 + "。"}]},
        limits={"b0": 40}, facts_text="蓝瓶", duration_seconds=20, strategy=strategy)
    assert too_long is None
    kept = apply_line_rewrite(
        original, {"lines": [{"beat_id": "b0", "narration": "3档风。"}]},
        limits=limits, facts_text="资料写明3档")
    assert kept["beats"][0]["narration"] == "3档风。"


def test_clean_script_does_not_call_and_a_bad_rewrite_keeps_the_original():
    strategy = typed("brand_tvc")
    clean = script_of(beat("肩线这一圈，是它的脸。"))

    async def forbidden(*args):
        raise AssertionError("clean script called the model")

    called, result = asyncio.run(rewrite_script_if_needed(forbidden, clean, strategy, 20))
    assert called is False and result is clean
    calls = []

    async def jargon(system, data, originals, max_tokens):
        calls.append(data)
        return {"lines": [{"beat_id": "b0", "narration": "继续关注。"}]}

    dirty = script_of(beat("关注秀发护理。"))
    called, result = asyncio.run(rewrite_script_if_needed(jargon, dirty, strategy, 20))
    assert called is True and calls and result["beats"][0]["narration"] == "关注秀发护理。"
    assert calls[0]["task_kind"] == "line_jargon_rewrite"
    assert calls[0]["stage"] == "ad_script"
    assert calls[0]["lines"][0]["max_characters"] <= 30

    async def spoken(system, data, originals, max_tokens):
        return {"lines": [{"beat_id": item["beat_id"], "narration": "肩线这一圈，是它的脸。"}
                          for item in data["lines"]]}

    called, result = asyncio.run(rewrite_script_if_needed(spoken, dirty, strategy, 20))
    assert called is True and result["beats"][0]["narration"] == "肩线这一圈，是它的脸。"
    locked = "关注秀发护理。"
    assert offending_lines(script_of(beat(locked)), locked_lines=[locked]) == []


def test_key_line_schema_is_optional_and_rewrite_loads_only_dialogue():
    plain = script_stage_schema()
    assert "key_line_options" not in plain["properties"]
    asked = script_stage_schema(max_beat_characters=30, key_lines=True)
    assert "key_line_options" in asked["properties"]
    assert "key_line_options" not in asked["required"]
    assert "key_line_options" not in script_stage_schema(review=True)["properties"]
    loaded = stage_skill_context("line_rewrite")
    assert [item["id"] for item in loaded["skills"]] == ["ads-video-dialogue-writing"]
    assert loaded["skills"][0]["loaded_sections"] == ["SKILL.md", "references/ad-dialogue-examples.md"]
    assert loaded["shared_references"] == []
    assert "说出口的对照" in loaded["skills"][0]["content"]
    assert "speech-timing.md" not in loaded["skills"][0]["loaded_sections"]
