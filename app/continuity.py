"""Repackage an approved short film without rewriting its content or timings.

This migration is deliberately narrow. It preserves the absolute storyboard and
speech timeline and creates a new plan version; it never changes old receipts.
"""
from copy import deepcopy

from .schemas import Plan


def upgrade_continuity_plan(plan):
    original = Plan.model_validate(deepcopy(plan)).model_dump()
    if original.get("generation_strategy", "legacy_10s") != "legacy_10s":
        raise ValueError("方案已使用连续性生成，无需重复升级")
    total = sum(segment["duration_seconds"] for segment in original["segments"])
    if not 10 <= total <= 30 or original["audio_strategy"] != "native":
        raise ValueError("此升级仅适用于10至30秒的已确认原声广告，长视频请按新版流程重新策划")
    if any(segment.get("speech_spans") is None for segment in original["segments"]):
        raise ValueError("此方案没有独立旁白时段，请重新策划后使用连续性生成")
    # Different reference sets may intentionally depict different products or
    # scenes. Combining those requests needs creative planning, not migration.
    references = original["segments"][0]["reference_asset_ids"]
    if any(set(s["reference_asset_ids"]) != set(references) for s in original["segments"]):
        raise ValueError("各段参考图片不同，请先重新策划连续性关系")
    shots, speech, cursor = [], [], 0
    for segment in original["segments"]:
        names = {}
        for shot in segment["shots"]:
            updated = deepcopy(shot)
            updated["id"] = f"shot_{len(shots) + 1}"
            names[shot["id"]] = updated["id"]
            updated["start"], updated["end"] = round(shot["start"] + cursor, 6), round(shot["end"] + cursor, 6)
            shots.append(updated)
        for span in segment["speech_spans"]:
            updated = deepcopy(span)
            updated["id"] = f"speech_{len(speech) + 1}"
            updated["start"], updated["end"] = round(span["start"] + cursor, 6), round(span["end"] + cursor, 6)
            updated["shot_ids"] = [names[sid] for sid in span["shot_ids"]]
            for anchor in updated["anchors"]:
                anchor["start"], anchor["end"] = round(anchor["start"] + cursor, 6), round(anchor["end"] + cursor, 6)
                anchor["shot_ids"] = [names[sid] for sid in anchor["shot_ids"]]
            speech.append(updated)
        cursor += segment["duration_seconds"]
    prompts = list(dict.fromkeys(s["prompt"] for s in original["segments"]))
    merged = {"id": "continuous_1", "duration_seconds": total,
              "purpose": "连续呈现已确认的广告内容，保持商品、场景与原生声音一致",
              "prompt": "\n".join(prompts), "narration": "".join(s["text"] for s in speech),
              "shots": shots, "speech_spans": speech, "reference_asset_ids": deepcopy(references)}
    result = {**original, "generation_strategy": "continuity_v1", "segments": [merged]}
    return Plan.model_validate(result).model_dump()
