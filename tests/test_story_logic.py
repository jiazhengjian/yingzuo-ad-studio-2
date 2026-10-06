"""剧情结尾、点题、高光证明、状态参考、不看镜头、背景人物。Offline must-fail and must-pass."""
import asyncio

from app.design import state_design_request
from app.line_quality import apply_line_rewrite, script_line_offenses
from app.picture_rules import action_issues, state_change_issues
from app.production import required_designs, validate_design_assets
from app.story_logic import (
    background_issues, line_is_punchline, plan_state_references, proof_highlight_issues,
    scrub_story_purposes, shot_state_change, story_ending_issues, story_gaze_line,
    story_plan_issues, with_background_line,
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


def test_state_change_gets_two_stills_and_a_hard_cut_not_a_walk_across_the_room():
    changed = {
        "id": "s0",
        "description": "她按下开关。",
        "continuity": {"start_state": "头发是湿的", "end_state": "头发已经干", "allowed_changes": ""},
    }
    assert shot_state_change(changed)[0:2] == ("湿", "已经干")
    assert shot_state_change({"id": "walk", "description": "她从门口走到台前。"}) is None
    assert shot_state_change({"id": "still", "description": "瓶子停在桌角。",
                               "continuity": {"start_state": "同一商品静置", "end_state": "同一商品静置"}}) is None
    plan = {"production": {"characters": []}, "segments": [{"shots": [changed]}]}
    refs = plan_state_references(plan)
    assert [item["phase"] for item in refs] == ["start", "result"]
    assert all(item["kind"] == "start_frame" for item in refs)
    designs = required_designs(plan)
    assert [item["kind"] for item in designs] == ["scene", "start_frame", "start_frame"]
    scene = {"kind": "scene", "metadata": {"generated": True}}
    with_missing = [scene]
    try:
        validate_design_assets(plan, with_missing)
    except ValueError as error:
        assert "状态变化" in str(error)
    else:
        raise AssertionError("state stills are required")
    stills = []
    for item in refs:
        stills.append({"kind": "start_frame", "metadata": {
            "generated": True, "state_reference": True, "state_id": item["state_id"],
            "phase": item["phase"], "shot_id": item["shot_id"], "state_label": item["label"],
        }})
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
    segment = _segment(ad_form="scene_story")
    segment["shots"][0]["continuity"] = {
        "entry_transition": "opening", "start_state": "头发是湿的", "end_state": "头发已经干",
        "allowed_changes": "", "camera_move": "机位不动",
    }
    segment["reference_roles"] = [
        *segment["reference_roles"],
        {"index": 4, "role": "state", "phase": "start", "shot_id": "s0", "label": "头发湿"},
        {"index": 5, "role": "state", "phase": "result", "shot_id": "s0", "label": "头发干"},
    ]
    prompt = compile_grouped_video(segment)
    opening = _shot_block(prompt, "镜头1 |")
    assert "起始状态参考图4" in opening
    assert "硬切到结果状态参考图5" in opening
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
