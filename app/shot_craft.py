"""Deterministic shot-list checks. No model, network, or provider imports.

A storyboard that names a video flow, or marks whether the product is in
frame, is checked for screen time, literal pictures, camera language, and a
hero hold. Older drafts omit those fields and are left alone. A rewrite, when
it runs, only replaces description and camera_move on the named shots.
Product films and presenter films also get a prompt-only framing lock: a
character action does not share a shot with a push, pull, or zoom, and the
last shot already shows the product at its end size. A story ending is rewritten
into a locked insert close-up. None of this rejects a plan.
"""
from __future__ import annotations

from copy import deepcopy
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ad_form_playbook import screen_time_target, speech_profile_for_strategy
from .review_policy import MAX_REVIEW_SUGGESTIONS


SINGLE_MOTION_SECONDS = 4.0
HERO_HOLD_SECONDS = 2.0
HOLD_ADVICE_FLOOR = 2.0
EVEN_CUT_TENTHS = 2

_FRAMING = re.compile(r"大特写|特写|中近景|近景|中景|全景|远景|微距|过肩")
_MOTION = re.compile(r"环绕|跳切|升降|手持|定格|固定|平视|低角|高角|正面|推|拉|摇|移|跟|停|俯|仰|侧")
_HOLD = re.compile(r"定格|停住|不再运镜|机位停|固定机位|保持不动|镜头停")
# 如 is a metaphor only when it is not part of 如果 / 例如 / 比如 / 如实 / 如今.
_METAPHOR = re.compile(
    r"如同|宛如|仿佛|好似|犹如|好像|像是|象征着|拟人|视觉隐喻|"
    r"高级感|高端质感|氛围感|情绪价值|"
    r"像[^，。；、\n]{1,12}一样|"
    r"(?<![例如比])如(?!果|何|实|下|今|此|意|常|初|说|图|同)"
)
_CLAIM_WORDS = ("根治", "治愈", "美白", "减肥", "抗衰", "医用", "治疗")
_FORM_LABEL = {"brand_tvc": "产品展示", "scene_story": "剧情", "presenter_pitch": "人物讲解"}
_HOLD_LINE = "机位停住，人物和手都不再动，商品稳稳留在手里或原位、正面朝镜头，不放下、不移出画面，不出现字幕、Logo或字卡。"
_PUT_DOWN = re.compile(r"放下|放回|收起|递")
_PRODUCT_FRAME_FORMS = frozenset({"brand_tvc", "presenter_pitch"})
_FRAMING_WORD = re.compile(r"大特写|特写|中近景|近景|中景|全景|远景|微距|过肩")
_PUSH_MOVE = re.compile(
    r"(?:镜头[^，。；、\n]{0,6})?(?:缓慢|慢慢|轻微|轻轻|仍在|继续)?(?:向前)?"
    r"(?:推近|推进|前推|慢推|拉近|变焦)"
    r"|(?:推|拉)(?:到|至|成)(?:大特写|特写|近景|中近景)"
)
_CHAR_ACTION = re.compile(
    r"放下|放回|收起|递给|递出|递向|关机|关掉|关闭电源|松手|松开|拿开|"
    r"移出|出画|离开画面|举起|拿起|按下|按键|换挡|换风嘴|"
    r"转给|拧开|拧上|盖上|打开盖|对着[^，。；]{0,8}吹|吹几下|"
    r"手把|用手|手(?:把|用|里)?[^，。；\s]{0,6}转"
)
_LEAVE_FRAME = re.compile(r"放下|放回|收起|递给|递出|递向|松手|松开|拿开|移出|出画|离开画面")
_STACKED_ACTION = re.compile(r"关机|关掉|关闭|放下|放回|收起|松手|松开|拿开|递出|递给|递向|按关|停机")
_WORLD_PREFIX = re.compile(r"^(生活世界：|产品世界：|衔接：)")
_NOT_YET_FRAMED = re.compile(r"画面边缘|还在远处|很小|尚未入画|还没入画|只露|看不清|腰部以上|半身以外")
_BRAND_FRAME = "商品从这一镜第一帧就在画面里，正面朝镜头，大小约占画面四成到六成"
_PRESENTER_FRAME = "商品从这一镜第一帧就在画面里，留在手里或身前，不放下、不移出画面"
_PRESENTER_HOLD = "人和商品停住，商品留在手里或身前，正面可认，不再做别的动作"
_COMPLEX_HAND = re.compile(r"换风嘴|按小按钮|两手交接|两手|快速放下|快速收起|快速转动|翻转|按键|换挡")
_CONTINUE_LINE = "动作或运镜进行到切点，下一段从同一动作或同一机位方向接上。"
_WORLD_MARK = re.compile(r"生活世界|产品世界|衔接")
_DUAL_WORLD = {"world_intercut", "lifestyle"}
_WORLD_NOTE = "这条要穿过生活世界和产品世界。每镜用「生活世界：」「产品世界：」或「衔接：」起头，两边才不会混成一段。防变形只提醒保持商品外形。这是建议，不因此重排。"


class _ShotText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1500)
    camera_move: str = Field(min_length=1, max_length=800)


class _ShotBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shots: list[_ShotText] = Field(min_length=1, max_length=18)


def _tenths(value) -> int:
    return int(round(float(value) * 10))


def _duration_tenths(shot) -> int | None:
    if not isinstance(shot, dict) or "start" not in shot or "end" not in shot:
        return None
    try:
        return _tenths(shot["end"]) - _tenths(shot["start"])
    except (TypeError, ValueError):
        return None


def _indexed_shots(plan):
    rows = []
    if not isinstance(plan, dict):
        return rows
    segments = plan.get("segments")
    if isinstance(segments, list) and segments:
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            for shot_index, shot in enumerate(segment.get("shots") or []):
                if isinstance(shot, dict):
                    rows.append((segment_index, shot_index, shot))
        return rows
    for shot_index, shot in enumerate(plan.get("shots") or []):
        if isinstance(shot, dict):
            rows.append((0, shot_index, shot))
    return rows


def craft_enabled(plan) -> bool:
    """New storyboards opt in by writing a flow or an explicit visibility flag."""
    if not isinstance(plan, dict):
        return False
    flow = plan.get("video_flow")
    if isinstance(flow, str) and flow.strip():
        return True
    for _, _, shot in _indexed_shots(plan):
        if isinstance(shot.get("product_visible"), bool):
            return True
    return False


def _camera_text(shot) -> str:
    continuity = shot.get("continuity") if isinstance(shot, dict) else None
    text = continuity.get("camera_move") if isinstance(continuity, dict) else ""
    return text if isinstance(text, str) else ""


def _camera_ok(text: str) -> bool:
    return bool(text and _FRAMING.search(text) and _MOTION.search(text))


def _hold_ok(description: str, camera: str) -> bool:
    return bool(_HOLD.search(description or "") or _HOLD.search(camera or ""))


def _metaphor_in(*texts) -> bool:
    return any(isinstance(text, str) and _METAPHOR.search(text) for text in texts)


def _snippet(text: str, match, radius=12) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    snippet = text[start:end].strip()
    if not snippet or snippet not in text:
        snippet = match.group(0)
    return snippet[:240]


def _selected_skeleton(strategy) -> str:
    if not isinstance(strategy, dict):
        return ""
    selected = strategy.get("selected_direction_id")
    for direction in strategy.get("directions") or []:
        if not isinstance(direction, dict):
            continue
        if selected and direction.get("id") != selected:
            continue
        concept = direction.get("film_concept") if isinstance(direction.get("film_concept"), dict) else {}
        return str(concept.get("visual_skeleton") or concept.get("structure") or "")
    return ""


def _form(strategy) -> str | None:
    profile = speech_profile_for_strategy(strategy)
    return profile.form if profile.form in _FORM_LABEL else None


def _plan_by_id(plan):
    return {shot.get("id"): shot for _, _, shot in _indexed_shots(plan)
            if isinstance(shot.get("id"), str)}


def shot_rewrite_issues(director, plan) -> list[dict]:
    """Shots whose picture text can be repaired without moving time."""
    if not craft_enabled(director) and not craft_enabled(plan):
        return []
    shots = director.get("shots") if isinstance(director, dict) else None
    if not isinstance(shots, list) or not shots:
        return []
    timed = _plan_by_id(plan)
    last_id = shots[-1].get("id") if isinstance(shots[-1], dict) else None
    issues = []
    for shot in shots:
        if not isinstance(shot, dict) or not isinstance(shot.get("id"), str):
            continue
        description = shot.get("description") if isinstance(shot.get("description"), str) else ""
        camera = _camera_text(shot)
        problems = []
        if _metaphor_in(description, camera):
            problems.append("metaphor")
        if not _camera_ok(camera):
            problems.append("camera")
        duration = _duration_tenths(timed.get(shot["id"]))
        needs_hold = False
        if shot["id"] == last_id and duration is not None and duration >= _tenths(HOLD_ADVICE_FLOOR):
            if not _hold_ok(description, camera):
                problems.append("hold")
                needs_hold = True
        if problems:
            issues.append({
                "shot_id": shot["id"],
                "description": description,
                "camera_move": camera,
                "problems": problems,
                "needs_hold": needs_hold,
                "duration_seconds": None if duration is None else duration / 10,
            })
    return issues


def _suggestion(kind_rank, target, quote, note):
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 240 or len(note) > 300:
        return None
    return {"_rank": kind_rank, "kind": "shot_craft", "target": target, "quote": quote[:240], "note": note}


def shot_suggestions(plan, strategy=None) -> list[dict]:
    """Non-blocking notes. Empty when this storyboard did not opt into the new fields."""
    if not isinstance(plan, dict) or not craft_enabled(plan):
        return []
    rows = _indexed_shots(plan)
    if not rows:
        return []
    suggestions = []
    for segment_index, shot_index, shot in rows:
        description = shot.get("description") if isinstance(shot.get("description"), str) else ""
        continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
        texts = [("description", description, f"/segments/{segment_index}/shots/{shot_index}/description")]
        for field in ("camera_move", "start_state", "allowed_changes", "end_state"):
            value = continuity.get(field)
            if isinstance(value, str) and value.strip():
                texts.append((field, value, f"/segments/{segment_index}/shots/{shot_index}/continuity/{field}"))
        for field, text, target in texts:
            match = _METAPHOR.search(text)
            if not match:
                continue
            item = _suggestion(0, target, _snippet(text, match),
                               "这里有比喻或抽象说法。视频模型会按字面去画，改成能拍到的光线、材质、位置和动作。")
            if item:
                suggestions.append(item)
        camera = continuity.get("camera_move") if isinstance(continuity.get("camera_move"), str) else ""
        if camera.strip() and not _camera_ok(camera):
            item = _suggestion(1, f"/segments/{segment_index}/shots/{shot_index}/continuity/camera_move",
                               camera[:240], "运镜要同时写景别和运动，例如微距、镜头缓慢推进。")
            if item:
                suggestions.append(item)
    direction = plan.get("visual_direction") if isinstance(plan.get("visual_direction"), dict) else {}
    for field in ("scene", "lighting", "color_palette", "camera_style"):
        text = direction.get(field)
        if not isinstance(text, str):
            continue
        match = _METAPHOR.search(text)
        if not match:
            continue
        item = _suggestion(0, f"/visual_direction/{field}", _snippet(text, match),
                           "视觉约定里的比喻会进视频提示词。改成能拍到的光线、材质和位置。")
        if item:
            suggestions.append(item)
    durations = [(shot, _duration_tenths(shot)) for _, _, shot in rows]
    if all(item[1] is not None for item in durations):
        visible_flags = [shot.get("product_visible") for shot, _ in durations]
        if visible_flags and all(isinstance(flag, bool) for flag in visible_flags):
            total = sum(duration for _, duration in durations)
            visible = sum(duration for shot, duration in durations if shot.get("product_visible"))
            form = _form(strategy)
            target = screen_time_target(form)
            if total > 0 and visible * 1000 < int(round(target * 1000)) * total:
                hidden = [(shot, duration) for shot, duration in durations if not shot.get("product_visible")]
                if not hidden:
                    hidden = durations
                quote_shot = max(hidden, key=lambda item: item[1])[0]
                quote = quote_shot.get("description") if isinstance(quote_shot.get("description"), str) else ""
                segment_index, shot_index, _ = next(row for row in rows if row[2] is quote_shot)
                label = _FORM_LABEL.get(form or "", "广告")
                note = (f"产品在画面里约占{round(100 * visible / total)}%，这条{label}的目标是约占{round(target * 100)}%。"
                        "可以把离镜的镜头缩短，或让产品留在画面里。")
                item = _suggestion(2, f"/segments/{segment_index}/shots/{shot_index}/description", quote[:240], note)
                if item:
                    suggestions.append(item)
        if len(durations) >= 3:
            span = max(duration for _, duration in durations) - min(duration for _, duration in durations)
            if span <= EVEN_CUT_TENTHS:
                description = durations[0][0].get("description") if isinstance(durations[0][0].get("description"), str) else ""
                item = _suggestion(4, "/segments/0/shots/0/description", description[:240],
                                   "这几镜时长几乎一样。切点放在动作完成或特点出现的时候，不要按秒均分。")
                if item:
                    suggestions.append(item)
    last_segment, last_index, last = rows[-1]
    last_duration = _duration_tenths(last)
    description = last.get("description") if isinstance(last.get("description"), str) else ""
    camera = _camera_text(last)
    quote = (description or camera)[:240]
    target = (f"/segments/{last_segment}/shots/{last_index}/description" if description
              else f"/segments/{last_segment}/shots/{last_index}/continuity/camera_move")
    if last_duration is not None and quote:
        if last_duration < _tenths(HOLD_ADVICE_FLOOR):
            hold_note = ("结尾镜头不到2秒，插入近景站不住。这是建议，不会为此重排时间。"
                         if _form(strategy) == "scene_story"
                         else "结尾镜头不到2秒，英雄定格站不住。这是建议，不会为此重排时间。")
            item = _suggestion(3, target, quote, hold_note)
            if item:
                suggestions.append(item)
        elif not _hold_ok(description, camera):
            item = _suggestion(3, target, quote, "结尾镜头没有写机位停住。最后约2秒保持结束姿态，产品留在画面里，不要另加字卡。")
            if item:
                suggestions.append(item)
    _append_hand_suggestions(suggestions, rows)
    _append_opening_suggestions(suggestions, rows, strategy)
    if _selected_skeleton(strategy) in _DUAL_WORLD:
        descriptions = [
            shot.get("description") if isinstance(shot.get("description"), str) else ""
            for _, _, shot in rows
        ]
        if descriptions and not any(_WORLD_MARK.search(text) for text in descriptions):
            segment_index, shot_index, shot = rows[0]
            quote = shot.get("description") if isinstance(shot.get("description"), str) else ""
            item = _suggestion(5, f"/segments/{segment_index}/shots/{shot_index}/description", quote[:240], _WORLD_NOTE)
            if item:
                suggestions.append(item)
    suggestions.sort(key=lambda item: (item["_rank"], item["target"]))
    seen, result = {}, []
    for item in suggestions:
        identity = (item["kind"], item["target"], item["quote"])
        if identity in seen:
            previous = seen[identity]
            from .planner_recovery import clip_server_text
            extra = item["note"]
            if extra and extra not in previous["note"] and len(previous["note"]) + len(extra) <= 300:
                previous["note"] = clip_server_text(previous["note"] + extra)
            continue
        kept = {key: value for key, value in item.items() if key != "_rank"}
        seen[identity] = kept
        result.append(kept)
    return result


def with_shot_suggestions(review, plan, strategy=None):
    """Attach craft notes only to a ready review. A bad quote is dropped, not raised."""
    extras = shot_suggestions(plan, strategy)
    ready = review.ready if hasattr(review, "ready") else (review.get("ready") if isinstance(review, dict) else False)
    if not extras or not ready:
        return review
    from .plan_review import validate_plan_review
    from .review_policy import review_suggestion_targets
    data = review.model_dump() if hasattr(review, "model_dump") else deepcopy(review)
    targets = review_suggestion_targets(plan)
    accepted = [item for item in extras if item["quote"] in targets.get(item["target"], "")]
    if not accepted:
        return review
    merged, seen = [], set()
    for item in accepted + list(data.get("suggestions") or []):
        identity = (item.get("kind"), item.get("target"), item.get("quote"))
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(item)
        if len(merged) >= MAX_REVIEW_SUGGESTIONS:
            break
    data["suggestions"] = merged
    try:
        return validate_plan_review(data, plan, require_speaker_visibility=True,
                                    require_temporal_omission=True, require_temporal_omission_evidence=True,
                                    require_visual_execution=True, require_review_policy=True)
    except (ValueError, KeyError, TypeError):
        return review


def _shot_text(shot) -> str:
    parts = [shot.get("description") or ""]
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    parts.extend(continuity.get(field) or "" for field in ("camera_move", "start_state", "allowed_changes", "end_state"))
    return " ".join(part for part in parts if isinstance(part, str))


def _hand_on_product(shot) -> bool:
    blob = _shot_text(shot)
    has_hand = "手" in blob or bool(re.search(r"五指|手指", blob))
    product_word = bool(re.search(r"商品|产品|瓶|机|杯|盒", blob))
    if shot.get("product_visible") is False and not product_word:
        return False
    # Putting the product down is a hand even when the shot never writes 手.
    # A flip or a button without a hand word is the product, not a hand.
    if _PUT_DOWN.search(blob) and product_word:
        return True
    if not has_hand:
        return False
    return product_word or bool(re.search(r"握|拿|按|递|换", blob))


def _append_hand_suggestions(suggestions, rows):
    """One note, and only when a person's hand is actually on the product."""
    note = "这一镜手和商品动作多，视频模型容易把手指和商品画变形。可以放慢、让商品拿稳，或拆成更简单的动作。这是建议。"
    for segment_index, shot_index, shot in rows:
        if not _hand_on_product(shot):
            continue
        blob = _shot_text(shot)
        if not (_PUT_DOWN.search(blob) or _COMPLEX_HAND.search(blob) or re.search(r"手", blob)):
            continue
        quote = (shot.get("description") or blob)[:240]
        item = _suggestion(5, f"/segments/{segment_index}/shots/{shot_index}/description", quote, note)
        if item:
            suggestions.append(item)
            return


def _append_opening_suggestions(suggestions, rows, strategy):
    form = _form(strategy)
    first_segment, first_index, first = rows[0]
    quote = (first.get("description") if isinstance(first.get("description"), str) else "")[:240]
    target = f"/segments/{first_segment}/shots/{first_index}/description"
    if form == "presenter_pitch" and first.get("product_visible") is False and quote:
        item = _suggestion(3, target, quote, "人物讲解的第一镜还没看见商品。这是建议，方案会继续。")
        if item:
            suggestions.append(item)
    elif form == "brand_tvc" and quote and _window_hides_product(rows, 1.5):
        item = _suggestion(3, target, quote, "前大约1.5秒还没看见商品。这是建议，方案会继续。")
        if item:
            suggestions.append(item)
    elif form == "scene_story" and quote and _window_hides_product(rows, 3.0):
        item = _suggestion(3, target, quote, "前大约3秒还没看见商品或用法。这是建议，方案会继续。")
        if item:
            suggestions.append(item)


def _window_hides_product(rows, seconds=3.0) -> bool:
    window = []
    for _, _, shot in rows:
        try:
            start = float(shot.get("start"))
        except (TypeError, ValueError):
            return False
        if start < seconds + 1e-6:
            window.append(shot)
    if not window or any(shot.get("product_visible") is not False for shot in window):
        return False
    return True


def _shot_beats(shot, plan):
    beats = [item for item in (shot.get("beat_ids") or []) if isinstance(item, str) and item]
    if not beats and isinstance(shot.get("beat_id"), str) and shot["beat_id"]:
        beats = [shot["beat_id"]]
    if beats:
        return beats
    script_beats = ((plan.get("ad_script") or {}).get("beats") or [])
    if script_beats and isinstance(script_beats[0], dict) and script_beats[0].get("id"):
        return [script_beats[0]["id"]]
    return []


def craft_refine_request(plan, strategy) -> dict | None:
    """One narrow rewrite for opening, ending length, or a missing on-camera speaker.

    Hand motion is not included. A missing request means this pass can continue.
    """
    if not isinstance(plan, dict) or not craft_enabled(plan):
        return None
    rows = _indexed_shots(plan)
    if not rows:
        return None
    form = _form(strategy)
    issues = []
    beat_ids, shot_ids = [], []

    def take(shot, *, beats=None):
        shot_id = shot.get("id")
        if isinstance(shot_id, str) and shot_id and shot_id not in shot_ids:
            shot_ids.append(shot_id)
        for beat_id in beats if beats is not None else _shot_beats(shot, plan):
            if beat_id not in beat_ids:
                beat_ids.append(beat_id)

    first = rows[0][2]
    if form == "presenter_pitch" and first.get("product_visible") is False:
        issues.append("opening_product")
        take(first)
    elif form == "brand_tvc" and _window_hides_product(rows, 1.5):
        issues.append("opening_product_window")
        take(first)
    elif form == "scene_story" and _window_hides_product(rows, 3.0):
        issues.append("opening_product_window")
        take(first)
    last = rows[-1][2]
    last_duration = _duration_tenths(last)
    if last_duration is not None and last_duration < _tenths(HOLD_ADVICE_FLOOR):
        issues.append("short_ending")
        earlier = []
        for beat in (plan.get("ad_script") or {}).get("beats") or []:
            if isinstance(beat, dict) and isinstance(beat.get("id"), str):
                earlier.append(beat["id"])
        if not earlier:
            earlier = []
            for _, _, shot in rows:
                earlier.extend(_shot_beats(shot, plan))
        take(last, beats=earlier or _shot_beats(last, plan))
    production = plan.get("production") if isinstance(plan.get("production"), dict) else {}
    speaker_id = production.get("speaker_id") if production.get("speech_mode") == "character_dialogue" else ""
    if speaker_id:
        spans = [span for segment in plan.get("segments") or [] if isinstance(segment, dict)
                 for span in (segment.get("speech_spans") or []) if isinstance(span, dict)]
        first_shot_id = (spans[0].get("shot_ids") or [None])[0] if spans else None
        spoken = next((shot for _, _, shot in rows if shot.get("id") == first_shot_id), None)
        if spoken is not None and speaker_id not in (spoken.get("character_ids") or []):
            issues.append("speaker_missing")
            take(spoken)
    if not issues or not shot_ids or not beat_ids:
        return None
    labels = {
        "opening_product": "人物讲解的第一镜还没看见商品",
        "opening_product_window": "前大约1.5秒还没看见商品" if form == "brand_tvc" else "前大约3秒还没看见商品或用法",
        "short_ending": "结尾镜头不到2秒",
        "speaker_missing": "第一句所在的镜头没有说话的人",
    }
    quote = ""
    for _, _, shot in rows:
        if shot.get("id") == shot_ids[0] and isinstance(shot.get("description"), str):
            quote = shot["description"][:240]
            break
    return {
        "scope": {
            "beat_ids": beat_ids,
            "shot_ids": shot_ids,
            "adjacent_state_shot_ids": [],
            "adjacent_state_beat_ids": [],
            "allow_summary_update": False,
        },
        "feedback": {
            "kind": "opening_ending_craft",
            "issue": "；".join(labels[name] for name in issues),
            "repair_contract": (
                "只改列出的开头或结尾。人物讲解的第一镜要让商品清楚可认。"
                "剧情可以先给处境，但前约3秒要看见商品或用法。产品片大约1.5秒内要认出商品。"
                "最后一镜至少2秒，机位停住，商品不动。可以缩短前面的台词给结尾留时间。"
                "出镜讲解的第一句所在镜头要有说话人。改完仍不通过就保留当前稿继续。"
            ),
        },
        "issues": {("craft", name): labels[name] for name in issues},
        "suggestion": {
            "kind": "shot_craft",
            "target": f"/segments/0/shots/0/description",
            "quote": quote,
            "note": "开头或结尾还可以再清楚一点。这是建议，方案会继续。",
        },
    }


def _clauses(description: str) -> list[str]:
    text = description or ""
    parts, buffer, quote = [], [], None
    closing = {"“": "”", "\"": "\"", "「": "」", "『": "』"}
    for character in text:
        if quote:
            buffer.append(character)
            if character == quote:
                quote = None
            continue
        if character in closing:
            quote = closing[character]
            buffer.append(character)
            continue
        if character in "。；":
            part = "".join(buffer).strip()
            if part:
                parts.append(part)
            buffer = []
            continue
        buffer.append(character)
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts


def _phase_windows(start_tenths: int, end_tenths: int, *, hold_tail: bool):
    duration = end_tenths - start_tenths
    if duration <= _tenths(SINGLE_MOTION_SECONDS):
        return []
    hold = None
    motion_end = end_tenths
    if hold_tail and duration >= 30:
        hold = (end_tenths - _tenths(HERO_HOLD_SECONDS), end_tenths)
        motion_end = hold[0]
    windows = []
    motion = motion_end - start_tenths
    if motion > 0:
        count = max(1, (motion + 29) // 30)
        while count and motion / count > 40:
            count += 1
        base, extra = divmod(motion, count)
        cursor = start_tenths
        for index in range(count):
            step = base + (1 if index < extra else 0)
            windows.append((cursor, cursor + step, False))
            cursor += step
    if hold is not None:
        windows.append((hold[0], hold[1], True))
    return windows


# A place-and-light sentence has none of these. The action that follows must stay
# on the timing line instead of being cut off after the setting.
_ACTION_CUE = re.compile(
    r"把|将|拿|放|推|拉|递|举|握|端|切|倒|送|走|跑|坐|站|转|看|说|问|笑|"
    r"打开|关上|伸手|点头|摇头|开口|入画|入镜|推近|前推|后退|低下|抬起|翻身"
)


def _motion_sentence(clause: str, *, index: int) -> str:
    lead = "从起始状态开始，只做一个运动" if index == 0 else "只做一个运动"
    if clause:
        return f"{lead}：{clause.rstrip('。')}。"
    return f"{lead}，变化限于本镜允许变化。"


def _fold_timing_clauses(clauses: list[str], motion_count: int) -> list[str]:
    """One clause per motion window. A setting-only lead joins the next action.

    Extra clauses used to be dropped, so a split line could keep only
    「厨房台面前，顶灯暖光。」 and lose the action.
    """
    if motion_count <= 0:
        return []
    folded = list(clauses)
    if len(folded) >= 2 and not _ACTION_CUE.search(folded[0]) and any(_ACTION_CUE.search(item) for item in folded[1:]):
        folded = [folded[0].rstrip("。") + "。" + folded[1], *folded[2:]]
    if not folded:
        return [""] * motion_count
    if len(folded) <= motion_count:
        return folded + [""] * (motion_count - len(folded))
    head = folded[: motion_count - 1]
    tail = "。".join(folded[motion_count - 1 :])
    return head + [tail]


def shot_prompt_lines(shot, *, hold_tail: bool, continue_tail: bool = False, label: str | None = None, hold_line: str | None = None) -> list[str]:
    """Same request, more specific time. Shots of 4 seconds or less stay one line.

    hold_tail is the film's last shot. continue_tail is the last shot of an
    earlier generation segment: it names the cut into the next segment and
    does not add a hold. ``label`` replaces the internal shot id in the header
    sent to a video model.
    """
    start = _tenths(shot["start"])
    end = _tenths(shot["end"])
    name = label if isinstance(label, str) and label else shot["id"]
    header = f"{name} | {shot['start']:g}–{shot['end']:g}秒 | {shot['description']}"
    lines = [header]
    windows = _phase_windows(start, end, hold_tail=hold_tail)
    if windows:
        clauses = _clauses(shot.get("description") or "")
        if clauses and _HOLD.search(clauses[-1]):
            clauses = clauses[:-1]
        motion_count = sum(1 for _, _, is_hold in windows if not is_hold)
        packed = _fold_timing_clauses(clauses, motion_count)
        motion_index = 0
        lines.append("  分阶段：同一条视频请求内，每段只做一个运动。")
        for begin, finish, is_hold in windows:
            if is_hold:
                text = hold_line or _HOLD_LINE
            else:
                clause = packed[motion_index] if motion_index < len(packed) else ""
                text = _motion_sentence(clause, index=motion_index)
                motion_index += 1
            lines.append(f"  {begin / 10:g}–{finish / 10:g}秒：{text}")
        if continue_tail and not hold_tail:
            lines.append(f"  段末：{_CONTINUE_LINE}")
        return lines
    duration = end - start
    if hold_tail and duration >= _tenths(HERO_HOLD_SECONDS):
        hold_at = end - _tenths(HERO_HOLD_SECONDS)
        lines.append(f"  {hold_at / 10:g}–{end / 10:g}秒：{hold_line or _HOLD_LINE}")
    elif continue_tail:
        lines.append(f"  段末：{_CONTINUE_LINE}")
    return lines


def rewrite_schema(issues: list[dict]) -> dict:
    schema = _ShotBatch.model_json_schema()
    shot = schema.pop("$defs")["_ShotText"]
    items = []
    for issue in issues:
        one = deepcopy(shot)
        one["properties"]["shot_id"]["const"] = issue["shot_id"]
        one["properties"]["description"]["description"] = "只改这一镜能拍到的画面，不改时长"
        one["properties"]["camera_move"]["description"] = "景别加运动，例如微距、镜头缓慢推进"
        items.append(one)
    schema["properties"]["shots"].update(minItems=len(items), maxItems=len(items), items={"oneOf": items})
    return schema


def _selected_copy(strategy):
    if strategy is None:
        return "", "", []
    if isinstance(strategy, dict):
        selected_id = strategy.get("selected_direction_id")
        directions = strategy.get("directions") or []
        brief = strategy.get("brief") or {}
        facts = brief.get("facts") or []
        fact_text = [fact.get("text", "") for fact in facts if isinstance(fact, dict)]
    else:
        selected_id = getattr(strategy, "selected_direction_id", None)
        directions = getattr(strategy, "directions", []) or []
        brief = getattr(strategy, "brief", None)
        facts = getattr(brief, "facts", []) if brief is not None else []
        fact_text = [getattr(fact, "text", "") for fact in facts]
    selected = next((item for item in directions if (item.get("id") if isinstance(item, dict) else item.id) == selected_id), None)
    if selected is None:
        return "", "", fact_text
    if isinstance(selected, dict):
        return selected.get("product_expression") or selected.get("big_idea") or "", selected.get("hook") or "", fact_text
    return getattr(selected, "product_expression", "") or getattr(selected, "big_idea", "") or "", getattr(selected, "hook", "") or "", fact_text


def apply_shot_rewrite(director: dict, raw, issues: list[dict], *, facts_text: str = "") -> dict | None:
    """Accept a complete text fix for the named shots, or leave the draft untouched."""
    try:
        batch = _ShotBatch.model_validate(raw)
    except (ValidationError, TypeError):
        return None
    expected = [issue["shot_id"] for issue in issues]
    got = [item.shot_id for item in batch.shots]
    if len(got) != len(set(got)) or set(got) != set(expected):
        return None
    by_id = {item.shot_id: item for item in batch.shots}
    revised = deepcopy(director)
    flow = revised.get("video_flow") if isinstance(revised.get("video_flow"), str) else ""
    shots = {shot.get("id"): shot for shot in revised.get("shots") or [] if isinstance(shot, dict)}
    for issue in issues:
        shot = shots.get(issue["shot_id"])
        incoming = by_id.get(issue["shot_id"])
        if shot is None or incoming is None or not isinstance(shot.get("continuity"), dict):
            return None
        source = f"{issue['description']}{issue['camera_move']}{facts_text}{flow}"
        combined = incoming.description + incoming.camera_move
        if any(word in combined and word not in source for word in _CLAIM_WORDS):
            return None
        if set(re.findall(r"\d+", combined)) - set(re.findall(r"\d+", source)):
            return None
        if _metaphor_in(incoming.description, incoming.camera_move):
            return None
        if not _camera_ok(incoming.camera_move):
            return None
        if issue.get("needs_hold") and not _hold_ok(incoming.description, incoming.camera_move):
            return None
        shot["description"] = incoming.description
        shot["continuity"]["camera_move"] = incoming.camera_move
    return revised


SHOT_REWRITE_SYSTEM = """你只改列出的镜头文字，只输出 JSON 对象，对象里只有 schema 的 shots，每项只有 shot_id、description、camera_move。
description 写能拍到的主体、动作、光线、产品是否在画面里、环境。不写如同、仿佛、好像、像什么一样、高级感、氛围感、象征。
camera_move 同时写景别和运动，例如“微距特写，镜头缓慢推进”。
needs_hold 为 true 时，在这一镜写明最后约 2 秒机位停住，产品留在画面里，不写字幕、Logo 或字卡。不要改时长。
只使用该镜原文和 facts 里已经有的商品信息，不新增功效和数字。不改台词、coverage、product_visible 和其他镜头。不解释。"""


def _rewrite_payload(director, issues, strategy):
    big_idea, hook, facts = _selected_copy(strategy)
    form = _form(strategy)
    return {
        "stage": "storyboard",
        "task_kind": "shot_craft_rewrite",
        "schema": rewrite_schema(issues),
        "ad_form": form,
        "big_idea": big_idea,
        "hook": hook,
        "video_flow": director.get("video_flow") or "",
        "facts": facts,
        "shots": issues,
    }, "".join(facts)


async def rewrite_shots_if_needed(stage_call, director: dict, plan, strategy):
    """Return (called, replacement). Clean shot lists do not call the model."""
    issues = shot_rewrite_issues(director, plan)
    if not issues:
        return False, None
    payload, facts_text = _rewrite_payload(director, issues, strategy)
    try:
        raw = await stage_call(SHOT_REWRITE_SYSTEM, payload, [], 1200)
    except Exception:
        return True, None
    revised = apply_shot_rewrite(director, raw, issues, facts_text=facts_text)
    return True, revised


def _negated_at(text, start):
    prefix = text[max(0, start - 3):start]
    return bool(re.search(r"(?:不要|别要|别|不|没|未)$", prefix))


def _affirmed_in(pattern, text):
    if not isinstance(text, str) or not text:
        return False
    return any(not _negated_at(text, match.start()) for match in pattern.finditer(text))


def _affirmed_count(pattern, text):
    if not isinstance(text, str) or not text:
        return 0
    return sum(1 for match in pattern.finditer(text) if not _negated_at(text, match.start()))


def _picture_fields(shot):
    parts = []
    description = shot.get("description")
    if isinstance(description, str):
        parts.append(description)
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    for key in ("start_state", "allowed_changes", "end_state"):
        value = continuity.get(key)
        if isinstance(value, str):
            parts.append(value)
    return parts


def _shot_seconds(shot):
    try:
        return float(shot["end"]) - float(shot["start"])
    except (TypeError, ValueError, KeyError):
        return None


def _drop_clauses(text, pattern):
    if not isinstance(text, str) or not text:
        return text
    pieces = re.split(r"([，,。；;、])", text)
    kept = []
    for index in range(0, len(pieces), 2):
        chunk = pieces[index]
        delim = pieces[index + 1] if index + 1 < len(pieces) else ""
        if chunk.strip() and _affirmed_in(pattern, chunk):
            continue
        kept.append(chunk + delim)
    result = "".join(kept)
    result = re.sub(r"^[，,。；;、\s]+", "", result)
    result = re.sub(r"[，,；;、\s]+$", "", result)
    return result.strip()


def _strip_push(text):
    if not isinstance(text, str) or not text:
        return text

    def replacer(match):
        if _negated_at(text, match.start()):
            return match.group(0)
        return "机位停住"

    updated = _PUSH_MOVE.sub(replacer, text)
    updated = re.sub(r"(?:机位停住[，、]?){2,}", "机位停住", updated)
    return updated


def _framing_word(camera):
    found = _FRAMING_WORD.search(camera or "")
    return found.group(0) if found else "中近景"


def _locked_camera(camera, form, final):
    size = _framing_word(camera)
    if final and form == "brand_tvc":
        return f"{size}，机位停住，产品从第一帧就约占画面四成到六成"
    if final and form == "presenter_pitch":
        return f"{size}，机位停住，产品从第一帧就在手里或身前"
    return f"{size}，机位停住"


def _has_push(shot):
    camera = _camera_text(shot)
    description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    return _affirmed_in(_PUSH_MOVE, camera) or _affirmed_in(_PUSH_MOVE, description) or bool(
        re.search(r"zoom", f"{camera} {description}", re.IGNORECASE)
    )


def _has_character_action(shot):
    return any(_affirmed_in(_CHAR_ACTION, part) for part in _picture_fields(shot))


def _in_frame_from_first(shot):
    if shot.get("product_visible") is False:
        return False
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    start = continuity.get("start_state") if isinstance(continuity.get("start_state"), str) else ""
    description = shot.get("description") if isinstance(shot.get("description"), str) else ""
    lead = (start or description)[:48]
    return not bool(re.search(r"不在画面|尚未入画|还没入画|画外|出画|移出", lead))


def _keep_closing_push(shot):
    """A closing push stays only when it is already its own quiet, framed shot."""
    if not _has_push(shot) or _has_character_action(shot):
        return False
    seconds = _shot_seconds(shot)
    if seconds is None or seconds < 3 - 1e-6:
        return False
    return _in_frame_from_first(shot)


def _frame_clause(form):
    return _BRAND_FRAME if form == "brand_tvc" else _PRESENTER_FRAME


def _already_framed(text, form):
    if not isinstance(text, str):
        return False
    if form == "brand_tvc":
        return "四成到六成" in text and "第一帧" in text
    return "留在手里或身前" in text and "第一帧" in text


def _append_clause(text, clause, limit=800):
    if not isinstance(text, str) or not text.strip():
        return clause[:limit]
    if clause in text:
        return text
    joiner = "" if text.endswith(("。", "，")) else "。"
    return (text + joiner + clause + "。")[:limit]


def _lead_clause(text, clause, limit=800):
    """The target size is the first thing this shot is allowed to show."""
    if not isinstance(text, str) or not text.strip():
        return clause[:limit]
    if clause in text:
        return text
    return (clause + "。" + text)[:limit]


def _hold_description(description, form):
    prefix = ""
    match = _WORLD_PREFIX.match(description or "")
    if match:
        prefix = match.group(1)
    body = _PRESENTER_HOLD if form == "presenter_pitch" else "商品停在画面里，正面朝镜头，机位停住，不再做别的动作"
    return prefix + body


def _write_continuity(revised, **updates):
    continuity = revised.get("continuity")
    if not isinstance(continuity, dict) or "entry_transition" not in continuity:
        return False
    copied = {**continuity}
    for key, value in updates.items():
        if isinstance(value, str) and value:
            copied[key] = value[:800]
    revised["continuity"] = copied
    return True


def lock_product_frame(shot, *, form, final=False):
    """Drop a push that shares a shot with a person doing something.

    Brand and presenter prompts only. A closing push is kept when this shot is
    already at least 3 seconds, has no character action, and the product is in
    frame on the first frame. The last shot still starts at the target size, so
    a push the model skips does not leave the product. Presenter endings do not
    put the product down or stack several actions. Story shots are unchanged.
    Splitting a shot would move speech windows, so a combined shot loses the
    move instead.
    """
    if form not in _PRODUCT_FRAME_FORMS or not isinstance(shot, dict):
        return shot
    revised = {**shot}
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else None
    if continuity is not None:
        revised["continuity"] = {**continuity}
    description = revised.get("description") if isinstance(revised.get("description"), str) else ""
    if final:
        fields = _picture_fields(revised)
        stacked = form == "presenter_pitch" and sum(_affirmed_count(_STACKED_ACTION, part) for part in fields) >= 2
        if stacked or any(_affirmed_in(_LEAVE_FRAME, part) for part in fields):
            description = _drop_clauses(description, _LEAVE_FRAME)
            if stacked:
                description = _drop_clauses(description, _STACKED_ACTION)
            if not _WORLD_PREFIX.sub("", description).strip("，。；、 "):
                description = _hold_description(
                    shot.get("description") if isinstance(shot.get("description"), str) else "", form)
            revised["description"] = description[:1500]
            if isinstance(revised.get("continuity"), dict):
                for key, fallback in (
                    ("start_state", _frame_clause(form)),
                    ("allowed_changes", "人物和商品保持不动"),
                    ("end_state", "商品仍留在画面里"),
                ):
                    current = revised["continuity"].get(key)
                    if not isinstance(current, str):
                        continue
                    cleaned = _drop_clauses(current, _LEAVE_FRAME)
                    if stacked:
                        cleaned = _drop_clauses(cleaned, _STACKED_ACTION)
                    revised["continuity"][key] = (cleaned or fallback)[:800]
    keep_push = bool(final and _keep_closing_push(shot))
    drop_push = _has_push(shot) and not keep_push and (final or _has_character_action(shot))
    if drop_push:
        description = revised.get("description") if isinstance(revised.get("description"), str) else description
        revised["description"] = _strip_push(description)[:1500]
        camera = _camera_text(revised)
        if isinstance(revised.get("continuity"), dict) and isinstance(revised["continuity"].get("camera_move"), str):
            revised["continuity"]["camera_move"] = _locked_camera(camera, form, final)
    if final:
        clause = _frame_clause(form)
        wrote = False
        if isinstance(revised.get("continuity"), dict) and isinstance(revised["continuity"].get("start_state"), str):
            current = _drop_clauses(revised["continuity"]["start_state"], _NOT_YET_FRAMED)
            if not _already_framed(current, form):
                current = _lead_clause(current, clause)
            revised["continuity"]["start_state"] = (current or clause)[:800]
            wrote = True
        elif _write_continuity(revised, start_state=clause):
            wrote = True
        current_description = revised.get("description") if isinstance(revised.get("description"), str) else ""
        if not wrote and not _already_framed(current_description, form):
            revised["description"] = _lead_clause(current_description, clause, 1500)
        if drop_push and isinstance(revised.get("continuity"), dict):
            camera = revised["continuity"].get("camera_move")
            if isinstance(camera, str) and "机位停住" not in camera:
                revised["continuity"]["camera_move"] = _locked_camera(camera, form, True)
        revised["product_visible"] = True
    if revised == shot:
        return shot
    return revised


_STORY_PEOPLE = re.compile(r"离开|出门|走出|带上门|转身|走了|伸手")
_STORY_EXIT = re.compile(r"带上门|离开|出门|走出|转身走|人已经走|人走了")
_STORY_LIFE = re.compile(r"火苗|烛|火焰|摇曳|热气|蒸汽|雾气|笑声|笑了一声|一只手|人手|手伸|伸进|切下|第一刀|一角|画外")
_STORY_CLOSE = frozenset({"大特写", "特写", "近景", "中近景", "微距"})
_STORY_FRAME = "产品从这一镜第一帧就以近景或特写留在故事里它该在的位置，大而清楚"
_STORY_LIFE_LINE = "留一个故事里的小动静：一只手或人的一角入画，产品仍是主体"
_STORY_INSERT = "产品近景，机位停住。产品从第一帧就大而清楚，留在故事里它该在的位置。一只手或人的一角入画，产品仍是主体。"


def _people_busy(shot):
    if not isinstance(shot, dict):
        return False
    return _has_character_action(shot) or any(
        _affirmed_in(_STORY_PEOPLE, part) for part in _picture_fields(shot))


def _story_close_camera(camera):
    found = _FRAMING_WORD.search(camera or "")
    size = found.group(0) if found and found.group(0) in _STORY_CLOSE else "特写"
    return f"{size}，机位停住，产品从第一帧就占满这一景"


def _story_framed(text):
    return isinstance(text, str) and "第一帧" in text and "近景或特写" in text


def lock_story_insert(shot, *, final=False, previous=None):
    """Prompt-only story framing. A push does not share a shot with a person acting.

    The last shot becomes an insert close-up: the product is already large, the
    camera stays locked, and one small in-story sign remains. If the previous
    shot is still a person's action, this insert is a hard cut. A plan is not
    rejected for keeping the old push-in.
    """
    if not isinstance(shot, dict):
        return shot
    revised = {**shot}
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else None
    if continuity is not None:
        revised["continuity"] = {**continuity}
    description = revised.get("description") if isinstance(revised.get("description"), str) else ""
    stripped_exit = False
    if final and description:
        cleaned = _drop_clauses(description, _STORY_EXIT)
        stripped_exit = cleaned != description
        description = cleaned
        if not _WORLD_PREFIX.sub("", description).strip("，。；、 "):
            description = _STORY_INSERT
        revised["description"] = description[:1500]
    drop_push = _has_push(shot) and (final or _people_busy(shot))
    if drop_push:
        current = revised.get("description") if isinstance(revised.get("description"), str) else ""
        revised["description"] = _strip_push(current)[:1500]
        camera = _camera_text(revised)
        if isinstance(revised.get("continuity"), dict) and isinstance(revised["continuity"].get("camera_move"), str):
            revised["continuity"]["camera_move"] = (
                _story_close_camera(camera) if final else _locked_camera(camera, "scene_story", False))
    if not final:
        if revised == shot:
            return shot
        return revised
    camera_now = _camera_text(revised)
    if isinstance(revised.get("continuity"), dict) and isinstance(revised["continuity"].get("camera_move"), str):
        if "机位停住" not in revised["continuity"]["camera_move"] or _has_push(revised):
            revised["continuity"]["camera_move"] = _story_close_camera(camera_now)
        elif _FRAMING_WORD.search(camera_now) and _FRAMING_WORD.search(camera_now).group(0) not in _STORY_CLOSE:
            revised["continuity"]["camera_move"] = _story_close_camera(camera_now)
        elif "第一帧" not in revised["continuity"]["camera_move"]:
            revised["continuity"]["camera_move"] = _story_close_camera(camera_now)
    elif not isinstance(revised.get("continuity"), dict):
        current = revised.get("description") if isinstance(revised.get("description"), str) else ""
        if "机位停住" not in current:
            revised["description"] = _lead_clause(current, "特写，机位停住", 1500)
    wrote = False
    if isinstance(revised.get("continuity"), dict) and isinstance(revised["continuity"].get("start_state"), str):
        current = _drop_clauses(revised["continuity"]["start_state"], _NOT_YET_FRAMED)
        current = _drop_clauses(current, _STORY_EXIT)
        if not _story_framed(current):
            current = _lead_clause(current, _STORY_FRAME)
        revised["continuity"]["start_state"] = (current or _STORY_FRAME)[:800]
        wrote = True
    elif _write_continuity(revised, start_state=_STORY_FRAME):
        wrote = True
    current_description = revised.get("description") if isinstance(revised.get("description"), str) else ""
    if not wrote and not _story_framed(current_description):
        revised["description"] = _lead_clause(current_description, _STORY_FRAME, 1500)
        current_description = revised["description"]
    if not _affirmed_in(_STORY_LIFE, current_description):
        revised["description"] = _append_clause(current_description, _STORY_LIFE_LINE, 1500)
    force_cut = previous is not None and (_people_busy(previous) or stripped_exit)
    if force_cut and isinstance(revised.get("continuity"), dict):
        if revised["continuity"].get("entry_transition") in ("continuous", "opening"):
            revised["continuity"]["entry_transition"] = "cut"
            if isinstance(revised["continuity"].get("entry_reason"), str):
                revised["continuity"]["entry_reason"] = "前一镜的人物动作已经结束，这一镜硬切到产品近景。"
    if force_cut:
        current_description = revised.get("description") if isinstance(revised.get("description"), str) else ""
        if "硬切" not in current_description:
            revised["description"] = _append_clause(current_description, "这一镜硬切到产品近景", 1500)
    revised["product_visible"] = True
    if revised == shot:
        return shot
    return revised
