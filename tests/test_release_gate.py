"""One release decision, the hard-gate table, locked lines, and pre-spend confirmation."""
import pytest

from app.db import Store
from app.release_gate import (
    RELEASE_TABLE, assert_spend_confirmed, classify_release, duration_needed, duration_question,
    evaluate_release_table, images_blocked_reason, lengthen_target, line_locks,
    present_checked_plan, settle_manual_edit, stamp_honest_label,
)
from app.strategy import StrategyRecord
from test_line_quality import typed
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
    assert images_blocked_reason({"review_status": "passed"}) is None
    clean = {"review_status": "conservative_fallback", "segments": [
        {"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    assert images_blocked_reason(clean) is None
    placeholder = {"review_status": "conservative_fallback",
                   "ad_script": {"beats": [{"narration": "看这里。"}, {"narration": "就是这个。"}]},
                   "shots": [{"description": "商品放在画面中间。"}, {"description": "包装放在画面中间。"}]}
    assert images_blocked_reason(placeholder)
    assert images_blocked_reason({"review_status": "not_ready"})


def test_unlabeled_passing_board_gets_an_honest_label_and_a_failing_one_does_not_ship():
    """Legacy, salvage and recovery boards used to leave with no label or a false fallback."""
    plain = {"segments": [{"shots": [{"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    labelled = stamp_honest_label(plain)
    assert labelled == "conservative_fallback"
    assert images_blocked_reason(plain) is None

    strategy = chosen_strategy({"id": "original-product"})
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    strategy = StrategyRecord.model_validate(raw)
    stacked = "它带负离子，吹完是顺的。这风不吵。"
    failing = _board(strategy, [stacked, "看清这件。"], ["瓶身留在桌角。", "瓶身仍在原处。"])
    shown = present_checked_plan(
        failing, strategy, duration=15, generation_strategy="continuity_v1",
        asset_ids=["original-product"], note="策划回复没有收完，硬门已经核对过上一稿。这条已记下。")
    assert shown is not None
    assert shown.get("review_status") == "conservative_fallback"
    assert images_blocked_reason(shown, strategy=shown.get("strategy")) is None
    spoken = "".join(beat.get("narration") or "" for beat in shown["ad_script"]["beats"])
    assert "负离子" not in spoken


def test_manual_edit_locks_the_new_line_and_refuses_a_board_that_fails_elsewhere():
    previous = {"segments": [{"speech_spans": [{"text": "看清这件。"}], "shots": [
        {"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    submitted = {"segments": [{"speech_spans": [{"text": "商家要的这句。"}], "shots": [
        {"description": "瓶身在左侧。"}, {"description": "瓶身靠近一些。"}]}]}
    saved, error = settle_manual_edit(previous, submitted, 15)
    assert error is None
    assert saved["review_status"] == "conservative_fallback"
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
    assert "没能通过检查" in reason


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
