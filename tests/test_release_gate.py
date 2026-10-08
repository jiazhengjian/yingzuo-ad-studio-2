"""One release decision, the hard-gate table, locked lines, and pre-spend confirmation."""
import asyncio
import json
from copy import deepcopy

import pytest

from app.config import Settings
from app.db import Store
from app.pipeline import Pipeline
from app.merchant_cards import SYSTEM_CARD
from app.release_gate import (
    RELEASE_TABLE, SPEND_RECHECK, MerchantLineChoice, assert_spend_confirmed, choose_hard_gate_release,
    classify_release, conservative_usable_plan, duration_needed, duration_question, evaluate_release_table,
    images_blocked_reason, lengthen_target, line_locks, present_checked_plan, record_auto_release,
    settle_before_spend, settle_manual_edit, stamp_honest_label,
)
from app.strategy import StrategyRecord
from test_line_quality import typed
from test_pipeline import PNG
from test_progressive_planning import chosen_strategy


HARD_IDS = (
    "story_selling_line", "story_looks_at_camera", "story_hero_freeze",
    "story_presents_product", "story_logo_to_camera", "story_slogan_or_link",
    "voice_identity", "brand_claim_stack",
)


def _strategy(form):
    return typed(form)


def _board(strategy, lines, descriptions, mode="voiceover"):
    beats = [{"id": f"b{index}", "narration": line, "purpose": "看清商品", "message": "看清外观",
              "visual_role": "商品留在画面里"} for index, line in enumerate(lines)]
    return {
        "strategy": strategy.model_dump(),
        "production": {"speech_mode": mode, "speaker_id": "", "characters": []},
        "ad_script": {"core_message": "看清商品", "audience_takeaway": "记住商品", "beats": beats},
        "segments": [{
            "id": "seg_1", "duration_seconds": 15,
            "shots": [{"id": f"s{index}", "description": text} for index, text in enumerate(descriptions)],
            "speech_spans": [{"id": f"sp{index}", "text": line} for index, line in enumerate(lines) if line],
        }],
    }


def test_hard_gate_table_keeps_eight_blocking_checks_and_names_ad_types():
    by_id = {gate.id: gate for gate in RELEASE_TABLE}
    assert list(by_id) == [*HARD_IDS, "degenerate_plan"]
    for gate_id in HARD_IDS:
        assert by_id[gate_id].kind == "hard"
    assert by_id["degenerate_plan"].kind == "structure"
    assert by_id["story_selling_line"].applies_to == ("scene_story",)
    assert by_id["story_looks_at_camera"].applies_to == ("scene_story",)
    assert by_id["voice_identity"].applies_to == ("presenter_pitch", "brand_tvc", "scene_story")
    assert by_id["brand_claim_stack"].applies_to == ("brand_tvc",)
    assert by_id["degenerate_plan"].applies_to == ("presenter_pitch", "brand_tvc", "scene_story")


def test_missing_review_record_is_not_released_and_fallback_must_pass_the_table():
    assert images_blocked_reason({"segments": []})
    # A stored pass with no concrete ad type is not a release. The label is ignored.
    assert images_blocked_reason({"review_status": "passed"})
    clean = {"review_status": "conservative_fallback", "segments": [
        {"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    assert images_blocked_reason(clean, project={"ad_form": "brand_tvc"})
    assert images_blocked_reason(clean)
    placeholder = {"review_status": "conservative_fallback",
                   "ad_script": {"beats": [{"narration": "看这里。"}, {"narration": "就是这个。"}]},
                   "shots": [{"description": "商品放在画面中间。"}, {"description": "包装放在画面中间。"}]}
    assert images_blocked_reason(placeholder, project={"ad_form": "brand_tvc"})
    assert images_blocked_reason({"review_status": "not_ready"})


def test_unlabeled_passing_board_gets_an_honest_label_and_a_failing_one_does_not_ship():
    """Legacy, salvage and recovery boards used to leave with no label or a false fallback."""
    plain = {"segments": [{"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    missing = deepcopy(plain)
    assert stamp_honest_label(missing) == "not_ready"
    assert images_blocked_reason(missing)
    labelled = stamp_honest_label(plain, project={"ad_form": "brand_tvc"})
    assert labelled is None
    assert images_blocked_reason(plain, project={"ad_form": "brand_tvc"}) is None

    strategy = chosen_strategy({"id": "original-product"})
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    strategy = StrategyRecord.model_validate(raw)
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    failing = _board(strategy, [stacked, "看清这件。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    shown = present_checked_plan(
        failing, strategy, duration=15, generation_strategy="continuity_v1",
        asset_ids=["original-product"], note="策划回复没有收完，硬门已经核对过上一稿。这条已记下。")
    assert shown is None


def test_manual_edit_locks_the_new_line_and_refuses_a_board_that_fails_elsewhere():
    previous = {"segments": [{"speech_spans": [{"text": "看清这件。"}], "shots": [
        {"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    submitted = {"segments": [{"speech_spans": [{"text": "商家要的这句。"}], "shots": [
        {"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    saved, error = settle_manual_edit(previous, submitted, 15, project={"ad_form": "brand_tvc"})
    assert error is None
    assert saved.get("review_status") is None
    assert "商家要的这句。" in saved["merchant_locked_lines"]

    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    edited = _board(strategy, [stacked], ["瓶身留在桌角。", "瓶身仍在原处。"])
    before = _board(strategy, ["看清这件。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    held, ask = settle_manual_edit(before, edited, 15, strategy)
    assert held is None
    assert stacked in ask and "堆卖点" in ask

    same_shots = {"segments": [
        {"shots": [{"description": "同一句画面。"}]},
        {"shots": [{"description": "同一句画面。"}]},
    ]}
    blocked, reason = settle_manual_edit(None, same_shots, 15)
    assert blocked is None
    assert reason == "还没确定这是哪种广告，上一版还在。"
    assert "检查" not in reason
    assert "硬门" not in reason and "没通过" not in reason

    unreadable, plain = settle_manual_edit(previous, ["不是一份方案"], 15)
    assert unreadable is None
    assert plain == "这版读不出来，上一版还在。"

    story = _strategy("scene_story")
    selling = "这风不吵。"
    pitched = _board(story, [selling], ["她看着桌上的瓶子。", "瓶子还在桌上。"], mode="character_dialogue")
    quiet = _board(story, ["先放这儿。"], ["她看着桌上的瓶子。", "瓶子还在桌上。"], mode="character_dialogue")
    held_story, ask_story = settle_manual_edit(quiet, pitched, 15, story)
    assert held_story is None
    assert selling in ask_story and "像在推销" in ask_story
    assert "碰到了这条规则" not in ask_story
    assert "没通过" not in ask_story and "硬门" not in ask_story and "不是最好" not in ask_story

    looked = _board(story, ["先放这儿。"], ["她看着镜头，瓶子在桌上。", "瓶子还在桌上。"], mode="character_dialogue")
    blocked_look, look_reason = settle_manual_edit(quiet, looked, 15, story)
    assert blocked_look is None
    assert "看着镜头" in look_reason and "上一版还在" in look_reason
    assert "检查" not in look_reason and "硬门" not in look_reason and "没通过" not in look_reason


def test_spoken_line_repair_names_a_plain_reason():
    from app.release_gate import _plain_line_reason, spoken_repair_message

    story = _strategy("scene_story")
    old = "这风不吵。"
    new = "先放这儿。"
    before = _board(story, [old], ["她看着桌上的瓶子。", "瓶子还在桌上。"], mode="character_dialogue")
    note = spoken_repair_message(before, [(old, new)])
    assert note.startswith("出图前改了这句台词：")
    assert f"「{old}」改成了「{new}」" in note
    assert "这句在剧情片里像在推销，改得更自然了" in note
    assert "没通过" not in note and "硬门" not in note and "不是最好" not in note
    quiet = _board(story, ["先放这儿。"], ["瓶子在桌上。", "瓶子还在桌上。"], mode="character_dialogue")
    plain = spoken_repair_message(quiet, [("先放这儿。", "你拿着。")])
    assert "改得更自然了" in plain
    assert "因为" not in plain
    assert _plain_line_reason("综合检查没通过，不是最好") == "改得更自然了"
    assert _plain_line_reason("") == "改得更自然了"


def test_a_repeated_line_is_explained_as_already_said():
    from app.release_gate import _plain_line_reason, spoken_repair_message

    story = _strategy("scene_story")
    line = "这风不吵。"
    before = _board(story, [line, line], ["她看着桌上的瓶子。", "瓶子还在桌上。"], mode="character_dialogue")
    note = spoken_repair_message(before, [(line, "你先坐。")])
    assert "这句前面已经说过，换了个说法" in note
    assert "像在推销" not in note
    assert _plain_line_reason("台词重复") == "这句前面已经说过，换了个说法"
    assert _plain_line_reason("相邻台词重复") == "这句前面已经说过，换了个说法"


def test_insisted_line_is_the_only_exemption_and_other_content_still_fails():
    strategy = _strategy("brand_tvc")
    locked_line = "它带负离子，吹完是顺的。这风不吵。"
    other = "几下就干，这风也不吵。"
    plan = _board(strategy, [locked_line, other], ["瓶身留在桌角。", "瓶身仍在原处。"])
    kind, findings = classify_release(plan, strategy, locked_lines=[locked_line, other], insisted_lines_=())
    assert kind == "ask"
    assert {item.line for item in findings} == {locked_line, other}

    kind, findings = classify_release(plan, strategy, locked_lines=[locked_line], insisted_lines_=[locked_line])
    assert kind == "repair"
    assert any(item.line == other for item in findings)
    assert all(item.line != locked_line for item in findings)

    voice = _board(strategy, ["看清这件。"], ["瓶身在桌上。", "瓶身靠近一些。"], mode="character_dialogue")
    voice["production"]["speech_mode"] = "character_dialogue"
    kind, findings = classify_release(voice, strategy, locked_lines=["看清这件。"], insisted_lines_=["看清这件。"])
    assert kind == "repair"
    assert any(item.gate_id == "voice_identity" for item in findings)

    story = _strategy("scene_story")
    looking = _board(story, ["先放这儿。"], ["她看着镜头，瓶子在桌上。", "瓶子留在桌上。"], mode="character_dialogue")
    looking["production"]["speech_mode"] = "character_dialogue"
    hits = evaluate_release_table(looking, story, exempt_lines=["先放这儿。"])
    assert any(item.gate_id == "story_looks_at_camera" for item in hits)


def test_locked_line_card_offers_lengthening_only_inside_the_cap():
    line = "商家要原句保留，" * 12
    longer = "商家要原句保留，" * 23
    offered = duration_question(line, 20, 29)
    assert [item["id"] for item in offered["options"]] == ["lengthen", "shorten"]
    assert "多花一些" in offered["prompt"]
    at_cap = duration_question(longer, 60, 29)
    assert at_cap is not None
    assert [item["id"] for item in at_cap["options"]] == ["shorten"]
    assert "已经到60秒" in at_cap["prompt"]
    overflow = duration_question(longer, 30, 29)
    assert [item["id"] for item in overflow["options"]] == ["shorten"]
    assert "加到60秒也放不下" in overflow["prompt"]
    messages = [{"role": "user", "content": "把视频加长"}]
    assert lengthen_target(messages, 20, "", {"merchant_locked_lines": [line]}) == duration_needed(line, 29)
    assert lengthen_target(messages, 60, "", {"merchant_locked_lines": [line]}) is None
    assert lengthen_target([{"role": "user", "content": "就这样"}], 20, "", {"merchant_locked_lines": [line]}) is None


def test_chat_lock_keeps_an_insisted_quote_and_releases_a_line_the_merchant_rewrites():
    brief = "做一条广告"
    dictated = {"role": "user", "content": "把第3句台词改成“先放这儿。”"}
    insisted_turn = {"role": "user", "content": "第1句「先放这儿。」碰到了这条规则：剧情对白还在说卖点。：就按这句来"}
    locked, insisted = line_locks(brief, [dictated, insisted_turn])
    assert "先放这儿。" in locked and "先放这儿。" in insisted
    rewrite = {"role": "user", "content": "第1句「先放这儿。」碰到了这条规则：剧情对白还在说卖点。：让系统改写这句"}
    locked, insisted = line_locks(brief, [dictated, rewrite])
    assert "先放这儿。" not in locked and "先放这儿。" not in insisted


def test_generate_job_records_confirmation_and_spend_requires_that_revision(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "media")
    project = store.create_project("蓝瓶", "product", duration_seconds=20)
    plan = {"title": "方案", "script": "看看。", "segments": [
        {"id": "s1", "duration_seconds": 10, "purpose": "看", "prompt": "看瓶身",
         "shots": [{"start": 0, "end": 10, "description": "瓶身在左。"}], "reference_asset_ids": []},
        {"id": "s2", "duration_seconds": 10, "purpose": "看", "prompt": "看瓶口",
         "shots": [{"start": 0, "end": 10, "description": "瓶口靠近。"}], "reference_asset_ids": []},
    ]}
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"])
    assert job["payload"]["merchant_confirmed_revision"] == project["revision"]
    assert_spend_confirmed(job, project)
    job["payload"]["merchant_confirmed_revision"] = project["revision"] - 1
    with pytest.raises(ValueError, match="没有确认"):
        assert_spend_confirmed(job, project)
    missing = {"revision": project["revision"], "payload": {}}
    with pytest.raises(ValueError, match="没有确认"):
        assert_spend_confirmed(missing, project)


def _spoken(plan):
    return [beat.get("narration") for beat in plan["ad_script"]["beats"] if beat.get("narration")]


def _brand():
    strategy = chosen_strategy({"id": "original-product"})
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    return StrategyRecord.model_validate(raw)


def _story():
    strategy = chosen_strategy({"id": "original-product"}, "character_dialogue")
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "scene_story"
    return StrategyRecord.model_validate(raw)


def test_conservative_plan_keeps_every_locked_line_or_asks():
    selling = "它带负离子，吹完是顺的。"
    story = _story()
    kept = conservative_usable_plan(
        story, 20, "continuity_v1", ["original-product"],
        locked_lines=[selling, "先放这儿。"], insisted_lines_=[selling])
    assert kept is not None
    assert _spoken(kept)[:2] == [selling, "先放这儿。"]
    from app.release_gate import _gates_block_spend
    assert _gates_block_spend(kept, strategy=kept.get("strategy")) is None
    assert images_blocked_reason(kept, strategy=kept.get("strategy")) is None

    brand = _brand()
    both = conservative_usable_plan(
        brand, 20, "continuity_v1", ["original-product"],
        locked_lines=["看清这件。", "留在桌上。"])
    assert both is not None
    assert _spoken(both) == ["看清这件。", "留在桌上。"]

    stacked = "它带负离子，吹完是顺的。这风不吵。"
    with pytest.raises(MerchantLineChoice) as asked:
        conservative_usable_plan(
            brand, 20, "continuity_v1", ["original-product"], locked_lines=[stacked])
    assert stacked in asked.value.questions[0]["prompt"]
    assert any(item["label"] == "就按这句来" for item in asked.value.questions[0]["options"])

    silent = chosen_strategy({"id": "original-product"}, "none")
    raw = silent.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    silent = StrategyRecord.model_validate(raw)
    with pytest.raises(MerchantLineChoice) as quiet:
        conservative_usable_plan(
            silent, 15, "continuity_v1", ["original-product"], locked_lines=["看清这件。"])
    assert "看清这件。" in quiet.value.questions[0]["prompt"]

    long_line = "商家要原句保留，" * 12
    with pytest.raises(MerchantLineChoice) as duration:
        conservative_usable_plan(
            _strategy("brand_tvc"), 15, "legacy_10s", ["original-product"], locked_lines=[long_line])
    assert duration.value.questions[0]["id"] == "locked_line_duration"
    assert long_line in duration.value.questions[0]["prompt"]


def test_manual_edit_ignores_client_review_and_insist_markers():
    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    previous = _board(strategy, ["看清这件。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    submitted = _board(strategy, [stacked], ["瓶身留在桌角。", "瓶身仍在原处。"])
    submitted["review_status"] = "passed"
    submitted["plan_review"] = {"ready": True, "checks": [{"criterion": "画面", "passed": True}]}
    submitted["merchant_insisted_lines"] = [stacked]
    submitted["segments"][0]["speech_spans"][0]["merchant_insisted"] = True
    saved, error = settle_manual_edit(previous, submitted, 15, strategy)
    assert saved is None
    assert stacked in error and "堆卖点" in error

    clean = _board(strategy, ["看清这件。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    clean["review_status"] = "passed"
    clean["plan_review"] = {"ready": True, "checks": [{"criterion": "画面", "passed": True}]}
    clean["merchant_insisted_lines"] = ["看清这件。"]
    clean["segments"][0]["speech_spans"][0]["merchant_insisted"] = True
    saved, error = settle_manual_edit(previous, clean, 15, strategy)
    assert error is None
    assert saved.get("review_status") is None
    assert not saved.get("merchant_insisted_lines")
    assert all(not span.get("merchant_insisted") for span in saved["segments"][0]["speech_spans"])

    recorded = _board(strategy, [stacked], ["瓶身留在桌角。", "瓶身仍在原处。"])
    recorded["merchant_insisted_lines"] = [stacked]
    recorded["segments"][0]["speech_spans"][0]["merchant_insisted"] = True
    same = deepcopy(recorded)
    same["review_status"] = "not_ready"
    saved, error = settle_manual_edit(recorded, same, 15, strategy)
    assert error is None
    assert saved.get("review_status") is None
    assert stacked in saved["merchant_insisted_lines"]


def test_lengthen_uses_the_ad_type_speech_rate():
    line = "商家要原句保留，" * 10
    messages = [{"role": "user", "content": "把视频加长"}]
    presenter = {"merchant_locked_lines": [line], "strategy": {"brief": {"ad_form": "presenter_pitch"}}}
    brand = {"merchant_locked_lines": [line], "strategy": {"brief": {"ad_form": "brand_tvc"}}}
    assert lengthen_target(messages, 15, "", presenter) == duration_needed(line, 48)
    assert lengthen_target(messages, 15, "", brand) == duration_needed(line, 30)
    assert lengthen_target(messages, 15, "", {"merchant_locked_lines": [line]}) == duration_needed(line, 29)
    assert duration_needed(line, 48) != duration_needed(line, 29)


def test_unknown_ad_form_blocks_release_until_the_project_supplies_one():
    bare = {
        "review_status": "conservative_fallback",
        "production": {"speech_mode": "voiceover", "speaker_id": "", "characters": []},
        "ad_script": {"beats": [{"id": "b0", "narration": "先放这儿。"}]},
        "segments": [{"duration_seconds": 15, "shots": [
            {"description": "她看着镜头，瓶子在桌上。"},
            {"description": "瓶子留在桌上。"},
        ], "speech_spans": [{"text": "先放这儿。"}]}],
    }
    assert evaluate_release_table(bare) == []
    assert stamp_honest_label(deepcopy(bare)) == "not_ready"
    assert images_blocked_reason(bare)
    found = evaluate_release_table(bare, project={"ad_form": "scene_story"})
    assert any(item.gate_id == "voice_identity" for item in found)
    assert images_blocked_reason(bare, project={"ad_form": "scene_story"})
    quiet = {"review_status": "conservative_fallback", "segments": [
        {"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    assert images_blocked_reason(quiet, project={"ad_form": "brand_tvc"})


def test_continuity_only_fallback_is_released_only_when_hard_gates_pass():
    """Bypass 1: a continuity-only miss used to ship as conservative_fallback."""
    note = "状态承接没有对上，本地对过一次道具交接，没有再送回模型。综合检查已留下。这条已记下。"
    unfinished = {
        "review_status": "conservative_fallback", "review_status_note": note,
        "plan_review": {"ready": False, "checks": [{"criterion": "状态衔接", "passed": False}]},
        "segments": [{"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}],
    }
    assert images_blocked_reason(unfinished, project={"ad_form": "brand_tvc"})
    board = {
        "review_status": "conservative_fallback", "review_status_note": note,
        "plan_review": _failed_review(),
        "segments": [{"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}],
    }
    board["plan_review"]["execution_check"]["state_continuity"] = False
    assert images_blocked_reason(board, project={"ad_form": "brand_tvc"}) is None
    failing = _board(_strategy("brand_tvc"), ["它带负离子，吹完是顺的。这风不吵。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    failing["review_status"] = "conservative_fallback"
    failing["review_status_note"] = note
    assert images_blocked_reason(failing, strategy=failing["strategy"])


def test_incomplete_review_salvage_is_labelled_or_blocked():
    """Bypass 2: an unfinished review used to release whatever board was in hand."""
    strategy = _strategy("brand_tvc")
    passing = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    shown = present_checked_plan(
        passing, strategy, duration=15, generation_strategy="continuity_v1", asset_ids=["original-product"],
        note="综合检查没有完成，已按硬门核对留下可用分镜。这条已记下。")
    assert shown is None
    failing = _board(strategy, ["它带负离子，吹完是顺的。这风不吵。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    shown = present_checked_plan(
        failing, strategy, duration=15, generation_strategy="continuity_v1", asset_ids=["original-product"],
        note="综合检查没有完成，已按硬门核对留下可用分镜。这条已记下。")
    assert shown is None


def test_earlier_draft_without_a_passing_review_is_not_a_false_pass():
    """Bypass 3: accepting an earlier draft no longer pretends the review passed."""
    strategy = _strategy("brand_tvc")
    earlier = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    assert stamp_honest_label(earlier, strategy) is None
    assert earlier.get("review_status") != "passed"
    claimed = deepcopy(earlier)
    claimed["review_status"] = "conservative_fallback"
    assert stamp_honest_label(claimed, strategy) == "not_ready"
    assert images_blocked_reason(claimed, strategy=strategy)
    stacked = _board(strategy, ["它带负离子，吹完是顺的。这风不吵。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    stacked["review_status"] = "conservative_fallback"
    assert images_blocked_reason(stacked, strategy=strategy)


def test_truncated_planner_reply_does_not_release_an_unchecked_board():
    """Bypass 4: a cut-off planner reply used to hand back the previous board unlabeled."""
    strategy = _strategy("brand_tvc")
    earlier = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    shown = present_checked_plan(
        earlier, strategy, duration=15, generation_strategy="continuity_v1", asset_ids=["original-product"],
        note="策划回复没有收完，硬门已经核对过上一稿。这条已记下。")
    assert shown is None
    failing = _board(strategy, ["它带负离子，吹完是顺的。这风不吵。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    shown = present_checked_plan(
        failing, strategy, duration=15, generation_strategy="continuity_v1", asset_ids=["original-product"],
        note="策划回复没有收完，硬门已经核对过上一稿。这条已记下。")
    assert shown is None


def _failed_review(**overrides):
    review = {
        "ready": False,
        "checks": [{"criterion": name, "passed": True} for name in (
            "product_relevance", "audience_relevance", "factual_support",
            "audiovisual_complement", "natural_speech", "narrative_progression")],
        "execution_check": {"action_capacity": True, "state_continuity": True},
        "alignment": {"ready": False, "ad_expression_passed": True, "shots": [], "spans": []},
    }
    review.update(overrides)
    return review


def test_a_finished_failed_review_names_the_failed_items_on_the_fallback_note():
    strategy = _strategy("scene_story")
    board = _board(strategy, ["先放这儿。"], ["她把瓶子放在桌角。", "瓶子留在原处。"], mode="character_dialogue")
    board["plan_review"] = _failed_review()
    board["plan_review"]["alignment"]["ad_expression_passed"] = False
    board["plan_review"]["checks"][4]["passed"] = False
    assert stamp_honest_label(board, strategy) == "conservative_fallback"
    assert board["review_status_note"] == "综合检查没通过（台词自然度、广告表达），硬门已通过"
    assert "没有完整的综合检查" not in board["review_status_note"]


def test_a_plan_without_a_finished_review_cannot_be_confirmed_or_spend():
    """Hard gates can be clear and the plan still must not be confirmed or submitted."""
    strategy = _strategy("brand_tvc")
    board = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    board["review_status"] = "conservative_fallback"
    board["review_status_note"] = "没有完整的综合检查，硬门已通过"
    assert evaluate_release_table(board, strategy) == []
    assert images_blocked_reason(board, strategy=strategy)
    with pytest.raises(ValueError, match="这次没生成出来"):
        settle_before_spend(board, strategy=strategy, project_id="proj", duration=15,
                            generation_strategy="legacy_10s", asset_ids=["original-product"])
    truncated = deepcopy(board)
    truncated["plan_review"] = {"ready": False, "checks": [{"criterion": "product_relevance", "passed": False}]}
    assert images_blocked_reason(truncated, strategy=strategy)
    with pytest.raises(ValueError, match="这次没生成出来"):
        settle_before_spend(truncated, strategy=strategy, project_id="proj", duration=15,
                            generation_strategy="legacy_10s", asset_ids=["original-product"])
    finished = deepcopy(board)
    finished["plan_review"] = _failed_review()
    assert images_blocked_reason(finished, strategy=strategy) is None


def test_a_missing_truncated_or_errored_review_says_the_check_did_not_finish():
    strategy = _strategy("brand_tvc")
    missing = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    assert stamp_honest_label(missing, strategy) is None
    assert "review_status" not in missing
    assert images_blocked_reason(missing, strategy=strategy) is None
    truncated = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    truncated["plan_review"] = {"ready": False, "checks": [{"criterion": "product_relevance", "passed": False}]}
    assert stamp_honest_label(truncated, strategy) == "not_ready"
    assert truncated["review_status_note"] == "没有完整的综合检查，硬门已通过"
    assert images_blocked_reason(truncated, strategy=strategy)
    errored = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    errored["plan_review"] = {"error": "综合检查没有返回"}
    assert stamp_honest_label(errored, strategy) == "not_ready"
    assert errored["review_status_note"] == "没有完整的综合检查，硬门已通过"
    assert images_blocked_reason(errored, strategy=strategy)


def test_legacy_unlabeled_plan_is_labelled_and_unknown_form_is_blocked():
    """Bypass 5: the old picture-and-speech path no longer ships with a missing label."""
    plain = {"segments": [{"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    labelled_board = deepcopy(plain)
    assert stamp_honest_label(labelled_board, project={"ad_form": "brand_tvc"}) is None
    assert images_blocked_reason(labelled_board, project={"ad_form": "brand_tvc"}) is None
    blocked = deepcopy(plain)
    assert stamp_honest_label(blocked) == "not_ready"
    assert images_blocked_reason(blocked)


@pytest.mark.parametrize("label", ["passed", "repaired", "conservative_fallback"])
def test_a_labeled_plan_whose_content_later_breaks_a_gate_is_blocked(label):
    """The stored label was written at planning time. Later words are checked again."""
    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    changed = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    changed["review_status"] = label
    changed["ad_script"]["beats"][0]["narration"] = stacked
    changed["segments"][0]["speech_spans"][0]["text"] = stacked
    assert images_blocked_reason(changed, strategy=changed["strategy"]) == SPEND_RECHECK


def test_labeled_plan_with_no_concrete_type_is_blocked():
    """auto and a missing project type are not a guess from the brief text."""
    labelled = {
        "review_status": "passed",
        "strategy": {"brief": {"ad_form": "auto", "product_name": "蓝瓶"}},
        "segments": [{"shots": [
            {"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"},
        ]}],
    }
    assert images_blocked_reason(labelled) == SPEND_RECHECK
    assert images_blocked_reason(labelled, project={"ad_form": "auto"}) == SPEND_RECHECK
    assert images_blocked_reason(labelled, project={"brief": {"ad_form": "根据商品推荐"}}) == SPEND_RECHECK
    assert images_blocked_reason(labelled, project={}) == SPEND_RECHECK


def test_a_normal_passed_plan_still_goes_through():
    strategy = _strategy("brand_tvc")
    board = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    board["review_status"] = "passed"
    board["plan_review"] = _failed_review(ready=True)
    assert images_blocked_reason(board, strategy=board["strategy"]) is None


def test_merchant_insisted_line_still_goes_through():
    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    board = _board(strategy, [stacked], ["瓶身留在桌角。", "瓶身仍在原处。"])
    board["review_status"] = "passed"
    board["plan_review"] = _failed_review(ready=True)
    board["merchant_insisted_lines"] = [stacked]
    board["segments"][0]["speech_spans"][0]["merchant_insisted"] = True
    assert images_blocked_reason(board, strategy=board["strategy"]) is None


def test_changed_passed_plan_is_repaired_before_image_spend_without_asking_the_merchant(tmp_path):
    store = Store(tmp_path / "db.sqlite", tmp_path / "media")
    project = store.create_project("蓝瓶", "product", duration_seconds=10, ad_form="brand_tvc")
    path = store.media_root / "product.png"
    path.write_bytes(PNG)
    asset = store.add_asset(project["id"], "product", "原图", path, "image/png")
    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    plan = {
        "title": "方案", "script": stacked, "language": "zh-CN", "aspect_ratio": "9:16",
        "audio_strategy": "voiceover", "review_status": "passed", "strategy": strategy.model_dump(),
        "facts": {"name": "蓝瓶", "claims": []},
        "ad_script": {"core_message": "看清商品", "beats": [{
            "id": "b0", "narration": stacked, "purpose": "看清商品", "message": "看清外观",
            "visual_role": "商品留在画面里"}]},
        "segments": [{"id": "s0", "duration_seconds": 10, "purpose": "展示", "prompt": "真实商品展示",
                      "shots": [{"start": 0, "end": 5, "description": "瓶身在左侧。"},
                                {"start": 5, "end": 10, "description": "瓶身靠近一些。"}],
                      "reference_asset_ids": [asset["id"]]}],
    }
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"])

    class _Gateway:
        def __init__(self):
            self.designs = 0
            self.videos = 0
            self.plans = []

        async def create_designs(self, project, assets, plan, folder, **kwargs):
            self.designs += 1
            self.plans.append(plan)
            return []

        async def submit_video(self, *args, **kwargs):
            self.videos += 1
            return {}

    gateway = _Gateway()
    settings = Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY",
                        ffmpeg_path="TEST_ONLY", max_video_submissions=1, max_active_runs=1,
                        poll_seconds=0.01, provider_timeout_seconds=1, tts_voice="TEST_ONLY",
                        video_model="TEST_ONLY")
    result = asyncio.run(Pipeline(store, gateway, settings).execute(job["id"]))
    assert result["submission_count"] == 0
    assert gateway.videos == 0
    assert gateway.designs == 1
    spent = gateway.plans[0]
    assert images_blocked_reason(spent, strategy=spent.get("strategy"), project=project) is None
    assert stacked not in json.dumps(spent, ensure_ascii=False)
    assert "auto_release" not in spent
    current = store.get_project(project["id"])
    assert current["revision"] == project["revision"] + 1
    previous = store.revision_plan(project["id"], project["revision"])
    assert stacked in json.dumps(previous, ensure_ascii=False)
    saved = store.revision_plan(project["id"], current["revision"])
    assert stacked not in json.dumps(saved, ensure_ascii=False)
    finished = store.get_job(job["id"])
    assert finished["revision"] == current["revision"]
    assert finished["payload"]["merchant_confirmed_revision"] == current["revision"]
    assert finished["payload"]["repaired_from_revision"] == project["revision"]
    assert "这版方案需要重新检查" not in (result.get("error") or "")
    assert "回到对话修改" not in (result.get("error") or "")
    assert result["progress"].get("error_code") != "release_recheck"
    messages = store.project_detail(project["id"])["messages"]
    assert not any("这版方案需要重新检查" in item["content"] for item in messages)
    assert not any("请修改" in item["content"] for item in messages)
    notes = [item["content"] for item in messages if item["role"] == "assistant"]
    assert len(notes) == 1
    assert stacked in notes[0]
    assert "它带负离子。" in notes[0]
    assert "堆卖点" in notes[0]
    assert "出图前改了这句台词" in notes[0]
    assert "改得更自然了" in notes[0]
    assert "没通过" not in notes[0]
    assert "硬门" not in notes[0]
    assert "不是最好" not in notes[0]


def _pipeline(store, tmp_path):
    class _Gateway:
        def __init__(self):
            self.designs = 0
            self.videos = 0
            self.plans = []

        async def create_designs(self, project, assets, plan, folder, **kwargs):
            self.designs += 1
            self.plans.append(plan)
            return []

        async def submit_video(self, *args, **kwargs):
            self.videos += 1
            return {}

    gateway = _Gateway()
    settings = Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY",
                        ffmpeg_path="TEST_ONLY", max_video_submissions=1, max_active_runs=1,
                        poll_seconds=0.01, provider_timeout_seconds=1, tts_voice="TEST_ONLY",
                        video_model="TEST_ONLY")
    return gateway, settings


def test_conservative_replacement_waits_for_another_confirm_before_any_spend(tmp_path, monkeypatch):
    """An unreviewed substitute is not shown for confirmation and does not spend."""
    monkeypatch.setattr("app.release_gate.local_hard_gate_repair", lambda *args, **kwargs: None)
    store = Store(tmp_path / "db.sqlite", tmp_path / "media")
    project = store.create_project("蓝瓶", "product", duration_seconds=10, ad_form="brand_tvc")
    path = store.media_root / "product.png"
    path.write_bytes(PNG)
    asset = store.add_asset(project["id"], "product", "原图", path, "image/png")
    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    plan = {
        "title": "方案", "script": stacked, "language": "zh-CN", "aspect_ratio": "9:16",
        "audio_strategy": "voiceover", "review_status": "passed", "strategy": strategy.model_dump(),
        "facts": {"name": "蓝瓶", "claims": []},
        "ad_script": {"core_message": "看清商品", "beats": [{
            "id": "b0", "narration": stacked, "purpose": "看清商品", "message": "看清外观",
            "visual_role": "商品留在画面里"}]},
        "segments": [{"id": "s0", "duration_seconds": 10, "purpose": "展示", "prompt": "真实商品展示",
                      "shots": [{"start": 0, "end": 5, "description": "瓶身在左侧。"},
                                {"start": 5, "end": 10, "description": "瓶身靠近一些。"}],
                      "reference_asset_ids": [asset["id"]]}],
    }
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"])
    gateway, settings = _pipeline(store, tmp_path)
    result = asyncio.run(Pipeline(store, gateway, settings).execute(job["id"]))
    assert gateway.designs == 0 and gateway.videos == 0
    assert result["submission_count"] == 0
    assert result["status"] == "canceled"
    stopped = store.get_job(job["id"])
    assert stopped["revision"] == project["revision"]
    assert stopped["payload"]["merchant_confirmed_revision"] == project["revision"]
    current = store.get_project(project["id"])
    assert current["revision"] == project["revision"] + 1
    assert current["status"] == "awaiting_generate"
    previous = store.revision_plan(project["id"], project["revision"])
    assert stacked in json.dumps(previous, ensure_ascii=False)
    saved = store.revision_plan(project["id"], current["revision"])
    assert stacked not in json.dumps(saved, ensure_ascii=False)
    assert saved["segments"] != plan["segments"]
    assert images_blocked_reason(saved, strategy=saved.get("strategy"), project=current) is None
    notes = [item["content"] for item in store.project_detail(project["id"])["messages"] if item["role"] == "assistant"]
    assert notes[0].startswith("方案做了一些调整，确认后开始出图。")
    assert "硬门" not in notes[0] and "检查没" not in notes[0] and "不是最好" not in notes[0]
    assert stacked in notes[0]
    assert "重新生成" not in notes[0]
    assert "能通过检查" not in (stopped.get("error") or "")
    assert "硬门" not in (stopped.get("error") or "")
    again = store.create_job(project["id"], "generate", current["revision"])
    assert again["id"] != job["id"]
    assert again["payload"]["merchant_confirmed_revision"] == current["revision"]
    second = asyncio.run(Pipeline(store, gateway, settings).execute(again["id"]))
    assert gateway.designs == 1 and gateway.videos == 0
    assert second["submission_count"] == 0
    assert "请再按确认" not in (second.get("error") or "")


def test_unknown_type_does_not_spend_or_ask_the_merchant_to_rewrite():
    labelled = {
        "review_status": "passed",
        "strategy": {"brief": {"ad_form": "auto", "product_name": "蓝瓶"}},
        "segments": [{"duration_seconds": 10, "shots": [
            {"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"},
        ]}],
    }
    with pytest.raises(ValueError, match=SYSTEM_CARD):
        settle_before_spend(labelled, project={"ad_form": "auto"}, project_id="p1")


def test_fallback_keeps_the_hard_gate_passer_with_the_fewest_review_issues():
    strategy = _strategy("brand_tvc")
    worse = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    worse["plan_review"] = _failed_review()
    worse["plan_review"]["checks"][0]["passed"] = False
    worse["plan_review"]["checks"][1]["passed"] = False
    worse["plan_review"]["alignment"]["ad_expression_passed"] = False
    better = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    better["plan_review"] = _failed_review()
    better["plan_review"]["alignment"]["shots"] = [{"passed": False}]
    blocked = _board(strategy, ["它带负离子，吹完是顺的。这风不吵。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    blocked["plan_review"] = _failed_review()
    blocked["plan_review"]["ready"] = True
    chosen = choose_hard_gate_release([worse, blocked, better], strategy=strategy)
    assert chosen is better
    incomplete = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    assert choose_hard_gate_release([incomplete, better], strategy=strategy) is better


def test_auto_release_records_a_failed_review_and_an_unfinished_one(caplog):
    import logging
    strategy = _strategy("scene_story")
    failed = _board(strategy, ["先放这儿。"], ["她把瓶子放在桌角。", "瓶子留在原处。"], mode="character_dialogue")
    failed["plan_review"] = _failed_review()
    failed["plan_review"]["alignment"]["ad_expression_passed"] = False
    failed["review_status"] = "conservative_fallback"
    with caplog.at_level(logging.INFO, logger="yingzuo.auto_release"):
        record_auto_release(failed, project_id="proj-story", ad_form="scene_story", repair_rounds=2)
    assert failed["auto_release"]["note"] == "综合检查没通过（广告表达），硬门已通过"
    assert failed["auto_release"]["failed_checks"] == ["广告表达"]
    assert failed["auto_release"]["repair_rounds"] == 2
    assert failed["auto_release"]["project_id"] == "proj-story"
    assert failed["auto_release"]["ad_form"] == "scene_story"
    assert "没有完整的综合检查" not in failed["auto_release"]["note"]
    line = caplog.records[-1].message
    assert "proj-story" in line and "scene_story" in line and "广告表达" in line and "2" in line
    missing = _board(strategy, ["先放这儿。"], ["她把瓶子放在桌角。", "瓶子留在原处。"], mode="character_dialogue")
    missing["review_status"] = "conservative_fallback"
    record_auto_release(missing, project_id="proj-2", ad_form="scene_story", repair_rounds=1)
    assert missing["auto_release"]["note"] == "没有完整的综合检查，硬门已通过"
    assert missing["auto_release"]["failed_checks"] == []


def test_hard_gate_miss_uses_the_conservative_board_when_local_repair_cannot():
    """(a) A voice mismatch is not a claim the local rewrite can collapse."""
    strategy = _strategy("brand_tvc")
    board = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"], mode="character_dialogue")
    board["segments"][0]["duration_seconds"] = 10
    board["generation_strategy"] = "legacy_10s"
    assert any(item.gate_id == "voice_identity" for item in evaluate_release_table(board, strategy))
    settled = settle_before_spend(
        board, strategy=strategy, project_id="proj", duration=10,
        generation_strategy="legacy_10s", asset_ids=["original-product"])
    assert settled is not board
    assert board["production"]["speech_mode"] == "character_dialogue"
    assert images_blocked_reason(settled, strategy=settled.get("strategy")) is None
    assert not any(item.gate_id == "voice_identity" for item in evaluate_release_table(settled, settled.get("strategy")))
    assert "auto_release" not in settled


def test_hard_gate_miss_stops_when_the_conservative_board_also_fails(monkeypatch):
    """(b) Nothing is released when repair and the conservative board both miss a gate."""
    strategy = _strategy("brand_tvc")
    board = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"], mode="character_dialogue")
    monkeypatch.setattr("app.release_gate.conservative_usable_plan", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="这次没生成出来"):
        settle_before_spend(board, strategy=strategy, project_id="proj", duration=15,
                            generation_strategy="legacy_10s", asset_ids=["original-product"])


def test_auto_release_reaches_the_startup_handler_without_raising_the_level():
    """The line must hit the handler configured at startup, not a test-only level bump."""
    import io
    import logging

    from app.release_gate import configure_auto_release_log
    configure_auto_release_log()
    log = logging.getLogger("yingzuo.auto_release")
    assert log.isEnabledFor(logging.INFO)
    handler = next(item for item in log.handlers if getattr(item, "yingzuo_auto_release", False))
    buffer = io.StringIO()
    previous = handler.stream
    handler.stream = buffer
    try:
        strategy = _strategy("scene_story")
        failed = _board(strategy, ["先放这儿。"], ["她把瓶子放在桌角。", "瓶子留在原处。"], mode="character_dialogue")
        failed["plan_review"] = _failed_review()
        failed["plan_review"]["alignment"]["ad_expression_passed"] = False
        record_auto_release(failed, project_id="proj-live", ad_form="scene_story", repair_rounds=1)
    finally:
        handler.stream = previous
    text = buffer.getvalue()
    assert text.startswith("auto_release ")
    assert "proj-live" in text and "scene_story" in text and "广告表达" in text


def test_auto_release_only_when_every_hard_gate_passes_and_the_review_failed():
    """(c) A clean pass, a missing review, and a hard-gate failure are not this record."""
    from app.release_gate import should_record_auto_release
    strategy = _strategy("brand_tvc")
    failed = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    failed["plan_review"] = _failed_review()
    failed["plan_review"]["checks"][0]["passed"] = False
    assert images_blocked_reason(failed, strategy=strategy) is None
    assert should_record_auto_release(failed) is True
    clean = deepcopy(failed)
    clean["plan_review"]["ready"] = True
    assert should_record_auto_release(clean) is False
    missing = _board(strategy, ["看清这件。"], ["瓶身在左侧。", "瓶身靠近一些。"])
    assert should_record_auto_release(missing) is False
    stacked = _board(strategy, ["它带负离子，吹完是顺的。这风不吵。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    stacked["plan_review"] = deepcopy(failed["plan_review"])
    assert images_blocked_reason(stacked, strategy=strategy)
    assert should_record_auto_release(stacked) is False


def test_both_repairs_missing_a_hard_gate_spend_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.release_gate.local_hard_gate_repair", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.release_gate.conservative_usable_plan", lambda *args, **kwargs: None)
    store = Store(tmp_path / "db.sqlite", tmp_path / "media")
    project = store.create_project("蓝瓶", "product", duration_seconds=10, ad_form="brand_tvc")
    path = store.media_root / "product.png"
    path.write_bytes(PNG)
    asset = store.add_asset(project["id"], "product", "原图", path, "image/png")
    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    plan = {
        "title": "方案", "script": stacked, "language": "zh-CN", "aspect_ratio": "9:16",
        "audio_strategy": "voiceover", "review_status": "passed", "strategy": strategy.model_dump(),
        "facts": {"name": "蓝瓶", "claims": []},
        "ad_script": {"core_message": "看清商品", "beats": [{
            "id": "b0", "narration": stacked, "purpose": "看清商品", "message": "看清外观",
            "visual_role": "商品留在画面里"}]},
        "segments": [{"id": "s0", "duration_seconds": 10, "purpose": "展示", "prompt": "真实商品展示",
                      "shots": [{"start": 0, "end": 5, "description": "瓶身在左侧。"},
                                {"start": 5, "end": 10, "description": "瓶身靠近一些。"}],
                      "reference_asset_ids": [asset["id"]]}],
    }
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"])

    class _Gateway:
        def __init__(self):
            self.designs = 0
            self.videos = 0

        async def create_designs(self, *args, **kwargs):
            self.designs += 1
            return []

        async def submit_video(self, *args, **kwargs):
            self.videos += 1
            return {}

    gateway = _Gateway()
    settings = Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY",
                        ffmpeg_path="TEST_ONLY", max_video_submissions=1, max_active_runs=1,
                        poll_seconds=0.01, provider_timeout_seconds=1, tts_voice="TEST_ONLY",
                        video_model="TEST_ONLY")
    result = asyncio.run(Pipeline(store, gateway, settings).execute(job["id"]))
    assert gateway.designs == 0 and gateway.videos == 0
    assert result["submission_count"] == 0
    assert result["error"] == "这次没生成出来"
    assert result["progress"]["error_code"] == "planning_not_produced"
    assert result["progress"]["retryable"] is True
    assert result["progress"]["retry_action"] == "retry_planning"
    assert store.get_project(project["id"])["revision"] == project["revision"]
    assert store.revision_plan(project["id"], project["revision"])["script"] == stacked


def test_regenerate_after_not_produced_starts_text_planning(tmp_path):
    from fastapi.testclient import TestClient

    from app.db import StaleRevision
    from app.main import create_app

    settings = Settings(data_root=tmp_path, deepseek_api_key="TEST_ONLY", tudou_api_key="TEST_ONLY",
                        ffmpeg_path="TEST_ONLY", max_video_submissions=1, max_active_runs=1,
                        poll_seconds=0.01, provider_timeout_seconds=1, tts_voice="TEST_ONLY",
                        video_model="TEST_ONLY")
    store = Store(settings.database_path, settings.media_root)
    project = store.create_project("蓝瓶", "product", duration_seconds=10, ad_form="brand_tvc")
    path = store.media_root / "product.png"
    path.write_bytes(PNG)
    asset = store.add_asset(project["id"], "product", "原图", path, "image/png")
    plan = {
        "title": "方案", "script": "看清这件。", "language": "zh-CN", "aspect_ratio": "9:16",
        "audio_strategy": "voiceover",
        "segments": [{"id": "s0", "duration_seconds": 10, "purpose": "展示", "prompt": "真实商品展示",
                      "shots": [{"start": 0, "end": 10, "description": "瓶身在左侧。"}],
                      "reference_asset_ids": [asset["id"]]}],
    }
    project = store.update_plan(project["id"], plan)
    job = store.create_job(project["id"], "generate", project["revision"])
    store.update_job(job["id"], status="failed", error="供应商生成失败", progress={
        "error_code": "unknown", "retryable": False, "retry_action": None})
    with pytest.raises(StaleRevision, match="不会提交图片或视频"):
        store.retry_generate_job(project["id"], job["id"])
    store.update_job(job["id"], status="failed", error="这次没生成出来", progress={
        "error_code": "planning_not_produced", "retryable": True, "retry_action": "retry_planning"})
    with pytest.raises(StaleRevision, match="不会提交图片或视频"):
        store.retry_generate_job(project["id"], job["id"])
    assert store.get_job(job["id"])["status"] == "failed"
    assert store.get_job(job["id"])["submission_count"] == 0
    chatted = store.create_turn(project["id"], {
        "client_turn_id": "chat-redo",
        "expected_context_version": store.get_project(project["id"])["context_version"],
        "content": "重新生成",
    })
    assert chatted["id"] == job["id"] and chatted["kind"] == "generate" and chatted["status"] == "failed"
    assert store.get_job(job["id"])["submission_count"] == 0
    client = TestClient(create_app(settings, run_worker=False))
    response = client.post(f"/api/projects/{project['id']}/retry", json={"job_id": job["id"]})
    assert response.status_code == 200, response.text
    body = response.json()["job"]
    assert body["kind"] == "plan" and body["id"] != job["id"]
    assert store.get_job(job["id"])["status"] == "failed"
    assert store.get_job(job["id"])["submission_count"] == 0


def test_merchant_insisted_line_is_not_replaced_before_spend():
    strategy = _strategy("brand_tvc")
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    board = _board(strategy, [stacked], ["瓶身留在桌角。", "瓶身仍在原处。"])
    board["review_status"] = "passed"
    board["plan_review"] = _failed_review(ready=True)
    board["merchant_insisted_lines"] = [stacked]
    board["segments"][0]["speech_spans"][0]["merchant_insisted"] = True
    settled = settle_before_spend(board, strategy=strategy, project_id="proj")
    assert settled is board
    assert "auto_release" not in board
    assert settled["ad_script"]["beats"][0]["narration"] == stacked
    rewritten = deepcopy(board)
    rewritten["ad_script"]["beats"][0]["narration"] = "看清这件。"
    from app.release_gate import spoken_line_changes
    assert spoken_line_changes(board, rewritten) is None
