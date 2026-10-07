"""Spoken-line checks, candidate selection and the one rewrite gate. Offline."""
import asyncio

import pytest

from app.ad_script import derive_script_evidence, script_stage_schema
from app.line_quality import (
    LINE_REWRITE_SYSTEM, apply_brand_claim_collapse, apply_line_rewrite, brand_stack_remaining,
    collapse_brand_claims, drop_stale_story_lines, line_offenses, line_style, narration_budget_ok,
    offending_lines, prepare_script_lines, prepare_speech_draft, product_terms, rewrite_director_if_needed,
    rewrite_script_if_needed, script_line_offenses, story_explainer_remaining, type_line_score,
    voice_identity_issues,
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
    assert type_line_score("这风不吵。", "brand_tvc", "hook") > type_line_score(
        "肩线这一圈，是它的脸。", "brand_tvc", "hook")
    assert type_line_score("吹完是顺的。", "brand_tvc", "hook") < type_line_score(
        "这风不吵。", "brand_tvc", "hook")
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
        original, {"lines": [{"beat_id": "b0", "narration": "吹完头发顺了。"}]},
        limits=limits, facts_text="蓝瓶", duration_seconds=20, strategy=strategy)
    assert accepted["beats"][0]["narration"] == "吹完头发顺了。"
    stiff = apply_line_rewrite(
        original, {"lines": [{"beat_id": "b0", "narration": "吹完是顺的。"}]},
        limits=limits, facts_text="蓝瓶", duration_seconds=20, strategy=strategy)
    assert stiff is None
    literary = apply_line_rewrite(
        original, {"lines": [{"beat_id": "b0", "narration": "肩线这一圈，是它的脸。"}]},
        limits=limits, facts_text="蓝瓶", duration_seconds=20, strategy=strategy)
    assert literary is None
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
    clean = script_of(beat("几下就干。"))

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
        return {"lines": [{"beat_id": item["beat_id"], "narration": "吹完头发顺了。"}
                          for item in data["lines"]]}

    called, result = asyncio.run(rewrite_script_if_needed(spoken, dirty, strategy, 20))
    assert called is True and result["beats"][0]["narration"] == "吹完头发顺了。"
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


def _focus(script, form, plan=None, duration_seconds=None):
    found = []
    for item in script_line_offenses(script, typed(form), plan=plan, duration_seconds=duration_seconds):
        found.extend(item["offenses"])
    return found


def _lines(*texts):
    return {"beats": [{"id": f"b{index}", "narration": text} for index, text in enumerate(texts)]}


def test_presenter_selling_lines_need_a_subject_and_a_connective():
    bad = _focus(_lines(
        "早上吹头发，你一般要多久？",
        "风大，这几下就吹开了。",
        "你听，我正常说话都听得清。",
        "带负离子，吹完头发顺。",
        "399，链接放下面了。",
    ), "presenter_pitch")
    assert "卖点没有主语" in bad
    assert "卖点之间缺少承接" in bad
    assert "开场问题没有下文" not in bad
    good = _focus(_lines(
        "早上吹头发，你一般要多久？",
        "我用这款吹风机，几下就吹开了。",
        "而且它声音很小，我正常说话都听得清。",
        "而且它带负离子。",
        "399，链接放下面了。",
    ), "presenter_pitch")
    assert good == []
    assert _focus(_lines("看轮廓。", "看桌角。", "留一处。"), "presenter_pitch") == []


def test_thin_brief_lines_flag_mismatch_repetition_filler_and_label_voiceover():
    presenter = _focus(_lines(
        "头发多吹得久？这台徕芬吹风机，吹得快还安静。",
        "你看，它吹得快，而且我说话你还听得清。",
        "它还带负离子，吹完头发顺。",
        "洗完头，吹完就能出门。",
        "你想要的话，点下面链接看看这款。",
    ), "presenter_pitch")
    assert "问句答非所问" in presenter
    assert "一句里塞了两个卖点" in presenter
    assert "卖点重复" in presenter
    assert "空话" in presenter
    brand = _focus(_lines(
        "徕芬高速吹风机，吹得快。",
        "声音不吵，还带负离子。",
        "吹完头发顺。",
        "点下面链接，看看这款。",
    ), "brand_tvc")
    assert "标签式旁白" in brand
    assert "一句里塞了两个卖点" in brand


def test_line_logic_rewrite_failure_keeps_the_original_lines():
    strategy = typed("presenter_pitch")
    script = _lines(
        "头发多吹得久？这台徕芬吹风机，吹得快还安静。",
        "洗完头，吹完就能出门。",
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("rewrite down")

    called, result = asyncio.run(rewrite_script_if_needed(boom, script, strategy, 20))
    assert called is True
    assert result["beats"][0]["narration"] == script["beats"][0]["narration"]
    assert result["beats"][1]["narration"] == script["beats"][1]["narration"]


def test_brand_visual_narration_and_a_bare_name_are_flagged():
    strategy = typed("brand_tvc")
    raw = strategy.model_dump()
    raw["brief"]["product_name"] = "徕芬高速吹风机"
    named_strategy = StrategyRecord.model_validate(raw)
    found = []
    for item in script_line_offenses(_lines(
        "银色吹风机，放在台面上。",
        "风很大，头发干得快。",
        "徕芬高速吹风机。",
    ), named_strategy):
        found.extend(item["offenses"])
    assert found.count("只描述画面") == 3


def test_ungrounded_prices_are_removed_and_confirmed_prices_stay():
    from app.cta import scrub_ungrounded_prices
    assert "399" not in scrub_ungrounded_prices("价格399。想要的话，看看这个。", "吹风机")
    assert "三百九十九" not in scrub_ungrounded_prices("三百九十九，点下面看看。", "吹风机")
    assert "优惠" not in scrub_ungrounded_prices("现在有优惠。", "吹风机")
    kept = scrub_ungrounded_prices("价格399。", "售价399元")
    assert "399" in kept


def test_brand_lines_can_state_the_benefit_without_a_subject():
    visual = _focus(_lines(
        "吹风机，先亮起一道银线。",
        "光扫过去，机身才好看。",
        "风一开，这缕发丝先飘起来。",
        "徕芬，银色的一道线。",
    ), "brand_tvc")
    assert "只描述画面" in visual
    assert "卖点没有主语" not in visual
    assert "卖点之间缺少承接" not in visual
    plain = _focus(_lines("吹完是顺的。", "发尾是顺的。"), "brand_tvc")
    assert plain.count("电报体") == 2
    assert "旁白在堆卖点" not in plain
    terse = _focus(_lines(
        "十万转高速马达，三分钟吹干。",
        "一亿负离子，吹完不毛躁。",
        "机身带负离子护发。",
        "肩线这一圈，是它的脸。",
    ), "brand_tvc")
    assert "旁白像文案" in terse
    assert "旁白在堆卖点" in terse
    assert "卖点没有主语" not in terse
    copied = "肩线这一圈停在桌角这边。"
    overlap = _focus(_lines(copied), "brand_tvc", plan={"segments": [{"shots": [
        {"beat_id": "b0", "description": copied + "侧光停住。"}]}]})
    assert "台词贴着画面" in overlap
    assert "旁白像文案" in overlap


def test_scene_lines_follow_the_character_and_mention_the_product_once():
    bare = _focus(_lines("风大，这几下就吹开了。"), "scene_story")
    assert "卖点没有主语" not in bare
    assert "卖点之间缺少承接" not in bare
    assert "人物没带到商品" in bare
    mixed = _focus(_lines("她走过窗边。", "就坐这儿吧。"), "scene_story")
    assert "人称不统一" in mixed
    assert "只描述画面" in mixed
    assert "人物没带到商品" in mixed
    assert "卖点没有主语" not in mixed
    assert _focus(_lines("这块给你。"), "scene_story") == []
    assert _focus(_lines("又卡在这一步。", "你先按这里。", "嘿，开了。", "那就放这儿吧。"), "scene_story") == []
    assert "人物没带到商品" not in _focus(_lines("看轮廓。", "看桌角。", "留一处。"), "scene_story")
    narrated = _focus(_lines("室友在睡，她拿吹风机。"), "scene_story")
    assert "在念画面" in narrated
    pitch = _focus(_lines("带负离子，吹完是顺的。"), "scene_story")
    assert "口播腔" in pitch
    assert "人物没带到商品" not in pitch
    assert "人物没带到商品" not in _focus(_lines("终于不毛躁了。"), "scene_story")
    assert "口播腔" not in _focus(_lines("终于不毛躁了。"), "scene_story")
    assert "在念画面" not in _focus(_lines("太吵了。", "终于不毛躁了。"), "scene_story")


def test_scene_dialogue_does_not_restate_the_shot():
    """A line that only retells the sleeper already on screen is rewritten once.

    A complaint and the freeze call-to-action stay. If the rewrite fails, this
    helper keeps the original line. The planner then refuses to mark that plan ready.
    """
    from pathlib import Path

    opening = "不敢开，怕吵醒她。"
    script = _lines(opening, "声音也太大了。", "这块给你。", "想带走，就点下面链接。")
    shot = "夜里，室友侧躺在床上睡着。她站在镜前，手指停在开关上。"
    plan = {"segments": [{"shots": [
        {"beat_id": "b0", "description": shot},
        {"beat_id": "b1", "description": "风声起来，她皱了一下眉。"},
        {"beat_id": "b2", "description": "发尾被吹开，她看了一眼。"},
        {"beat_id": "b3", "description": "产品停在台面上定格。"},
    ]}]}
    flags = _focus(script, "scene_story", plan=plan)
    assert flags.count("在念画面") == 1
    assert "在念画面" not in _focus(script, "scene_story")
    assert "在念画面" not in _focus(script, "presenter_pitch", plan=plan)
    quoted = {"segments": [{"shots": [
        {"beat_id": "b0", "description": shot + "她说不敢开，怕吵醒她。"},
    ]}]}
    assert "在念画面" in _focus(_lines(opening), "scene_story", plan=quoted)
    scene_book = Path("app/skills/shared/ad-form-playbooks/scene-story.md").read_text()
    assert "不敢开，怕吵醒她" in scene_book
    assert "定格" in scene_book

    strategy = typed("scene_story")
    director = {"ad_script": script}

    async def rewrite(system, data, originals, max_tokens):
        assert [item["narration"] for item in data["lines"]] == [opening]
        assert "在念画面" in data["lines"][0]["offenses"]
        assert "睡" in data["lines"][0]["picture"]
        return {"lines": [{"beat_id": "b0", "narration": "太响了。"}]}

    called, replacement = asyncio.run(rewrite_director_if_needed(
        rewrite, director, strategy, 20, plan=plan))
    assert called is True and replacement is not None
    assert [beat["narration"] for beat in replacement["ad_script"]["beats"]] == [
        "太响了。", "声音也太大了。", "这块给你。", "想带走，就点下面链接。",
    ]

    async def boom(*args, **kwargs):
        raise RuntimeError("rewrite down")

    called, kept = asyncio.run(rewrite_director_if_needed(
        boom, director, strategy, 20, plan=plan))
    assert called is True and kept is None
    assert director["ad_script"]["beats"][0]["narration"] == opening
    assert story_explainer_remaining(script, strategy, plan=plan) is True
    assert "再轻点" not in LINE_REWRITE_SYSTEM
    assert "风口转开一点" not in LINE_REWRITE_SYSTEM
    assert "声音也太大了" not in LINE_REWRITE_SYSTEM


def test_voice_track_identity_follows_the_ad_type():
    presenter = typed("presenter_pitch")
    brand = typed("brand_tvc")
    story = typed("scene_story")
    voice = {"speech_mode": "voiceover"}
    talk = {"speech_mode": "character_dialogue", "speaker_id": "char_host"}
    assert voice_identity_issues(presenter, voice) == ["口播被写成了旁白"]
    assert voice_identity_issues(presenter, {"speech_mode": "none"}) == ["口播被写成了旁白"]
    assert voice_identity_issues(presenter, talk) == []
    assert voice_identity_issues(brand, talk) == ["产品展示被写成了对口型推销"]
    assert voice_identity_issues(brand, voice) == []
    assert voice_identity_issues(story, voice) == ["剧情被写成了整条旁白"]
    assert voice_identity_issues(story, talk) == []
    assert voice_identity_issues(story, {"speech_mode": "none"}) == []
    held, changed = apply_brand_claim_collapse(
        {"ad_script": _lines("几下就干。", "这风不吵。")}, presenter, (), 15)
    assert changed is False
    assert [beat["narration"] for beat in held["ad_script"]["beats"]] == ["几下就干。", "这风不吵。"]
    third = _lines("她打开了开关。", "终于不毛躁了。")
    assert "第三人称解说" in _focus(third, "scene_story")
    assert "第三人称解说" not in _focus(third, "presenter_pitch")
    assert story_explainer_remaining(third, story) is True
    assert story_explainer_remaining(_lines("太吵了。", "终于不毛躁了。"), story) is False
    assert story_explainer_remaining(
        _lines("想带走，就点下面链接。", "终于不毛躁了。"), story) is False
    assert "再轻点" not in line_style("scene_story")


def test_brand_voiceover_stays_thin_and_does_not_invent_a_scene():
    five = _lines("几下就干。", "再看这一下。", "就这点。", "再看一眼。", "停在这儿。")
    assert "旁白太密" in _focus(five, "brand_tvc")
    assert "旁白太密" in _focus(five, "brand_tvc", duration_seconds=18)
    assert "旁白太密" not in _focus(five, "brand_tvc", duration_seconds=45)
    assert "旁白太密" not in _focus(_lines(
        "十万转高速马达，三分钟吹干。",
        "一亿负离子，吹完不毛躁。",
        "机身带负离子护发。",
        "肩线这一圈，是它的脸。",
    ), "brand_tvc")
    assert "标签式旁白" not in _focus(_lines("吹完头发顺。"), "brand_tvc")
    assert "旁白补了画面外" in _focus(_lines("旁边的人还在睡。", "吹完不毛躁。"), "brand_tvc")
    assert "旁白开成了讲解" in _focus(_lines("头发多，早上要吹多久？", "三分钟就干。"), "brand_tvc")
    showcase = _focus(_lines("开盖的声音，先到了。", "肩线这一圈，是它的脸。", "放回桌角，还是这一只。"), "brand_tvc")
    assert "旁白像文案" in showcase
    assert "旁白补了画面外" not in showcase
    assert "旁白开成了讲解" not in showcase
    assert "旁白太密" not in showcase
    assert "旁白在堆卖点" in _focus(_lines("几下就干。", "这风不吵。"), "brand_tvc")
    assert "旁白在堆卖点" in _focus(_lines("几下就干。", "发尾是顺的。"), "brand_tvc")

    strategy = typed("brand_tvc")

    async def blank(system, data, originals, max_tokens):
        return {"lines": [{"beat_id": item["beat_id"], "narration": "" if "旁白太密" in item["offenses"] else item["narration"]}
                          for item in data["lines"]]}

    called, result = asyncio.run(rewrite_script_if_needed(blank, five, strategy, 20))
    assert called is True
    assert [beat["narration"] for beat in result["beats"]] == [
        "几下就干。", "再看这一下。", "就这点。", "", "",
    ]

    async def boom(*args, **kwargs):
        raise RuntimeError("rewrite down")

    called, kept = asyncio.run(rewrite_script_if_needed(boom, five, strategy, 20))
    assert called is True
    assert kept["beats"][4]["narration"] == "停在这儿。"


def test_a_bare_selling_line_is_rewritten_once_and_the_same_words_stay():
    strategy = typed("presenter_pitch")
    script = script_of(
        beat("风大，这几下就吹开了。", "b0"),
        beat("你听，我正常说话都听得清。", "b1"),
    )
    calls = []

    async def same_words(system, data, originals, max_tokens):
        calls.append(data["lines"])
        return {"lines": [{"beat_id": item["beat_id"], "narration": item["narration"]} for item in data["lines"]]}

    called, result = asyncio.run(rewrite_script_if_needed(same_words, script, strategy, 20))
    assert called is True and len(calls) == 1
    assert result["beats"][0]["narration"] == "风大，这几下就吹开了。"
    assert result["beats"][1]["narration"] == "你听，我正常说话都听得清。"


def test_skills_and_replan_show_the_per_type_line_focus_rule(capsys):
    from pathlib import Path
    from scripts.replan_offline import _print_report, _script_lines

    root = Path("app/skills")
    presenter = (root / "shared/ad-form-playbooks/presenter-pitch.md").read_text()
    brand = (root / "shared/ad-form-playbooks/brand-tvc.md").read_text()
    scene = (root / "shared/ad-form-playbooks/scene-story.md").read_text()
    examples = (root / "ads-video-dialogue-writing/references/ad-dialogue-examples.md").read_text()
    review = (root / "ads-video-plan-review/references/plan-review.md").read_text()
    assert "风大，这几下就吹开了" in presenter and "我用这款吹风机，几下就吹开了" in presenter
    assert "十万转高速马达，三分钟吹干" in brand and "不必" in brand
    assert "至少" in scene and "不套" in scene
    assert "说出口的对照" in examples
    assert "一亿负离子，吹完不毛躁" in examples
    assert "台词是否对焦商品、前后连贯" in review and "line_focus" in review
    plan = {"ad_script": {"beats": [
        {"id": "a", "narration": "吹风机，先亮起一道银线。"},
        {"id": "b", "narration": "十万转高速马达，三分钟吹干。"},
    ]}, "segments": [{"speech_spans": [
        {"beat_id": "a", "start": 0, "end": 2},
        {"beat_id": "b", "start": 2, "end": 4},
    ], "shots": []}]}
    rows = _script_lines(plan, typed("brand_tvc"))
    assert rows[0]["flags"] == ["只描述画面"]
    assert rows[1]["flags"] == ["旁白像文案"]
    _print_report({
        "job_id": "job", "direction_id": "direction", "lines": rows, "shots": [],
        "review": {"ready": True, "skipped": True, "suggestions": []}, "notes": [], "prompt": "",
    })
    printed = capsys.readouterr().out
    assert "只描述画面" in printed and "旁白像文案" in printed


def test_stacked_brand_voiceover_keeps_one_benefit_and_the_call_to_action():
    strategy = typed("brand_tvc")
    live = _lines(
        "夜里吹，几下就干，这风不吵。",
        "带负离子，吹完是顺的。",
        "点下面链接，把它看清楚。",
    )
    assert brand_stack_remaining(live, strategy) is True
    assert "旁白在堆卖点" in _focus(live, "brand_tvc")
    assert "一句里塞了两个卖点" in _focus(live, "brand_tvc")
    collapsed, changed = collapse_brand_claims(live)
    assert changed is True
    assert [beat["narration"] for beat in collapsed["beats"]] == [
        "夜里吹，几下就干。", "", "点下面链接，把它看清楚。",
    ]
    assert brand_stack_remaining(collapsed, strategy) is False
    story = typed("scene_story")
    scene = _lines("太吵了。", "终于不毛躁了。")
    assert brand_stack_remaining(scene, story) is False
    pitch = typed("presenter_pitch")

    async def keep_pitch(*args, **kwargs):
        raise RuntimeError("rewrite down")

    _, pitch_result = asyncio.run(rewrite_script_if_needed(
        keep_pitch, _lines("几下就干。", "这风不吵。"), pitch, 20))
    assert [beat["narration"] for beat in pitch_result["beats"]] == ["几下就干。", "这风不吵。"]

    director = {"title": "夜里轻响", "ad_script": live, "shots": [
        {"id": "shot_0", "description": "夜里吹，几下就干，这风不吵。产品停在桌面。"},
        {"id": "shot_1", "description": "带负离子，吹完是顺的。发丝落下。"},
    ]}

    async def boom(*args, **kwargs):
        raise RuntimeError("rewrite down")

    called, replacement = asyncio.run(rewrite_director_if_needed(boom, director, strategy, 20))
    assert called is True and replacement is not None
    assert [beat["narration"] for beat in replacement["ad_script"]["beats"]] == [
        "夜里吹，几下就干。", "", "点下面链接，把它看清楚。",
    ]
    assert "这风不吵" not in replacement["shots"][0]["description"]
    assert "几下就干" in replacement["shots"][0]["description"]
    assert "带负离子" not in replacement["shots"][1]["description"]
    assert brand_stack_remaining(replacement["ad_script"], strategy) is False
    locked = "带负离子，吹完是顺的。"
    held, held_changed = collapse_brand_claims(live, [locked])
    assert held_changed is True
    assert held["beats"][1]["narration"] == locked
    assert held["beats"][0]["narration"] == "夜里吹，几下就干。"
    from pathlib import Path
    scene_book = Path("app/skills/shared/ad-form-playbooks/scene-story.md").read_text()
    sound = Path("app/skills/ads-video-sound-direction/SKILL.md").read_text()
    assert "出镜人物开口说" in scene_book
    assert "不要整条都写成画外音" in scene_book
    assert "剧情可以用旁白" not in sound
    assert "character_dialogue" in sound and "定格那一句" in sound


def test_brand_claim_capacity_follows_duration_and_the_whole_piece():
    strategy = typed("brand_tvc")
    live = _lines(
        "夜里吹，几下就干。",
        "这风不吵。",
        "带负离子。",
        "点下面链接，把它看清楚。",
    )
    short, _ = collapse_brand_claims(live, duration_seconds=15)
    assert [beat["narration"] for beat in short["beats"]] == [
        "夜里吹，几下就干。", "", "", "点下面链接，把它看清楚。",
    ]
    assert brand_stack_remaining(live, strategy, duration_seconds=15) is True
    assert brand_stack_remaining(short, strategy, duration_seconds=15) is False
    medium, _ = collapse_brand_claims(live, duration_seconds=30)
    assert [beat["narration"] for beat in medium["beats"]] == [
        "夜里吹，几下就干。", "这风不吵。", "", "点下面链接，把它看清楚。",
    ]
    assert brand_stack_remaining(medium, strategy, duration_seconds=30) is False
    assert "旁白在堆卖点" in _focus(live, "brand_tvc", duration_seconds=30)
    assert "旁白在堆卖点" not in _focus(medium, "brand_tvc", duration_seconds=30)
    long, _ = collapse_brand_claims(live, duration_seconds=45)
    assert [beat["narration"] for beat in long["beats"]] == [
        "夜里吹，几下就干。", "这风不吵。", "带负离子。", "点下面链接，把它看清楚。",
    ]
    crowded = _lines("几下就干。", "这风不吵。", "带负离子。", "机身很轻。")
    capped, _ = collapse_brand_claims(crowded, duration_seconds=60)
    assert [beat["narration"] for beat in capped["beats"]] == ["几下就干。", "这风不吵。", "带负离子。", ""]
    assert "一句里塞了两个卖点" in _focus(
        _lines("几下就干，这风不吵。"), "brand_tvc", duration_seconds=45)
    pitch = typed("presenter_pitch")

    async def keep_pitch(*args, **kwargs):
        raise RuntimeError("rewrite down")

    _, pitch_result = asyncio.run(rewrite_script_if_needed(
        keep_pitch, _lines("几下就干。", "这风不吵。", "带负离子。"), pitch, 15))
    assert [beat["narration"] for beat in pitch_result["beats"]] == [
        "几下就干。", "这风不吵。", "带负离子。",
    ]

    director = {
        "title": "夜里轻响",
        "video_flow": "先说干得快，再演示不吵和负离子。",
        "ad_script": {
            "core_message": "干得快，也不吵，还带负离子。",
            "beats": live["beats"],
        },
        "shots": [
            {"id": "shot_0", "coverage": [{"beat_id": "b0", "weight": 1}],
             "description": "几下就干。旁边还在演示不吵。",
             "purpose": "看见干得快", "visual_motif": "负离子"},
            {"id": "shot_1", "coverage": [{"beat_id": "b1", "weight": 1}],
             "description": "这风不吵。旁白说这句话时画面基本停住。",
             "purpose": "听见安静", "visual_motif": "安静"},
            {"id": "shot_2", "coverage": [{"beat_id": "b2", "weight": 1}],
             "description": "带负离子的光扫过。",
             "purpose": "看见负离子", "visual_motif": "负离子"},
        ],
    }

    async def boom(*args, **kwargs):
        raise RuntimeError("rewrite down")

    called, replacement = asyncio.run(rewrite_director_if_needed(boom, director, strategy, 15))
    assert called is True and replacement is not None
    assert replacement["shots"][0]["description"] == "几下就干。"
    assert "不吵" not in replacement["shots"][0]["description"]
    assert replacement["shots"][0]["visual_motif"] == ""
    assert "旁白说这句话" not in replacement["shots"][1]["description"]
    assert "不吵" not in replacement["shots"][1]["description"]
    assert "负离子" not in replacement["shots"][2]["description"]
    assert "负离子" not in replacement["ad_script"]["core_message"]
    assert "不吵" not in replacement["video_flow"] and "负离子" not in replacement["video_flow"]
    longer, changed = apply_brand_claim_collapse(director, strategy, (), 30)
    assert changed is True
    assert longer["ad_script"]["beats"][1]["narration"] == "这风不吵。"
    assert longer["shots"][1]["description"].startswith("这风不吵")
    assert "负离子" not in longer["shots"][2]["description"]
    assert longer["shots"][0]["visual_motif"] == ""
    assert longer["shots"][1]["visual_motif"] == ""


def test_blanked_voiceover_pointer_is_scrubbed_from_the_shot():
    strategy = typed("brand_tvc")
    director = {"ad_script": _lines("几下就干。", "带负离子。"), "shots": [
        {"id": "shot_0", "coverage": [{"beat_id": "b0", "weight": 1}],
         "description": "镜头快速推近，风口对着发尾。"},
        {"id": "shot_1", "coverage": [{"beat_id": "b1", "weight": 1}],
         "description": "旁白说这句话时画面基本停住。"},
    ]}
    folded, changed = apply_brand_claim_collapse(director, strategy, (), 15)
    assert changed is True
    assert folded["ad_script"]["beats"][1]["narration"] == ""
    assert "旁白说这句话" not in folded["shots"][1]["description"]
    assert folded["shots"][1]["description"] == "这一拍不说话，画面停住。"
    assert "快速推近" in folded["shots"][0]["description"]
    assert folded["shots"][0]["description"].endswith("风口对着发尾。")


def _cake_story(*lines, shots):
    return {
        "ad_script": _lines(*lines),
        "shots": shots,
    }


def test_a_repeated_cut_line_is_blanked_after_the_cut_has_happened():
    strategy = typed("scene_story")
    director = _cake_story(
        "第一刀归你。", "", "蛋糕第一刀，你来。", "再尝一口。",
        shots=[
            {"id": "s1", "coverage": [{"beat_id": "b0", "weight": 1}],
             "description": "她把刀递过去。",
             "continuity": {"end_state": "蛋糕还是完整的", "allowed_changes": ""}},
            {"id": "s2", "coverage": [{"beat_id": "b1", "weight": 1}],
             "description": "她已经把蛋糕切开。",
             "continuity": {"end_state": "切开的蛋糕", "allowed_changes": "第一刀已经切下"}},
            {"id": "s3", "coverage": [{"beat_id": "b2", "weight": 1}],
             "description": "她看着蛋糕说「蛋糕第一刀，你来。」",
             "continuity": {"end_state": "盘里还有蛋糕", "allowed_changes": ""}},
            {"id": "s4", "coverage": [{"beat_id": "b3", "weight": 1}],
             "description": "她叉起一块。",
             "continuity": {"end_state": "盘里还有蛋糕", "allowed_changes": ""}},
        ],
    )
    dropped, changed = drop_stale_story_lines(director, strategy)
    assert changed is True
    narrations = [beat["narration"] for beat in dropped["ad_script"]["beats"]]
    assert narrations[0] == "第一刀归你。"
    assert narrations[2] == ""
    assert narrations[3] == "再尝一口。"
    assert "蛋糕第一刀" not in dropped["shots"][2]["description"]
    again, twice = drop_stale_story_lines(dropped, strategy)
    assert twice is False
    assert [beat["narration"] for beat in again["ad_script"]["beats"]] == narrations


def test_an_invitation_after_the_action_is_blanked_without_a_matching_earlier_line():
    strategy = typed("scene_story")
    director = _cake_story(
        "先坐下。", "你来切。", "再尝一口。",
        shots=[
            {"id": "s1", "coverage": [{"beat_id": "b0", "weight": 1}],
             "description": "她拉开椅子。", "continuity": {"end_state": "人已经坐下"}},
            {"id": "s2", "coverage": [{"beat_id": "b1", "weight": 1}],
             "description": "蛋糕已经切开。", "continuity": {"end_state": "切开的蛋糕"}},
            {"id": "s3", "coverage": [{"beat_id": "b2", "weight": 1}],
             "description": "她叉起一块。", "continuity": {"end_state": "盘里还有蛋糕"}},
        ],
    )
    # The cut is on the same shot as 「你来切。」, so that line is not yet late.
    same_shot, changed = drop_stale_story_lines(director, strategy)
    assert changed is False
    assert same_shot["ad_script"]["beats"][1]["narration"] == "你来切。"
    late = _cake_story(
        "先坐下。", "", "你来切。",
        shots=[
            {"id": "s1", "coverage": [{"beat_id": "b0", "weight": 1}],
             "description": "她拉开椅子。", "continuity": {"end_state": "人已经坐下"}},
            {"id": "s2", "coverage": [{"beat_id": "b1", "weight": 1}],
             "description": "蛋糕已经切开。", "continuity": {"end_state": "切开的蛋糕"}},
            {"id": "s3", "coverage": [{"beat_id": "b2", "weight": 1}],
             "description": "她把叉子递过来。", "continuity": {"end_state": "叉子在手里"}},
        ],
    )
    dropped, changed = drop_stale_story_lines(late, strategy)
    assert changed is True
    assert [beat["narration"] for beat in dropped["ad_script"]["beats"]] == ["先坐下。", "", ""]


def test_a_locked_line_and_the_only_spoken_line_stay():
    strategy = typed("scene_story")
    locked = _cake_story(
        "第一刀归你。", "蛋糕第一刀，你来。",
        shots=[
            {"id": "s1", "coverage": [{"beat_id": "b0", "weight": 1}],
             "description": "她把刀递过去。", "continuity": {"end_state": "蛋糕还是完整的"}},
            {"id": "s2", "coverage": [{"beat_id": "b1", "weight": 1}],
             "description": "蛋糕已经切开。", "continuity": {"end_state": "切开的蛋糕"}},
        ],
    )
    kept, changed = drop_stale_story_lines(locked, strategy, ["蛋糕第一刀，你来。"])
    assert changed is False
    assert kept["ad_script"]["beats"][1]["narration"] == "蛋糕第一刀，你来。"
    only = _cake_story(
        "蛋糕第一刀，你来。",
        shots=[
            {"id": "s1", "coverage": [{"beat_id": "b0", "weight": 1}],
             "description": "蛋糕已经切开。", "continuity": {"end_state": "切开的蛋糕"}},
        ],
    )
    _same, only_changed = drop_stale_story_lines(only, strategy)
    assert only_changed is False
    pitch = typed("presenter_pitch")
    _untouched, pitch_changed = drop_stale_story_lines(locked, pitch)
    assert pitch_changed is False


def test_a_generic_shared_phrase_is_not_treated_as_a_repeat():
    strategy = typed("scene_story")
    director = _cake_story(
        "今天下午茶。", "下午茶见。",
        shots=[
            {"id": "s1", "coverage": [{"beat_id": "b0", "weight": 1}], "description": "杯子放上桌。"},
            {"id": "s2", "coverage": [{"beat_id": "b1", "weight": 1}], "description": "她坐下。"},
        ],
    )
    _same, changed = drop_stale_story_lines(director, strategy)
    assert changed is False
