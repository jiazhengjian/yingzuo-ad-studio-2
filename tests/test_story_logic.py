"""剧情结尾、点题、高光证明、状态参考、不看镜头、背景人物。Offline must-fail and must-pass."""
import asyncio

import pytest

from app.design import state_design_request
from app.line_quality import apply_line_rewrite, script_line_offenses
from app.picture_rules import action_issues, state_change_issues
from app.production import required_designs, validate_design_assets
from app.story_logic import (
    apply_background_rewrite, background_issues, cast_distinction_issues, line_is_punchline,
    plan_state_references, proof_highlight_issues, rewrite_background_if_needed,
    scrub_story_purposes, shot_state_change, stamp_background_lines, state_pairing_issues,
    story_ending_issues, story_gaze_line, story_plan_issues, storyboard_content_failure,
    with_background_line,
)
from app.strategy import StrategyRecord
from app.video_prompt import compile_grouped_video
from test_line_quality import _lines, typed
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
    assert story_plan_issues(carried, strategy)


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
    assert story_plan_issues(_board(_shot("她转身。"), script=_lines("它让我没白折回来。")), strategy)


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


def test_state_change_splits_the_process_shot_from_a_later_result():
    changed = _hair_process("s0", "头发已经干")
    assert shot_state_change(changed)[0:2] == ("湿", "已经干")
    assert shot_state_change({"id": "walk", "description": "她从门口走到台前。"}) is None
    assert shot_state_change({"id": "still", "description": "瓶子停在桌角。",
                               "continuity": {"start_state": "同一商品静置", "end_state": "同一商品静置"}}) is None
    same_shot = {"production": {"characters": []}, "segments": [{"shots": [changed]}]}
    assert [item["phase"] for item in plan_state_references(same_shot)] == ["start"]
    assert "结果状态没有单独的结果镜" in state_pairing_issues(same_shot)
    halfway = {"production": {"characters": []}, "segments": [{"shots": [_hair_process("s3", "被吹开一半")]}]}
    assert "状态结果还停在半程" in state_pairing_issues(halfway)
    assert "一半" not in "".join(item["label"] for item in plan_state_references(halfway))
    plan = {"production": {"characters": []}, "segments": [{"shots": [
        _hair_process("s3", "被吹开一半"), _hair_result(),
    ]}]}
    refs = plan_state_references(plan)
    assert [item["phase"] for item in refs] == ["start", "result"]
    assert refs[0]["shot_id"] == "s3" and refs[1]["shot_id"] == "s6"
    assert refs[1]["label"].endswith("顺了")
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
    assert on_story["reference_kinds"] == ["character"]
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
    assert all(shot_state_change(shot) is None for shot in shots)
    plan = {"production": {"characters": []}, "segments": [{"shots": shots}]}
    refs = plan_state_references(plan)
    assert [item["phase"] for item in refs] == ["start", "result"]
    assert refs[0]["shot_id"] == "sh5" and refs[1]["shot_id"] == "sh6"
    assert refs[0]["label"].endswith("湿") and refs[1]["label"].endswith("干了")
    assert state_pairing_issues(plan) == []
    gapped = {"production": {"characters": []}, "segments": [{"shots": [
        shots[0],
        _state_shot("sh5", "室友侧躺、脸朝房间一侧睡着", "室友保持睡姿", "过肩看床上的室友。"),
        shots[2],
    ]}]}
    gapped_refs = plan_state_references(gapped)
    assert [item["shot_id"] for item in gapped_refs] == ["sh4", "sh6"]
    halfway = {"production": {"characters": []}, "segments": [{"shots": [
        _state_shot("sh4", "头发是湿的", "头发仍是湿的", "她吹着湿发。"),
        _state_shot("sh6", "头发被吹开一半", "头发被吹开一半", "头发被吹开一半。"),
    ]}]}
    assert "状态结果还停在半程" in state_pairing_issues(halfway)
    assert "一半" not in "".join(item["label"] for item in plan_state_references(halfway))


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
    assert "按放下时的样子留在原位" in ending
    assert "正面朝镜头" in compile_grouped_video(_segment())


def test_existing_door_and_vapor_checks_stay():
    assert "摔门砸了安静" in action_issues("她摔门出去。", "夜里要安静")
    assert "带门要轻" in action_issues("她带门离开", "别吵")
    raw = {"description": "筒口喷出白烟。", "continuity": {"end_state": "发尾湿塌", "allowed_changes": ""}}
    assert "喷嘴喷烟" in state_change_issues(raw, "吹干了")


def test_story_plan_issues_names_the_blocking_problem():
    strategy = _dryer()
    plan = _board(
        _shot("她吹干头发。", id="s0"),
        _shot("她拿着吹风机出门。", "英雄定格", id="s1"),
        script=_lines("它让我没白折回来。"),
    )
    issues = story_plan_issues(plan, strategy)
    assert "剧情末镜写成了英雄定格" in issues
    assert "剧情末镜把家用物品带出了门" in issues
    assert "剧情末句还在点题" in issues
    assert "剧情卖点没有高光证明镜" in issues
    assert story_plan_issues(plan, typed("brand_tvc")) == []


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
    ]}]}
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
