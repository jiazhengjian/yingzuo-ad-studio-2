"""Full five-stage planner transport tests; no credentials or media calls."""
import asyncio
from copy import deepcopy

import pytest

from app.providers import ProviderError
from app.providers.gateway import Gateway
from ad_script_fixtures import script_stages, alignment_review
from test_providers import settings, source, plan, strategy_stage, install_transport, chat_output, request_context


def compiled_from_reservations(context):
    """An explicit offline director consumes the exact wire reservations."""
    approved = {beat["id"]: beat for beat in context["approved_ad_script"]["beats"]}
    segments = []
    for index, schedule in enumerate(context["expression_schedule"]):
        shots, spans = [], []
        for number, group in enumerate(schedule["groups"]):
            beat = approved[group["beat_id"]]
            # The first expression has a real cut inside the same speech.
            start, end = group["start"], group["end"]
            cuts = [start, end]
            if number == 0 and group["text"]:
                cuts.insert(1, round((group["speech_start"] + group["speech_end"]) / 2, 1))
            ids = []
            for part in range(len(cuts) - 1):
                shot_id = f"s{index}_b{number}_shot{part}"
                ids.append(shot_id)
                shots.append({"id": shot_id, "beat_id": beat["id"], "start": cuts[part], "end": cuts[part + 1],
                              "description": group["visual_role"] if len(cuts) == 2 else f"{group['visual_role']}，镜头{part + 1}",
                              "purpose": beat["purpose"],
                              "evidence_ids": beat["evidence_ids"], "narration": ""})
            if group["text"]:
                timing = {"start": group["speech_start"], "end": group["speech_end"]}
                spans.append({"id": f"s{index}_speech{number}", "beat_id": beat["id"], "text": group["text"],
                              **timing, "shot_ids": ids,
                              "anchors": [{"text": group["text"], **timing, "shot_ids": ids,
                                           "visual_evidence": group["visual_role"]}]})
        segments.append({"id": f"s{index}", "duration_seconds": schedule["duration_seconds"],
                         "purpose": "蓝瓶外观与日常场景", "prompt": "同一蓝瓶和展示台面",
                         "reference_asset_ids": [context["assets"][0]["id"]], "shots": shots, "speech_spans": spans})
    result = {"title": "蓝瓶外观广告", "language": "zh-CN", "aspect_ratio": "9:16", "audio_strategy": "native", "segments": segments}
    if context.get("directing_contract_required"):
        result["visual_direction"] = {
            "version": "1.0.0", "product_anchor_ids": [context["creative_strategy"]["brief"]["facts"][0]["id"]],
            "scene": "办公桌与远处的办公椅", "lighting": "左侧柔和窗光", "color_palette": "灰色桌面保留蓝瓶本色",
            "camera_style": "中景建立场景，近景看清瓶身", "sound": "自然普通话，轻声介绍，环境声较低"}
        for index, shot in enumerate(shot for segment in segments for shot in segment["shots"]):
            shot["continuity"] = {"start_state": "蓝瓶完整直立在同一台面，瓶盖关闭",
                "allowed_changes": "主体保持静止，不改变颜色或形状", "end_state": "蓝瓶完整直立在同一台面，瓶盖关闭",
                "camera_move": "镜头缓慢靠近瓶身", "entry_transition": "opening" if index == 0 else "continuous",
                "entry_reason": "建立商品与台面的配色关系" if index == 0 else "看清商品或完成展示收束"}
    return result


def directed_review(compiled):
    return {**alignment_review(compiled), "shots": [
        {"segment_id": segment["id"], "shot_id": shot["id"], "passed": True,
         "note": "商品保持摆位，承担环境、细节或收束作用"}
        for segment in compiled["segments"] for shot in segment["shots"]]}


@pytest.mark.parametrize("duration", [20, 40])
def test_new_five_stage_gateway_uses_real_semantic_grouping_not_ten_second_slots(
        settings, source, plan, strategy_stage, install_transport, duration):
    settings.planning_generation_strategy = "continuity_v1"
    script, review, _ = script_stages(strategy_stage, plan)
    script["beats"][1]["boundary_before"] = "scene_change"
    script["beats"][2]["boundary_before"] = "continuous"
    review["script"] = deepcopy(script)
    observed, compiled = [], None

    def handle(request):
        nonlocal compiled
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        _, context = request_context(request)
        stage = context["stage"]
        observed.append(stage)
        if stage == "strategy":
            assert "segment_durations" not in context
            assert context["generation_constraints"]["max_seconds"] == 30
            return chat_output(strategy_stage)
        if stage == "ad_script":
            assert context["generation_strategy"] == "continuity_v1"
            assert context["runtime_skill"]["version"] == "1.3.0"
            return chat_output(script)
        if stage == "script_review":
            assert context["schedule_feasible"] is True
            assert context["draft_generation_groups"]["voice_continuity"].endswith("unverified")
            return chat_output(review)
        if stage == "storyboard":
            grouping = context["generation_grouping"]
            if duration == 20:
                assert context["segment_durations"] == [20]
                assert grouping["beat_groups"] == [[0, 1, 2, 3]]
            else:
                assert grouping["beat_groups"] == [[0], [1, 2, 3]]
                assert grouping["split_reasons"] == ["scene_change"]
                assert context["segment_durations"] != [10, 10, 10, 10]
            assert [segment["duration_seconds"] for segment in context["expression_schedule"]] == grouping["durations"]
            assert [[g["beat_id"] for g in s["groups"]] for s in context["expression_schedule"]] == [
                [script["beats"][i]["id"] for i in group] for group in grouping["beat_groups"]]
            compiled = compiled_from_reservations(context)
            return chat_output(compiled)
        assert stage == "alignment_review"
        assert context["segments"][0]["speech_spans"][0]["shot_ids"] == ["s0_b0_shot0", "s0_b0_shot1"]
        assert context["visual_direction"]["product_anchors"][0]["text"] == strategy_stage["brief"]["facts"][0]["text"]
        assert all("continuity" in shot for segment in context["segments"] for shot in segment["shots"])
        assert all("purpose" not in shot for segment in context["segments"] for shot in segment["shots"])
        return chat_output(directed_review(compiled))

    install_transport(handle)
    result = asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品", "duration_seconds": duration}, [source], []))
    assert observed == ["strategy", "ad_script", "script_review", "storyboard", "alignment_review"]
    assert result["generation_strategy"] == "continuity_v1"
    assert result["ad_script"] == script and result["script_review"] == review
    assert sum(segment["duration_seconds"] for segment in result["segments"]) == duration
    assert len(result["segments"]) == (1 if duration == 20 else 2)
    assert len(result["segments"][0]["shots"]) > 1
    assert result["script"] == "".join(beat["narration"] for beat in script["beats"])
    assert result["visual_direction"] == compiled["visual_direction"]
    assert len(result["alignment_review"]["shots"]) == sum(len(segment["shots"]) for segment in result["segments"])


@pytest.mark.parametrize("invalid", ["missing_direction", "orphan_continuity", "missing_shot_review", "failed_shot"])
def test_new_planning_stops_on_incomplete_or_failed_director_review(
        settings, source, plan, strategy_stage, install_transport, invalid):
    settings.planning_generation_strategy = "continuity_v1"
    script, review, _ = script_stages(strategy_stage, plan)
    observed, compiled = [], None
    def handle(request):
        nonlocal compiled
        _, context = request_context(request)
        stage = context["stage"]
        observed.append(stage)
        if stage == "strategy": return chat_output(strategy_stage)
        if stage == "ad_script": return chat_output(script)
        if stage == "script_review": return chat_output(review)
        if stage == "storyboard":
            compiled = compiled_from_reservations(context)
            if invalid in ("missing_direction", "orphan_continuity"):
                compiled.pop("visual_direction")
                if invalid == "missing_direction":
                    for segment in compiled["segments"]:
                        for shot in segment["shots"]: shot.pop("continuity")
            return chat_output(compiled)
        assert stage == "alignment_review"
        result = directed_review(compiled)
        if invalid == "missing_shot_review": result["shots"].pop()
        else:
            result["ready"] = False
            result["shots"][-1].update(passed=False, note="结尾重复，未增加信息也没有收束作用")
        return chat_output(result)
    install_transport(handle)
    with pytest.raises(ProviderError):
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品", "duration_seconds": 20}, [source], []))
    assert observed == ["strategy", "ad_script", "script_review", "storyboard"] + (
        ["alignment_review"] if invalid in ("missing_shot_review", "failed_shot") else [])


def test_gateway_blocks_long_script_with_no_safe_cut_before_director_or_media(
        settings, source, plan, strategy_stage, install_transport):
    settings.planning_generation_strategy = "continuity_v1"
    script, review, _ = script_stages(strategy_stage, plan)
    for beat in script["beats"]:
        beat["continuity_group"] = "unfinished_pour"
    review["script"] = deepcopy(script)
    observed = []
    def handle(request):
        _, context = request_context(request)
        observed.append(context["stage"])
        if context["stage"] == "strategy": return chat_output(strategy_stage)
        if context["stage"] == "ad_script": return chat_output(script)
        assert context["stage"] == "script_review"
        assert context["schedule_feasible"] is False
        return chat_output(review)
    install_transport(handle)
    with pytest.raises(ProviderError):
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品", "duration_seconds": 40}, [source], []))
    assert observed == ["strategy", "ad_script", "script_review"]
