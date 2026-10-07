"""Brand and presenter shots do not ask the video model to push and act at once."""
from pathlib import Path

from app.progressive_planning import DIRECTOR_SYSTEM
from app.shot_craft import lock_product_frame, lock_story_insert
from app.video_prompt import compile_grouped_video


def _continuity(camera, start="瓶身直立在桌面", allowed="商品保持不动", end="瓶身仍直立"):
    return {
        "entry_transition": "opening",
        "entry_reason": "看到商品",
        "start_state": start,
        "allowed_changes": allowed,
        "end_state": end,
        "camera_move": camera,
    }


def _shot(description, camera, *, start=0, end=3, visible=True, **continuity):
    state = _continuity(camera)
    state.update(continuity)
    return {
        "id": "s0",
        "start": start,
        "end": end,
        "description": description,
        "product_visible": visible,
        "continuity": state,
    }


def test_playbooks_separate_action_from_a_push_and_leave_the_story_ending():
    root = Path("app/skills/shared/ad-form-playbooks")
    brand = (root / "brand-tvc.md").read_text()
    presenter = (root / "presenter-pitch.md").read_text()
    story = (root / "scene-story.md").read_text()
    assert "不要同时写人物动作和改变景别" in brand
    assert "至少 3 秒" in brand and "四到六成" in brand
    assert "推近、拉开、变焦到特写" in presenter
    assert "不要在最后一镜放下、递出或移出画面" in presenter
    assert "关机、放下、松手" in presenter
    assert "镜头可以推近作余韵" not in story
    assert "插入的近景或特写" in story
    assert "可以由镜头推近作余韵" not in DIRECTOR_SYSTEM
    assert "插入的近景或特写" in DIRECTOR_SYSTEM
    assert "不要把人物动作和推近" in DIRECTOR_SYSTEM


def test_a_hand_action_loses_the_push_and_a_product_only_push_can_stay():
    mixed = _shot("手把瓶身转向窗光。", "微距特写，镜头缓慢推进")
    locked = lock_product_frame(mixed, form="brand_tvc", final=False)
    assert "推进" not in locked["description"]
    assert "推进" not in locked["continuity"]["camera_move"]
    assert "机位停住" in locked["continuity"]["camera_move"]
    assert "微距" in locked["continuity"]["camera_move"]
    assert locked["continuity"]["start_state"] == mixed["continuity"]["start_state"]
    turning = _shot("瓶身沿桌面缓慢转动。", "微距特写，镜头缓慢推进")
    assert lock_product_frame(turning, form="brand_tvc", final=False) is turning


def test_a_short_closing_push_starts_at_the_target_size():
    shot = _shot("瓶身从画面边缘推近。", "中景，镜头缓慢推进", end=2.4, start_state="瓶身还在画面边缘")
    locked = lock_product_frame(shot, form="brand_tvc", final=True)
    camera = locked["continuity"]["camera_move"]
    start = locked["continuity"]["start_state"]
    assert "推进" not in camera and "机位停住" in camera
    assert start.startswith("商品从这一镜第一帧")
    assert "画面边缘" not in start
    assert "第一帧" in start and "四成到六成" in start
    assert locked["product_visible"] is True
    again = lock_product_frame(locked, form="brand_tvc", final=True)
    assert again["continuity"] == locked["continuity"]
    assert again["description"] == locked["description"]


def test_a_quiet_closing_push_of_at_least_three_seconds_can_stay():
    shot = _shot("瓶身已经在画面中央。", "中近景，镜头缓慢推进", end=3.4, start_state="瓶身已经在画面中央")
    kept = lock_product_frame(shot, form="brand_tvc", final=True)
    assert "推进" in kept["continuity"]["camera_move"]
    assert "四成到六成" in kept["continuity"]["start_state"]
    assert "第一帧" in kept["continuity"]["start_state"]


def test_presenter_ending_keeps_the_product_in_frame_and_drops_stacked_actions():
    shot = _shot(
        "她关掉吹风机，把它放下，松开手，说完最后一句。",
        "中近景，镜头缓慢推进",
        end=8.8,
        start_state="吹风机还开着，拿在手里",
        allowed_changes="关机，放下，手离开",
        end_state="吹风机离开画面，只剩人",
    )
    locked = lock_product_frame(shot, form="presenter_pitch", final=True)
    blob = " ".join([
        locked["description"],
        locked["continuity"]["start_state"],
        locked["continuity"]["allowed_changes"],
        locked["continuity"]["end_state"],
        locked["continuity"]["camera_move"],
    ])
    assert "推进" not in blob
    assert "把它放下" not in blob and "松开" not in blob and "离开画面" not in blob
    assert "关掉" in blob
    assert "不放下" in blob
    assert "留在手里或身前" in locked["continuity"]["start_state"]
    assert "第一帧" in locked["continuity"]["start_state"]
    assert locked["product_visible"] is True
    assert lock_product_frame(shot, form="scene_story", final=True) is shot


def test_ending_shots_keep_one_main_action_for_brand_and_presenter():
    from app.progressive_planning import images_blocked_reason
    from app.shot_craft import settle_ending_actions, simplify_ending_shot

    stacked = _shot(
        "她摘下银色耳机，放到桌上，收回手。",
        "中近景，机位停住",
        start=6.4,
        end=10.0,
        start_state="耳机还戴在头上",
        allowed_changes="摘下，放下，收手",
        end_state="手已经收回，耳机在桌上",
    )
    for form in ("brand_tvc", "presenter_pitch"):
        simplified = simplify_ending_shot(stacked)
        assert "摘下" in simplified["description"]
        assert "放到" not in simplified["description"] and "收回" not in simplified["description"]
        plan = {
            "ad_form": form,
            "strategy": {"brief": {"ad_form": form}, "selected_direction_id": "d1",
                         "directions": [{"id": "d1", "film_concept": {"structure": "product_visual"}}]},
            "segments": [{"id": "seg_1", "duration_seconds": 10, "shots": [
                {"id": "s6", "start": 0, "end": 6.4, "description": "她拿着耳机看向镜头。"},
                dict(stacked, id="s7"),
            ], "speech_spans": []}],
        }
        assert settle_ending_actions(plan) is True
        last = plan["segments"][-1]["shots"][-1]
        assert "摘下" in last["description"]
        assert "放到" not in last["description"] and "收回" not in last["description"]
        assert len(plan["segments"][-1]["shots"]) == 2
    story = {
        "strategy": {"brief": {"ad_form": "scene_story"}, "selected_direction_id": "d1",
                     "directions": [{"id": "d1", "film_concept": {"structure": "scene_action"}}]},
        "segments": [{"shots": [dict(stacked, id="s7")]}],
    }
    assert settle_ending_actions(story) is False
    assert "放到桌上" in story["segments"][0]["shots"][0]["description"]
    long = _shot(
        "她摘下耳机，放到桌上。",
        "中近景，机位停住",
        start=0,
        end=8,
    )
    long["id"] = "s7"
    quiet = {
        "strategy": {"brief": {"ad_form": "brand_tvc"}, "selected_direction_id": "d1",
                     "directions": [{"id": "d1", "film_concept": {"structure": "product_visual"}}]},
        "segments": [{"id": "seg_1", "duration_seconds": 8, "shots": [long], "speech_spans": []}],
    }
    assert settle_ending_actions(quiet) is True
    pieces = quiet["segments"][0]["shots"]
    assert len(pieces) == 2
    assert "摘下" in pieces[0]["description"] and "放到" not in pieces[0]["description"]
    assert "放到" in pieces[1]["description"] and "摘下" not in pieces[1]["description"]
    assert images_blocked_reason({"review_status": "not_ready", "plan_review": {"ready": False}})
    assert images_blocked_reason({"review_status": "passed"}) is None
    assert images_blocked_reason({"review_status": "conservative_fallback"}) is None
    # A board with no review record used to be released. It is blocked now.
    assert images_blocked_reason({"plan_review": {"ready": True}})
    placeholder = {"review_status": "conservative_fallback",
                   "ad_script": {"beats": [{"narration": "看这里。"}, {"narration": "就是这个。"}]},
                   "shots": [{"description": "商品放在画面中间。"}, {"description": "包装放在画面中间。"}]}
    assert images_blocked_reason(placeholder)


def test_compiled_prompt_locks_brand_and_presenter_but_not_a_story_push():
    def segment(form, last):
        return {
            "duration_seconds": 6,
            "generation_strategy": "continuity_v1",
            "ad_form": form,
            "speech_spans": [],
            "shots": [
                {"id": "s0", "start": 0, "end": 3, "description": "瓶口先入画。", "product_visible": True},
                last,
            ],
        }

    closing = {
        "id": "s1", "start": 3, "end": 6, "product_visible": True,
        "description": "手把瓶身转正，镜头缓慢推进。",
        "continuity": _continuity("中景，镜头缓慢推进", start="瓶身还在画面边缘"),
    }
    brand = compile_grouped_video(segment("brand_tvc", closing))
    tail = brand.split("镜头2 |", 1)[1]
    assert "推进" not in tail
    assert "四成到六成" in tail and "机位停住" in tail
    presenter_shot = {
        "id": "s1", "start": 3, "end": 6, "product_visible": True,
        "description": "她关机，放下吹风机，松手，停住。",
        "continuity": _continuity("中近景，镜头推近", start="吹风机还在手里"),
    }
    presenter_segment = segment("presenter_pitch", presenter_shot)
    presenter_segment["production"] = {
        "version": "1.0.0",
        "product_constraints": [],
        "characters": [{"id": "char_host", "name": "主讲", "description": "短发，灰色上衣"}],
        "speech_mode": "character_dialogue",
        "speaker_id": "char_host",
        "voice_description": "自然的成年女性普通话",
    }
    presenter_segment["reference_roles"] = [
        {"index": 1, "role": "product"},
        {"index": 2, "role": "scene"},
        {"index": 3, "role": "character", "character_id": "char_host"},
    ]
    presenter = compile_grouped_video(presenter_segment)
    spoken = presenter.split("镜头2 |", 1)[1]
    assert "推近" not in spoken and "放下吹风机" not in spoken
    assert "留在手里或身前" in spoken
    story_shot = {
        "id": "s1", "start": 3, "end": 6, "product_visible": True,
        "description": "她带上门离开。镜头推近洗手台，吹风机留在台上。",
        "continuity": _continuity("近景，镜头推近", start="吹风机留在台上"),
    }
    story = compile_grouped_video(segment("scene_story", story_shot))
    ending = story.split("镜头2 |", 1)[1]
    assert "镜头推近" not in ending
    assert "机位停住" in ending and "第一帧" in ending
    assert "四成到六成" not in ending
    assert "英雄定格" not in ending and "正面朝镜头" not in ending
    assert "硬切" in ending


def test_story_insert_locks_the_last_shot_and_cuts_after_a_person():
    mixed = _shot("她伸手去拿瓶子，镜头缓慢推进。", "中景，镜头缓慢推进")
    locked = lock_story_insert(mixed, final=False, previous=None)
    assert "推进" not in locked["continuity"]["camera_move"]
    assert "机位停住" in locked["continuity"]["camera_move"]
    assert "四成到六成" not in locked["continuity"]["start_state"]
    quiet = _shot("瓶身沿桌面缓慢转动。", "微距特写，镜头缓慢推进")
    assert lock_story_insert(quiet, final=False) is quiet
    earlier = _shot("她放下瓶子，转身离开。", "中景，机位停住")
    closing = _shot(
        "镜头推近台面，瓶子留在原处。",
        "中景，镜头推近",
        end=5,
        start_state="瓶子还在画面边缘",
        entry_transition="continuous",
    )
    insert = lock_story_insert(closing, final=True, previous=earlier)
    assert "推近" not in insert["description"]
    assert "推近" not in insert["continuity"]["camera_move"]
    assert "特写" in insert["continuity"]["camera_move"] or "近景" in insert["continuity"]["camera_move"]
    assert insert["continuity"]["start_state"].startswith("产品从这一镜第一帧")
    assert "画面边缘" not in insert["continuity"]["start_state"]
    assert insert["continuity"]["entry_transition"] == "cut"
    assert "硬切" in insert["continuity"]["entry_reason"]
    assert "英雄定格" not in insert["description"]
    again = lock_story_insert(insert, final=True, previous=earlier)
    assert again["continuity"]["camera_move"] == insert["continuity"]["camera_move"]
    assert again["continuity"]["start_state"] == insert["continuity"]["start_state"]
