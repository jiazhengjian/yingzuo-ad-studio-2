"""Shot-list checks stay local unless a named shot's picture text is dirty."""
import asyncio
from copy import deepcopy

import pytest

from app.progressive_planning import compile_director, compose_refinement, director_schema
from app.plan_review import validate_plan_review
from app.runtime_skills import stage_skill_context
from app.shot_craft import (apply_shot_rewrite, craft_refine_request, rewrite_shots_if_needed,
                            shot_prompt_lines, shot_suggestions, with_shot_suggestions, _HOLD_LINE)
from app.video_prompt import compiled_video_request
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import source


def _film(shots, form="brand_tvc", flow="先看到瓶口，再看到整瓶停住。"):
    cursor = 0.0
    built = []
    for index, shot in enumerate(shots):
        duration = shot["duration"]
        item = {
            "id": shot.get("id", f"s{index}"),
            "start": round(cursor, 1),
            "end": round(cursor + duration, 1),
            "description": shot["description"],
            "continuity": {
                "camera_move": shot.get("camera", "微距特写，镜头缓慢推进"),
                "start_state": "瓶身静置",
                "allowed_changes": "只改变机位",
                "end_state": "瓶身静置",
            },
        }
        if "visible" in shot:
            item["product_visible"] = shot["visible"]
        built.append(item)
        cursor += duration
    return {
        "video_flow": flow,
        "strategy": {
            "brief": {"ad_form": form, "facts": [{"id": "appearance", "text": "蓝瓶"}]},
            "selected_direction_id": "d1",
            "directions": [{"id": "d1", "big_idea": "桌角这一只", "hook": "先看见瓶口",
                            "film_concept": {"structure": "product_visual"}}],
        },
        "visual_direction": {"scene": "办公桌", "lighting": "左侧窗光", "color_palette": "浅灰", "camera_style": "靠近瓶身"},
        "segments": [{"shots": built}],
    }


def _notes(plan):
    return [item["note"] for item in shot_suggestions(plan, plan["strategy"])]


def test_metaphor_detector_ignores_ordinary_ru_and_texture_words():
    silky = _film([{"duration": 3, "description": "瓶身如丝绸般反光，机位停住。", "visible": True}])
    assert any("比喻" in note for note in _notes(silky))
    plain = _film([{"duration": 3, "description": "如果侧光打在拉丝纹理上，瓶盖的如意纹仍清楚，机位停住。", "visible": True}])
    assert not any("比喻" in note for note in _notes(plain))
    texture = _film([{"duration": 3, "description": "侧光留下瓶肩的质感，机位停住。", "visible": True}])
    assert not any("比喻" in note for note in _notes(texture))


def test_camera_needs_both_framing_and_a_move():
    weak = _film([{"duration": 3, "description": "瓶身直立在桌角，机位停住。", "visible": True, "camera": "缓慢展示产品质感"}])
    assert any("景别" in note for note in _notes(weak))
    held = _film([{"duration": 3, "description": "瓶身直立在桌角，机位停住。", "visible": True,
                   "camera": "微距特写，镜头缓慢推进"}])
    assert not any("景别" in note for note in _notes(held))


def test_screen_time_follows_the_playbook_and_skips_unmarked_shots():
    def ratio(form, first, second):
        plan = _film([
            {"duration": first, "description": "瓶口特写，侧光沿着瓶肩。", "visible": True, "camera": "微距特写，镜头缓慢推进"},
            {"duration": second, "description": "整瓶停在桌角，机位停住。", "visible": False, "camera": "中景，机位停住"},
        ], form=form)
        return _notes(plan)
    assert not any("目标" in note for note in ratio("brand_tvc", 8, 2))
    assert any("80%" in note for note in ratio("brand_tvc", 6, 4))
    assert not any("目标" in note for note in ratio("scene_story", 5, 5))
    assert any("50%" in note for note in ratio("scene_story", 4, 6))
    assert not any("目标" in note for note in ratio("presenter_pitch", 7, 3))
    unmarked = _film([
        {"duration": 2, "description": "瓶口特写。", "camera": "微距特写，镜头缓慢推进"},
        {"duration": 8, "description": "整瓶停在桌角，机位停住。", "camera": "中景，机位停住"},
    ])
    assert not any("目标" in note for note in _notes(unmarked))
    legacy = _film([{"duration": 3, "description": "瓶身如丝绸般反光。", "camera": "缓慢展示"}], flow="")
    assert shot_suggestions(legacy, legacy["strategy"]) == []


def _with_beats(plan):
    for index, shot in enumerate(plan["segments"][0]["shots"]):
        shot["beat_id"] = f"b{index}"
        shot["beat_ids"] = [f"b{index}"]
    plan["ad_script"] = {"beats": [{"id": f"b{index}"} for index in range(len(plan["segments"][0]["shots"]))]}
    return plan


def test_opening_and_short_ending_ask_for_one_narrow_refine():
    hidden = _with_beats(_film([
        {"duration": 3, "description": "人站在洗手台前。", "visible": False, "camera": "中近景，镜头固定"},
        {"duration": 4, "description": "吹风机对着头发，机位停住。", "visible": True, "camera": "中景，机位停住"},
    ], form="presenter_pitch"))
    opening = craft_refine_request(hidden, hidden["strategy"])
    assert opening["scope"]["shot_ids"] == ["s0"]
    assert opening["scope"]["beat_ids"] == ["b0"]
    assert "opening_product" in opening["issues"][("craft", "opening_product")] or "opening_product" in {
        key[1] for key in opening["issues"]}
    scene = _with_beats(_film([
        {"duration": 4, "description": "早上有人皱眉。", "visible": False, "camera": "中近景，镜头固定"},
    ], form="scene_story"))
    assert "opening_product_window" in {key[1] for key in craft_refine_request(scene, scene["strategy"])["issues"]}
    later = _with_beats(_film([
        {"duration": 2, "description": "人皱眉。", "visible": False, "camera": "中近景，镜头固定"},
        {"duration": 3, "description": "吹风机已经开着。", "visible": True, "camera": "近景，镜头轻推"},
    ], form="scene_story"))
    assert craft_refine_request(later, later["strategy"]) is None
    short = _with_beats(_film([
        {"duration": 4, "description": "吹风机在手里。", "visible": True, "camera": "中近景，镜头固定"},
        {"duration": 1.5, "description": "商品一闪。", "visible": True, "camera": "中景，镜头定格"},
    ], form="presenter_pitch"))
    ending = craft_refine_request(short, short["strategy"])
    assert ending["scope"]["shot_ids"] == ["s1"]
    assert "b0" in ending["scope"]["beat_ids"] and "b1" in ending["scope"]["beat_ids"]
    lowered = _with_beats(_film([
        {"duration": 3, "description": "拿着吹风机讲。", "visible": True, "camera": "中近景，镜头固定"},
        {"duration": 3, "description": "风停了，放下吹风机，机位停住。", "visible": True, "camera": "中景，机位停住"},
    ], form="presenter_pitch"))
    assert craft_refine_request(lowered, lowered["strategy"]) is None
    assert any("手指" in note for note in _notes(lowered))
    assert "人物和手都不再动" in _HOLD_LINE and "不放下" in _HOLD_LINE


def test_end_hold_and_even_cuts_are_advice():
    held = _film([{"duration": 2.5, "description": "瓶身停在中轴，机位定格。", "visible": True}])
    assert not any("结尾" in note for note in _notes(held))
    short = _film([{"duration": 0.8, "description": "瓶身一闪。", "visible": True, "camera": "微距特写，镜头定格"}])
    assert any("不到2秒" in note for note in _notes(short))
    loose = _film([{"duration": 2.5, "description": "瓶身仍在转动。", "visible": True, "camera": "微距特写，镜头缓慢推进"}])
    assert any("停住" in note for note in _notes(loose))
    even = _film([
        {"duration": 2, "description": "瓶口特写。", "visible": True, "camera": "微距特写，镜头缓慢推进"},
        {"duration": 2, "description": "瓶肩侧光。", "visible": True, "camera": "近景，镜头轻摇"},
        {"duration": 2, "description": "整瓶停住。", "visible": True, "camera": "中景，机位停住"},
    ])
    assert any("均分" in note for note in _notes(even))


_HOLD_WORDS = ("定格", "停住", "不再运镜", "机位停", "固定机位", "保持不动", "镜头停")


def _picture(prompt):
    return prompt.split("画面镜头", 1)[1]


def _compile_segment(shots, *, closes_film, duration):
    segment = {
        "duration_seconds": duration,
        "generation_strategy": "continuity_v1",
        "speech_spans": [],
        "shots": shots,
        "closes_film": closes_film,
    }
    prompt = compiled_video_request(segment)["prompt"]
    assert "closes_film" not in prompt
    return prompt


def _shot(shot_id, start, end, description):
    return {"id": shot_id, "start": start, "end": end, "description": description}


def test_only_the_films_last_segment_holds():
    moving = _shot("move", 0, 3, "手把瓶身转向窗光，镜头仍在前推。")
    ending = _shot("end", 0, 3, "瓶身留在窗光里。")
    long_move = _shot("long", 0, 8, "手把瓶身转向窗光。镜头仍在前推。")
    two = [
        _picture(_compile_segment([moving], closes_film=False, duration=3)),
        _picture(_compile_segment([ending], closes_film=True, duration=3)),
    ]
    assert "下一段" in two[0] and "段末" in two[0]
    assert not any(word in two[0] for word in _HOLD_WORDS)
    assert "停住" in two[1] and "下一段" not in two[1] and "分阶段" not in two[1]
    three = [
        _picture(_compile_segment([moving], closes_film=False, duration=3)),
        _picture(_compile_segment([long_move], closes_film=False, duration=8)),
        _picture(_compile_segment([ending], closes_film=True, duration=3)),
    ]
    assert "分阶段" in three[1] and "5.4–8秒" in three[1] and "下一段" in three[1]
    assert not any(word in three[0] or word in three[1] for word in _HOLD_WORDS)
    assert "停住" in three[2] and "下一段" not in three[2]
    bare = _picture(compiled_video_request({
        "duration_seconds": 3, "generation_strategy": "continuity_v1", "speech_spans": [],
        "shots": [ending],
    })["prompt"])
    assert "停住" in bare and "下一段" not in bare
    opened = _compile_segment([
        _shot("a", 0, 2, "瓶口先入画。"),
        _shot("b", 2, 4, "手把瓶身转向窗光。"),
    ], closes_film=False, duration=4)
    head, _, tail = _picture(opened).partition("镜头2 | 2–4秒")
    assert "下一段" not in head and "段末" not in head
    assert "下一段" in tail and not any(word in tail for word in _HOLD_WORDS)
    closed = _compile_segment([
        _shot("a", 0, 2, "瓶口先入画。"),
        _shot("b", 2, 5, "瓶身留在窗光里。"),
    ], closes_film=True, duration=5)
    head, _, tail = _picture(closed).partition("镜头2 | 2–5秒")
    assert "停住" not in head and "下一段" not in head
    assert "停住" in tail and "下一段" not in tail


def test_submission_spec_marks_only_the_last_segment():
    from app.pipeline import video_submission_spec
    segments = [{"id": f"seg_{index}", "duration_seconds": 10} for index in range(1, 4)]
    plan = {"audio_strategy": "native", "segments": segments}
    assert [video_submission_spec(plan, segment)["closes_film"] for segment in segments] == [False, False, True]
    pair = segments[:2]
    assert [video_submission_spec({"audio_strategy": "native", "segments": pair}, segment)["closes_film"]
            for segment in pair] == [False, True]
    lone = video_submission_spec({"audio_strategy": "native"}, {"reference_asset_ids": ["original"]})
    assert lone["closes_film"] is True


def _film_segments(groups):
    segments = []
    for group in groups:
        cursor = 0.0
        built = []
        for index, shot in enumerate(group):
            duration = shot["duration"]
            built.append({
                "id": shot["id"],
                "start": round(cursor, 1),
                "end": round(cursor + duration, 1),
                "description": shot["description"],
                "product_visible": shot["visible"],
                "continuity": {
                    "camera_move": shot["camera"],
                    "start_state": "瓶身静置",
                    "allowed_changes": "只改变机位",
                    "end_state": "瓶身仍在转动",
                },
            })
            cursor += duration
        segments.append({"id": f"seg_{len(segments) + 1}", "shots": built})
    return {
        "video_flow": "先看到瓶口，再看到瓶身转向窗光。",
        "strategy": {
            "brief": {"ad_form": "brand_tvc", "facts": [{"id": "appearance", "text": "蓝瓶"}]},
            "selected_direction_id": "d1",
            "directions": [{"id": "d1", "big_idea": "桌角这一只", "hook": "先看见瓶口",
                            "film_concept": {"structure": "product_visual"}}],
        },
        "visual_direction": {"scene": "办公桌", "lighting": "左侧窗光", "color_palette": "浅灰", "camera_style": "靠近瓶身"},
        "segments": segments,
    }


def test_end_hold_check_names_only_the_films_last_shot():
    from app.shot_craft import shot_rewrite_issues
    pieces = [
        {"id": "a", "duration": 4, "description": "手把瓶身转向窗光。", "visible": True, "camera": "近景，镜头前推"},
        {"id": "b", "duration": 0.8, "description": "瓶肩掠过窗光。", "visible": True, "camera": "微距特写，镜头轻摇"},
        {"id": "c", "duration": 3, "description": "瓶身仍在转向窗光。", "visible": True, "camera": "中景，镜头继续前推"},
    ]
    director = {
        "video_flow": "先看到瓶口，再看到瓶身转向窗光。",
        "shots": [
            {"id": shot["id"], "description": shot["description"],
             "continuity": {"camera_move": shot["camera"]}}
            for shot in pieces
        ],
    }
    for count, groups in ((2, [[pieces[0], pieces[1]], [pieces[2]]]),
                          (3, [[pieces[0]], [pieces[1]], [pieces[2]]])):
        plan = _film_segments(groups)
        items = shot_suggestions(plan, plan["strategy"])
        holds = [item for item in items if "结尾" in item["note"]]
        assert len(holds) == 1
        assert holds[0]["target"] == f"/segments/{count - 1}/shots/0/description"
        assert "停住" in holds[0]["note"] and "1.5" not in holds[0]["note"]
        assert not any("1.5" in item["note"] for item in items)
        issues = shot_rewrite_issues(director, plan)
        assert [item["shot_id"] for item in issues if "hold" in item["problems"]] == ["c"]
        assert issues[-1]["needs_hold"] is True
        assert all(item["shot_id"] == "c" or "hold" not in item["problems"] for item in issues)


def test_phase_lines_stay_inside_one_request():
    short = shot_prompt_lines({"id": "a", "start": 0, "end": 3, "description": "瓶身直立。"}, hold_tail=False)
    assert short == ["a | 0–3秒 | 瓶身直立。"]
    exact = shot_prompt_lines({"id": "b", "start": 3, "end": 7, "description": "瓶身直立。"}, hold_tail=False)
    assert "分阶段" not in "\n".join(exact)
    long = "\n".join(shot_prompt_lines(
        {"id": "c", "start": 0, "end": 8, "description": "瓶口先入画。镜头推近整瓶。"}, hold_tail=True))
    assert "分阶段" in long and "停住" in long and "0–8秒" in long
    ending = "\n".join(shot_prompt_lines(
        {"id": "d", "start": 7, "end": 10, "description": "瓶身停在桌角。"}, hold_tail=True))
    assert "分阶段" not in ending and "停住" in ending
    segment = {"duration_seconds": 8, "generation_strategy": "continuity_v1", "speech_spans": [], "shots": [{
        "id": "c", "start": 0, "end": 8, "description": "瓶口先入画。镜头推近整瓶。"}]}
    request = compiled_video_request(segment)
    assert set(request) == {"prompt", "skill_trace"}
    assert "images" not in request and "分阶段" in request["prompt"]


def test_optional_motif_and_match_cut_survive_and_stay_off_old_shots(source):
    from pydantic import ValidationError
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=2)
    plan, _, _ = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    assert "visual_motif" not in plan["segments"][0]["shots"][0]
    assert "match_cut" not in plan["segments"][0]["shots"][0]
    raw["shots"][1]["visual_motif"] = "蓝光"
    raw["shots"][1]["match_cut"] = "color"
    plan, _, _ = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    shots = [shot for segment in plan["segments"] for shot in segment["shots"]]
    assert shots[1]["visual_motif"] == "蓝光"
    assert shots[1]["match_cut"] == "color"
    raw["shots"][1]["match_cut"] = "logo"
    with pytest.raises(ValidationError):
        compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])


def test_initial_schema_asks_for_flow_without_changing_the_stored_contract():
    initial = director_schema(initial=True)
    assert "video_flow" in initial["required"]
    assert initial["properties"]["video_flow"]["minLength"] == 12
    shot = initial["$defs"]["DirectedShot"]
    assert "product_visible" in shot["required"]
    assert shot["properties"]["product_visible"]["type"] == "boolean"
    plain = director_schema()
    assert set(plain["properties"]) == {"title", "ad_script", "visual_direction", "shots"}


def test_empty_flow_stays_out_of_old_plans_and_refinement_keeps_it(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=2)
    plan, _, dumped = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    assert "video_flow" not in plan and "video_flow" not in dumped
    assert "product_visible" not in plan["segments"][0]["shots"][0]
    raw["video_flow"] = "先看到瓶口，再看到整瓶停住。"
    raw["shots"][0]["product_visible"] = False
    raw["shots"][1]["product_visible"] = True
    plan, _, dumped = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    assert plan["video_flow"].startswith("先看到") and dumped["video_flow"].startswith("先看到")
    assert [shot["product_visible"] for shot in plan["segments"][0]["shots"]] == [False, True]
    revised = {"ad_script": deepcopy(raw["ad_script"]), "shots": deepcopy(raw["shots"])}
    revised["shots"][0].pop("product_visible")
    revised.pop("video_flow", None)
    merged = compose_refinement(revised, raw, {})
    assert merged["video_flow"].startswith("先看到")
    assert merged["shots"][0]["product_visible"] is False


def test_shot_rewrite_stage_loads_only_the_short_literal_note():
    loaded = stage_skill_context("shot_craft_rewrite")
    skill = loaded["skills"][0]
    assert skill["loaded_sections"] == ["SKILL.md", "references/shot-literal.md"]
    assert "references/coordinated-storyboard.md" not in skill["loaded_sections"]
    assert loaded["shared_references"] == []


def _dirty_director():
    director = {
        "video_flow": "先看到瓶口，再看到整瓶停住。",
        "ad_script": {"beats": [{"id": "b0", "narration": "看轮廓。"}]},
        "shots": [{
            "id": "s0",
            "coverage": [{"beat_id": "b0", "weight": 1}],
            "description": "瓶身如丝绸般反光。",
            "purpose": "看瓶肩",
            "product_visible": True,
            "continuity": {
                "start_state": "瓶身静置",
                "end_state": "瓶身静置",
                "allowed_changes": "只改变机位",
                "camera_move": "缓慢展示产品质感",
                "entry_transition": "opening",
                "entry_reason": "开场",
            },
        }],
    }
    plan = _film([{"id": "s0", "duration": 2.5, "description": "瓶身如丝绸般反光。", "visible": True,
                   "camera": "缓慢展示产品质感"}])
    return director, plan


def test_rewrite_accepts_only_a_literal_camera_and_keeps_the_line():
    director, plan = _dirty_director()
    issues_holder = {}

    async def stage_call(system, payload, assets, max_tokens=7000):
        issues_holder["payload"] = payload
        issues_holder["assets"] = assets
        issues_holder["max_tokens"] = max_tokens
        return {"shots": [{"shot_id": "s0",
                           "description": "侧光打在瓶肩，瓶身直立，最后机位停住。",
                           "camera_move": "微距特写，镜头缓慢推进"}]}

    called, revised = asyncio.run(rewrite_shots_if_needed(stage_call, director, plan, plan["strategy"]))
    assert called is True and revised is not None
    assert issues_holder["assets"] == [] and issues_holder["max_tokens"] == 1200
    assert issues_holder["payload"]["task_kind"] == "shot_craft_rewrite"
    assert revised["ad_script"]["beats"][0]["narration"] == "看轮廓。"
    assert revised["shots"][0]["coverage"] == director["shots"][0]["coverage"]
    assert revised["shots"][0]["product_visible"] is True
    assert revised["shots"][0]["continuity"]["start_state"] == "瓶身静置"
    assert "如丝绸" not in revised["shots"][0]["description"]
    from app.shot_craft import shot_rewrite_issues
    issues = shot_rewrite_issues(director, plan)
    assert apply_shot_rewrite(director, {"shots": []}, issues) is None
    assert apply_shot_rewrite(director, {"shots": [{"shot_id": "s0", "description": "侧光打在瓶肩，机位停住。",
                                                    "camera_move": "微距特写，镜头缓慢推进", "extra": 1}]}, issues) is None
    assert apply_shot_rewrite(director, {"shots": [{"shot_id": "s0", "description": "侧光打在瓶肩，3档，机位停住。",
                                                    "camera_move": "微距特写，镜头缓慢推进"}]}, issues, facts_text="蓝瓶") is None
    assert apply_shot_rewrite(director, {"shots": [{"shot_id": "s0", "description": "侧光打在瓶肩，美白见效，机位停住。",
                                                    "camera_move": "微距特写，镜头缓慢推进"}]}, issues, facts_text="蓝瓶") is None


def test_clean_shot_list_does_not_call_the_model():
    director, plan = _dirty_director()
    director["shots"][0]["description"] = "侧光打在瓶肩，最后机位停住。"
    director["shots"][0]["continuity"]["camera_move"] = "微距特写，镜头缓慢推进"
    plan["segments"][0]["shots"][0]["description"] = director["shots"][0]["description"]
    plan["segments"][0]["shots"][0]["continuity"]["camera_move"] = "微距特写，镜头缓慢推进"

    async def stage_call(*args, **kwargs):
        raise AssertionError("clean shot list must not call")

    called, revised = asyncio.run(rewrite_shots_if_needed(stage_call, director, plan, plan["strategy"]))
    assert called is False and revised is None


def test_craft_notes_do_not_change_a_ready_review(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy, count=3)
    raw["video_flow"] = "先看到瓶口的一条边，再看到整瓶停在桌角。"
    for shot in raw["shots"]:
        shot["product_visible"] = False
        shot["description"] = "瓶身如丝绸般反光，办公桌角只有轮廓。"
        shot["continuity"]["camera_move"] = "缓慢展示产品质感"
    plan, _, _ = compile_director(raw, strategy, 12, "continuity_v1", [source["id"]])
    review = validate_plan_review(accepted_review(plan), plan, require_speaker_visibility=True,
                                  require_temporal_omission=True, require_temporal_omission_evidence=True,
                                  require_visual_execution=True, require_review_policy=True)
    assert review.ready and review.suggestions == []
    attached = with_shot_suggestions(review, plan, strategy)
    assert attached.ready is True
    assert attached.suggestions and all(item.kind == "shot_craft" for item in attached.suggestions)
    assert attached.reviewed_plan_sha256 == review.reviewed_plan_sha256
