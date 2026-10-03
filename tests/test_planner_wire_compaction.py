"""Payload compaction must retain authored evidence and durable replay identity."""
import asyncio
from copy import deepcopy

import pytest

from app.planner_runtime import (
    PLANNER_MODEL_CONTEXT_VERSION,
    planner_model_context,
    planner_wire_context,
)
from app.planning_session import PlanningSession, PlanningSessionError, fingerprint
from app.runtime_skills import stage_skill_context, skill_trace


def test_method_text_and_locked_requirements_survive_without_mutating_provenance():
    bundle = stage_skill_context("ad_script")
    context = {
        "stage": "ad_script", "stage_skills": bundle,
        "merchant_request": "固定台词：这句请完整保留。", "fixed_speech": ["这句请完整保留。"],
        "conversation": [{"role": "user", "content": "只展示原图中确认的外观。"}],
        "facts": [{"id": "f1", "text": "银色机身", "source_id": "asset1"}],
    }
    before = deepcopy(context)
    trace = skill_trace(bundle)
    result = planner_model_context(context)
    assert context == before and skill_trace(bundle) == trace
    for collection in ("skills", "shared_references"):
        assert [(item["id"], item["content"]) for item in result["stage_skills"][collection]] == [
            (item["id"], item["content"]) for item in bundle[collection]]
    assert "resources" not in result["stage_skills"]["skills"][0]
    for key in ("merchant_request", "fixed_speech", "conversation", "facts"):
        assert result[key] == before[key]


def test_provenance_change_cannot_replay_an_equal_compacted_payload():
    context = {"stage": "ad_script", "stage_skills": stage_skill_context("ad_script")}
    changed = deepcopy(context)
    changed["stage_skills"]["skills"][0]["version"] = "999.0.0"
    assert planner_model_context(context) == planner_model_context(changed)

    def signature(value):
        return {"context": planner_wire_context(value), "wire_context_version": PLANNER_MODEL_CONTEXT_VERSION}

    assert fingerprint(signature(context)) != fingerprint(signature(changed))
    session = PlanningSession(None, None, {"request": "unchanged"})
    calls = []

    async def perform():
        calls.append(True)
        return {"draft": "existing result"}

    asyncio.run(session.call("ad_script", signature(context), perform))
    resumed = PlanningSession(session.state, None, {"request": "unchanged"})
    with pytest.raises(PlanningSessionError, match="规则已变化"):
        asyncio.run(resumed.call("ad_script", signature(changed), perform))
    assert len(calls) == 1


@pytest.mark.parametrize("stage", ["ad_script", "storyboard", "plan_refinement"])
def test_content_only_authoring_keeps_facts_and_user_intent_without_old_author_assumptions(stage):
    context = {"stage": stage, "draft_format": "content_only",
        "brief": {"facts": [{"id": "claim", "text": "可折叠", "source_kind": "merchant"}],
            "assumptions": ["只介绍外观"], "unknowns": ["折后尺寸未知"],
            "creative_intent": {"reference_intent": "展示折叠用法"}},
        "selected_direction": {"id": "chosen", "production": {"speech_mode": "voiceover"},
            "product_expression": "实际折叠动作与便携说明"},
        "fixed_speech": ["折起来再出发。"],
        "conversation": [{"role": "user", "content": "保持可折叠介绍和这句台词"}]}
    before = deepcopy(context)
    result = planner_model_context(context)
    expected = deepcopy(before)
    expected["brief"].pop("assumptions")
    assert result == expected and context == before
    assert planner_wire_context(context)["brief"]["assumptions"] == ["只介绍外观"]


@pytest.mark.parametrize("condition", ["legacy", "joint", "review", "no_production"])
def test_unselected_or_non_authoring_paths_keep_existing_assumptions(condition):
    context = {"stage": "storyboard", "draft_format": "content_only",
        "brief": {"assumptions": ["作者原稿"]},
        "selected_direction": {"production": {"speech_mode": "voiceover"}}}
    if condition in {"legacy", "joint"}:
        context.pop("draft_format")
    elif condition == "review":
        context["stage"] = "plan_review"
    else:
        context["selected_direction"].pop("production")
    assert planner_model_context(context)["brief"]["assumptions"] == ["作者原稿"]


def test_schema_compaction_preserves_business_titles_and_literal_constraints():
    product = {"title": "商品名不可改", "state": "未开启"}
    schema = {
        "title": "Payload", "type": "object", "required": ["title", "locked"],
        "properties": {
            "title": {"title": "Product Title", "type": "string", "const": "商品名不可改"},
            "locked": {"title": "Locked Value", "const": product, "examples": [product], "default": product},
        },
        "$defs": {"title": {"title": "Reusable Title", "type": "string", "minLength": 1}},
        "allOf": [{"title": "Keep Limits", "properties": {"title": {"$ref": "#/$defs/title"}}}],
        "if": {"title": "Condition", "properties": {"locked": {"const": product}}},
        "then": {"title": "Consequence", "additionalProperties": False},
        "enum": [product],
    }
    context = {"stage": "storyboard", "schema": schema, "title": "真实片名", "facts": [product]}
    before = deepcopy(context)
    result = planner_model_context(context)
    assert context == before and result["title"] == "真实片名" and result["facts"] == [product]
    reduced = result["schema"]
    assert "title" not in reduced
    assert reduced["properties"]["title"] == {"type": "string", "const": "商品名不可改"}
    assert reduced["required"] == ["title", "locked"]
    for key in ("const", "examples", "default"):
        assert reduced["properties"]["locked"][key] == schema["properties"]["locked"][key]
    assert reduced["enum"] == [product]
    assert reduced["$defs"]["title"] == {"type": "string", "minLength": 1}
    assert reduced["allOf"][0]["properties"]["title"]["$ref"] == "#/$defs/title"
    assert reduced["if"]["properties"]["locked"]["const"] == product
    assert reduced["then"] == {"additionalProperties": False}


def _review_context():
    span = {"id": "speech1", "beat_id": "beat1", "text": "先看外观。", "start": 0.0, "end": 3.0,
            "shot_ids": ["shot1"], "speaker_id": ""}
    span["anchors"] = [{key: deepcopy(span[key]) for key in ("text", "start", "end", "shot_ids")}]
    plan = {
        "title": "产品展示", "script": span["text"],
        "facts": {"claims": ["银色机身"]}, "production": {"speech_mode": "voiceover"},
        "ad_script": {"beats": [{"id": "beat1", "narration": span["text"], "evidence_ids": ["f1"],
            "timing": {"action_plan": "商品保持静止", "time_transition": "continuous"}}]},
        "segments": [{"id": "seg1", "narration": span["text"], "speech_spans": [span],
            "shots": [{"id": "shot1", "description": "从侧面观察静止商品", "evidence_ids": ["f1"],
                "continuity": {"start_state": "静止", "end_state": "静止", "entry_transition": "cut"}}]}],
    }
    return {"stage": "plan_review", "final_plan": plan, "fixed_speech": [span["text"]],
        "transition_boundaries": [{"from": "shot0", "to": "shot1", "entry_transition": "cut"}]}


def test_exact_review_duplicates_can_be_rebuilt_without_changing_actions_or_speech():
    context = _review_context()
    before = deepcopy(context)
    result = planner_model_context(context)
    reduced = result["final_plan"]
    assert "script" not in reduced and "narration" not in reduced["segments"][0]
    span = reduced["segments"][0]["speech_spans"][0]
    assert "anchors" not in span
    # Rebuild only mechanical copies; all authored states, timing, sources and
    # fixed lines must then match the complete review input byte-for-byte.
    reduced["script"] = "".join(beat["narration"] for beat in reduced["ad_script"]["beats"])
    reduced["segments"][0]["narration"] = "".join(item["text"] for item in reduced["segments"][0]["speech_spans"])
    span["anchors"] = [{key: deepcopy(span[key]) for key in ("text", "start", "end", "shot_ids")}]
    assert result == before and context == before


@pytest.mark.parametrize("kind", ["split_phrase", "different_window", "extra_evidence", "different_summary"])
def test_distinct_review_content_is_never_treated_as_a_duplicate(kind):
    context = _review_context()
    plan = context["final_plan"]
    span = plan["segments"][0]["speech_spans"][0]
    if kind == "split_phrase":
        span["anchors"] = [{"text": "先看", "start": 0.0, "end": 1.0, "shot_ids": ["shot1"]},
                           {"text": "外观。", "start": 1.0, "end": 3.0, "shot_ids": ["shot2"]}]
    elif kind == "different_window":
        span["anchors"][0]["end"] = 2.0
    elif kind == "extra_evidence":
        span["anchors"][0]["visual_evidence"] = "本短语只在镜头后半出现商品"
    else:
        plan["script"] = "与实际台词冲突的整片文字"
        plan["segments"][0]["narration"] = "与实际台词冲突的分段文字"
    reduced = planner_model_context(context)["final_plan"]
    if kind == "different_summary":
        assert reduced["script"] == plan["script"]
        assert reduced["segments"][0]["narration"] == plan["segments"][0]["narration"]
    else:
        assert reduced["segments"][0]["speech_spans"][0]["anchors"] == span["anchors"]
