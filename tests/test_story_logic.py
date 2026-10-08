"""剧情结尾、点题、高光证明、状态参考、不看镜头、背景人物。Offline must-fail and must-pass."""
import asyncio
import json
from pathlib import Path

import pytest

from app.design import (
    character_design_request, character_sheet_product_notes, state_design_request, state_sheet_clothing_notes,
)
from app.line_quality import apply_line_rewrite, script_line_offenses, voice_identity_issues
from app.picture_rules import action_issues, state_change_issues
from app.production import required_designs, validate_design_assets
from app.story_logic import (
    apply_background_rewrite, background_issues, cast_distinction_issues, film_scene_shots, line_is_punchline,
    align_declared_continuity, plan_state_references, proof_highlight_issues, repair_prop_handoff,
    rewrite_background_if_needed,
    hands_only_shot, line_is_private_thought, scene_groups,
    scrub_story_purposes, settle_inner_monologue, settle_speech_coverage, shot_awake_listener,
    snap_declared_speech,
    stamp_background_lines, state_change_warnings,
    state_pairing_issues, story_ending_issues, story_gaze_line, story_plan_issues,
    storyboard_content_failure, wardrobe_warnings, with_background_line,
)
from app.strategy import StrategyRecord
from app.video_prompt import compile_grouped_video
from test_line_quality import _lines, typed
from test_providers import source
from test_video_prompt_lipsync import _segment


def _dryer(expression="夜里吹得快，风也很安静，吹完是顺的"):
    strategy = typed("scene_story")
    raw = strategy.model_dump()
    raw["brief"]["product_name"] = "吹风机"
    selected = next(item for item in raw["directions"] if item["id"] == raw["selected_direction_id"])
    selected["product_expression"] = expression
    selected["hook"] = "湿着头发又折回来"
    selected["payoff"] = "头发干了，人先走"
    return StrategyRecord.model_validate(raw)


def _board(*shots, script=None):
    return {"ad_script": script or _lines("算了，先吹干。", "轻点。"), "segments": [{"shots": list(shots)}]}


def _shot(description, purpose="", **extra):
    shot = {"id": extra.pop("id", "shot_end"), "description": description, "purpose": purpose, "beat_id": "b0"}
    shot.update(extra)
    return shot


def _shot_block(prompt, label):
    rest = prompt.split(label, 1)[1]
    cuts = [pos for pos in (rest.find("\n镜头"), rest.find("\n台词时间")) if pos >= 0]
    return rest[:min(cuts)] if cuts else rest


def test_story_ending_fails_when_a_home_good_leaves_or_the_shot_is_a_hero_freeze():
    strategy = _dryer()
    carried = _board(_shot("她拿着吹风机出门，门外站住。"))
    assert "剧情末镜把家用物品带出了门" in story_ending_issues(carried, strategy)
    hero = _board(_shot("她站在门外。", "英雄定格"))
    assert "剧情末镜写成了英雄定格" in story_ending_issues(hero, strategy)
    presented = _board(_shot("她把吹风机举向镜头。"))
    assert "剧情末镜人物对着镜头举着产品" in story_ending_issues(presented, strategy)
    assert "剧情末镜把家用物品带出了门" in story_ending_issues(carried, strategy)
    assert story_plan_issues(carried, strategy) == []


def test_story_ending_allows_a_push_in_after_the_person_leaves():
    strategy = _dryer()
    linger = _board(_shot("她带上门离开。镜头推近洗手台，吹风机留在台上。"))
    assert story_ending_issues(linger, strategy) == []
    desk = _board(_shot("瓶子停在桌角，人已经走了。"))
    assert story_ending_issues(desk, strategy, product_text="玻璃瓶") == []
    brand = typed("brand_tvc")
    assert story_ending_issues(_board(_shot("产品英雄定格，她拿着吹风机出门。")), brand) == []
    assert story_plan_issues(_board(_shot("她拿着吹风机出门。")), typed("presenter_pitch")) == []


def test_story_last_line_must_not_sum_the_event_up():
    strategy = _dryer()
    for line in ("它让我没白折回来。", "终于干了。", "还好有它。", "幸亏带了。", "这个吹风机让我松了口气。"):
        script = _lines("算了，先吹干。", line)
        names = [name for item in script_line_offenses(script, strategy) for name in item["offenses"]]
        assert "在点题" in names, line
        assert line_is_punchline(line)
    pointed = _lines("算了，先吹干。")
    plan = {"segments": [{"shots": [_shot("她在门口停住。", "替观众把画面里看见的干和顺说实", beat_id="b0")]}]}
    names = [name for item in script_line_offenses(pointed, strategy, plan=plan) for name in item["offenses"]]
    assert "在点题" in names
    clean = _lines("算了，先吹干。", "轻点。")
    clean_plan = {"segments": [{"shots": [_shot("她转身回去。", "停在这一下刚发生的动作上。", beat_id="b0")]}]}
    names = [name for item in script_line_offenses(clean, strategy, plan=clean_plan) for name in item["offenses"]]
    assert "在点题" not in names
    assert "在点题" not in [name for item in script_line_offenses(_lines("终于干了。"), typed("presenter_pitch")) for name in item["offenses"]]
    assert "在点题" not in [name for item in script_line_offenses(_lines("终于干了。"), typed("brand_tvc")) for name in item["offenses"]]
    middle = _lines("终于干了。", "轻点。")
    assert "在点题" not in [name for item in script_line_offenses(middle, strategy) for name in item["offenses"]]


def test_punchline_rewrite_is_rejected_until_the_line_is_in_the_moment():
    strategy = _dryer()
    script = _lines("它让我没白折回来。")
    rejected = apply_line_rewrite(
        script, {"lines": [{"beat_id": "b0", "narration": "它让我没白折回来。"}]},
        limits={"b0": 40}, strategy=strategy, duration_seconds=20)
    assert rejected is None
    accepted = apply_line_rewrite(
        script, {"lines": [{"beat_id": "b0", "narration": "算了，先吹干。"}]},
        limits={"b0": 40}, strategy=strategy, duration_seconds=20)
    assert accepted["beats"][0]["narration"] == "算了，先吹干。"
    director = {"shots": [_shot("她看了一眼。", "替观众把画面里看见的干和顺说实")]}
    scrubbed, changed = scrub_story_purposes(director, strategy)
    assert changed is True
    assert scrubbed["shots"][0]["purpose"] == "停在这一下刚发生的动作上。"
    assert story_plan_issues(_board(_shot("她转身。"), script=_lines("它让我没白折回来。")), strategy) == []


def test_each_story_benefit_needs_a_technique_shot():
    strategy = _dryer()
    flat = _board(_shot("她吹干头发。"))
    assert proof_highlight_issues(flat, strategy) == ["剧情卖点没有高光证明镜"]
    shown = _board(
        _shot("微距，逆光勾出发丝边缘，风把头发吹开。", id="proof_hair"),
        _shot("慢动作，焦点从前景的吹风机移到睡着的人脸上。", id="proof_quiet"),
    )
    assert proof_highlight_issues(shown, strategy) == []
    quiet_only = _board(_shot("慢动作，焦点从前景移到睡着的人脸上。"))
    assert proof_highlight_issues(quiet_only, strategy) == ["剧情卖点没有高光证明镜"]
    assert proof_highlight_issues(_board(_shot("她吹干头发。")), typed("scene_story")) == []
    assert "剧情卖点没有高光证明镜" not in story_plan_issues(shown, strategy)


def _hair_process(shot_id, end):
    # Halfway wording only shows up when the board itself writes 由湿到半程.
    allowed = "" if ("已经" in end or "干了" in end or "顺了" in end) else f"发丝由湿到{end}"
    return {
        "id": shot_id,
        "description": "她在床边吹着湿发。",
        "continuity": {"start_state": "头发是湿的", "end_state": end, "allowed_changes": allowed},
    }


def _hair_result(shot_id="s6"):
    return {
        "id": shot_id,
        "description": "同一间卧室，她关掉吹风机，头发已经顺了。",
        "continuity": {"start_state": "头发半干", "end_state": "头发顺开", "allowed_changes": "发丝由半干到顺开"},
    }


def _declared(start_shot, result_shot, owner="product", subject="头发", start="湿", result="干顺"):
    return {"subject_label": subject, "owner": owner, "start": start, "result": result,
            "start_shot": start_shot, "result_shot": result_shot}


def test_state_change_splits_the_process_shot_from_a_later_result():
    changed = _hair_process("s0", "头发已经干")
    same_shot = {"production": {"characters": []}, "segments": [{"shots": [changed]}],
                 "state_changes": [_declared("s0", "s0")]}
    assert plan_state_references(same_shot) == []
    assert state_change_warnings(same_shot)
    assert state_pairing_issues(same_shot) == []
    halfway = {"production": {"characters": []}, "segments": [{"shots": [_hair_process("s3", "被吹开一半")]}]}
    assert state_pairing_issues(halfway) == []
    assert plan_state_references(halfway) == []
    assert "状态结果还停在半程" not in story_plan_issues(halfway, _dryer())
    plan = {"production": {"characters": []}, "segments": [{"shots": [
        _hair_process("s3", "被吹开一半"), _hair_result(),
    ]}], "state_changes": [_declared("s3", "s6")]}
    refs = plan_state_references(plan)
    assert [item["phase"] for item in refs] == ["start", "result"]
    assert refs[0]["shot_id"] == "s3" and refs[1]["shot_id"] == "s6"
    assert refs[0]["label"] == "头发湿·起始" and refs[1]["label"] == "头发干顺·结果"
    assert "一半" not in refs[1]["label"] and "半干" not in refs[0]["label"]
    assert state_pairing_issues(plan) == []
    assert all(item["kind"] == "start_frame" for item in refs)
    designs = required_designs(plan)
    assert [item["kind"] for item in designs] == ["scene", "start_frame", "start_frame"]
    scene = {"kind": "scene", "metadata": {"generated": True}}
    try:
        validate_design_assets(plan, [scene])
    except ValueError as error:
        assert "状态变化" in str(error)
    else:
        raise AssertionError("state stills are required")
    stills = [{"kind": "start_frame", "metadata": {
        "generated": True, "state_reference": True, "state_id": item["state_id"],
        "phase": item["phase"], "shot_id": item["shot_id"], "state_label": item["label"],
    }} for item in refs]
    ordered = validate_design_assets(plan, [scene, *stills])
    assert [asset["metadata"].get("phase") for asset in ordered[1:]] == ["start", "result"]
    static = {"production": {"characters": []}, "segments": [{"shots": [{"description": "瓶子停在桌角。"}]}]}
    extra = {"kind": "start_frame", "metadata": {"generated": True, "state_reference": True, "state_id": "other:start"}}
    assert validate_design_assets(static, [scene, extra]) == [scene]
    request = state_design_request(
        {}, {"strategy": {"brief": {"product_name": "吹风机", "facts": [{"text": "不出镜"}]}}, "facts": {"name": ""}},
        {"phase": "result", "label": "干发", "subject": "头发", "state_id": "s0:result", "shot_id": "s0"})
    assert "不要人脸" in request["prompt"]
    assert "商品原图" in request["prompt"]
    assert "画成真人头" in request["prompt"]
    assert "必须露脸" not in request["prompt"]
    assert request["reference_kinds"] == ["product"]
    assert "sheet_layout" not in request
    on_story = state_design_request(
        {},
        {"facts": {"name": "吹风机"}, "visual_direction": {"scene": "合住卧室", "lighting": "清晨窗边"},
         "production": {"characters": [
             {"id": "room", "name": "室友", "description": "碎花睡衣"},
             {"id": "girl", "name": "女生", "description": "灰色连帽卫衣"},
         ], "speaker_id": "girl"},
         "segments": [{"shots": [{
             "id": "s3", "character_ids": ["room", "girl"],
             "description": "合住卧室里，灰卫衣女生站在床边吹湿发。",
             "continuity": {"start_state": "头发是湿的", "end_state": "被吹开一半"},
         }]}]},
        {"phase": "start", "label": "头发湿", "subject": "头发", "state_id": "s3:start", "shot_id": "s3"})
    assert on_story["reference_kinds"] == []
    assert on_story["character_id"] == "girl"
    assert on_story["aspect_ratio"] == "16:9"
    assert on_story["sheet_layout"] == "horizontal_16x9"
    assert on_story["template_version"] == "chinese-horizontal-v1"
    assert "左侧约40%" in on_story["prompt"]
    assert "背面全身" in on_story["prompt"]
    assert "白或浅灰无缝背景" in on_story["prompt"]
    assert "灰色连帽卫衣" in on_story["prompt"]
    assert "头发湿" in on_story["prompt"]
    assert "合住卧室" not in on_story["prompt"]
    assert "清晨窗边" not in on_story["prompt"]
    assert "床边" not in on_story["prompt"]
    assert "碎花睡衣" not in on_story["prompt"]
    assert "室友" not in on_story["prompt"]
    assert "场景图" in on_story["prompt"]
    cup = state_design_request(
        {},
        {"facts": {"name": "杯子"}, "visual_direction": {"scene": "合住卧室"},
         "production": {"characters": [{"id": "girl", "name": "女生", "description": "灰色连帽卫衣"}]},
         "segments": [{"shots": [{"id": "s1", "character_ids": ["girl"],
                                  "description": "合住卧室里，杯子还是满的。"}]}]},
        {"phase": "result", "label": "空了", "subject": "杯", "state_id": "s1:result", "shot_id": "s1"})
    assert cup["reference_kinds"] == ["product"]
    assert "sheet_layout" not in cup
    assert "商品原图" in cup["prompt"]
    assert "白或浅灰" in cup["prompt"]
    assert "合住卧室" not in cup["prompt"]
    assert "灰色连帽卫衣" not in cup["prompt"]
    segment = _segment(ad_form="scene_story")
    segment["shots"][0]["description"] = "她在床边吹着湿发。"
    segment["shots"][0]["continuity"] = {
        "entry_transition": "opening", "start_state": "头发是湿的", "end_state": "被吹开一半",
        "allowed_changes": "发丝由湿到被吹开一半", "camera_move": "机位不动",
    }
    segment["shots"][1]["description"] = "她关掉吹风机，头发已经顺了。"
    segment["shots"][1]["continuity"] = {
        "entry_transition": "cut", "start_state": "头发半干", "end_state": "头发顺开",
        "allowed_changes": "", "camera_move": "机位不动",
    }
    segment["reference_roles"] = [
        *segment["reference_roles"],
        {"index": 4, "role": "state", "phase": "start", "shot_id": "s0", "label": "头发湿"},
        {"index": 5, "role": "state", "phase": "result", "shot_id": "s1", "label": "头发顺了"},
    ]
    prompt = compile_grouped_video(segment)
    opening = _shot_block(prompt, "镜头1 |")
    ending = _shot_block(prompt, "镜头2 |")
    assert "起始状态参考图4" in opening
    assert "停在过程里" in opening
    assert "硬切到结果" not in opening
    assert "硬切到结果状态参考图5" in ending
    assert "商品原图" in prompt


def test_story_shots_default_to_no_eye_contact_unless_the_board_asks():
    away = _segment(ad_form="scene_story")
    away["shots"][0]["description"] = "她对着镜子吹发。"
    prompt = compile_grouped_video(away)
    opening = _shot_block(prompt, "镜头1 |")
    assert "本镜人物不看镜头" in opening
    assert "不转头看镜头" in opening
    looking = _segment(ad_form="scene_story")
    looking["shots"][0]["description"] = "她看着镜头，手里拿着吹风机。"
    looked = compile_grouped_video(looking)
    first = _shot_block(looked, "镜头1 |")
    assert "本镜人物不看镜头" not in first
    assert "不转头看镜头" not in first
    presenter = compile_grouped_video(_segment(delivery="on_and_off_screen", ad_form="presenter_pitch"))
    assert "不看镜头" not in presenter
    assert "看着镜头亲口说" in presenter
    brand = compile_grouped_video(_segment(mode="voiceover", ad_form="brand_tvc"))
    assert "不看镜头" not in brand
    assert "声音方式：画外旁白" in brand
    bare = {"id": "s0", "description": "她低头。", "character_ids": ["char_host"]}
    assert story_gaze_line(bare, "scene_story") == "  本镜人物不看镜头。"
    assert story_gaze_line({**bare, "description": "她看着镜头。"}, "scene_story") is None


def test_a_background_person_stays_with_the_bed():
    asleep = _shot("夜里，室友侧躺在床上睡着。", id="s0")
    missing = _board(asleep, _shot("镜头回到床边，被子还在。", id="s1"))
    assert "背景人物没有跟着床入画" in background_issues(missing)
    hands = _board(
        asleep,
        _shot("微距，只有手和机身。", id="s2", continuity={
            "start_state": "人站在床边，头发是湿的", "end_state": "人仍在床边", "camera_move": "手部特写",
        }),
    )
    assert background_issues(hands) == []
    turned = _board(asleep, _shot("床上的室友面向镜头。", id="s1"))
    assert background_issues(turned)
    kept = _board(asleep, _shot("床边，室友仍侧躺。", id="s1"), _shot("她走到门口，门在身后。", id="s2"))
    assert background_issues(kept) == []
    compiled = with_background_line("镜头回到床边。", [asleep, {"description": "镜头回到床边。"}])
    assert "室友仍侧躺，朝向不变" in compiled
    assert with_background_line("她走到门口。", [asleep]) == "她走到门口。"
    segment = _segment(ad_form="scene_story")
    segment["shots"][0]["description"] = "夜里，室友侧躺在床上睡着。她对着镜子。"
    segment["shots"][1]["description"] = "镜头回到床边。"
    segment["shots"][1]["character_ids"] = ["char_host"]
    prompt = compile_grouped_video(segment)
    assert "室友仍侧躺，朝向不变" in prompt


def _state_shot(shot_id, start, end, description):
    return {
        "id": shot_id, "description": description,
        "continuity": {"start_state": start, "end_state": end, "allowed_changes": ""},
    }


def test_a_dry_cut_between_shots_still_gets_start_and_result_stills():
    # r16: sh4/sh5 stay wet, sh6 opens already dry. No single shot contains the change.
    shots = [
        _state_shot("sh4", "吹风机已启动并对着头发，头发仍是湿的", "头发仍是湿的，吹风机仍对着发根吹", "她快速分区吹湿发。"),
        _state_shot("sh5", "头发仍是湿的，吹风机对着发根", "头发仍是湿的，吹风机仍握在她手里运转", "近景，一缕湿发。"),
        _state_shot("sh6", "省略继续吹干的过程后，头发已经吹干，吹风机刚停下", "头发吹干且顺，吹风机在她手里刚停", "她把已经吹干的头发从发根拨到发尾。"),
    ]
    assert plan_state_references({"production": {"characters": []}, "segments": [{"shots": shots}]}) == []
    plan = {"production": {"characters": []}, "segments": [{"shots": shots}],
            "state_changes": [_declared("sh5", "sh6", start="湿", result="吹干")]}
    refs = plan_state_references(plan)
    assert [item["phase"] for item in refs] == ["start", "result"]
    assert refs[0]["shot_id"] == "sh5" and refs[1]["shot_id"] == "sh6"
    assert refs[0]["label"] == "头发湿·起始" and refs[1]["label"] == "头发吹干·结果"
    assert state_pairing_issues(plan) == []
    gapped = {"production": {"characters": []}, "segments": [{"shots": [
        shots[0],
        _state_shot("sh5", "室友侧躺、脸朝房间一侧睡着", "室友保持睡姿", "过肩看床上的室友。"),
        shots[2],
    ]}], "state_changes": [_declared("sh4", "sh6", start="湿", result="吹干")]}
    gapped_refs = plan_state_references(gapped)
    assert [item["shot_id"] for item in gapped_refs] == ["sh4", "sh6"]
    halfway = {"production": {"characters": []}, "segments": [{"shots": [
        _state_shot("sh4", "头发是湿的", "头发仍是湿的", "她吹着湿发。"),
        _state_shot("sh6", "头发被吹开一半", "头发被吹开一半", "头发被吹开一半。"),
    ]}]}
    assert state_pairing_issues(halfway) == []
    assert plan_state_references(halfway) == []


def test_r18b_wet_process_then_dry_result_pairs_start_and_result():
    plan = {"production": {"speaker_id": "girl", "characters": [
        {"id": "girl", "name": "女生", "description": "丸子头，灰色卫衣"},
        {"id": "room", "name": "室友", "description": "长发盘起，深蓝睡衣"},
    ]}, "segments": [{"shots": [
        {"id": "s03", "character_ids": ["girl", "room"],
         "description": "中长发湿着，落回肩侧。",
         "continuity": {"start_state": "头发被吹开、开始顺下来但仍然湿着",
                        "end_state": "头发被吹开、开始顺下来但仍然湿着"}},
        {"id": "s04", "character_ids": ["room"], "description": "室友侧躺睡着。",
         "continuity": {"start_state": "室友侧躺", "end_state": "室友仍侧躺"}},
        {"id": "s05", "character_ids": ["girl"], "description": "头发已经吹干、顺下来。",
         "continuity": {"start_state": "头发已经吹干、顺下来", "end_state": "头发已经吹干、顺下来"}},
    ]}], "state_changes": [_declared("s03", "s05", owner="girl", start="湿", result="吹干")]}
    refs = plan_state_references(plan)
    assert [(item["shot_id"], item["phase"]) for item in refs] == [("s03", "start"), ("s05", "result")]
    assert refs[0]["label"] == "头发湿·起始" and refs[1]["label"] == "头发吹干·结果"
    assert refs[0]["character_id"] == "girl" and refs[1]["character_id"] == "girl"
    designs = required_designs(plan)
    assert [item["kind"] for item in designs] == ["scene", "character", "start_frame", "start_frame"]
    assert designs[1]["character_id"] == "room"
    request = state_design_request({}, plan, refs[0])
    assert "披到肩膀" in request["prompt"] and "不要丸子头" in request["prompt"]
    assert "丸子" not in request["prompt"].split("不要丸子头", 1)[-1]
    assert request["reference_kinds"] == []
    result = state_design_request({}, plan, refs[1])
    assert result["reference_kinds"] == ["state_start"]
    assert "起始状态四视图" in result["prompt"]


def test_r19_state_label_is_the_subject_and_later_shots_use_the_result_sheet():
    from app.directing import ShotContinuity, prepare_storyboard_wire

    blown = {
        "id": "s3", "character_ids": ["girl"],
        "description": "她低头，风扫过头发。",
        "continuity": {"start_state": "吹风机开着，风扫过湿发", "end_state": "吹风机仍开着，湿发被吹开"},
    }
    dry = {
        "id": "s5", "character_ids": ["girl"],
        "description": "她拨开已经干顺的头发。",
        "continuity": {"start_state": "头发已经干顺", "end_state": "头发已经干顺"},
    }
    plan = {"production": {"speaker_id": "girl", "characters": [
        {"id": "girl", "name": "女生", "description": "炭灰软裤，居家拖鞋"},
    ]}, "segments": [{"shots": [blown, dry]}]}
    assert plan_state_references(plan) == []
    plan["state_changes"] = [_declared("s3", "s5", owner="girl")]
    refs = plan_state_references(plan)
    assert [item["phase"] for item in refs] == ["start", "result"]
    assert refs[0]["label"] == "头发湿·起始" and "吹风机" not in refs[0]["label"]
    assert refs[1]["label"] == "头发干顺·结果"
    start_prompt = state_design_request({}, plan, refs[0])["prompt"]
    assert "湿" in start_prompt and "吹风机" not in start_prompt
    assert "双手空手" in start_prompt and "不要商品" in start_prompt and "不要道具" in start_prompt
    assert "白或浅灰" in start_prompt
    declared = {
        "id": "s3", "character_ids": ["girl"], "description": "她低头。",
        "continuity": {
            "start_state": "吹风机开着，风扫过湿发", "end_state": "头发已经干顺",
            "subject_states": [{"subject": "她的头发", "start": "湿", "result": "干顺"}],
        },
    }
    wired = prepare_storyboard_wire({"shots": [{
        **declared,
        "continuity": {**declared["continuity"], "allowed_changes": "吹", "camera_move": "不动",
                       "entry_transition": "opening", "entry_reason": "开场"},
    }]})
    kept = wired["shots"][0]["continuity"]["subject_states"]
    assert kept == [{"subject": "她的头发", "start": "湿", "result": "干顺"}]
    assert ShotContinuity.model_validate({
        "start_state": "头发是湿的", "allowed_changes": "吹", "end_state": "头发干了",
        "camera_move": "不动", "entry_transition": "opening", "entry_reason": "开场",
        "subject_states": kept,
    }).subject_states[0].start == "湿"
    named = {"production": {"speaker_id": "girl", "characters": [
        {"id": "girl", "name": "女生", "description": "炭灰软裤，居家拖鞋"},
    ]}, "segments": [{"shots": [declared]}],
            "state_changes": [_declared("s3", "s3", owner="girl")]}
    assert plan_state_references(named) == []
    named["state_changes"] = [_declared("s3", "s5", owner="girl")]
    named["segments"][0]["shots"].append(dry)
    named_refs = plan_state_references(named)
    assert named_refs[0]["label"] == "头发湿·起始"
    assert named_refs[1]["label"] == "头发干顺·结果"
    assert "吹风机" not in named_refs[0]["label"] and "吹风机" not in named_refs[1]["label"]
    start_request = state_design_request({}, named, named_refs[0])
    result_request = state_design_request({}, named, named_refs[1])
    appearance = (
        "外观原文照抄，脸、五官、肤色、身材、头发长度和发色、衣服、鞋子都按这句："
        "默认出镜为中国人，East Asian Chinese appearance，日常大陆长相。炭灰软裤，居家拖鞋"
    )
    assert appearance in start_request["prompt"] and appearance in result_request["prompt"]
    assert start_request["identity_edit"] is False
    assert result_request["identity_edit"] is True
    assert result_request["reference_kinds"] == ["state_start"]
    assert "不是重画一个人" in result_request["prompt"]
    assert "炭灰软裤，居家拖鞋" in result_request["prompt"]
    assert "双手空手，不要商品，不要道具，背景只用白或浅灰。" in result_request["prompt"]
    segment = _segment(ad_form="scene_story")
    segment["production"]["characters"] = [
        {"id": "char_host", "name": "女生", "description": "炭灰软裤，居家拖鞋"},
        {"id": "room", "name": "室友", "description": "碎花睡衣"},
    ]
    segment["duration_seconds"] = 9
    segment["speech_spans"] = []
    segment["shots"] = [
        {"id": "s0", "start": 0, "end": 3, "description": "她在吹还湿着的头发。", "character_ids": ["char_host"]},
        {"id": "s1", "start": 3, "end": 6, "description": "头发已经干了，她拨开发尾。", "character_ids": ["char_host"]},
        {"id": "s2", "start": 6, "end": 9, "description": "她把已经干的头发拨到肩前。", "character_ids": ["char_host"]},
    ]
    segment["reference_roles"] = [
        {"index": 1, "role": "product"},
        {"index": 2, "role": "scene"},
        {"index": 3, "role": "character", "character_id": "room"},
        {"index": 4, "role": "state", "phase": "start", "shot_id": "s0", "label": "头发湿",
         "character_id": "char_host", "sheet_layout": "horizontal_16x9"},
        {"index": 5, "role": "state", "phase": "result", "shot_id": "s1", "label": "头发干顺",
         "character_id": "char_host", "sheet_layout": "horizontal_16x9"},
    ]
    prompt = compile_grouped_video(segment)
    wet = _shot_block(prompt, "镜头1 |")
    dry_shot = _shot_block(prompt, "镜头2 |")
    later = _shot_block(prompt, "镜头3 |")
    assert "女生（参考图4）" in wet and "参考图5" not in wet
    assert "女生（参考图5）" in dry_shot and "结果状态参考图5" in dry_shot and "参考图4" not in dry_shot
    assert "女生（参考图5）" in later and "结果状态参考图5" in later and "参考图4" not in later


def test_r20_dryer_and_clothes_are_not_state_refs_and_hair_is_one_pair():
    girl = {"id": "girl", "name": "女生", "description": "灰色卫衣，居家拖鞋，发梢半湿"}
    shots = [
        {"id": "s2", "character_ids": ["girl"], "description": "她按下吹风机，机器开着。",
         "continuity": {"start_state": "主体仍关着", "end_state": "主体已经开着", "allowed_changes": "按下开关"}},
        {"id": "s3", "character_ids": ["girl"], "description": "同一套衣服，她站在床边。",
         "continuity": {"start_state": "同一套衣服", "end_state": "同一套衣服", "allowed_changes": ""}},
        {"id": "s5", "character_ids": ["girl"], "description": "发梢湿成缕。",
         "continuity": {"start_state": "头发是湿的，发梢成缕", "end_state": "头发仍是湿的", "allowed_changes": "风吹过"}},
        {"id": "s7", "character_ids": ["girl"], "description": "头发已经干了，她拨了拨。",
         "continuity": {"start_state": "头发已经干顺", "end_state": "头发已经干顺", "allowed_changes": ""}},
        {"id": "s8", "character_ids": ["girl"], "description": "头发干顺，蓬松披肩。",
         "continuity": {"start_state": "头发干顺", "end_state": "头发干顺，蓬松披肩", "allowed_changes": ""}},
        {"id": "s9", "character_ids": ["girl"], "description": "她关掉吹风机。",
         "continuity": {"start_state": "吹风机已经关掉", "end_state": "吹风机已经关掉", "allowed_changes": "关掉"}},
    ]
    declared = {"subject_label": "头发", "owner": "girl", "start": "湿，发梢成缕",
                "result": "干顺，蓬松披肩", "start_shot": "s5", "result_shot": "s8"}
    plan = {"production": {"speaker_id": "girl", "characters": [
        girl, {"id": "room", "name": "室友", "description": "碎花睡衣"}]},
        "segments": [{"duration_seconds": 15, "shots": shots}],
        "state_changes": [
            {"subject_label": "衣服", "owner": "nobody", "start": "皱", "result": "平",
             "start_shot": "s3", "result_shot": "s5"},
            {"subject_label": "吹风机", "owner": "product", "start": "关", "result": "开",
             "start_shot": "s9", "result_shot": "s2"},
            declared,
            {"subject_label": "头发", "owner": "girl", "start": "乱", "result": "顺",
             "start_shot": "s2", "result_shot": "s3"},
        ]}
    warnings = state_change_warnings(plan)
    assert any("主体不存在" in item for item in warnings)
    assert any("起始镜不在结果镜之前" in item for item in warnings)
    assert any("只保留一条" in item for item in warnings)
    refs = plan_state_references(plan)
    assert [(item["shot_id"], item["phase"], item["label"]) for item in refs] == [
        ("s5", "start", "头发湿·起始"), ("s8", "result", "头发干顺·结果")]
    assert refs[0]["state_change_id"] == refs[1]["state_change_id"]
    assert "衣服" not in "".join(item["subject"] for item in refs)
    assert "主体" not in "".join(item["label"] for item in refs)
    start_prompt = state_design_request({}, plan, refs[0])["prompt"]
    result_prompt = state_design_request({}, plan, refs[1])["prompt"]
    assert "半湿" not in start_prompt and "半湿" not in result_prompt
    assert "灰色卫衣" in start_prompt and "灰色卫衣" in result_prompt
    assert "湿，发梢成缕" in start_prompt
    assert "干顺，蓬松披肩" in result_prompt
    assert "吹风机" not in start_prompt and "同一套衣服" not in start_prompt


def test_r20_dryer_handoff_follows_the_next_start_without_a_keyword_table():
    board = {"shots": [
        {"id": "s8", "description": "她关掉吹风机。", "continuity": {
            "start_state": "吹风机开着", "end_state": "吹风机开着", "allowed_changes": "她把风机关上",
            "entry_transition": "cut"}},
        {"id": "s9", "description": "吹风机已经停了。", "continuity": {
            "start_state": "吹风机已经关掉", "end_state": "吹风机已经关掉", "allowed_changes": "",
            "entry_transition": "cut"}},
    ], "state_changes": []}
    aligned, changed = align_declared_continuity(board)
    assert changed is True
    assert aligned["shots"][0]["continuity"]["end_state"] == "吹风机已经关掉"
    assert aligned["shots"][1]["continuity"]["start_state"] == "吹风机已经关掉"
    hair = {"segments": [{"shots": [
        {"id": "s5", "continuity": {"start_state": "湿发", "end_state": "仍湿", "entry_transition": "opening"}},
        {"id": "s7", "continuity": {"start_state": "已经干", "end_state": "已经干", "entry_transition": "continuous"}},
        {"id": "s8", "continuity": {"start_state": "干了一点", "end_state": "干了一点", "entry_transition": "continuous"}},
        {"id": "s9", "continuity": {"start_state": "吹风机开着", "end_state": "吹风机开着",
                                    "entry_transition": "continuous"}},
    ]}], "state_changes": [_declared("s5", "s8", owner="product", start="湿，发梢成缕", result="干顺，蓬松披肩")]}
    fixed, did = align_declared_continuity(hair)
    assert did is True
    states = [shot["continuity"] for shot in fixed["segments"][0]["shots"]]
    assert states[0]["end_state"] == "湿，发梢成缕"
    assert states[1]["start_state"] == "湿，发梢成缕" and states[1]["end_state"] == "湿，发梢成缕"
    assert states[2]["start_state"] == "干顺，蓬松披肩" and states[2]["end_state"] == "干顺，蓬松披肩"
    assert states[2]["entry_transition"] == "cut"
    assert states[3]["start_state"] == "干顺，蓬松披肩"
    again, twice = align_declared_continuity(fixed)
    assert twice is False and again == fixed


def test_a_result_shot_followed_by_a_hard_cut_settles_instead_of_flipping():
    """The cake hang: result end was rewritten to the next cut, then written back."""
    board = {"shots": [
        {"id": "s1", "continuity": {
            "start_state": "完整蛋糕", "end_state": "完整蛋糕", "allowed_changes": "她把蛋糕切开",
            "entry_transition": "opening"}},
        {"id": "s2", "continuity": {
            "start_state": "切开的蛋糕", "end_state": "切开的蛋糕", "allowed_changes": "她叉起一小块",
            "entry_transition": "cut"}},
        {"id": "s3", "continuity": {
            "start_state": "盘中近景", "end_state": "盘中近景", "allowed_changes": "奶油轻轻晃一下",
            "entry_transition": "cut"}},
    ], "state_changes": [_declared("s1", "s2", subject="蛋糕", start="完整蛋糕", result="切开的蛋糕")]}
    aligned, changed = align_declared_continuity(board)
    assert aligned["shots"][1]["continuity"]["end_state"] == "切开的蛋糕"
    assert aligned["shots"][1]["continuity"]["start_state"] == "切开的蛋糕"
    assert aligned["shots"][2]["continuity"]["start_state"] == "盘中近景"
    again, twice = align_declared_continuity(aligned)
    assert twice is False
    assert again["shots"][1]["continuity"]["end_state"] == "切开的蛋糕"
    third, third_time = align_declared_continuity(again)
    assert third_time is False and third == again


def test_a_prop_count_mismatch_is_copied_forward_and_then_settles():
    board = {"shots": [
        {"id": "s4", "continuity": {
            "start_state": "蛋糕还没切", "end_state": "蛋糕上有两颗樱桃",
            "allowed_changes": "她切下一刀", "entry_transition": "cut"}},
        {"id": "s5", "continuity": {
            "start_state": "蛋糕上剩下一颗樱桃", "end_state": "盘中近景",
            "allowed_changes": "奶油轻轻晃一下", "entry_transition": "cut"}},
    ]}
    repaired, changed = repair_prop_handoff(board)
    assert changed is True
    assert repaired["shots"][0]["continuity"]["end_state"] == "蛋糕上有两颗樱桃"
    assert repaired["shots"][1]["continuity"]["start_state"] == "蛋糕上有两颗樱桃"
    again, twice = repair_prop_handoff(repaired)
    assert twice is False
    assert again["shots"][1]["continuity"]["start_state"] == "蛋糕上有两颗樱桃"
    missing = {"shots": [
        {"id": "s4", "continuity": {"end_state": "蛋糕上有两颗樱桃", "start_state": "蛋糕上有两颗樱桃"}},
        {"id": "s5", "continuity": {"start_state": "蛋糕上没有樱桃", "end_state": "盘中近景"}},
    ]}
    fixed, did = repair_prop_handoff(missing)
    assert did is True
    assert fixed["shots"][1]["continuity"]["start_state"] == "蛋糕上有两颗樱桃"
    people = {"shots": [
        {"id": "s4", "continuity": {"end_state": "桌边有两个人", "start_state": "桌边有两个人"}},
        {"id": "s5", "continuity": {"start_state": "桌边有一个人", "end_state": "桌边有一个人"}},
    ]}
    _same, people_changed = repair_prop_handoff(people)
    assert people_changed is False


def test_a_declared_result_shot_keeps_its_opening_when_the_prop_count_differs():
    board = {"shots": [
        {"id": "s4", "continuity": {
            "start_state": "蛋糕上有两颗樱桃", "end_state": "蛋糕上有两颗樱桃", "entry_transition": "cut"}},
        {"id": "s5", "continuity": {
            "start_state": "蛋糕上剩下一颗樱桃", "end_state": "蛋糕上剩下一颗樱桃", "entry_transition": "cut"}},
    ], "state_changes": [_declared("s4", "s5", subject="蛋糕", start="蛋糕上有两颗樱桃", result="蛋糕上剩下一颗樱桃")]}
    repaired, changed = repair_prop_handoff(board)
    assert changed is False
    assert repaired["shots"][1]["continuity"]["start_state"] == "蛋糕上剩下一颗樱桃"
    aligned, _did_align = align_declared_continuity(board)
    settled, did_repair = repair_prop_handoff(aligned)
    assert did_repair is False
    assert settled["shots"][1]["continuity"]["start_state"] == "蛋糕上剩下一颗樱桃"
    _again, twice = align_declared_continuity(settled)
    assert twice is False


def test_prop_repair_does_not_restart_the_result_shot_flip():
    board = {"shots": [
        {"id": "s1", "continuity": {
            "start_state": "完整蛋糕", "end_state": "完整蛋糕", "allowed_changes": "她把蛋糕切开",
            "entry_transition": "opening"}},
        {"id": "s2", "continuity": {
            "start_state": "切开的蛋糕", "end_state": "切开的蛋糕", "allowed_changes": "她叉起一小块",
            "entry_transition": "cut"}},
        {"id": "s3", "continuity": {
            "start_state": "盘中近景", "end_state": "盘中近景", "allowed_changes": "奶油轻轻晃一下",
            "entry_transition": "cut"}},
    ], "state_changes": [_declared("s1", "s2", subject="蛋糕", start="完整蛋糕", result="切开的蛋糕")]}
    aligned, _changed = align_declared_continuity(board)
    repaired, did_repair = repair_prop_handoff(aligned)
    assert did_repair is False
    again, twice = align_declared_continuity(repaired)
    assert twice is False
    assert again["shots"][1]["continuity"]["end_state"] == "切开的蛋糕"


def test_state_references_off_gives_every_character_one_ordinary_sheet(monkeypatch):
    from app.config import Settings
    from app.story_logic import state_references_enabled

    plan = {"production": {"speaker_id": "girl", "characters": [
        {"id": "girl", "name": "女生", "description": "中长发"},
        {"id": "room", "name": "室友", "description": "短发"},
    ]}, "segments": [{"shots": [
        {"id": "s03", "character_ids": ["girl"], "description": "中长发湿着。",
         "continuity": {"start_state": "头发是湿的", "end_state": "头发是湿的"}},
        {"id": "s05", "character_ids": ["girl"], "description": "头发已经吹干。",
         "continuity": {"start_state": "头发已经吹干", "end_state": "头发已经吹干"}},
    ]}], "state_changes": [_declared("s03", "s05", owner="girl", start="湿", result="吹干")]}
    monkeypatch.setenv("AD_STATE_REFERENCES", "off")
    assert state_references_enabled() is False
    assert plan_state_references(plan) == []
    designs = required_designs(plan)
    assert [item["kind"] for item in designs] == ["scene", "character", "character"]
    assert [item["character_id"] for item in designs if item["kind"] == "character"] == ["girl", "room"]
    assert Settings.from_env().state_references_enabled is False
    monkeypatch.setenv("AD_STATE_REFERENCES", "maybe")
    with pytest.raises(ValueError, match="AD_STATE_REFERENCES"):
        state_references_enabled()
    monkeypatch.setenv("AD_STATE_REFERENCES", "on")
    assert Settings.from_env().state_references_enabled is True
    refs = plan_state_references(plan)
    assert [item["phase"] for item in refs] == ["start", "result"]
    assert [item["kind"] for item in required_designs(plan)] == ["scene", "character", "start_frame", "start_frame"]
    assert required_designs(plan)[1]["character_id"] == "room"


def test_background_pose_follows_the_latest_declared_turn():
    shots = [
        {"id": "sh1", "description": "清晨房间，床上室友长发散在枕上，背对房间侧睡。"},
        {"id": "sh2", "description": "床还在后面，室友还睡着。"},
        {"id": "sh3", "description": "过肩看床。室友翻了个身，脸朝房间一侧，仍闭着眼睡着。"},
        {"id": "sh4", "description": "背景床上，室友还睡着。"},
    ]
    plan = {"segments": [{"shots": shots}]}
    assert background_issues(plan) == []
    turned = with_background_line(shots[2]["description"], shots, 2)
    assert "仍背对" not in turned
    assert "脸朝房间一侧" in turned
    carried = with_background_line(shots[3]["description"], shots, 3)
    assert "室友仍脸朝房间" in carried
    assert "仍背对" not in carried
    assert "同一套衣服" in carried
    earlier = with_background_line(shots[1]["description"], shots, 1)
    assert "室友仍背对" in earlier
    assert background_issues(_board(
        _shot("夜里，室友侧躺在床上睡着。"),
        _shot("床上的室友面向镜头。"),
    ))
    assert "背景人物朝向和前面不一致" in background_issues(_board(
        _shot("夜里，室友侧躺在床上睡着。"),
        _shot("床上的室友面向镜头。"),
    ))


def _r17_quiet_night():
    """The local e2e board: the sleeper stays, named once and then only as 床上的人."""
    return [
        _shot("夜里，室友侧躺在床上，脸朝房间一侧。", id="s1"),
        _shot("床上，室友保持侧躺。", id="s2"),
        _shot("吹风机还在响。床上的人仍在睡。", id="s3", continuity={
            "start_state": "吹风机对着头发", "end_state": "室友还在睡", "allowed_changes": "",
        }),
        _shot("镜头留在床边。床上的人仍在睡。", id="s4"),
        _shot("床还在画面里。焦点从吹风机移到室友脸上，没有翻身。", id="s5"),
        _shot("床上的人仍侧躺睡着。", id="s6"),
        _shot("窗外微亮，床上的人仍侧躺睡着。", id="s7"),
        _shot("她放下吹风机。床上的人仍侧躺睡着。", id="s8"),
    ]


def test_equivalent_bed_references_keep_the_sleeper_and_the_pose():
    shots = _r17_quiet_night()
    plan = {"segments": [{"shots": shots}]}
    assert background_issues(plan) == []
    stamped, changed = stamp_background_lines({"shots": shots})
    assert changed is True
    filled = {shot["id"]: shot["description"] for shot in stamped["shots"]}
    assert "室友仍侧躺，朝向不变" in filled["s3"]
    assert "室友仍侧躺，朝向不变" in filled["s4"]
    assert "同一套衣服" in filled["s4"]
    assert "室友" not in shots[3]["description"]
    assert "仍侧躺" in filled["s5"]
    assert background_issues(stamped) == []
    assert background_issues(_board(
        shots[0], _shot("她还在睡，床没有动。", id="s9"),
    )) == []
    missing = _board(shots[0], _shot("镜头回到床边，被子还在。", id="s9"))
    assert "背景人物没有跟着床入画" in background_issues(missing)
    assert "背景人物没有跟着床入画" in background_issues(_board(
        shots[0], _shot("她坐在床沿吹头发。", id="s9"),
    ))
    assert "背景人物没有跟着床入画" in background_issues(_board(
        shots[0], _shot("床是空的，室友已经起来。", id="s9"),
    ))
    assert with_background_line("床上没有人。", shots) == "床上没有人。"
    assert storyboard_content_failure("背景人物没有跟着床入画，这次不采用", ["背景人物没有跟着床入画"])
    assert storyboard_content_failure("剧情末句还在点题") is False


def test_background_contradiction_is_rewritten_once_then_kept_only_if_fixed():
    director = {"shots": [
        _shot("夜里，室友侧躺在床上睡着。", id="s1"),
        _shot("镜头回到床边，被子还在。", id="s2"),
    ]}
    fixed = {"shots": [{"shot_id": "s2", "description": "镜头回到床边，室友仍侧躺，朝向不变。"}]}
    revised = apply_background_rewrite(director, fixed, [{
        "shot_id": "s2", "description": director["shots"][1]["description"], "name": "室友", "pose": "侧躺",
    }])
    assert revised is not None and "室友仍侧躺" in revised["shots"][1]["description"]
    assert apply_background_rewrite(director, {"shots": [{
        "shot_id": "s2", "description": "镜头回到床边，被子还在。",
    }]}, [{"shot_id": "s2", "description": "镜头回到床边，被子还在。", "name": "室友", "pose": "侧躺"}]) is None
    turned = {"shots": [
        _shot("夜里，室友侧躺在床上睡着。", id="s1"),
        _shot("床上的室友面向镜头。", id="s2"),
    ]}
    assert apply_background_rewrite(turned, {"shots": [{
        "shot_id": "s2", "description": "床上的室友面向镜头，看向镜头。",
    }]}, [{"shot_id": "s2", "description": "床上的室友面向镜头。", "name": "室友", "pose": "侧躺"}]) is None

    async def repair(system, payload, assets, max_tokens=1200):
        assert payload["task_kind"] == "background_continuity_rewrite"
        assert payload["shots"][0]["shot_id"] == "s2"
        return fixed

    called, replacement = asyncio.run(rewrite_background_if_needed(repair, director, director))
    assert called is True and replacement is not None
    assert background_issues(replacement) == []

    async def still_wrong(*args, **kwargs):
        return {"shots": [{"shot_id": "s2", "description": "镜头回到床边，被子还在。"}]}

    called, replacement = asyncio.run(rewrite_background_if_needed(still_wrong, director, director))
    assert called is True and replacement is None

    async def unused(*args, **kwargs):
        raise AssertionError("continuous board")

    called, replacement = asyncio.run(rewrite_background_if_needed(unused, {"shots": _r17_quiet_night()}, {"shots": _r17_quiet_night()}))
    assert called is False and replacement is None


def test_two_people_in_one_frame_are_told_apart_on_the_character_sheet():
    from app.design import character_design_request
    lead = {"id": "xia", "name": "小夏", "description": "及肩黑发，穿浅色碎花睡衣"}
    roommate = {"id": "room", "name": "室友", "description": "长黑发，穿粉色碎花睡衣，侧躺"}
    plan = {"production": {"speaker_id": "xia", "characters": [lead, roommate]}, "segments": [{"shots": [
        {"id": "sh3", "description": "小夏看向床上的室友。", "character_ids": ["xia", "room"]},
    ]}]}
    lead_prompt = character_design_request({}, plan, lead)["prompt"]
    assert "同框区分" in lead_prompt and "纯色" in lead_prompt and "不要碎花" in lead_prompt
    assert "粉色碎花睡衣" in lead_prompt
    roommate_prompt = character_design_request({}, plan, roommate)["prompt"]
    assert "明显不同的颜色" in roommate_prompt and "不要碎花" in roommate_prompt
    alone = {"id": "xia", "name": "小夏", "description": "及肩黑发，纯色白上衣"}
    solo = {"production": {"characters": [alone]}, "segments": [{"shots": [
        {"id": "sh1", "description": "她站在台前。", "character_ids": ["xia"]},
    ]}]}
    assert "同框区分" not in character_design_request({}, solo, alone)["prompt"]
    assert cast_distinction_issues(plan) == ["同框角色的衣服和发型太接近"]
    distinct = {"production": {"speaker_id": "xia", "characters": [
        {"id": "xia", "name": "小夏", "description": "及肩黑发，纯色白上衣"},
        {"id": "room", "name": "室友", "description": "长发盘起，深蓝色睡衣"},
    ]}, "segments": plan["segments"]}
    assert cast_distinction_issues(distinct) == []


def test_story_ending_does_not_aim_the_logo_at_the_camera():
    segment = _segment(ad_form="scene_story")
    segment["shots"][1]["description"] = "她把吹风机放回台面，机身 Laifen 字样朝向镜头，筒口朝上。"
    segment["shots"][1]["continuity"] = {
        "entry_transition": "cut", "start_state": "吹风机握在手里", "allowed_changes": "放回台面",
        "end_state": "吹风机立在台面，机身 Laifen 字样朝镜头", "camera_move": "推近后停住",
    }
    ending = _shot_block(compile_grouped_video(segment), "镜头2 |")
    assert "字样朝向镜头" not in ending and "字样朝镜头" not in ending
    assert "正面朝镜头" not in ending
    assert "按放下时的样子留在原位" not in ending
    assert "第一帧" in ending and "机位停住" in ending
    assert "镜头推近" not in ending and "推近后" not in ending
    assert "正面朝镜头" in compile_grouped_video(_segment())


def test_existing_door_and_vapor_checks_stay():
    assert "摔门砸了安静" in action_issues("她摔门出去。", "夜里要安静")
    assert "带门要轻" in action_issues("她带门离开", "别吵")
    raw = {"description": "筒口喷出白烟。", "continuity": {"end_state": "发尾湿塌", "allowed_changes": ""}}
    assert "喷嘴喷烟" in state_change_issues(raw, "吹干了")


def test_r21_negated_ending_words_do_not_trip_hard_gates():
    strategy = _dryer()
    denied = _board(_shot("她把吹风机放回台面。没有字卡或标语。"))
    assert story_plan_issues(denied, strategy) == []
    assert story_ending_issues(_board(_shot("不要英雄定格，也不举向镜头。")), strategy) == []
    quiet = _board(_shot("她不看镜头，不要把字样朝向镜头，没有链接。"))
    assert story_plan_issues(quiet, strategy) == []
    assert story_gaze_line({**quiet["segments"][0]["shots"][0], "character_ids": ["char_host"]}, "scene_story") == "  本镜人物不看镜头。"
    assert "剧情末镜在喊口号或链接" in story_plan_issues(_board(_shot("结尾打出标语。")), strategy)
    assert "剧情末镜写成了英雄定格" in story_ending_issues(_board(_shot("英雄定格。")), strategy)
    assert "剧情末镜人物对着镜头举着产品" in story_ending_issues(_board(_shot("她把吹风机举向镜头。")), strategy)
    assert "剧情末镜把商标朝向镜头" in story_plan_issues(_board(_shot("机身字样朝向镜头。")), strategy)
    assert "剧情人物看着镜头" in story_plan_issues(_board(_shot("她看着镜头。")), strategy)


def test_r21_bed_out_of_frame_does_not_keep_the_sleeper():
    asleep = _shot("夜里，室友侧躺在床上睡着。", id="s0")
    gone = "她披上外套。床不在这一镜的画面里。"
    board = _board(asleep, _shot(gone, id="s6"), _shot("门口站定。床不在这一镜的画面里。", id="s7"))
    assert background_issues(board) == []
    stamped, changed = stamp_background_lines(board)
    assert changed is False
    assert "仍在原来的床位" not in stamped["segments"][0]["shots"][1]["description"]
    assert "仍在原来的床位" not in stamped["segments"][0]["shots"][2]["description"]
    assert with_background_line(gone, board["segments"][0]["shots"], 1) == gone
    segment = _segment(ad_form="scene_story")
    segment["shots"][0]["description"] = "夜里，室友侧躺在床上睡着。"
    segment["shots"][1]["description"] = gone
    prompt = compile_grouped_video(segment)
    ending = _shot_block(prompt, "镜头2 |")
    assert "仍在原来的床位" not in ending
    assert "床不在这一镜的画面里" in ending


def _speech(span_id, beat_id, text, start, end, shot_id):
    return {"id": span_id, "beat_id": beat_id, "text": text, "start": start, "end": end,
            "shot_ids": [shot_id],
            "anchors": [{"text": text, "start": start, "end": end, "shot_ids": [shot_id]}]}


def test_r21_third_line_moves_into_the_shot_that_says_she_speaks():
    shots = [
        {"id": "s1", "start": 0.0, "end": 2.5, "beat_id": "b1", "beat_ids": ["b1"], "description": "她开口说第一句。"},
        {"id": "s2", "start": 2.5, "end": 4.0, "beat_id": "b2", "beat_ids": ["b2"], "description": "她开口说第二句。"},
        {"id": "s4", "start": 4.0, "end": 7.8, "beat_id": "b3", "beat_ids": ["b3"], "description": "她没说话，只看了室友一眼。"},
        {"id": "s5", "start": 7.8, "end": 12.0, "beat_id": "b4", "beat_ids": ["b4"], "description": "她开口说第三句。"},
    ]
    third = _speech("speech_3", "b3", "你接着睡", 6.1, 7.8, "s4")
    spans = [
        _speech("speech_1", "b1", "你醒着？", 0.4, 1.6, "s1"),
        _speech("speech_2", "b2", "那我吹了。", 2.8, 3.8, "s2"),
        third,
    ]
    groups = [
        {"beat_id": "b1", "start": 0.0, "end": 2.5, "speech_start": 0.4, "speech_end": 1.6},
        {"beat_id": "b2", "start": 2.5, "end": 4.0, "speech_start": 2.8, "speech_end": 3.8},
        {"beat_id": "b3", "start": 4.0, "end": 7.8, "speech_start": 6.1, "speech_end": 7.8},
        {"beat_id": "b4", "start": 7.8, "end": 12.0},
    ]
    segments = [{"shots": shots, "speech_spans": spans}]
    schedule = [{"groups": groups}]
    assert snap_declared_speech(segments, schedule) is True
    assert third["shot_ids"] == ["s5"]
    assert third["anchors"][0]["shot_ids"] == ["s5"]
    assert third["start"] == 6.1 and third["end"] == 7.8
    assert shots[2]["end"] == 6.1 and shots[3]["start"] == 6.1
    assert "b3" in shots[3]["beat_ids"]
    assert spans[0]["shot_ids"] == ["s1"] and spans[1]["shot_ids"] == ["s2"]
    assert snap_declared_speech(segments, schedule) is False


def test_r21_short_silent_shot_retimes_the_line_into_the_speaking_shot():
    shots = [
        {"id": "s0", "start": 0.0, "end": 6.0, "beat_id": "b1", "beat_ids": ["b1", "b2"],
         "description": "她开口说第一句，又说第二句。"},
        {"id": "s4", "start": 6.0, "end": 7.8, "beat_id": "b3", "beat_ids": ["b3"], "description": "她没说话。"},
        {"id": "s5", "start": 7.8, "end": 12.0, "beat_id": "b4", "beat_ids": ["b4"], "description": "她开口说第三句。"},
    ]
    third = _speech("speech_3", "b3", "你接着睡", 6.1, 7.8, "s4")
    spans = [
        _speech("speech_1", "b1", "你醒着？", 0.2, 1.0, "s0"),
        _speech("speech_2", "b2", "那我吹了。", 3.2, 4.2, "s0"),
        third,
    ]
    groups = [
        {"beat_id": "b1", "start": 0.0, "end": 3.0, "speech_start": 0.2, "speech_end": 1.0},
        {"beat_id": "b2", "start": 3.0, "end": 6.0, "speech_start": 3.2, "speech_end": 4.2},
        {"beat_id": "b3", "start": 6.0, "end": 7.8, "speech_start": 6.1, "speech_end": 7.8},
        {"beat_id": "b4", "start": 7.8, "end": 12.0},
    ]
    assert snap_declared_speech([{"shots": shots, "speech_spans": spans}], [{"groups": groups}]) is True
    assert third["shot_ids"] == ["s5"]
    assert third["start"] == 7.8 and abs(third["end"] - 9.5) < 1e-6
    assert groups[2]["speech_start"] == 7.8 and abs(groups[2]["speech_end"] - 9.5) < 1e-6
    assert shots[1]["end"] == 7.8 and shots[2]["start"] == 7.8


def test_a_jacket_in_the_shot_warns_when_the_appearance_only_has_the_sweatshirt():
    plan = {"production": {"characters": [
        {"id": "girl", "description": "二十多岁，黑色长发，灰色卫衣，帆布鞋。"},
    ]}, "segments": [{"shots": [
        _shot("她穿上外套，拿起吹风机。", id="s3", character_ids=["girl"]),
        _shot("她没有穿外套，还是灰色卫衣。", id="s4", character_ids=["girl"]),
    ]}]}
    assert wardrobe_warnings(plan) == ["镜头里的外套没有写进角色外观"]
    listed = {"production": {"characters": [
        {"id": "girl", "description": "二十多岁，黑色长发，灰色卫衣，外套稍后披上或拿着。"},
    ]}, "segments": [{"shots": [_shot("她穿上外套。", id="s3", character_ids=["girl"])]}]}
    assert wardrobe_warnings(listed) == []


def test_story_plan_issues_names_the_blocking_problem():
    strategy = _dryer()
    plan = _board(
        _shot("她吹干头发。", id="s0"),
        _shot("她拿着吹风机出门。", "英雄定格", id="s1"),
        script=_lines("它让我没白折回来。"),
    )
    issues = story_plan_issues(plan, strategy)
    assert issues == ["剧情末镜写成了英雄定格"]
    assert "剧情末镜把家用物品带出了门" in story_ending_issues(plan, strategy)
    assert "剧情末句还在点题" not in issues
    assert "剧情卖点没有高光证明镜" not in issues
    assert story_plan_issues(plan, typed("brand_tvc")) == []
    looking = _board(_shot("她看着镜头，手里拿着吹风机。"))
    assert "剧情人物看着镜头" in story_plan_issues(looking, strategy)
    logo = _board(_shot("吹风机立在台面，机身字样朝向镜头。"))
    assert "剧情末镜把商标朝向镜头" in story_plan_issues(logo, strategy)
    slogan = _board(_shot("她把吹风机放回台面。"), script=_lines("去看看链接。"))
    assert "剧情末镜在喊口号或链接" in story_plan_issues(slogan, strategy)


def test_story_lines_must_not_score_a_benefit():
    strategy = _dryer()
    for line in ("一拨就顺。", "干得快。", "这风不吵。", "太安静了。", "负离子护发。", "不毛躁了。"):
        names = [name for item in script_line_offenses(_lines("我尽量轻点。", line), strategy) for name in item["offenses"]]
        assert "在说卖点" in names, line
    for line in ("我尽量轻点。", "你醒着？", "那我吹了。", "算了，先吹干。", "别吵。"):
        names = [name for item in script_line_offenses(_lines(line), strategy) for name in item["offenses"]]
        assert "在说卖点" not in names, line
    presenter = [name for item in script_line_offenses(_lines("一拨就顺。"), typed("presenter_pitch")) for name in item["offenses"]]
    assert "在说卖点" not in presenter
    script = _lines("你醒着？", "一拨就顺。")
    rejected = apply_line_rewrite(
        script, {"lines": [{"beat_id": "b1", "narration": "一拨就顺。"}]},
        limits={"b1": 40}, strategy=strategy, duration_seconds=20)
    assert rejected is None
    accepted = apply_line_rewrite(
        script, {"lines": [{"beat_id": "b1", "narration": "那我吹了。"}]},
        limits={"b1": 40}, strategy=strategy, duration_seconds=20)
    assert accepted["beats"][1]["narration"] == "那我吹了。"
    issues = story_plan_issues(_board(_shot("她拨了拨头发。"), script=_lines("一拨就顺。")), strategy)
    assert "剧情对白还在说卖点" in issues


def test_rack_focus_onto_the_roommate_counts_as_the_quiet_highlight():
    strategy = _dryer("风很安静，夜里不吵")
    rack = _board(_shot("过肩近景，焦点从女生侧脸拉到室友脸上，室友睁眼。"))
    assert proof_highlight_issues(rack, strategy) == []
    glance = _board(_shot("她看了室友一眼。"))
    assert proof_highlight_issues(glance, strategy) == ["剧情卖点没有高光证明镜"]


def test_generated_state_stills_pass_the_creative_gate(tmp_path):
    from PIL import Image
    from app.db import Store

    store = Store(tmp_path / "gate.sqlite3", tmp_path / "media")
    project = store.create_project("状态参考过确认", "product", duration_seconds=20)
    story = {"production": {"characters": []}, "segments": [{"shots": [
        _hair_process("s3", "被吹开一半"), _hair_result(),
    ]}], "state_changes": [_declared("s3", "s6")]}
    refs = plan_state_references(story)
    assert [item["phase"] for item in refs] == ["start", "result"]
    saved = store.update_plan(project["id"], {"title": "先确认方案", "segments": [
        {"id": "seg_0", "duration_seconds": 10}, {"id": "seg_1", "duration_seconds": 10},
    ]})
    scene_path = store.media_root / "scene.png"
    Image.new("RGB", (8, 8), "gray").save(scene_path)
    scene = store.add_asset(saved["id"], "scene", "场景", scene_path, "image/png", metadata={"generated": True})
    plain_path = store.media_root / "plain.png"
    Image.new("RGB", (8, 8), "blue").save(plain_path)
    plain = store.add_asset(saved["id"], "start_frame", "普通首帧", plain_path, "image/png", metadata={"generated": True})
    job = store.create_job(saved["id"], "generate", saved["revision"], {"pause_for_creative": True})
    assert store.claim_job(job["id"])
    option = {"id": "current", "label": "保持当前图片", "description": "确认场景和状态参考", "segment": {
        "id": "seg_0", "duration_seconds": 10, "purpose": "呈现已知操作",
        "prompt": "商品操作计划", "narration": "三档风速自己选。", "reference_asset_ids": ["product"],
        "shots": [{"id": "shot_1", "start": 0, "end": 10, "description": "手按已确认的风速键。",
                   "narration": "", "purpose": "演示操作", "beat_id": "beat_0"}],
        "speech_spans": [{"id": "speech_1", "beat_id": "beat_0", "text": "三档风速自己选。",
                          "start": 0, "end": 10, "shot_ids": ["shot_1"], "anchors": [
                              {"text": "三档风速自己选。", "start": 0, "end": 10, "shot_ids": ["shot_1"],
                               "visual_evidence": "编排器提供的待审画面说明"}]}]}}
    payload = store.get_job(job["id"])["payload"]
    payload["plan"] = story
    payload["design_asset_ids"] = [scene["id"], plain["id"]]
    store.update_job(job["id"], payload=payload)
    with pytest.raises(ValueError, match="请先准备并确认状态变化的起始与结果参考图"):
        store.create_creative_gate(job["id"], [option], "确认图片")
    stills = []
    for ref in refs:
        path = store.media_root / f"{ref['state_id'].replace(':', '_')}.png"
        Image.new("RGB", (8, 8), "green").save(path)
        stills.append(store.add_asset(
            project["id"], "start_frame", ref["label"], path, "image/png",
            metadata={"generated": True, "state_reference": True, "state_id": ref["state_id"],
                      "phase": ref["phase"], "shot_id": ref["shot_id"], "state_label": ref["label"]}))
    original = path.read_bytes()
    path.write_bytes(b"changed-bytes")
    payload = store.get_job(job["id"])["payload"]
    payload["design_asset_ids"] = [scene["id"], *[item["id"] for item in stills]]
    store.update_job(job["id"], payload=payload)
    with pytest.raises(ValueError, match="缺失或已经变化"):
        store.create_creative_gate(job["id"], [option], "确认图片")
    path.write_bytes(original)
    gate = store.create_creative_gate(job["id"], [option], "确认图片")
    assert gate["kind"] == "creative" and gate["status"] == "pending"


def _r22():
    return json.loads(Path(__file__).parent.joinpath("fixtures/r22_scene.json").read_text())


def _r22_plan():
    raw = _r22()
    characters = [
        {**raw["characters"][0], "name": "小夏"},
        {**raw["characters"][1], "name": "阿宁"},
    ]
    shots = []
    for shot in raw["shots"]:
        shots.append({**shot, "beat_id": shot["beat_ids"][0], "purpose": "这一下"})
    spans = []
    for index, span in enumerate(raw["speech_spans"], start=1):
        spans.append({**span, "id": f"speech_{index}",
                      "anchors": [{"text": span["text"], "start": span["start"], "end": span["end"],
                                   "shot_ids": list(span["shot_ids"])}]})
    return {
        "production": {"characters": characters, "speaker_id": "char_xia",
                       "speech_mode": "character_dialogue", "voice_description": "自然的成年女性普通话"},
        "strategy": _dryer().model_dump(),
        "state_changes": raw["state_changes"],
        "segments": [{"id": "seg_1", "duration_seconds": 20, "shots": shots, "speech_spans": spans,
                      "ad_form": "scene_story"}],
    }


def test_r22_line_lands_in_the_shot_that_says_she_speaks():
    plan = _r22_plan()
    segment = plan["segments"][0]
    shots = segment["shots"]
    spans = segment["speech_spans"]
    groups = [
        {"beat_id": "b1", "start": 0.0, "end": 2.6, "speech_start": 0.2, "speech_end": 1.9},
        {"beat_id": "b2", "start": 2.6, "end": 7.9},
        {"beat_id": "b3", "start": 7.9, "end": 12.7, "speech_start": 8.1, "speech_end": 9.8},
        {"beat_id": "b4", "start": 12.7, "end": 17.4, "speech_start": 12.9, "speech_end": 15.6},
        {"beat_id": "b5", "start": 17.4, "end": 20.0},
    ]
    second = spans[1]
    assert snap_declared_speech([segment], [{"groups": groups}]) is True
    assert second["shot_ids"] == ["s5"]
    assert second["start"] == 9.8 and abs(second["end"] - 11.5) < 1e-6
    assert shots[3]["start"] == 7.9 and shots[3]["end"] == 9.8
    assert shots[4]["start"] == 9.8 and shots[4]["end"] == 12.7
    video = {
        "duration_seconds": 20, "generation_strategy": "continuity_v1", "ad_form": "scene_story",
        "production": plan["production"], "shots": shots, "speech_spans": spans,
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_xia"},
            {"index": 3, "role": "character", "character_id": "char_ning"},
        ],
    }
    prompt = compile_grouped_video(video)
    fourth = _shot_block(prompt, "镜头4 |")
    fifth = _shot_block(prompt, "镜头5 |")
    assert "嘴型与台词同步" not in prompt
    assert "开口说" not in fourth
    assert "你接着睡" not in fourth
    assert "不对口型" in fifth
    assert "内心声音" in fifth
    assert "9.8–11.5秒" in prompt
    assert "关联镜头 镜头5" in prompt
    assert "声音方式：画外旁白" not in prompt
    assert "内心独白" in prompt


def test_silence_is_not_an_empty_bed():
    raw = _r22()
    asleep = _shot("夜里，室友侧躺在床上睡着。", id="s0")
    from app.story_logic import _background_absent
    assert _background_absent(raw["shots"][-1]["description"]) is False
    assert _background_absent(raw["r21b_s8_description"]) is False
    quiet = _board(asleep, _shot("床上的室友仍侧躺闭眼睡着。画面里没有人开口。", id="s7"))
    assert background_issues(quiet) == []
    muted = _board(asleep, _shot(raw["r21b_s8_description"], id="s8"))
    assert background_issues(muted) == []
    empty = _board(asleep, _shot("镜头回到床边，床上没有人。", id="s1"))
    assert "背景人物没有跟着床入画" in background_issues(empty)
    bare = _board(asleep, _shot("床是空的。", id="s1"))
    assert "背景人物没有跟着床入画" in background_issues(bare)
    assert with_background_line("床上没有人。", [asleep, {"description": "床上没有人。"}]) == "床上没有人。"


def test_state_sheet_wears_only_this_shots_clothes():
    plan = _r22_plan()
    person = plan["production"]["characters"][0]
    sheet = character_design_request({}, plan, person)
    assert "薄外套" in sheet["prompt"]
    assert "手里拿吹风机" not in sheet["prompt"]
    assert "双手空手，不要商品" in sheet["prompt"]
    assert "手里拿吹风机" not in person["description"]
    assert "薄外套" in person["description"]
    start = state_design_request({}, plan, {
        "phase": "start", "label": "湿", "subject": "头发", "shot_id": "s3",
        "character_id": "char_xia", "state_text": "湿，发梢成缕"})
    result = state_design_request({}, plan, {
        "phase": "result", "label": "干顺", "subject": "头发", "shot_id": "s4",
        "character_id": "char_xia", "state_text": "干顺，蓬松披肩"})
    for request in (start, result):
        assert "家居短袖" in request["prompt"]
        assert "薄外套" not in request["prompt"]
        assert "手里拿" not in request["prompt"]
    assert "与起始图保持一致" not in start["prompt"]
    assert "不要换颜色" in result["prompt"]
    assert "与起始图保持一致" in result["prompt"]
    assert state_sheet_clothing_notes(plan) == ["状态图只画这一镜身上的衣服，稍后才披的没有提前画上。"]
    assert "状态图只画这一镜身上的衣服" not in story_plan_issues(plan, _dryer())


def test_asleep_company_becomes_inner_monologue_and_awake_company_stays_dialogue():
    plan = _r22_plan()
    segment = plan["segments"][0]
    groups = [
        {"beat_id": "b1", "start": 0.0, "end": 2.6, "speech_start": 0.2, "speech_end": 1.9},
        {"beat_id": "b2", "start": 2.6, "end": 7.9},
        {"beat_id": "b3", "start": 7.9, "end": 12.7, "speech_start": 8.1, "speech_end": 9.8},
        {"beat_id": "b4", "start": 12.7, "end": 17.4, "speech_start": 12.9, "speech_end": 15.6},
        {"beat_id": "b5", "start": 17.4, "end": 20.0},
    ]
    snap_declared_speech([segment], [{"groups": groups}])
    notes = settle_inner_monologue(plan)
    assert notes == ["这一场没有醒着的人听，台词改成她自己的内心独白，不对口型。"]
    assert plan["production"]["speech_mode"] == "character_dialogue"
    spoken = plan["segments"][0]["shots"][4]["description"]
    assert "开口说" not in spoken
    assert "心里想" in spoken
    assert "放轻脚步" not in spoken and "回头看" not in spoken and "屏住" not in spoken
    assert spoken.count("这句是她的内心独白，嘴没有动。") == 1
    again = settle_inner_monologue(plan)
    assert again == notes
    assert plan["segments"][0]["shots"][4]["description"].count("嘴没有动") == 1
    assert voice_identity_issues(_dryer(), plan["production"]) == []
    assert voice_identity_issues(_dryer(), {"speech_mode": "voiceover"}) == ["剧情被写成了整条旁白"]
    awake = _r22_plan()
    awake["production"]["characters"][1]["description"] = "二十多岁，穿深色家居睡衣裤，坐在床边。"
    for shot in awake["segments"][0]["shots"]:
        shot["description"] = shot["description"].replace("睡着", "坐着").replace("闭眼", "睁眼")
    kept = awake["segments"][0]["shots"][4]["description"]
    assert settle_inner_monologue(awake) == []
    assert awake["segments"][0]["shots"][4]["description"] == kept
    segment = _segment(ad_form="scene_story")
    segment["production"]["characters"].append({"id": "room", "name": "室友", "description": "坐在旁边，醒着"})
    segment["shots"][0]["description"] = "她开口说，室友坐在旁边看着她。"
    segment["shots"][0]["character_ids"] = ["char_host", "room"]
    segment["reference_roles"].append({"index": 4, "role": "character", "character_id": "room"})
    prompt = compile_grouped_video(segment)
    assert "嘴型与台词同步" in _shot_block(prompt, "镜头1 |")


def test_a_line_said_alone_drops_lip_sync_even_when_someone_else_is_awake():
    production = {
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_ning", "name": "阿宁", "description": "二十多岁，坐在旁边，醒着"},
        ],
        "speaker_id": "char_xia",
        "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }
    shots = [
        {"id": "s1", "start": 0.0, "end": 4.0, "character_ids": ["char_xia", "char_ning"],
         "description": "小夏看着醒着的阿宁，开口说第一句。"},
        {"id": "s2", "start": 4.0, "end": 8.0, "character_ids": ["char_xia"],
         "description": "小夏一个人把蛋糕往柜子里推，开口说第二句。"},
    ]
    spans = [
        {"id": "speech_1", "start": 0.4, "end": 2.0, "shot_ids": ["s1"], "text": "你先出去。",
         "anchors": [{"text": "你先出去。", "start": 0.4, "end": 2.0, "shot_ids": ["s1"]}]},
        {"id": "speech_2", "start": 4.4, "end": 6.0, "shot_ids": ["s2"], "text": "再进一点点。",
         "anchors": [{"text": "再进一点点。", "start": 4.4, "end": 6.0, "shot_ids": ["s2"]}]},
    ]
    plan = {"production": production, "strategy": _dryer().model_dump(),
            "segments": [{"id": "seg_1", "duration_seconds": 8, "shots": shots, "speech_spans": spans}]}
    notes = settle_inner_monologue(plan)
    assert notes == ["这一镜没有醒着的人听，台词改成她自己的内心独白，不对口型。"]
    assert "开口说" in shots[0]["description"]
    assert "心里想" in shots[1]["description"]
    assert "开口说" not in shots[1]["description"]
    assert "内心独白" in shots[1]["description"]
    prompt = compile_grouped_video({
        "duration_seconds": 8, "generation_strategy": "continuity_v1", "ad_form": "scene_story",
        "production": production, "shots": shots, "speech_spans": spans,
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_xia"},
            {"index": 3, "role": "character", "character_id": "char_ning"},
        ],
    })
    first = _shot_block(prompt, "镜头1 |")
    second = _shot_block(prompt, "镜头2 |")
    assert "嘴型与台词同步" in first
    assert "不对口型" not in first
    assert "嘴型与台词同步" not in second
    assert "不对口型" in second
    assert "再进一点点。" in prompt
    playbook = Path("app/skills/shared/ad-form-playbooks/scene-story.md").read_text()
    review = Path("app/skills/ads-video-plan-review/references/plan-review.md").read_text()
    assert "再进一点点。" in playbook and "再进一点点。" in review
    assert "这一镜里没有另一个醒着的人" in playbook


def test_bare_wear_is_stripped_and_shot_gender_is_locked():
    from app.design import settle_cast_appearance
    from app.strategy import prepare_strategy_ingress

    person = {"id": "char_host", "name": "主讲", "description": "戴银色头戴式耳机，穿深色上衣。"}
    plan = {"facts": {"name": "降噪耳机"}, "production": {"characters": [person]}, "segments": [{"shots": [
        {"id": "s1", "description": "她把耳机举到镜头前。", "character_ids": ["char_host"]},
        {"id": "s7", "description": "她摘下耳机。", "character_ids": ["char_host"]},
    ]}]}
    assert settle_cast_appearance(plan) is True
    assert "耳机" not in person["description"]
    assert "深色上衣" in person["description"]
    prompt = character_design_request({}, plan, person)["prompt"]
    assert "耳机" not in prompt
    assert "不要画成男性" in prompt
    assert "深色上衣" in prompt
    offered = prepare_strategy_ingress({
        "kind": "directions",
        "message": "三个方向",
        "brief": {"product_name": "降噪耳机", "ad_form": "brand_tvc", "audience": "通勤的人", "scenario": "地铁"},
        "directions": [{
            "id": "d1",
            "audience": "通勤的人",
            "scenario": "地铁",
            "production_notes": ["不编功效"],
            "hook_type": "mechanism",
            "production": {"characters": [{"id": "char_host", "name": "主讲", "description": "戴银色头戴式耳机，短发。"}]},
        }],
    })
    stored = offered["directions"][0]["production"]["characters"][0]["description"]
    assert "耳机" not in stored and "短发" in stored
    male = {"id": "char_host", "name": "主讲", "description": "成年男性，他短发。"}
    shot = {"id": "s1", "description": "她把耳机举到镜头前。", "character_ids": ["char_host"]}
    male_plan = {"facts": {"name": "降噪耳机"}, "production": {"characters": [male]}, "segments": [{"shots": [shot]}]}
    assert settle_cast_appearance(male_plan) is True
    assert male["gender"] == "male"
    assert male["description"] == "成年男性，他短发。"
    assert shot["description"] == "他把耳机举到镜头前。"
    prompt = character_design_request({}, male_plan, male)["prompt"]
    assert "不要画成女性" in prompt
    assert "按镜头" not in prompt


def test_character_sheet_does_not_wear_the_product():
    person = {"id": "char_host", "name": "小夏",
              "description": "二十多岁，穿浅色针织衫，全程佩戴银色头戴式耳机，手里拿着银色头戴式耳机。"}
    plan = {"facts": {"name": "头戴式耳机"}, "production": {"characters": [person]}, "segments": [{"shots": [
        {"id": "s1", "description": "她手里拿着银色头戴式耳机。", "character_ids": ["char_host"]},
    ]}]}
    assert character_sheet_product_notes(plan) == [
        "角色设定图不画商品。全程佩戴和镜头里拿在手里对不上，已记下，不因此停下。"]
    prompt = character_design_request({}, plan, person)["prompt"]
    assert "浅色针织衫" in prompt
    assert "全程佩戴" not in prompt and "头戴式耳机" not in prompt
    assert "手里拿" not in prompt
    assert "不要画在身上或手里" in prompt
    assert "女性" in prompt and "不要画成男性" in prompt
    assert "耳机" not in person["description"]
    assert "浅色针织衫" in person["description"]
    worn = {"production": {"characters": [{"id": "char_host", "name": "小夏",
                                           "description": "二十多岁，全程佩戴银色头戴式耳机。"}]},
            "segments": [{"shots": [{"id": "s1", "description": "她戴着银色头戴式耳机看向窗外。",
                                     "character_ids": ["char_host"]}]}]}
    assert character_sheet_product_notes(worn) == []


def test_a_friend_opposite_keeps_the_line_and_a_hands_macro_stays_in_frame():
    production = {
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_friend", "name": "朋友", "description": "二十多岁，坐在对面，醒着"},
        ],
        "speaker_id": "char_xia",
        "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }
    cake = "特写，只有她的手和蛋糕。她开口说第二句。"
    shots = [
        {"id": "s1", "start": 0.0, "end": 4.0, "character_ids": ["char_xia", "char_friend"],
         "description": "对面坐着朋友，她开口说第一句。"},
        {"id": "s3", "start": 4.0, "end": 8.0, "character_ids": ["char_xia"],
         "description": cake},
    ]
    spans = [
        {"id": "speech_1", "start": 0.4, "end": 2.0, "shot_ids": ["s1"], "text": "这块给你。",
         "anchors": [{"text": "这块给你。", "start": 0.4, "end": 2.0, "shot_ids": ["s1"]}]},
        {"id": "speech_2", "start": 4.4, "end": 6.2, "shot_ids": ["s3"], "text": "樱桃我留着。",
         "anchors": [{"text": "樱桃我留着。", "start": 4.4, "end": 6.2, "shot_ids": ["s3"]}]},
    ]
    plan = {"production": production, "strategy": _dryer().model_dump(),
            "segments": [{"id": "seg_1", "duration_seconds": 8, "shots": shots, "speech_spans": spans}]}
    assert settle_inner_monologue(plan) == []
    assert plan["production"]["speech_mode"] == "character_dialogue"
    assert shots[1]["description"] == cake
    assert "内心独白" not in shots[1]["description"]
    assert "放轻脚步" not in shots[1]["description"]
    prompt = compile_grouped_video({
        "duration_seconds": 8, "generation_strategy": "continuity_v1", "ad_form": "scene_story",
        "production": production, "shots": shots, "speech_spans": spans,
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_xia"},
            {"index": 3, "role": "character", "character_id": "char_friend"},
        ],
    })
    close = _shot_block(prompt, "镜头2 |")
    assert "嘴型与台词同步" not in close
    assert "嘴不要动" in close
    assert "放轻脚步" not in close and "回头看" not in close and "屏住" not in close
    assert "内心独白" not in close
    later = {
        "production": production, "strategy": _dryer().model_dump(),
        "segments": [{"id": "seg_1", "duration_seconds": 8, "speech_spans": spans, "shots": [
            shots[0],
            {"id": "s3", "start": 4.0, "end": 8.0, "character_ids": ["char_xia"],
             "continuity": {"entry_transition": "scene_change"},
             "description": "换到厨房后，她一个人开口说第二句。"},
        ]}],
    }
    notes = settle_inner_monologue(later)
    assert notes == ["这一镜没有醒着的人听，台词改成她自己的内心独白，不对口型。"]
    assert "开口说" in later["segments"][0]["shots"][0]["description"]
    assert "心里想" in later["segments"][0]["shots"][1]["description"]
    assert "开口说" not in later["segments"][0]["shots"][1]["description"]
    macro = "特写，只有她的手和蛋糕。她开口说。"
    alone = {
        "production": {"characters": [{"id": "char_xia", "name": "小夏", "description": "二十多岁"}],
                       "speaker_id": "char_xia", "speech_mode": "character_dialogue",
                       "voice_description": "自然的成年女性普通话"},
        "strategy": _dryer().model_dump(),
        "segments": [{"id": "seg_1", "duration_seconds": 4, "shots": [
            {"id": "s3", "start": 0.0, "end": 4.0, "character_ids": ["char_xia"], "description": macro},
        ], "speech_spans": [
            {"id": "speech_1", "start": 0.4, "end": 2.0, "shot_ids": ["s3"], "text": "樱桃我留着。",
             "anchors": [{"text": "樱桃我留着。", "start": 0.4, "end": 2.0, "shot_ids": ["s3"]}]},
        ]}],
    }
    assert settle_inner_monologue(alone)
    written = alone["segments"][0]["shots"][0]["description"]
    assert "心里想" in written
    assert "开口说" not in written
    assert "放轻脚步" not in written and "回头看" not in written and "屏住" not in written
    assert "嘴没有动" in written
    assert alone["production"]["speech_mode"] == "character_dialogue"
    alone_prompt = compile_grouped_video({
        "duration_seconds": 4, "generation_strategy": "continuity_v1", "ad_form": "scene_story",
        "production": alone["production"], "shots": alone["segments"][0]["shots"],
        "speech_spans": alone["segments"][0]["speech_spans"],
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_xia"},
        ],
    })
    alone_block = _shot_block(alone_prompt, "镜头1 |")
    assert "放轻脚步" not in alone_block and "回头看" not in alone_block and "屏住" not in alone_block
    assert "心里想" in alone_block
    assert "开口说" not in alone_block


def test_this_shot_alone_is_checked_before_someone_opposite():
    production = {
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_friend", "name": "朋友", "description": "二十多岁，醒着"},
        ],
        "speaker_id": "char_xia",
        "speech_mode": "character_dialogue",
    }
    alone = {"id": "s1", "description": "她一个人坐着，对面没有人", "character_ids": ["char_xia"]}
    sofa = {"id": "s2", "description": "她坐在对面的沙发上", "character_ids": ["char_xia"]}
    facing = {"id": "s3", "description": "两人对坐着喝茶", "character_ids": ["char_xia"]}
    friend = {"id": "s4", "description": "朋友坐在对面，醒着", "character_ids": ["char_xia", "char_friend"]}
    assert shot_awake_listener(production, alone) is False
    assert shot_awake_listener(production, sofa) is False
    assert shot_awake_listener(production, facing) is False
    assert shot_awake_listener(production, friend) is True


def test_a_push_in_on_hands_does_not_invent_footsteps():
    production = {
        "characters": [{"id": "char_xia", "name": "小夏", "description": "二十多岁"}],
        "speaker_id": "char_xia", "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }
    macro = "特写，镜头推近，只有她的手和蛋糕。她开口说。"
    plan = {"production": production, "strategy": _dryer().model_dump(), "segments": [{
        "id": "seg_1", "duration_seconds": 4, "shots": [
            {"id": "s1", "start": 0.0, "end": 4.0, "character_ids": ["char_xia"], "description": macro},
        ], "speech_spans": [{
            "id": "speech_1", "start": 0.4, "end": 2.0, "shot_ids": ["s1"], "text": "樱桃我留着。",
            "anchors": [{"text": "樱桃我留着。", "start": 0.4, "end": 2.0, "shot_ids": ["s1"]}],
        }],
    }]}
    assert settle_inner_monologue(plan)
    written = plan["segments"][0]["shots"][0]["description"]
    assert "放轻脚步" not in written and "回头看" not in written and "屏住" not in written
    assert "嘴没有动" in written
    prompt = compile_grouped_video({
        "duration_seconds": 4, "generation_strategy": "continuity_v1", "ad_form": "scene_story",
        "production": production, "shots": plan["segments"][0]["shots"],
        "speech_spans": plan["segments"][0]["speech_spans"],
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_xia"},
        ],
    })
    block = _shot_block(prompt, "镜头1 |")
    assert "放轻脚步" not in block and "回头看" not in block
    assert "嘴不要动" in block and "嘴型与台词同步" not in block
    wider = {"production": production, "strategy": _dryer().model_dump(), "segments": [{
        "id": "seg_1", "duration_seconds": 4, "shots": [
            {"id": "s2", "start": 0.0, "end": 4.0, "character_ids": ["char_xia"],
             "description": "她一个人坐在桌边，开口说。"},
        ], "speech_spans": [{
            "id": "speech_1", "start": 0.4, "end": 2.0, "shot_ids": ["s2"], "text": "你先坐。",
            "anchors": [{"text": "你先坐。", "start": 0.4, "end": 2.0, "shot_ids": ["s2"]}],
        }],
    }]}
    assert settle_inner_monologue(wider)
    solo = wider["segments"][0]["shots"][0]["description"]
    assert "放轻脚步" not in solo and "回头看一眼" not in solo and "屏住一点呼吸" not in solo
    assert "嘴没有动" in solo
    from app.story_logic import _hands_or_cake_macro
    assert _hands_or_cake_macro(macro) is True
    assert _hands_or_cake_macro("特写，只有她的手和蛋糕，她把蛋糕往柜子里推。") is False


def test_another_person_is_company_and_someone_across_the_table_is_not_alone():
    from app.story_logic import _is_alone_now

    production = {
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_friend", "name": "朋友", "description": "二十多岁，醒着"},
        ],
        "speaker_id": "char_xia",
        "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }
    assert _is_alone_now("另一个人") is False
    assert _is_alone_now("她和另一个人坐在桌边") is False
    assert _is_alone_now("不是一个人") is False
    assert _is_alone_now("对面坐着一个人") is False
    assert _is_alone_now("她一个人坐着") is True
    friend = {"id": "s1", "start": 0.0, "end": 4.0, "character_ids": ["char_xia", "char_friend"],
              "description": "朋友坐在对面，醒着"}
    company = {"id": "s2", "start": 4.0, "end": 8.0, "character_ids": ["char_xia"],
               "description": "她和另一个人坐在桌边，开口说"}
    assert shot_awake_listener(production, company, [friend, company]) is True
    assert shot_awake_listener(production, {"id": "s3", "description": "不是一个人", "character_ids": ["char_xia"]},
                               [friend, {"id": "s3", "description": "不是一个人"}]) is True
    assert shot_awake_listener(production, {"id": "s4", "description": "对面坐着一个人", "character_ids": ["char_xia"]},
                               [friend, {"id": "s4", "description": "对面坐着一个人"}]) is True
    assert shot_awake_listener(production, {"id": "s5", "description": "她一个人坐着", "character_ids": ["char_xia"]},
                               [friend, {"id": "s5", "description": "她一个人坐着"}]) is False
    plan = {"production": production, "strategy": _dryer().model_dump(), "segments": [{
        "id": "seg_1", "duration_seconds": 8, "shots": [friend, company],
        "speech_spans": [{
            "id": "speech_1", "start": 4.4, "end": 6.0, "shot_ids": ["s2"], "text": "你先坐。",
            "anchors": [{"text": "你先坐。", "start": 4.4, "end": 6.0, "shot_ids": ["s2"]}],
        }],
    }]}
    assert settle_inner_monologue(plan) == []
    assert "开口说" in company["description"]
    assert "内心独白" not in company["description"] and "嘴没有动" not in company["description"]


def test_a_slow_push_in_on_hands_stays_a_hand_macro():
    from app.story_logic import _hands_or_cake_macro

    sentence = "特写，只有她的手和蛋糕，镜头缓慢推近"
    assert _hands_or_cake_macro(sentence) is True
    production = {
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_friend", "name": "朋友", "description": "二十多岁，醒着"},
        ],
        "speaker_id": "char_xia", "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }
    prompt = compile_grouped_video({
        "duration_seconds": 6, "generation_strategy": "continuity_v1", "ad_form": "scene_story",
        "production": production,
        "shots": [
            {"id": "s1", "start": 0.0, "end": 2.0, "character_ids": ["char_xia", "char_friend"],
             "description": "朋友坐在对面，醒着。"},
            {"id": "s2", "start": 2.0, "end": 6.0, "character_ids": ["char_xia"], "description": sentence},
        ],
        "speech_spans": [{
            "id": "speech_1", "start": 2.2, "end": 4.0, "shot_ids": ["s2"], "text": "这块给你。",
            "anchors": [{"text": "这块给你。", "start": 2.2, "end": 4.0, "shot_ids": ["s2"]}],
        }],
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_xia"},
            {"index": 3, "role": "character", "character_id": "char_friend"},
        ],
    })
    block = _shot_block(prompt, "镜头2 |")
    assert "嘴不要动" in block
    assert "嘴型与台词同步" not in block


def test_inner_monologue_clears_aloud_cues_on_the_whole_shot():
    production = {
        "characters": [{"id": "char_xia", "name": "小夏", "description": "二十多岁"}],
        "speaker_id": "char_xia", "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }
    shot = {
        "id": "s1", "start": 0.0, "end": 4.0, "character_ids": ["char_xia"],
        "description": "她开口说第一句。",
        "purpose": "她说出第一句。",
        "continuity": {
            "start_state": "她开口说第一句",
            "allowed_changes": "开口说第一句",
            "end_state": "她说出第一句",
            "camera_move": "中景，机位停住",
            "entry_transition": "opening",
            "entry_reason": "开口说第一句",
        },
    }
    span = {
        "id": "speech_1", "start": 0.4, "end": 2.0, "shot_ids": ["s1"], "text": "樱桃我留着。",
        "anchors": [{"text": "樱桃我留着。", "start": 0.4, "end": 2.0, "shot_ids": ["s1"],
                     "visual_evidence": "她开口说第一句。"}],
    }
    segment = {"id": "seg_1", "duration_seconds": 4, "shots": [shot], "speech_spans": [span],
               "prompt": "0–4秒：她开口说第一句。", "purpose": "她说出第一句。"}
    plan = {"production": production, "strategy": _dryer().model_dump(), "segments": [segment]}
    assert settle_inner_monologue(plan)
    assert "开口说" not in json.dumps(shot, ensure_ascii=False)
    assert "说出第一句" not in json.dumps(shot, ensure_ascii=False)
    assert "心里想第一句" in shot["description"]
    assert "嘴没有动" in shot["description"]
    assert "心里想" in shot["purpose"] and "心里想" in shot["continuity"]["allowed_changes"]
    assert "开口说" not in segment["prompt"] and "开口说" not in span["anchors"][0]["visual_evidence"]
    assert span["text"] == "樱桃我留着。"
    again = settle_inner_monologue(plan)
    assert shot["description"].count("嘴没有动") == 1
    assert again


def test_repeated_shot_ids_keep_their_own_scene():
    production = {
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_friend", "name": "朋友", "description": "二十多岁，醒着"},
        ],
        "speaker_id": "char_xia", "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }

    def move(entry):
        return {"start_state": "人已经在画面里", "allowed_changes": "保持姿势",
                "end_state": "人仍在画面里", "camera_move": "中景，机位停住",
                "entry_transition": entry, "entry_reason": "接着看"}

    first_friend = {"id": "s1", "start": 0.0, "end": 4.0, "character_ids": ["char_xia", "char_friend"],
                    "description": "对面坐着朋友，醒着。", "continuity": move("opening")}
    first_line = {"id": "s2", "start": 4.0, "end": 8.0, "character_ids": ["char_xia"],
                  "description": "她接着开口说。", "continuity": move("continuous")}
    second_line = {"id": "s2", "start": 0.0, "end": 8.0, "character_ids": ["char_xia"],
                   "description": "她接着开口说。", "continuity": move("scene_change")}
    roles = [
        {"index": 1, "role": "product"},
        {"index": 2, "role": "character", "character_id": "char_xia"},
        {"index": 3, "role": "character", "character_id": "char_friend"},
    ]
    plan = {
        "audio_strategy": "native", "generation_strategy": "continuity_v1",
        "production": production, "strategy": _dryer().model_dump(),
        "segments": [
            {"id": "seg_1", "duration_seconds": 8, "shots": [first_friend, first_line], "speech_spans": [{
                "id": "speech_1", "start": 4.2, "end": 6.0, "shot_ids": ["s2"], "text": "这块给你。",
                "anchors": [{"text": "这块给你。", "start": 4.2, "end": 6.0, "shot_ids": ["s2"]}],
            }]},
            {"id": "seg_2", "duration_seconds": 8, "shots": [second_line], "speech_spans": [{
                "id": "speech_2", "start": 0.4, "end": 2.0, "shot_ids": ["s2"], "text": "我自己留着。",
                "anchors": [{"text": "我自己留着。", "start": 0.4, "end": 2.0, "shot_ids": ["s2"]}],
            }]},
        ],
    }
    shared = film_scene_shots(plan, plan["segments"][0])
    assert [shot["segment_id"] for shot in shared] == ["seg_1", "seg_1", "seg_2"]
    assert [shot["id"] for shot in shared] == ["s1", "s2", "s2"]
    first_prompt = compile_grouped_video({
        "id": "seg_1", "duration_seconds": 8, "generation_strategy": "continuity_v1",
        "ad_form": "scene_story", "production": production,
        "shots": [first_friend, first_line], "speech_spans": plan["segments"][0]["speech_spans"],
        "reference_roles": roles,
    }, shared)
    first_block = _shot_block(first_prompt, "镜头2 |")
    assert "嘴型与台词同步" in first_block
    assert "内心独白" not in first_block
    second_prompt = compile_grouped_video({
        "id": "seg_2", "duration_seconds": 8, "generation_strategy": "continuity_v1",
        "ad_form": "scene_story", "production": production,
        "shots": [second_line], "speech_spans": plan["segments"][1]["speech_spans"],
        "reference_roles": roles,
    }, shared)
    second_block = _shot_block(second_prompt, "镜头1 |")
    assert "嘴型与台词同步" not in second_block
    assert "本镜不对口型" in second_block


def test_a_scene_spanning_segments_uses_the_same_listener():
    from app.pipeline import video_submission_spec

    production = {
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_friend", "name": "朋友", "description": "二十多岁，坐在对面，醒着"},
        ],
        "speaker_id": "char_xia", "speech_mode": "character_dialogue",
        "voice_description": "自然的成年女性普通话",
    }
    first = {"id": "s1", "start": 0.0, "end": 15.0, "character_ids": ["char_xia", "char_friend"],
             "description": "对面坐着朋友，她开口说第一句。"}
    second = {"id": "s2", "start": 0.0, "end": 15.0, "character_ids": ["char_xia"],
              "description": "她接着开口说第二句。"}
    spans = [
        {"id": "speech_1", "start": 0.4, "end": 2.0, "shot_ids": ["s1"], "text": "这块给你。",
         "anchors": [{"text": "这块给你。", "start": 0.4, "end": 2.0, "shot_ids": ["s1"]}]},
        {"id": "speech_2", "start": 0.4, "end": 2.0, "shot_ids": ["s2"], "text": "樱桃我留着。",
         "anchors": [{"text": "樱桃我留着。", "start": 0.4, "end": 2.0, "shot_ids": ["s2"]}]},
    ]
    plan = {
        "audio_strategy": "native", "generation_strategy": "continuity_v1",
        "production": production, "strategy": _dryer().model_dump(),
        "segments": [
            {"id": "seg_1", "duration_seconds": 15, "shots": [first], "speech_spans": [spans[0]]},
            {"id": "seg_2", "duration_seconds": 15, "shots": [second], "speech_spans": [spans[1]]},
        ],
    }
    assert settle_inner_monologue(plan) == []
    assert "开口说" in second["description"]
    assert "内心独白" not in second["description"] and "放轻脚步" not in second["description"]
    spec = video_submission_spec(plan, plan["segments"][1])
    shared = spec["scene_film_shots"]
    assert [shot["id"] for shot in shared] == ["s1", "s2"]
    assert [shot["segment_id"] for shot in shared] == ["seg_1", "seg_2"]
    assert film_scene_shots(plan, plan["segments"][1])[0]["id"] == "s1"
    prompt = compile_grouped_video({
        "duration_seconds": 15, "generation_strategy": "continuity_v1", "ad_form": "scene_story",
        "production": production, "shots": [second], "speech_spans": [spans[1]],
        "reference_roles": [
            {"index": 1, "role": "product"},
            {"index": 2, "role": "character", "character_id": "char_xia"},
            {"index": 3, "role": "character", "character_id": "char_friend"},
        ],
    }, shared)
    block = _shot_block(prompt, "镜头1 |")
    assert "内心独白" not in block and "放轻脚步" not in block and "不对口型" not in block
    assert "嘴型与台词同步" in block


def _spoken_board(shots, span, groups):
    return [{"shots": shots, "speech_spans": [span]}], [{"groups": groups}]


def _line(start, end, *shot_ids, beat="b1", text="看这个。"):
    return {
        "id": "speech_1", "beat_id": beat, "text": text, "start": start, "end": end,
        "shot_ids": list(shot_ids),
        "anchors": [{"text": text, "start": start, "end": end, "shot_ids": list(shot_ids),
                     "visual_evidence": "画面"}],
    }


def _group(start, end, speech_start, speech_end, beat="b1"):
    return {"beat_id": beat, "start": start, "end": end,
            "speech_start": speech_start, "speech_end": speech_end}


def test_hands_in_frame_is_not_a_hands_only_close_up():
    assert hands_only_shot({"description": "她手里拿着瓶子，看着镜头开口说。"}) is False
    assert hands_only_shot({"description": "手上停着同一瓶身。"}) is False
    assert hands_only_shot({"description": "手部特写，只有手。"}) is True
    assert hands_only_shot({"description": "手部特写，正脸也在。"}) is False


def test_compiled_pitch_does_not_leave_a_line_on_the_silent_shot(source):
    from app.progressive_planning import compile_director
    from test_progressive_planning import chosen_strategy, director_output
    strategy = chosen_strategy(source, "character_dialogue")
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "presenter_pitch"
    strategy = StrategyRecord.model_validate(raw)
    draft = director_output(strategy, count=2)
    draft["shots"][0]["description"] = "她看着镜头开口说。"
    draft["shots"][1]["description"] = "她没说话，手里拿着瓶子。"
    plan, _, _ = compile_director(draft, strategy, 12, "continuity_v1", [source["id"]])
    shots = plan["segments"][0]["shots"]
    quiet = next(shot for shot in shots if "没说话" in shot["description"])
    for span in plan["segments"][0]["speech_spans"]:
        overlap = min(span["end"], quiet["end"]) - max(span["start"], quiet["start"])
        assert overlap <= 1e-6, span


def test_pitch_line_moves_off_the_shot_marked_not_speaking():
    shots = [
        {"id": "s1", "start": 0.0, "end": 4.0, "beat_id": "b1", "beat_ids": ["b1"],
         "description": "她看着镜头开口说。"},
        {"id": "s2", "start": 4.0, "end": 7.0, "beat_id": "b1", "beat_ids": ["b1"],
         "description": "她没说话，手里拿着瓶子。"},
    ]
    span = _line(3.2, 5.4, "s1", "s2")
    segments, schedule = _spoken_board(shots, span, [_group(0.0, 7.0, 3.2, 5.4)])
    production = {"speech_mode": "character_dialogue", "speech_delivery": "visible_only", "speaker_id": "host"}
    assert settle_speech_coverage(segments, schedule, form="presenter_pitch", production=production) is True
    assert span["shot_ids"] == ["s1"]
    assert span["end"] <= shots[0]["end"] + 1e-6
    assert min(span["end"], shots[1]["end"]) - max(span["start"], shots[1]["start"]) <= 1e-6
    assert settle_speech_coverage(segments, schedule, form="presenter_pitch", production=production) is False


def test_pitch_continuation_may_cross_a_hands_insert_when_the_face_is_in_the_line():
    shots = [
        {"id": "face", "start": 0.0, "end": 4.0, "beat_id": "b1", "beat_ids": ["b1"],
         "description": "她看着镜头说。"},
        {"id": "hands", "start": 4.0, "end": 6.0, "beat_id": "b1", "beat_ids": ["b1"],
         "description": "手部特写，只有手。"},
    ]
    span = _line(2.5, 5.2, "face", "hands")
    segments, schedule = _spoken_board(shots, span, [_group(0.0, 6.0, 2.5, 5.2)])
    production = {"speech_mode": "character_dialogue", "speech_delivery": "on_and_off_screen", "speaker_id": "host"}
    assert settle_speech_coverage(segments, schedule, form="presenter_pitch", production=production) is False
    assert span["shot_ids"] == ["face", "hands"]


def test_pitch_line_leaves_a_hands_only_close_up_when_no_face_shares_it():
    shots = [
        {"id": "face", "start": 0.0, "end": 3.0, "beat_id": "b1", "beat_ids": ["b1"],
         "description": "她看着镜头说。"},
        {"id": "hands", "start": 3.0, "end": 6.0, "beat_id": "b1", "beat_ids": ["b1"],
         "description": "手部特写，只有她的手。"},
    ]
    span = _line(3.4, 5.2, "hands")
    segments, schedule = _spoken_board(shots, span, [_group(0.0, 6.0, 3.4, 5.2)])
    production = {"speech_mode": "character_dialogue", "speech_delivery": "on_and_off_screen", "speaker_id": "host"}
    assert settle_speech_coverage(segments, schedule, form="presenter_pitch", production=production) is True
    assert span["shot_ids"] == ["face"]
    assert span["end"] <= shots[0]["end"] + 1e-6


def test_story_hands_close_up_does_not_carry_a_line_someone_awake_can_hear():
    production = {
        "speech_mode": "character_dialogue", "speaker_id": "char_xia",
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_ning", "name": "阿宁", "description": "醒着"},
        ],
    }
    shots = [
        {"id": "face", "start": 0.0, "end": 3.0, "beat_id": "b1", "beat_ids": ["b1"],
         "character_ids": ["char_xia", "char_ning"],
         "description": "对面坐着朋友，她开口说。"},
        {"id": "hands", "start": 3.0, "end": 6.0, "beat_id": "b1", "beat_ids": ["b1"],
         "character_ids": ["char_xia"],
         "description": "手部特写，只有手。她开口说。"},
    ]
    span = _line(3.3, 5.1, "hands", text="这块给你。")
    segments, schedule = _spoken_board(shots, span, [_group(0.0, 6.0, 3.3, 5.1)])
    assert settle_speech_coverage(segments, schedule, form="scene_story", production=production) is True
    assert span["shot_ids"] == ["face"]
    assert min(span["end"], shots[1]["end"]) - max(span["start"], shots[1]["start"]) <= 1e-6


def test_story_hands_close_up_can_keep_a_line_when_nobody_awake_is_listening():
    production = {
        "speech_mode": "character_dialogue", "speaker_id": "char_xia",
        "characters": [{"id": "char_xia", "name": "小夏", "description": "二十多岁"}],
    }
    shots = [
        {"id": "hands", "start": 0.0, "end": 4.0, "beat_id": "b1", "beat_ids": ["b1"],
         "character_ids": ["char_xia"],
         "description": "她一个人，手部特写，只有手。她开口说。"},
    ]
    span = _line(0.4, 2.0, "hands", text="樱桃我留着。")
    segments, schedule = _spoken_board(shots, span, [_group(0.0, 4.0, 0.4, 2.0)])
    assert settle_speech_coverage(segments, schedule, form="scene_story", production=production) is False
    assert span["shot_ids"] == ["hands"]
    plan = {"strategy": _dryer().model_dump(), "production": production,
            "segments": [{"id": "seg_1", "shots": shots, "speech_spans": [span]}]}
    assert line_is_private_thought(plan, "seg_1", "speech_1") is True


def test_listener_across_segments_uses_the_same_scene_as_inner_voice():
    production = {
        "speech_mode": "character_dialogue", "speaker_id": "char_xia",
        "characters": [
            {"id": "char_xia", "name": "小夏", "description": "二十多岁"},
            {"id": "char_ning", "name": "阿宁", "description": "醒着"},
        ],
    }

    def entry(kind):
        return {"entry_transition": kind}

    def board(hands_description):
        friend = {"id": "s1", "start": 0.0, "end": 8.0, "beat_id": "b1", "beat_ids": ["b1"],
                  "character_ids": ["char_xia", "char_ning"], "continuity": entry("opening"),
                  "description": "对面坐着朋友，醒着。"}
        face = {"id": "s2", "start": 0.0, "end": 4.0, "beat_id": "b2", "beat_ids": ["b2"],
                "character_ids": ["char_xia"], "continuity": entry("continuous"),
                "description": "她的脸在画面，看着桌面。"}
        hands = {"id": "s3", "start": 4.0, "end": 8.0, "beat_id": "b2", "beat_ids": ["b2"],
                 "character_ids": ["char_xia"], "continuity": entry("continuous"),
                 "description": hands_description}
        span = _line(4.2, 6.0, "s3", beat="b2", text="这块给你。")
        segments = [
            {"id": "seg_1", "shots": [friend], "speech_spans": []},
            {"id": "seg_2", "shots": [face, hands], "speech_spans": [span]},
        ]
        schedule = [
            {"groups": [_group(0.0, 8.0, 0.0, 0.0, beat="b1")]},
            {"groups": [_group(0.0, 8.0, 4.2, 6.0, beat="b2")]},
        ]
        return segments, schedule, span, hands

    heard, heard_schedule, heard_span, heard_hands = board("手部特写，只有手。她开口说。")
    heard_plan = {"strategy": _dryer().model_dump(), "production": production, "segments": heard}
    assert line_is_private_thought(heard_plan, "seg_2", "speech_1") is False
    assert settle_speech_coverage(heard, heard_schedule, form="scene_story", production=production) is True
    assert min(heard_span["end"], heard_hands["end"]) - max(heard_span["start"], heard_hands["start"]) <= 1e-6
    assert "s3" not in heard_span["shot_ids"]
    assert line_is_private_thought(heard_plan, "seg_2", "speech_1") is False

    alone, alone_schedule, alone_span, alone_hands = board("她一个人，手部特写，只有手。她开口说。")
    alone_plan = {"strategy": _dryer().model_dump(), "production": production, "segments": alone}
    film = [shot for segment in alone for shot in segment["shots"]]
    scene = next(group for group in scene_groups(film) if any(item is alone_hands for item in group))
    assert any(shot["id"] == "s1" for shot in scene)
    assert shot_awake_listener(production, alone_hands, scene) is False
    assert settle_speech_coverage(alone, alone_schedule, form="scene_story", production=production) is False
    assert alone_span["shot_ids"] == ["s3"]
    assert line_is_private_thought(alone_plan, "seg_2", "speech_1") is True


def test_asleep_company_selling_point_still_fails():
    selling = story_plan_issues(_board(_shot("室友还在睡着。"), script=_lines("太安静了。")), _dryer())
    assert "剧情对白还在说卖点" in selling
    quiet = story_plan_issues(_board(_shot("室友还在睡着。"), script=_lines("这风不吵。")), _dryer())
    assert "剧情对白还在说卖点" in quiet
