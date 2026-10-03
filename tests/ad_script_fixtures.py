"""Explicit offline ad-script outputs; these are fixtures, not production fallbacks."""
from copy import deepcopy

CRITERIA = ["product_relevance", "audience_relevance", "factual_support", "audiovisual_complement", "natural_speech", "narrative_progression"]


def compile_script_plan(script, plan):
    """Compile offline one-shot groups from the same reservations as the director."""
    from app.ad_script import AdScript
    from app.planning import expression_schedule
    from app.schemas import Plan
    compiled = deepcopy(plan)
    beats = {beat["id"]: beat for beat in script["beats"]}
    schedule = expression_schedule(AdScript.model_validate(script), [s["duration_seconds"] for s in compiled["segments"]])
    for segment, reserved in zip(compiled["segments"], schedule):
        shots, spans = [], []
        for index, group in enumerate(reserved["groups"]):
            beat = beats[group["beat_id"]]
            shot_id = f"{segment['id']}_shot_{index + 1}"
            shots.append({"id": shot_id, "beat_id": beat["id"], "start": group["start"], "end": group["end"],
                          "description": group["visual_role"], "purpose": beat["purpose"],
                          "evidence_ids": list(beat["evidence_ids"]), "narration": ""})
            if group["text"]:
                timing = {"start": group["speech_start"], "end": group["speech_end"]}
                spans.append({"id": f"{segment['id']}_speech_{index + 1}", "beat_id": beat["id"],
                              "text": group["text"], **timing, "shot_ids": [shot_id],
                              "anchors": [{"text": group["text"], **timing, "shot_ids": [shot_id],
                                           "visual_evidence": group["visual_role"]}]})
        segment.update(shots=shots, speech_spans=spans, narration="".join(span["text"] for span in spans))
    compiled["script"] = "".join(segment["narration"] for segment in compiled["segments"])
    return Plan.model_validate(compiled).model_dump()


def script_stages(strategy, plan):
    from app.ad_script import load_ad_expression_skill
    compiled = deepcopy(plan)
    selected = next(d for d in strategy["directions"] if d["id"] == strategy["selected_direction_id"])
    beats = []
    for segment in compiled["segments"]:
        for shot in segment["shots"]:
            beat_id = f"beat_{len(beats) + 1}"
            shot["beat_id"] = beat_id
            beats.append({"id": beat_id, "purpose": shot.get("purpose") or "建立商品认知",
                          "message": "让顾客了解这件商品适合的日常情境", "visual_role": shot["description"],
                          "narration": shot.get("narration", ""), "evidence_ids": list(shot.get("evidence_ids") or selected["evidence_ids"])})
    script = {"core_message": selected["angle"], "audience_takeaway": "这件商品能成为日常选择之一",
              "product_role": "商品是广告内容的主体", "evidence_ids": list(dict.fromkeys(i for b in beats for i in b["evidence_ids"])),
              "beats": beats, "skill_version": load_ad_expression_skill()["version"]}
    review = {"script": deepcopy(script), "ready": True,
              "checks": [{"criterion": c, "passed": True, "note": "离线审核夹具通过"} for c in CRITERIA]}
    return script, review, compile_script_plan(script, compiled)


def stage_outputs(strategy, plan):
    script, review, compiled = script_stages(strategy, plan)
    return {"strategy": strategy, "ad_script": script, "script_review": review, "storyboard": compiled,
            "alignment_review": alignment_review(compiled)}


def alignment_review(plan):
    """Explicit accepted verdicts for offline transport tests, never production."""
    return {"ready": True, "ad_expression_passed": True, "summary": "离线夹具的商品表达与时段对应通过",
            "spans": [{"segment_id": segment["id"], "span_id": span["id"], "passed": True,
                       "note": "离线夹具对应画面与完整旁白"}
                      for segment in plan["segments"] for span in segment["speech_spans"]]}


def assert_script_saved(plan, script, review):
    assert plan["ad_script"] == script
    assert plan["script_review"] == review
    assert plan["script"] == "".join(b["narration"] for b in script["beats"])
    shot_beats = [s["beat_id"] for seg in plan["segments"] for s in seg["shots"]]
    assert [beat for index, beat in enumerate(shot_beats) if not index or beat != shot_beats[index - 1]] == [b["id"] for b in script["beats"]]
    assert all(not shot["narration"] and shot["id"] for seg in plan["segments"] for shot in seg["shots"])
    assert [span["text"] for seg in plan["segments"] for span in seg["speech_spans"]] == [beat["narration"] for beat in script["beats"] if beat["narration"]]


def with_action_timing(script):
    """Explicit simple-product fixture for the new contract; no runtime fallback."""
    result = deepcopy(script)
    for beat in result["beats"]:
        beat["timing"] = {"visual_seconds": 1, "speech_overlap": True,
            "action_plan": "静置商品和自然说话可并行，保留辨认停留。",
            "start_state": "同一商品静置", "end_state": "同一商品静置",
            "time_transition": "continuous"}
    return result


def execution_check():
    return {"action_capacity": True, "state_continuity": True,
            "note": "明确的离线静置商品夹具，动作容量和前后状态可执行。"}
