"""Stage request profiles and content-free planner performance diagnostics.

These legacy-compatible profiles use DeepSeek's thinking switch and token key.
planner_transport converts them to the selected provider's wire protocol;
Tudou GPT uses stage-scoped effort and max_completion_tokens without thinking.
Output limits remain generous: lowering a JSON output cap
is not a latency optimization and can prevent a complete result from returning.
Provider references (checked 2026-10-02):
https://api-docs.deepseek.com/guides/thinking_mode/
https://api-docs.deepseek.com/guides/json_mode/
https://api-docs.deepseek.com/api/create-chat-completion/
"""
from __future__ import annotations

import math
from copy import deepcopy

from app.ad_script import script_request_options


PLANNER_STAGES = frozenset({
    "strategy", "direction_review", "ad_script", "script_review", "storyboard",
    "storyboard_repair", "alignment_review", "plan_review", "plan_refinement",
})


# This is part of request identity, not a model instruction. Keep the original
# planner_wire_context (including provenance) in the checkpoint signature.
PLANNER_MODEL_CONTEXT_VERSION = 1


def _schema_without_titles(schema):
    """Remove display annotations only from schema nodes, never JSON values.

    In particular, a property named ``title`` and ``const``/``enum``/``examples``
    containing product titles remain intact. Walking arbitrary dictionaries
    would silently change those business values.
    """
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    if isinstance(result.get("title"), str):
        result.pop("title")
    for keyword in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        children = result.get(keyword)
        if isinstance(children, dict):
            result[keyword] = {key: _schema_without_titles(value) for key, value in children.items()}
    for keyword in ("additionalProperties", "unevaluatedProperties", "propertyNames",
                    "additionalItems", "unevaluatedItems", "contains", "not", "if", "then", "else"):
        if keyword in result:
            result[keyword] = _schema_without_titles(result[keyword])
    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(result.get(keyword), list):
            result[keyword] = [_schema_without_titles(value) for value in result[keyword]]
    if isinstance(result.get("items"), list):
        result["items"] = [_schema_without_titles(value) for value in result["items"]]
    elif "items" in result:
        result["items"] = _schema_without_titles(result["items"])
    # Draft-07 dependencies mix schema values with literal property-name lists.
    if isinstance(result.get("dependencies"), dict):
        result["dependencies"] = {key: _schema_without_titles(value)
                                  for key, value in result["dependencies"].items()}
    return result


def _compact_skill_provenance(bundle):
    """Keep all method text and unknown fields; omit known server trace fields."""
    if not isinstance(bundle, dict):
        return
    for key in ("schema_version", "content_sha256"):
        bundle.pop(key, None)
    metadata = ("display_name", "version", "mode", "runtime_status", "resources",
                "content_sha256", "source_content_sha256", "loaded_sections",
                "consumed_resources", "execution_kind", "content_hash_scope")
    for collection in ("skills", "shared_references"):
        items = bundle.get(collection)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            for key in metadata:
                item.pop(key, None)
            if collection == "shared_references":
                item.pop("path", None)


def _compact_review_summaries(plan):
    """Omit only exact, reconstructible text/anchor duplication in a review."""
    if not isinstance(plan, dict):
        return
    script = plan.get("ad_script")
    beats = script.get("beats") if isinstance(script, dict) else None
    if (isinstance(beats, list) and beats
            and all(isinstance(beat, dict) and isinstance(beat.get("narration"), str) for beat in beats)
            and plan.get("script") == "".join(beat["narration"] for beat in beats)):
        plan.pop("script", None)
    segments = plan.get("segments")
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        spans = segment.get("speech_spans")
        if not isinstance(spans, list):
            continue
        if (spans and all(isinstance(span, dict) and isinstance(span.get("text"), str) for span in spans)
                and segment.get("narration") == "".join(span["text"] for span in spans)):
            segment.pop("narration", None)
        for span in spans:
            if not isinstance(span, dict):
                continue
            anchors = span.get("anchors")
            keys = {"text", "start", "end", "shot_ids"}
            if (isinstance(anchors, list) and len(anchors) == 1
                    and isinstance(anchors[0], dict) and set(anchors[0]) == keys
                    and keys.issubset(span)
                    and all(type(anchors[0][key]) is type(span[key]) and anchors[0][key] == span[key]
                            for key in keys)):
                span.pop("anchors")


def planner_model_context(context: dict) -> dict:
    """Project the actual model payload without altering checkpoint identity.

    Callers must continue signing planner_wire_context(context), plus
    PLANNER_MODEL_CONTEXT_VERSION. Provenance changes must invalidate replay
    even though those bookkeeping fields no longer consume model input.
    """
    result = planner_wire_context(context)
    if result.get("stage") == "ad_script" and result.get("draft_format") == "speech_only":
        # The signed source context retains the complete selected strategy.
        # This experiment renders its explicit copy brief once, without also
        # showing the older author summaries as a second writing assignment.
        result.pop("source_context", None)
    if (result.get("draft_format") == "content_only"
            and result.get("stage") in {"ad_script", "storyboard", "plan_refinement"}
            and isinstance(result.get("selected_direction"), dict)
            and isinstance(result["selected_direction"].get("production"), dict)):
        # Once a production direction has been selected, its superseded author
        # assumptions are not additional merchant constraints. Original user
        # messages, facts, unknowns, selected content and full checkpoint input
        # remain intact; neither history nor the stored brief is rewritten.
        if isinstance(result.get("brief"), dict):
            result["brief"].pop("assumptions", None)
    for key in ("stage_skills", "orchestrator_skills"):
        _compact_skill_provenance(result.get(key))
    if "schema" in result:
        result["schema"] = _schema_without_titles(result["schema"])
    if result.get("stage") == "plan_review":
        _compact_review_summaries(result.get("final_plan"))
    return result


def planner_wire_context(context: dict) -> dict:
    """Project a selected-direction revision without mutating saved artifacts.

    Only the new strategy request loses old audit records and unselected
    alternatives. Facts, the approved script, execution details and all original
    conversation/clarification text remain available to the strategy decision.
    """
    result = deepcopy(context)
    def direction_core(direction):
        # A selected story already carries its narrative and audience promise.
        # Repeating earlier execution drafts beside it anchors later revisions
        # to superseded actions. Historical directions without a story retain
        # their only available narrative fields.
        if isinstance(direction, dict) and direction.get("story_summary") and direction.get("attraction_mechanism"):
            for field in ("hook", "development", "payoff", "production_notes", "reason"):
                direction.pop(field, None)
        return direction

    if context.get("stage") in ("ad_script", "storyboard", "plan_refinement", "plan_review"):
        # Intent routing has already resolved the full dialogue into the brief
        # and selected story. Old assistant proposals are not extra merchant
        # requirements, especially when the user is correcting those proposals.
        if isinstance(result.get("conversation"), list):
            result["conversation"] = [message for message in result["conversation"]
                                      if message.get("role") == "user"]
        if isinstance(result.get("selected_direction"), dict):
            direction_core(result["selected_direction"])
        if context.get("stage") == "plan_refinement":
            # draft_storyboard is the current correction subject. The initial
            # content draft and its old capacity report are no longer inputs.
            for field in ("draft_script", "draft_speech", "draft_validation_issues", "draft_capacity", "draft_capacity_basis"):
                result.pop(field, None)
            if isinstance(result.get("final_plan"), dict) and isinstance(result.get("draft_storyboard"), dict):
                # The current draft above already contains all authored actions
                # and states. Supply its compiled clocks without a second full
                # copy of the story, claims and descriptions in another shape.
                result["final_execution"] = [{"id": segment["id"],
                    "duration_seconds": segment["duration_seconds"],
                    "shots": [{key: shot[key] for key in ("id", "start", "end", "beat_ids") if key in shot}
                              for shot in segment["shots"]],
                    "speech_spans": deepcopy(segment.get("speech_spans", []))}
                    for segment in result.pop("final_plan")["segments"]]
        plan = result.get("final_plan")
        if isinstance(plan, dict) and isinstance(plan.get("strategy"), dict):
            strategy = plan["strategy"]
            selected = [d for d in strategy.get("directions", [])
                        if d.get("id") == strategy.get("selected_direction_id")]
            if len(selected) == 1:
                strategy["directions"] = [direction_core(selected[0])]
        return result
    existing = context.get("existing_direction")
    if (context.get("stage") != "strategy" or not context.get("stage_skills")
            or not isinstance(existing, dict) or not existing.get("id")
            or context.get("direction_selection_required", True)):
        return result
    current_plan = result.get("current_plan")
    if isinstance(current_plan, dict):
        for key in ("skill_trace", "script_review", "alignment_review", "plan_review"):
            current_plan.pop(key, None)
        strategy = current_plan.get("strategy")
        if isinstance(strategy, dict) and strategy.get("selected_direction_id") == existing["id"]:
            selected = [direction for direction in strategy.get("directions", [])
                        if direction.get("id") == existing["id"]]
            if len(selected) == 1:
                strategy["directions"] = selected
                strategy["rules_used"] = [rule for rule in strategy.get("rules_used", [])
                                          if rule.get("id") == selected[0].get("rule_id")]
    for gate in result.get("gates", []):
        if (gate.get("kind") != "direction" or gate.get("status") != "answered"
                or not gate.get("selected_option_id")):
            continue
        selected = [option for option in gate.get("options", [])
                    if option.get("id") == gate["selected_option_id"]]
        if len(selected) == 1:
            gate["options"] = [{key: value for key, value in selected[0].items()
                                if key in ("id", "rule_id", "label", "angle", "audience_value",
                                           "product_expression", "evidence_ids", "story_summary", "attraction_mechanism", "film_concept",
                                           "consumer_insight", "big_idea", "hook_type", "brand_world")}]
    return result


def stage_request_options(stage: str, new_flow: bool) -> dict:
    """Return fresh transport overrides, preserving the legacy request profile.

    A real strategy call exhausted all 18K output tokens on reasoning and
    returned no JSON. Keep reasoning for source-bound concept work and leave
    space for both reasoning and the final object (the provider's current
    default thinking budget is 64K). This cap is not a target token count.
    The separate direction review retains its profile and all guards.
    Script drafting uses low reasoning; coupled directing, refinement and final
    review retain high reasoning after real acceptance exposed unresolved
    state and speaker-visibility errors. Direct draft output repeatedly
    exceeded the real task's joint action/speech budget.
    Independent reviews and deterministic checks remain in the pipeline.
    This policy is an optimization candidate, not a measured quality/speed claim.
    Unknown stages leave the caller's base options unchanged.
    """
    if new_flow:
        if stage == "strategy":
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "low", "max_tokens": 65536}
        if stage == "direction_review":
            # The budget includes reasoning and the three rewritten concepts.
            # A real 24K review exhausted 21,466 tokens on reasoning and cut
            # off its JSON; leave output room without adding another request.
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "high", "max_tokens": 65536}
        if stage == "ad_script":
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "low", "max_tokens": 24000}
        if stage == "script_review":
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "low", "max_tokens": 24000}
        if stage == "alignment_review":
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "low", "max_tokens": 32768}
        if stage in ("storyboard", "plan_refinement", "plan_review"):
            return {"thinking": {"type": "enabled"}, "reasoning_effort": "high", "max_tokens": 32768}
    if stage in ("ad_script", "script_review", "direction_review"):
        return script_request_options()
    if stage in ("strategy", "alignment_review", "storyboard_repair"):
        return {"thinking": {"type": "enabled"}, "reasoning_effort": "low",
                "max_tokens": 18000 if stage == "strategy" else 32768}
    return {}


def _finite_milliseconds(seconds):
    if type(seconds) not in (float, int):
        return None
    try:
        if math.isfinite(seconds) and seconds >= 0:
            milliseconds = seconds * 1000
            if math.isfinite(milliseconds):
                return round(milliseconds)
    except OverflowError:
        return None
    return None


def safe_planner_metrics(stage: object, elapsed_seconds: object, usage: object = None, *,
                         first_token_seconds: object = None, stream: object = None,
                         transport: object = None, stream_fallback: object = None) -> dict:
    """Allowlist numeric usage, wall time and time-to-first-token.

    Callers can log this once per completed transport request, including empty
    or invalid responses. Missing provider usage remains absent, never zero.
    Optional transport fields are omitted unless the caller measured them, so
    older log readers keep the historical keys.
    """
    elapsed_ms = _finite_milliseconds(elapsed_seconds)
    safe_usage = {}
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                safe_usage[key] = value
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            value = details.get("reasoning_tokens")
            if type(value) is int and value >= 0:
                safe_usage["reasoning_tokens"] = value
    result = {"stage": stage if isinstance(stage, str) and stage in PLANNER_STAGES else "unknown",
              "elapsed_ms": elapsed_ms, "usage": safe_usage}
    first_token_ms = _finite_milliseconds(first_token_seconds)
    if first_token_seconds is not None and first_token_ms is not None:
        result["first_token_ms"] = first_token_ms
    if stream is True or stream is False:
        result["stream"] = stream
    if transport in ("primary", "fallback"):
        result["transport"] = transport
    if stream_fallback is True:
        result["stream_fallback"] = True
    return result


def safe_planner_transport_failure(stage, elapsed_seconds, *, status_code=None, error_type=None):
    """Only transport status/type and timing; never stringify request errors."""
    metric = safe_planner_metrics(stage, elapsed_seconds)
    result = {"stage": metric["stage"], "elapsed_ms": metric["elapsed_ms"]}
    if type(status_code) is int and 100 <= status_code <= 599:
        result["status_code"] = status_code
    allowed = {"TimeoutError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
               "ConnectError", "ReadError", "WriteError", "CloseError", "RemoteProtocolError",
               "LocalProtocolError", "ProxyError", "UnsupportedProtocol"}
    if error_type is not None:
        result["error_type"] = error_type if type(error_type) is str and error_type in allowed else "TransportError"
    return result
