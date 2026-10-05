"""L1–L9 must-pass and must-fail checks from the product-rule brief. Offline."""
import asyncio

from app.ad_form import classify_narrative_axis, infer_ad_form, intake_strategy_response
from app.ad_form_playbook import ending_policy
from app.cta import apply_spoken_policy, should_ask_cta, spoken_cta_line
from app.line_quality import (
    LINE_REWRITE_SYSTEM, _claim_tags, apply_brand_claim_collapse, apply_line_rewrite,
    brand_stack_remaining, brand_visual_claim_overflow, collapse_brand_claims,
    rewrite_script_if_needed, script_line_offenses, voice_identity_issues,
)
from app.picture_rules import (
    action_issues, carrier_issues, ensure_hair_transition, prepare_prompt_shot,
    state_change_issues, stitch_actions,
)
from app.strategy import StrategyRecord, _BRAND_AUTO_SKELETONS
from app.video_prompt import compile_grouped_video
from test_line_quality import _lines, typed
from test_providers import source  # noqa: F401  pytest fixture
from test_video_prompt_lipsync import _segment


def _story_axis(expression):
    strategy = typed("scene_story")
    raw = strategy.model_dump()
    selected = next(item for item in raw["directions"] if item["id"] == raw["selected_direction_id"])
    selected["product_expression"] = expression
    return StrategyRecord.model_validate(raw)


def _shot_block(prompt, label):
    """One labeled shot, through the next shot heading. 背对镜头 stays inside."""
    rest = prompt.split(label, 1)[1]
    cuts = [pos for pos in (rest.find("\n镜头"), rest.find("\n台词时间")) if pos >= 0]
    return rest[:min(cuts)] if cuts else rest


# --- L1 类型判定 ---


def test_l1_life_pain_third_person_voiceover_is_product_showcase():
    form = classify_narrative_axis({
        "brief": "吹风机。生活痛点是早上赶出门头发吹不干。",
        "consumer_insight": "早上赶时间，头发总吹不干",
        "scenario": "卫生间",
        "product_expression": "第三人称旁白，画面证明吹得快",
        "speech_mode": "voiceover",
    })
    assert form == "brand_tvc"


def test_l1_roommate_conflict_is_story():
    form = classify_narrative_axis({
        "brief": "合租夜里不敢按下去",
        "consumer_insight": "合租时不敢按开关",
        "scenario": "夜里室友在睡",
        "product_expression": "她犹豫后轻轻按下，头发干了只是松了口气",
        "speech_mode": "character_dialogue",
    })
    assert form == "scene_story"


def test_l1_must_fail_sleeping_roommate_efficacy_lecture_is_not_story():
    form = classify_narrative_axis({
        "brief": "室友在睡。",
        "consumer_insight": "夜里室友在睡",
        "scenario": "合租卧室",
        "product_expression": "第三人称旁白讲护发功效，画面证明吹完顺",
        "speech_mode": "voiceover",
    })
    assert form == "brand_tvc"
    assert form != "scene_story"


def test_l1_must_fail_on_camera_alone_is_not_story():
    form = classify_narrative_axis({
        "brief": "有人出镜使用吹风机",
        "product_expression": "真人拿着吹风机，画面证明几下就干",
        "speech_mode": "voiceover",
    })
    assert form == "brand_tvc"
    assert form != "scene_story"


def test_l1_explicit_presenter_wins_over_conflict():
    form = classify_narrative_axis({
        "ad_form": "presenter_pitch",
        "brief": "合租夜里不敢按下去",
        "product_expression": "不敢按下去也要讲给观众",
        "speech_mode": "character_dialogue",
    })
    assert form == "presenter_pitch"


def test_l1_presenter_keyword_path_stays():
    text = "给吹风机做一条口播广告"
    assert infer_ad_form(text) == "presenter_pitch"
    assert classify_narrative_axis({"brief": text}) == "presenter_pitch"


# --- L2 声轨身份 ---


def test_l2_voice_identity_one_type_one_track():
    presenter = typed("presenter_pitch")
    brand = typed("brand_tvc")
    story = typed("scene_story")
    voice = {"speech_mode": "voiceover"}
    talk = {"speech_mode": "character_dialogue", "speaker_id": "char_host"}
    silent = {"speech_mode": "none"}
    assert voice_identity_issues(presenter, voice) == ["口播被写成了旁白"]
    assert voice_identity_issues(presenter, silent) == ["口播被写成了旁白"]
    assert voice_identity_issues(presenter, talk) == []
    assert voice_identity_issues(brand, talk) == ["产品展示被写成了对口型推销"]
    assert voice_identity_issues(brand, voice) == []
    assert voice_identity_issues(brand, silent) == []
    assert voice_identity_issues(story, voice) == ["剧情被写成了整条旁白"]
    assert voice_identity_issues(story, talk) == []
    assert voice_identity_issues(story, silent) == []


def test_l2_presenter_product_closeup_continues_off_screen():
    prompt = compile_grouped_video(_segment(delivery="on_and_off_screen", ad_form="presenter_pitch"))
    product = prompt.split("镜头2 |", 1)[1]
    assert "同一人物画外延续" in product
    assert "不是另配旁白" in prompt


# --- L3 剧情与转化 ---


def test_l3_story_clarification_hides_cta():
    project = {"ad_form": "scene_story", "cta_target": None, "brief": "合租夜里不敢按下去"}
    assert should_ask_cta(project) is False
    result = intake_strategy_response({
        "kind": "clarify",
        "message": "还差一个问题",
        "questions": [{"id": "audience", "prompt": "给谁看"}],
    }, project)
    assert all(item.get("id") != "cta_target" for item in result["questions"])
    presenter = {"ad_form": "presenter_pitch", "cta_target": None, "brief": "给吹风机做一条口播"}
    asked = intake_strategy_response({
        "kind": "clarify",
        "message": "还差一个问题",
        "questions": [{"id": "audience", "prompt": "给谁看"}],
    }, presenter)
    assert any(item.get("id") == "cta_target" for item in asked["questions"])
    policy = ending_policy("scene_story", "link")
    assert policy["cta_target"] == "none"
    assert "想带走" not in policy["instruction"]
    assert "点下面" not in policy["instruction"]
    assert "平台按钮" in policy["instruction"]
    assert "定格" in policy["instruction"]


def test_l3_story_plan_strips_click_link():
    strategy = {"brief": {"ad_form": "scene_story"}, "directions": [], "selected_direction_id": None}
    revised = apply_spoken_policy(
        _lines("轻点。", "想带走，就点下面链接。"), strategy, "link")
    spoken = "".join(beat["narration"] for beat in revised["beats"])
    assert "点下面" not in spoken
    assert "链接" not in spoken
    assert "轻点" in spoken


def test_l3_must_fail_last_shot_does_not_lipsync_the_link():
    segment = _segment(ad_form="scene_story")
    segment["shots"][1]["description"] = "她放下吹风机，头发已经干了。"
    segment["shots"][1]["character_ids"] = ["char_host"]
    segment["speech_spans"] = [
        {"id": "speech_1", "start": 0, "end": 3, "shot_ids": ["s0"], "text": "轻点。",
         "anchors": [{"text": "轻点。", "start": 0, "end": 3, "shot_ids": ["s0"]}]},
        {"id": "speech_2", "start": 3, "end": 6, "shot_ids": ["s1"], "text": "想带走，就点下面链接。",
         "anchors": [{"text": "想带走，就点下面链接。", "start": 3, "end": 6, "shot_ids": ["s1"]}]},
    ]
    prompt = compile_grouped_video(segment)
    last = _shot_block(prompt, "镜头2 |")
    assert "想带走" not in prompt
    assert "点下面链接" not in prompt
    assert "不对口型，不念链接" in last
    assert "嘴型与台词同步" not in last


def test_l3_presenter_and_brand_still_speak_the_link():
    assert "点下面链接" in spoken_cta_line("link", "presenter_pitch")
    assert "点下面链接" in spoken_cta_line("link", "brand_tvc")
    assert spoken_cta_line("link", "scene_story") == ""
    brand = ending_policy("brand_tvc", "link")["instruction"]
    presenter = ending_policy("presenter_pitch", "link")["instruction"]
    assert "点下面链接" in brand
    assert "点下面链接" in presenter


# --- L4 卖点容量与对齐 ---


def test_l4_smooth_lexicon_includes_shun():
    assert "smooth" in _claim_tags("发尾是顺的")
    assert "smooth" in _claim_tags("吹完是顺的")
    assert "smooth" in _claim_tags("顺滑")
    assert "smooth" in _claim_tags("头发顺了")
    assert "smooth" in _claim_tags("不毛躁")
    assert "smooth" in _claim_tags("终于顺了")
    assert "smooth" not in _claim_tags("顺便看一眼")
    assert "smooth" not in _claim_tags("顺利")
    assert "smooth" not in _claim_tags("顺口")


def test_l4_fifteen_second_dual_benefit_collapses():
    script = _lines("几下就干。", "发尾是顺的。")
    strategy = typed("brand_tvc")
    assert brand_stack_remaining(script, strategy, duration_seconds=15) is True
    revised, changed = collapse_brand_claims(script, (), 15)
    assert changed is True
    spoken = [beat["narration"] for beat in revised["beats"]]
    assert spoken[0] == "几下就干。"
    assert spoken[1] == ""
    assert brand_stack_remaining(revised, strategy, duration_seconds=15) is False


def test_l4_must_fail_blanked_voiceover_still_showing_another_claim():
    container = {
        "ad_script": _lines("几下就干。", ""),
        "shots": [{"id": "s0", "description": "这风不吵。"}],
    }
    assert brand_visual_claim_overflow(container, typed("brand_tvc"), duration_seconds=15) is True


def test_l4_must_fail_fifteen_second_brand_line_stacks_ions_smooth_and_noise():
    line = "它带负离子，吹完是顺的。这风不吵。"
    script = _lines(line, "点下面链接，把它看清楚。")
    strategy = typed("brand_tvc")
    assert _claim_tags(line) == {"ions", "smooth", "noise"}
    offenses = [
        name for item in script_line_offenses(script, strategy, duration_seconds=15)
        for name in item["offenses"]
    ]
    assert "一句里塞了两个卖点" in offenses
    assert brand_stack_remaining(script, strategy, duration_seconds=15) is True
    revised, changed = collapse_brand_claims(script, (), 15)
    assert changed is True
    kept = revised["beats"][0]["narration"]
    assert kept != line
    assert len(_claim_tags(kept)) == 1
    assert "点下面链接" in revised["beats"][1]["narration"]
    assert brand_stack_remaining(revised, strategy, duration_seconds=15) is False


def test_l4_must_pass_single_claim_fifteen_second_brand_voiceover():
    script = _lines("这风不吵。", "点下面链接，把它看清楚。")
    strategy = typed("brand_tvc")
    assert _claim_tags("这风不吵。") == {"noise"}
    assert brand_stack_remaining(script, strategy, duration_seconds=15) is False
    revised, changed = collapse_brand_claims(script, (), 15)
    assert changed is False
    assert [beat["narration"] for beat in revised["beats"]] == ["这风不吵。", "点下面链接，把它看清楚。"]


def test_l4_presenter_and_chain_is_not_collapsed():
    director = {"ad_script": _lines("而且几下就干。", "而且这风不吵。")}
    held, changed = apply_brand_claim_collapse(director, typed("presenter_pitch"), (), 15)
    assert changed is False
    assert [beat["narration"] for beat in held["ad_script"]["beats"]] == ["而且几下就干。", "而且这风不吵。"]
    chained = _lines(
        "我用这款吹风机，几下就吹开了。",
        "而且它声音很小，我正常说话都听得清。",
        "而且我用它吹完，头发顺了。",
    )
    assert brand_stack_remaining(chained, typed("presenter_pitch"), duration_seconds=20) is False


def test_l4_review_salvage_does_not_ship_a_stacked_fifteen_second_brand_line(source):
    """A failed storyboard must not leave ions+smooth+noise in the saved voiceover."""
    from app.progressive_planning import ProgressivePlanningError, progressive_plan
    from app.strategy import StrategyRecord
    from test_progressive_planning import chosen_strategy, director_output

    strategy = chosen_strategy(source)
    raw = strategy.model_dump()
    raw["brief"]["ad_form"] = "brand_tvc"
    strategy = StrategyRecord.model_validate(raw)
    line = "它带负离子，吹完是顺的。这风不吵。"
    draft = {"beats": [{"id": "b0", "narration": line, "evidence_ids": ["appearance"]}]}

    async def broken_board(system, data, assets, max_tokens=7000):
        value = director_output(strategy)
        for shot in value["shots"]:
            shot["character_ids"] = ["missing_person"]
        return value

    def run():
        return asyncio.run(progressive_plan(
            stage_call=broken_board, on_progress=lambda _phase: None, record_failure=lambda _item: None,
            strategy=strategy, draft=draft, draft_issues=[], duration=15,
            generation_strategy="continuity_v1", assets=[source], conversation=[],
            merchant_request="徕芬吹风机，做一条产品展示", current_plan=None, cta_target="none"))

    plan = run()
    spoken = "".join(beat.get("narration") or "" for beat in plan["ad_script"]["beats"])
    spans = "".join(
        span.get("text") or ""
        for segment in plan["segments"] for span in segment.get("speech_spans") or [])
    assert line not in spoken and line not in spans
    assert brand_stack_remaining(plan["ad_script"], strategy, duration_seconds=15) is False
    content = [
        beat.get("narration") or "" for beat in plan["ad_script"]["beats"]
        if beat.get("narration") and "链接" not in beat["narration"] and "点下面" not in beat["narration"]
    ]
    assert content
    assert all(len(_claim_tags(text)) <= 1 for text in content)
    assert len(set().union(*(_claim_tags(text) for text in content))) <= 1

    def keep_stack(director, _strategy, locked_lines=(), duration_seconds=None):
        return director, False

    import app.line_quality as line_quality
    original = line_quality.apply_brand_claim_collapse
    line_quality.apply_brand_claim_collapse = keep_stack
    try:
        try:
            run()
        except ProgressivePlanningError as error:
            assert "堆卖点" in str(error)
        else:
            raise AssertionError("stacked brand voiceover was accepted")
    finally:
        line_quality.apply_brand_claim_collapse = original


# --- L5 台词质量 ---


def test_l5_telegram_voiceover_is_rewritten():
    strategy = typed("brand_tvc")
    script = _lines("吹完是顺的。")
    assert "电报体" in script_line_offenses(script, strategy, duration_seconds=15)[0]["offenses"]

    async def speak(system, data, originals, max_tokens):
        assert "不要指定某一句替换" in system
        return {"lines": [{"beat_id": "b0", "narration": "吹完头发顺了。"}]}

    called, result = asyncio.run(rewrite_script_if_needed(speak, script, strategy, 15))
    assert called is True
    assert result["beats"][0]["narration"] == "吹完头发顺了。"
    assert "电报体" not in "".join(
        name for item in script_line_offenses(result, strategy, duration_seconds=15) for name in item["offenses"])


def test_l5_telegram_rewrite_that_stays_telegram_is_rejected():
    strategy = typed("brand_tvc")
    script = _lines("吹完是顺的。")
    rejected = apply_line_rewrite(
        script, {"lines": [{"beat_id": "b0", "narration": "吹完是顺的。"}]},
        limits={"b0": 40}, strategy=strategy, duration_seconds=15,
        offense_names={"b0": ["电报体"]})
    assert rejected is None

    async def still_telegram(system, data, originals, max_tokens):
        return {"lines": [{"beat_id": "b0", "narration": "吹完是顺的。"}]}

    called, kept = asyncio.run(rewrite_script_if_needed(still_telegram, script, strategy, 15))
    assert called is True
    assert kept["beats"][0]["narration"] == "吹完是顺的。"


def test_l5_story_axis_twist_is_rewritten():
    strategy = _story_axis("主打安静，夜里不吵人")
    script = _lines("终于顺了。")
    names = [name for item in script_line_offenses(script, strategy) for name in item["offenses"]]
    assert "主轴拧了" in names

    async def react(system, data, originals, max_tokens):
        return {"lines": [{"beat_id": "b0", "narration": "轻点。"}]}

    called, result = asyncio.run(rewrite_script_if_needed(react, script, strategy, 15))
    assert called is True
    assert result["beats"][0]["narration"] == "轻点。"
    leftover = [name for item in script_line_offenses(result, strategy) for name in item["offenses"]]
    assert "主轴拧了" not in leftover


def test_l5_story_axis_twist_rewrite_that_stays_twisted_is_rejected():
    strategy = _story_axis("主打安静，夜里不吵人")
    script = _lines("终于顺了。")
    rejected = apply_line_rewrite(
        script, {"lines": [{"beat_id": "b0", "narration": "终于顺了。"}]},
        limits={"b0": 40}, strategy=strategy, duration_seconds=15,
        offense_names={"b0": ["主轴拧了"]})
    assert rejected is None


# --- L6 出镜与载体 ---


def test_l6_off_camera_allows_an_independent_bundle():
    assert "lifestyle" in _BRAND_AUTO_SKELETONS
    assert carrier_issues(brief="不出镜来拍", characters=(), picture="独立发束放在台上") == []


def test_l6_without_off_camera_lock_a_person_result_is_allowed():
    assert carrier_issues(brief="可以出镜", characters=("char_host",), picture="真人吹完头发干了") == []


def test_l6_must_fail_bundle_is_not_a_live_hair_test():
    assert "发束被当成真人护发实测" in carrier_issues(
        brief="这条不出镜", characters=(), picture="发束真人护发实测")


def test_l6_must_fail_bundle_does_not_grow_a_head():
    assert "独立发束被写成了人头" in carrier_issues(
        brief="不出镜", characters=(), picture="发束旁边露出她的脸")


def test_l6_must_fail_off_camera_still_casts_a_person():
    assert "不出镜仍安排了真人" in carrier_issues(
        brief="不出镜", characters=("char_host",), picture="独立发束放在台上")


def test_l6_must_fail_forcing_a_bundle_without_the_lock():
    assert "未锁定不出镜却强制发束" in carrier_issues(
        brief="可以真人出镜", characters=(), picture="必须发束")


# --- L7 状态变化 ---


def _wet_shot():
    return {
        "id": "s0", "start": 0, "end": 3,
        "description": "筒口喷出白烟。",
        "continuity": {
            "entry_transition": "opening",
            "start_state": "发尾湿着",
            "allowed_changes": "",
            "end_state": "发尾湿塌",
            "camera_move": "机位不动",
        },
    }


def test_l7_prompt_drops_nozzle_smoke_and_writes_wet_to_dry():
    prepared = prepare_prompt_shot(_wet_shot(), spoken="吹干了")
    blob = prepared["description"] + prepared["continuity"]["allowed_changes"]
    assert "发丝由湿到干" in prepared["continuity"]["allowed_changes"]
    assert "喷白烟" not in blob and "蒸汽" not in blob
    assert state_change_issues(prepared, "吹干了") == []
    segment = _segment(mode="voiceover", ad_form="brand_tvc")
    segment["shots"][0] = {**_wet_shot(), "character_ids": [], "product_visible": True}
    segment["shots"][1]["character_ids"] = []
    segment["speech_spans"][0]["text"] = "吹干了。"
    segment["speech_spans"][0]["anchors"][0]["text"] = "吹干了。"
    prompt = compile_grouped_video(segment)
    assert "喷白烟" not in prompt and "蒸汽" not in prompt
    assert "发丝由湿到干" in prompt


def test_l7_identity_does_not_freeze_damp_tips_when_the_spot_claims_dry():
    segment = _segment(mode="voiceover", ad_form="brand_tvc")
    segment["production"]["characters"][0]["description"] = "发尾微湿，短发"
    segment["speech_spans"][0]["text"] = "吹干了。"
    segment["speech_spans"][0]["anchors"][0]["text"] = "吹干了。"
    prompt = compile_grouped_video(segment)
    assert "发尾微湿" not in prompt
    assert "发丝湿度不锁定" in prompt


def test_l7_damp_tips_stay_when_the_spot_does_not_claim_dry():
    segment = _segment(mode="voiceover", ad_form="brand_tvc")
    segment["production"]["characters"][0]["description"] = "发尾微湿，短发"
    segment["speech_spans"][0]["text"] = "看这一下。"
    segment["speech_spans"][0]["anchors"][0]["text"] = "看这一下。"
    prompt = compile_grouped_video(segment)
    assert "发尾微湿" in prompt


def test_l7_must_fail_dry_claim_with_wet_end_and_no_transition():
    raw = _wet_shot()
    issues = state_change_issues(raw, "吹完是顺的")
    assert "声称干顺但镜末仍湿且没有状态变化" in issues
    assert "喷嘴喷烟" in state_change_issues(raw, "吹干了")
    cleared = ensure_hair_transition(raw, "吹干了")
    assert "声称干顺但镜末仍湿且没有状态变化" not in state_change_issues(cleared, "吹干了")


# --- L8 分镜到视频保真 ---


def test_l8_roommate_back_of_head_in_the_same_clause_keeps_the_speaker_on_camera():
    segment = _segment(delivery="on_and_off_screen", ad_form="scene_story")
    segment["shots"][1]["description"] = "主讲看见室友后脑，仍对着镜子开口。"
    segment["shots"][1]["character_ids"] = ["char_host"]
    prompt = compile_grouped_video(segment)
    second = _shot_block(prompt, "镜头2 |")
    assert "嘴型与台词同步" in second
    assert "画外接着说" not in second


def test_l8_lips_outside_the_frame_do_not_compile_lip_sync():
    segment = _segment(delivery="on_and_off_screen", ad_form="presenter_pitch")
    segment["shots"][1]["description"] = "商品特写。画外接着说，嘴唇不在画面里。"
    segment["shots"][1]["character_ids"] = ["char_host"]
    close = _shot_block(compile_grouped_video(segment), "镜头2 |")
    assert "面向镜头说话" not in close
    assert "嘴型与台词同步" not in close
    assert "画外接着说" in close


def test_l8_speaker_own_back_of_head_stays_off_camera():
    segment = _segment(delivery="on_and_off_screen", ad_form="scene_story")
    segment["shots"][0]["description"] = "她背对镜头，只露后脑。"
    opening = _shot_block(compile_grouped_video(segment), "镜头1 |")
    assert "画外接着说" in opening


def test_l8_story_without_cta_last_shot_does_not_lipsync_a_pitch():
    segment = _segment(ad_form="scene_story")
    segment["shots"][1]["character_ids"] = ["char_host"]
    segment["speech_spans"] = [{
        "id": "speech_cta", "start": 3, "end": 6, "shot_ids": ["s1"],
        "text": "想带走，就点下面链接。",
        "anchors": [{"text": "想带走，就点下面链接。", "start": 3, "end": 6, "shot_ids": ["s1"]}],
    }]
    prompt = compile_grouped_video(segment)
    last = _shot_block(prompt, "镜头2 |")
    assert "嘴型与台词同步" not in last
    assert "不对口型，不念链接" in last
    assert "点下面" not in prompt


def test_l8_door_close_is_gentle():
    segment = _segment(ad_form="scene_story")
    segment["shots"][0]["description"] = "她摔门进屋。"
    segment["speech_spans"][0]["text"] = "别吵。"
    segment["speech_spans"][0]["anchors"][0]["text"] = "别吵。"
    prompt = compile_grouped_video(segment)
    opening = _shot_block(prompt, "镜头1 |")
    assert "摔门" not in opening
    assert "轻轻" in opening


def test_l8_key_actions_enter_the_state_line():
    allowed = stitch_actions("她翻身，手指按住，把声音压低，然后带门", "")
    assert "翻身" in allowed
    assert "手部动作" in allowed
    assert "压低声音" in allowed
    assert "轻轻带门" in allowed


def test_l8_must_fail_slamming_the_door_fights_a_quiet_claim():
    assert "摔门砸了安静" in action_issues("她摔门", "安静")
    assert "带门要轻" in action_issues("她带门离开", "别吵")


# --- L9 案例留在测试里 ---


def test_l9_rewrite_prompt_does_not_lock_a_replacement_sentence():
    assert "不要指定某一句替换" in LINE_REWRITE_SYSTEM
    assert "吹完头发顺了" not in LINE_REWRITE_SYSTEM
    assert "想带走，就点下面链接" not in LINE_REWRITE_SYSTEM
    assert "终于顺了" not in LINE_REWRITE_SYSTEM
