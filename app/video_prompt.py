"""Compile approved picture and speech plans into one native video request.

Times are desired directing windows, never a claim of measured alignment.
"""
import json
import re
from .production import cast_appearance_clause
from .runtime_skills import compiler_template


def _director_lines(context, production=None):
    """Compile approved, shared directing data; never forward unrelated fields."""
    if not context:
        return []
    lines = ["全片视觉与声音方向："]
    for key, label in (("scene", "场所与空间"), ("lighting", "光源与方向"),
                       ("color_palette", "色彩与影调"), ("camera_style", "全片机位与镜头原则"),
                       ("sound", "声音与音乐方向")):
        if context.get(key):
            label = "环境声与音乐" if key == "sound" and production else label
            lines.append(f"{label}：{context[key]}")
    return lines


def _product_lines(context, production):
    facts = [item.get("text", "") for item in context.get("product_anchors", [])]
    facts.extend(item.get("text", "") for item in (production or {}).get("product_constraints", []))
    facts = list(dict.fromkeys(text for text in facts if text))
    return ["商品必须保留：" + "；".join(facts)] if facts else []


def _continuity_lines(continuity, description):
    if not continuity:
        return []
    transitions = {"opening": "开场建立画面", "continuous": "延续上一镜状态与动作",
                   "cut": "有明确依据的切镜", "scene_change": "已策划的场所或时空转换"}
    lines = ["  入镜方式：" + transitions[continuity["entry_transition"]]]
    seen = {description}
    for key, label in (("start_state", "起始状态"),
                       ("allowed_changes", "本镜允许变化"), ("end_state", "结束状态"),
                       ("camera_move", "本镜机位与运镜")):
        text = continuity[key]
        if text and text not in seen:
            lines.append(f"  {label}：{text}")
            seen.add(text)
    return lines


def _reference_lines(segment):
    characters = {item["id"]: item for item in (segment.get("production") or {}).get("characters", [])}
    roles = segment.get("reference_roles")
    if roles is None:
        if characters:
            raise ValueError("角色四视图尚未绑定实际视频参考图片")
        # The legacy adapter always supplied exactly product, scene in that order.
        roles = [{"index": 1, "role": "product"}, {"index": 2, "role": "scene"}]
    if not isinstance(roles, list) or not roles:
        raise ValueError("视频参考图片职责不能为空")
    lines, referenced_characters = [], set()
    for index, role in enumerate(roles, start=1):
        if not isinstance(role, dict) or type(role.get("index")) is not int or role["index"] != index:
            raise ValueError("参考图编号须与真实图片顺序一致")
        if role.get("role") == "product":
            duty = "商品原图，保持商品外形、比例、颜色与已有标识"
        elif role.get("role") == "scene":
            duty = "场景参考，控制空间、材质、灯光与背景"
        elif role.get("role") == "character":
            cid = role.get("character_id")
            if cid not in characters or cid in referenced_characters:
                raise ValueError("角色参考须对应已确认的唯一角色")
            referenced_characters.add(cid)
            character = characters[cid]
            if role.get("sheet_layout") == "horizontal_16x9":
                sheet = "左侧是脸部和上胸特写，右侧同一排依次是正面、侧面、背面全身；版式不进入视频。"
            else:
                # Saved 2×2 sheets predate the horizontal contact sheet.
                sheet = "统一其外貌与服装；四格版式不进入视频。"
            description = character.get("description") or ""
            clause = cast_appearance_clause(description)
            if clause.rstrip("。") in description or ("日常大陆长相" in description and "日常大陆长相" in clause):
                appearance = description
            else:
                appearance = f"{clause}{description}"
            duty = f"角色“{character['name']}”的四视图，{sheet}{appearance}"
        else:
            raise ValueError("不支持的视频参考图片职责")
        lines.append(re.sub(r"。{2,}", "。", f"参考图{index}：{duty}。"))
    if referenced_characters != set(characters):
        raise ValueError("视频缺少已确认角色的四视图参考")
    return lines


def _character_labels(segment):
    indexes = {role["character_id"]: role["index"] for role in (segment.get("reference_roles") or [])
               if role.get("role") == "character"}
    return {person["id"]: f"{person['name']}（参考图{indexes[person['id']]}）"
            for person in (segment.get("production") or {}).get("characters", [])}


_HAND_PRODUCT = (
    "拿起|举起|放下|放回|递|转动|翻转|按|换|插|收起|握"
)
_HAND_PRODUCT_LINE = (
    "  手和商品：商品在画面里大而清楚，动作放慢；手简单握住商品，五指自然、不变形，商品形状和大小全程不变。"
)


def _voice_lines(production, director, spans, characters, *, ad_form=None):
    if not production:
        # Historical segments keep the voiceover sentence. A new presenter plan
        # must name its speech before any video request is built.
        if ad_form == "presenter_pitch":
            raise ValueError("人物讲解缺少声音制作约定，尚未提交视频")
        return [("旁白遵循已确认声音方向，用自然普通话表达；不依赖未提供的声纹或音频素材。"
                 if director.get("sound") else "自然亲切的成年女性普通话画外音，温暖克制，不播音腔。"),
                "本版使用画外音，不生成真人对口型。"]
    mode = production["speech_mode"]
    if mode == "none":
        if spans:
            raise ValueError("无人声方案不能包含台词时间表")
        return ["声音方式：无人声，只保留已确认环境声与音乐。"]
    voice = production["voice_description"]
    if mode == "voiceover":
        return [f"声音方式：画外旁白；{voice}。画面里的人物不说台词、不张嘴念词。"]
    if mode == "character_dialogue":
        speaker = next((item for item in production["characters"] if item["id"] == production["speaker_id"]), None)
        if speaker is None:
            raise ValueError("人物对白缺少已确认的说话角色")
        name = characters[speaker["id"]]
        if production.get("speech_delivery") == "on_and_off_screen":
            return [
                f"声音方式：同一人物{name}说全部台词。只要她的脸在画面里，就看着镜头亲口说，嘴型与字同步；"
                f"只有切到看不见嘴的商品特写时，才由她本人在画外接着说。其余角色不说话。声音：{voice}。"
                "声线保持连贯是执行要求，不是已验证的声纹。"
            ]
        return [
            f"声音方式：人物出镜讲话。{name}面对镜头，亲口说出下方每一句台词，从第一句的第一个字开始就张嘴说话，"
            f"嘴型与每个字同步，表情自然。这不是画外音，不另配旁白。声音：{voice}。"
        ]
    raise ValueError("不支持的声音方式")


def _speech_overlaps(shot, span):
    return min(shot["end"], span["end"]) - max(shot["start"], span["start"]) > 1e-7


_NO_FACE = re.compile(r"不露脸|看不见嘴|看不到脸|后脑|背对|手部特写|只拍手|只见手|嘴不在画面")
_FACE = re.compile(r"面向镜头|露脸|看镜头")
_MOUTH_CLOSED = re.compile(r"嘴未张开|嘴巴未张开|未开口|闭嘴|嘴闭合|嘴巴紧闭|嘴唇紧闭")


def _speaker_face_visible(shot):
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    blob = " ".join(part for part in (
        shot.get("description") or "", continuity.get("camera_move") or "", continuity.get("start_state") or "",
    ) if isinstance(part, str))
    if not _NO_FACE.search(blob):
        return True
    if _FACE.search(blob) and not re.search(r"不露脸|看不见嘴|看不到脸", blob):
        return True
    return False


def _mouth_open_state(text):
    if not isinstance(text, str) or not text:
        return text
    return _MOUTH_CLOSED.sub("已经开口", text)


def _hand_product_line(shot):
    if shot.get("product_visible") is not True:
        return None
    texts = [shot.get("description") or ""]
    continuity = shot.get("continuity") if isinstance(shot.get("continuity"), dict) else {}
    texts.extend(continuity.get(key) or "" for key in ("start_state", "allowed_changes", "end_state", "camera_move"))
    if re.search(_HAND_PRODUCT, " ".join(texts)):
        return _HAND_PRODUCT_LINE
    return None


def compile_grouped_video(segment):
    return compiled_video_request(segment)["prompt"]


def compiled_video_request(segment):
    """Return the actual media prompt with the provenance of the template used."""
    template, trace = compiler_template("ads-video-video-prompts", "references/video-template.txt")
    return {"prompt": _compile_grouped_video(segment, template), "skill_trace": [trace]}


def _compile_grouped_video(segment, template):
    """Caller validates Segment before any upload or paid submission."""
    shots = segment["shots"]
    continuous = segment.get("generation_strategy") == "continuity_v1"
    director = segment.get("director_context") or {}
    production = segment.get("production")
    lines = [template.strip(), *_reference_lines(segment)]
    characters = _character_labels(segment)
    if continuous:
        lines.extend([
            f"本次一次生成完整{segment['duration_seconds']}秒，以下各镜头属于同一条广告，不是独立短片。",
        ])
    lines.extend(_director_lines(director, production))
    lines.extend(_product_lines(director, production))
    lines.extend(_voice_lines(production, director, segment["speech_spans"], characters,
                              ad_form=segment.get("ad_form")))
    from .shot_craft import shot_prompt_lines
    lines.append("画面镜头（切镜不打断连续表达）：")
    closes_film = segment.get("closes_film", True) is not False
    last_index = len(shots) - 1
    first_speaking_shot = None
    spans = segment.get("speech_spans") or []
    if production and production.get("speech_mode") == "character_dialogue" and spans:
        for candidate in shots:
            if _speech_overlaps(candidate, spans[0]):
                first_speaking_shot = candidate["id"]
                break
    for index, shot in enumerate(shots):
        last = index == last_index
        lines.extend(shot_prompt_lines(
            shot, hold_tail=last and closes_film, continue_tail=last and not closes_film))
        if shot.get("character_ids"):
            if not set(shot["character_ids"]).issubset(characters):
                raise ValueError("镜头引用了未绑定参考图的角色")
            lines.append("  出镜角色：" + "、".join(characters[cid] for cid in shot["character_ids"]))
        if production and production.get("speech_mode") == "character_dialogue" and spans:
            speaker_id = production.get("speaker_id")
            speaking_here = any(_speech_overlaps(shot, span) for span in spans)
            name = characters.get(speaker_id, "")
            face_visible = _speaker_face_visible(shot)
            if speaking_here and speaker_id in shot.get("character_ids", []) and face_visible:
                lines.append(f"  本镜{name}面向镜头说话，嘴型与台词同步。")
                if shot.get("id") == first_speaking_shot:
                    lines.append("  从本镜第一帧开口说话，不先闭嘴停顿。")
            elif speaking_here and speaker_id in shot.get("character_ids", []) and not face_visible:
                lines.append("  本镜看不见说话人的嘴，台词由上述同一人物在画外接着说，不要求嘴型。")
            elif (speaking_here and production.get("speech_delivery") == "on_and_off_screen"
                    and speaker_id not in shot.get("character_ids", [])):
                lines.append("  本镜说话角色不入画，台词时窗内由上述同一人物画外延续，不补人物或嘴部。")
        partial = [person["id"] for person in (production or {}).get("characters", [])
                   if person.get("appearance") == "partial_only" and person["id"] in shot.get("character_ids", [])]
        if partial:
            lines.append("  仅局部出镜、不增加露脸：" + "、".join(characters[cid] for cid in partial)
                         + "；依实际镜头拍摄其手、头发或其他已确认局部，不把身份参考的组合版式当成本镜构图。")
        continuity = shot.get("continuity")
        speaking_face = (production and production.get("speech_mode") == "character_dialogue" and spans
                         and any(_speech_overlaps(shot, span) for span in spans)
                         and production.get("speaker_id") in shot.get("character_ids", [])
                         and _speaker_face_visible(shot))
        if speaking_face and isinstance(continuity, dict):
            continuity = {**continuity, "start_state": _mouth_open_state(continuity.get("start_state"))}
        lines.extend(_continuity_lines(continuity, shot["description"]))
        hand_line = _hand_product_line(shot)
        if hand_line:
            lines.append(hand_line)
    lines.append("台词时间（每句仅说一次，同一表达可以连续跨越关联镜头）：")
    for span in segment["speech_spans"]:
        lines.append(f"{span['start']:g}–{span['end']:g}秒 | 关联镜头 {', '.join(span['shot_ids'])} | "
                     + json.dumps(span["text"], ensure_ascii=False))
        cursor = 0
        anchors = []
        for anchor in span["anchors"]:
            end = cursor + len(anchor["text"])
            anchors.append(f"第{cursor + 1}–{end}字符 {anchor['start']:g}–{anchor['end']:g}秒→{', '.join(anchor['shot_ids'])}")
            cursor = end
        redundant = (len(span["anchors"]) == 1 and span["anchors"][0]["start"] == span["start"]
                     and span["anchors"][0]["end"] == span["end"]
                     and span["anchors"][0]["shot_ids"] == span["shot_ids"])
        if anchors and not redundant:
            lines.append("  句内节奏（含标点，指向上方原句，不是另一遍台词）：" + "；".join(anchors))
    if not segment["speech_spans"]:
        lines.append("本段无台词，只保留已安排的自然声音和背景音乐，不新增台词。")
    duration = segment["duration_seconds"]
    if duration < 10 and not continuous:
        lines.append(f"前{duration}秒完成动作和所有旁白，此后到10秒保持结尾画面，不新增信息；成片只使用前{duration}秒，不截断发音。")
    return "\n".join(lines)


def expected_grouped_speech(segment, offset_frames, fps=24):
    """Export planned speech windows independently from the picture cuts."""
    shots = {shot["id"]: shot for shot in segment["shots"]}
    return [{
        "start_frame": offset_frames + round(span["start"] * fps),
        "end_frame": offset_frames + round(span["end"] * fps),
        "segment_id": segment["id"], "speech_span_id": span["id"],
        "shot_ids": span["shot_ids"], "narration": span["text"],
        "visual_description": "；".join(shots[shot_id]["description"] for shot_id in span["shot_ids"]),
        "anchors": [{"text": anchor["text"],
                     "start_frame": offset_frames + round(anchor["start"] * fps),
                     "end_frame": offset_frames + round(anchor["end"] * fps),
                     "shot_ids": anchor["shot_ids"], "visual_evidence": anchor["visual_evidence"]}
                    for anchor in span["anchors"]],
    } for span in segment["speech_spans"]]
