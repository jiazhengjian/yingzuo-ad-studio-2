"""Compile approved picture and speech plans into one native video request.

Times are desired directing windows, never a claim of measured alignment.
"""
import json
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
            duty = (f"角色“{character['name']}”的四视图，{sheet}"
                    f"{cast_appearance_clause(character.get('description', ''))}{character['description']}")
        else:
            raise ValueError("不支持的视频参考图片职责")
        lines.append(f"参考图{index}：{duty}。")
    if referenced_characters != set(characters):
        raise ValueError("视频缺少已确认角色的四视图参考")
    return lines


def _character_labels(segment):
    indexes = {role["character_id"]: role["index"] for role in (segment.get("reference_roles") or [])
               if role.get("role") == "character"}
    return {person["id"]: f"{person['name']}（参考图{indexes[person['id']]}）"
            for person in (segment.get("production") or {}).get("characters", [])}


def _voice_lines(production, director, spans, characters):
    if not production:
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
        return [f"声音方式：画外音；{voice}。画面中的角色不对口型。"]
    if mode == "character_dialogue":
        speaker = next((item for item in production["characters"] if item["id"] == production["speaker_id"]), None)
        if speaker is None:
            raise ValueError("人物对白缺少已确认的说话角色")
        if production.get("speech_delivery") == "on_and_off_screen":
            return [f"声音方式：同一人物“{characters[speaker['id']]}”说出全部台词，其余角色不说台词。声音：{voice}。",
                "说话人可见开口时配合其口型；切到商品或仅有手、头发等局部且嘴部不可见的画面时，由同一人物画外延续，不能换成另一位旁白，也不为对口型增加露脸画面。声线和说话方式保持连贯，这是执行要求，不是已验证的声纹。"]
        return [f"声音方式：人物对白，由“{characters[speaker['id']]}”说出下方台词，与其画面口型配合，其余角色不说台词。声音：{voice}。"]
    raise ValueError("不支持的声音方式")


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
    lines.extend(_voice_lines(production, director, segment["speech_spans"], characters))
    from .shot_craft import shot_prompt_lines
    lines.append("画面镜头（切镜不打断连续表达）：")
    closes_film = segment.get("closes_film", True) is not False
    last_index = len(shots) - 1
    for index, shot in enumerate(shots):
        last = index == last_index
        lines.extend(shot_prompt_lines(
            shot, hold_tail=last and closes_film, continue_tail=last and not closes_film))
        if shot.get("character_ids"):
            if not set(shot["character_ids"]).issubset(characters):
                raise ValueError("镜头引用了未绑定参考图的角色")
            lines.append("  出镜角色：" + "、".join(characters[cid] for cid in shot["character_ids"]))
        if production and production.get("speech_delivery") == "on_and_off_screen":
            speaking_here = any(min(shot["end"], span["end"]) - max(shot["start"], span["start"]) > 1e-7
                                for span in segment["speech_spans"])
            if speaking_here and production["speaker_id"] not in shot.get("character_ids", []):
                lines.append("  本镜说话角色不入画，台词时窗内由上述同一人物画外延续，不补人物或嘴部。")
        partial = [person["id"] for person in (production or {}).get("characters", [])
                   if person.get("appearance") == "partial_only" and person["id"] in shot.get("character_ids", [])]
        if partial:
            lines.append("  仅局部出镜、不增加露脸：" + "、".join(characters[cid] for cid in partial)
                         + "；依实际镜头拍摄其手、头发或其他已确认局部，不把身份参考的组合版式当成本镜构图。")
        lines.extend(_continuity_lines(shot.get("continuity"), shot["description"]))
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
