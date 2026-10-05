"""Deterministic shot-list checks. No model, network, or provider imports.

A storyboard that names a video flow, or marks whether the product is in
frame, is checked for screen time, literal pictures, camera language, and a
hero hold. Older drafts omit those fields and are left alone. A rewrite, when
it runs, only replaces description and camera_move on the named shots.
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
_COMPLEX_HAND = re.compile(r"换风嘴|按小按钮|两手交接|两手|快速放下|快速收起|快速转动|翻转|按键|换挡")
_CONTINUE_LINE = "动作或运镜进行到切点，下一段从同一动作或同一机位方向接上。"


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
            item = _suggestion(3, target, quote, "结尾镜头不到2秒，英雄定格站不住。这是建议，不会为此重排时间。")
            if item:
                suggestions.append(item)
        elif not _hold_ok(description, camera):
            item = _suggestion(3, target, quote, "结尾镜头没有写机位停住。最后约2秒保持结束姿态，产品留在画面里，不要另加字卡。")
            if item:
                suggestions.append(item)
    _append_hand_suggestions(suggestions, rows)
    _append_opening_suggestions(suggestions, rows, strategy)
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


def _motion_sentence(clause: str, *, index: int) -> str:
    lead = "从起始状态开始，只做一个运动" if index == 0 else "只做一个运动"
    if clause:
        return f"{lead}：{clause.rstrip('。')}。"
    return f"{lead}，变化限于本镜允许变化。"


def shot_prompt_lines(shot, *, hold_tail: bool, continue_tail: bool = False) -> list[str]:
    """Same request, more specific time. Shots of 4 seconds or less stay one line.

    hold_tail is the film's last shot. continue_tail is the last shot of an
    earlier generation segment: it names the cut into the next segment and
    does not add a hold.
    """
    start = _tenths(shot["start"])
    end = _tenths(shot["end"])
    header = f"{shot['id']} | {shot['start']:g}–{shot['end']:g}秒 | {shot['description']}"
    lines = [header]
    windows = _phase_windows(start, end, hold_tail=hold_tail)
    if windows:
        clauses = _clauses(shot.get("description") or "")
        if clauses and _HOLD.search(clauses[-1]):
            clauses = clauses[:-1]
        motion_index = 0
        lines.append("  分阶段：同一条视频请求内，每段只做一个运动。")
        for begin, finish, is_hold in windows:
            if is_hold:
                text = _HOLD_LINE
            else:
                clause = clauses[motion_index] if motion_index < len(clauses) else ""
                text = _motion_sentence(clause, index=motion_index)
                motion_index += 1
            lines.append(f"  {begin / 10:g}–{finish / 10:g}秒：{text}")
        if continue_tail and not hold_tail:
            lines.append(f"  段末：{_CONTINUE_LINE}")
        return lines
    duration = end - start
    if hold_tail and duration >= _tenths(HERO_HOLD_SECONDS):
        hold_at = end - _tenths(HERO_HOLD_SECONDS)
        lines.append(f"  {hold_at / 10:g}–{end / 10:g}秒：{_HOLD_LINE}")
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
