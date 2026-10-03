"""Offline coverage for scoped instructions, latency profiles and safe metrics."""
import hashlib
import json
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app import runtime_skills
from app.ad_script import script_request_options
from app.planner_runtime import planner_wire_context, safe_planner_metrics, stage_request_options


@pytest.fixture
def revision_context():
    directions = [{"id": name, "rule_id": rule, "label": name,
        "angle": "已知商品特点", "audience_value": "受众为何在意", "product_expression": "对应展示方法",
        "story_summary": f"{name}完整故事", "attraction_mechanism": f"{name}观看动机",
        "production": {"characters": [{"id": "speaker", "voice": "固定声音"}]},
        "evidence_ids": ["fact_original"]} for name, rule in (("selected", "appearance"), ("other", "comparison"))]
    return {"stage": "strategy", "stage_skills": {"skills": [{"id": "strategy"}]},
        "existing_direction": deepcopy(directions[0]), "direction_selection_required": False,
        "conversation": [{"role": "user", "content": "历史原始事实"},
                         {"role": "assistant", "content": "历史回复"},
                         {"role": "user", "content": "保留方向与20秒，只修改动作和状态"}],
        "evidence_sources": {"brief": "原始事实", "message_old": "历史原始事实"},
        "current_plan": {"skill_trace": [{"sha256": "original_hash"}],
            "script_review": {"ready": True, "script": {"beats": ["旧审核全文"]}},
            "alignment_review": {"ready": True, "notes": ["旧声画审核全文"]},
            "ad_script": {"beats": [{"id": "b1", "narration": "审定台词", "timing": {"visual_seconds": 4}}]},
            "facts": {"claims": ["原始事实"]}, "production": deepcopy(directions[0]["production"]),
            "segments": [{"shots": [{"id": "shot1", "start": 0, "end": 20}], "reference_asset_ids": ["original"]}],
            "strategy": {"brief": {"facts": ["原始事实"]}, "selected_direction_id": "selected",
                "directions": deepcopy(directions), "rules_used": [{"id": "appearance"}, {"id": "comparison"}]}},
        "gates": [{"kind": "direction", "status": "answered", "id": "direction_gate",
            "message": "选择方向原文", "note": "保留商家选择说明", "selected_option_id": "selected",
            "options": deepcopy(directions)},
            {"kind": "clarification", "questions": ["原问题"], "answers": ["原答案"], "note": "原说明"}]}


def test_selected_revision_projection_preserves_original_and_authoritative_content(revision_context):
    original = deepcopy(revision_context)
    wire = planner_wire_context(revision_context)
    assert revision_context == original
    expected = deepcopy(original)
    for key in ("skill_trace", "script_review", "alignment_review"):
        del expected["current_plan"][key]
    expected["current_plan"]["strategy"]["directions"] = [original["existing_direction"]]
    expected["current_plan"]["strategy"]["rules_used"] = [{"id": "appearance"}]
    expected["gates"][0]["options"] = [{key: original["existing_direction"][key]
        for key in ("id", "rule_id", "label", "angle", "audience_value", "product_expression",
                    "evidence_ids", "story_summary", "attraction_mechanism")}]
    assert wire == expected
    # The transport copy cannot mutate nested script, facts or conversation.
    wire["current_plan"]["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 9
    wire["conversation"][-1]["content"] = "changed"
    assert revision_context == original


@pytest.mark.parametrize("change", [
    {"stage_skills": None}, {"existing_direction": None},
    {"direction_selection_required": True},
    *({"stage": stage} for stage in ("script_review", "storyboard_repair", "alignment_review")),
])
def test_projection_leaves_unselected_legacy_and_other_stages_unchanged(revision_context, change):
    revision_context.update(change)
    assert planner_wire_context(revision_context) == revision_context


@pytest.mark.parametrize("status,selected_id", [("pending", "selected"), ("answered", None), ("answered", "missing")])
def test_projection_does_not_guess_unresolved_direction_gates(revision_context, status, selected_id):
    revision_context["gates"][0].update(status=status, selected_option_id=selected_id)
    assert planner_wire_context(revision_context)["gates"] == revision_context["gates"]


def test_projection_does_not_reselect_a_different_saved_strategy(revision_context):
    revision_context["current_plan"]["strategy"]["selected_direction_id"] = "other"
    assert planner_wire_context(revision_context)["current_plan"]["strategy"] == revision_context["current_plan"]["strategy"]


def test_gateway_serializes_projection_only_and_keeps_strict_json_transport(revision_context, monkeypatch):
    from app.providers.gateway import Gateway
    original = deepcopy(revision_context)
    captured = []
    def respond(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        captured.append(json.loads(payload["messages"][1]["content"][0]["text"]))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"checked": true}'}}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        client(transport=httpx.MockTransport(respond), **kwargs))
    settings = SimpleNamespace(planner_model="deepseek-flash", deepseek_api_key="offline-placeholder")
    assert asyncio.run(Gateway(settings)._planner_json("json", revision_context, [])) == {"checked": True}
    assert len(captured) == 1
    contract = captured[0].pop("evidence_reference_contract")
    assert contract["version"] == 1
    expected = planner_wire_context(original)
    expected["evidence_sources"] = {"merchant_1": "原始事实", "merchant_2": "历史原始事实"}
    assert captured == [expected]
    assert revision_context == original


@pytest.mark.parametrize("stage,thinking,effort,budget", [
    ("ad_script", "enabled", "low", 24000),
    ("script_review", "enabled", "low", 24000),
    ("alignment_review", "enabled", "low", 32768),
    ("storyboard", "enabled", "high", 32768),
    ("plan_refinement", "enabled", "high", 32768),
    ("plan_review", "enabled", "high", 32768),
    ("storyboard_repair", "enabled", "low", 32768),
    ("strategy", "enabled", "low", 65536),
    ("direction_review", "enabled", "high", 65536),
])
def test_new_profiles_reduce_thinking_without_cutting_json_capacity(stage, thinking, effort, budget):
    options = stage_request_options(stage, True)
    assert options["thinking"] == {"type": thinking}
    assert options.get("reasoning_effort") == effort
    assert options["max_tokens"] == budget
    # Mutating a request must never leak into the next one.
    options["thinking"]["type"] = "changed"
    assert stage_request_options(stage, True)["thinking"]["type"] == thinking


@pytest.mark.parametrize("stage", ["ad_script", "script_review", "direction_review"])
def test_legacy_script_profiles_preserve_shared_eval_settings(stage):
    assert stage_request_options(stage, False) == script_request_options()


def test_unconfigured_and_legacy_storyboard_keep_callers_base_options():
    assert stage_request_options("storyboard", False) == {}
    assert stage_request_options("unrecognized", True) == {}
    assert stage_request_options("alignment_review", False) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "low", "max_tokens": 32768}
    assert stage_request_options("strategy", False) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "low", "max_tokens": 18000}


def test_strategy_transport_leaves_room_for_reasoning_and_json_and_keeps_review(monkeypatch):
    """The failed real call used 18005 reasoning tokens and returned no content.

    Verify the actual outgoing request, not only a settings helper. The same
    source-bound reasoning and separate review remain in force.
    """
    from app.providers.gateway import Gateway
    sent = []
    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"checked": true}'}}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        client(transport=httpx.MockTransport(respond), **kwargs))
    gateway = Gateway(SimpleNamespace(planner_model="deepseek-flash", deepseek_api_key="TEST_ONLY"))
    for stage in ("strategy", "direction_review"):
        asyncio.run(gateway._planner_json("Return JSON", {"stage": stage, "stage_skills": {"skills": []}}, []))
    assert sent[0]["thinking"] == {"type": "enabled"} and sent[0]["reasoning_effort"] == "low"
    assert sent[0]["max_tokens"] == 65536 and sent[0]["response_format"] == {"type": "json_object"}
    assert sent[1]["thinking"] == {"type": "enabled"} and sent[1]["reasoning_effort"] == "high"
    assert sent[1]["max_tokens"] == 65536


def test_metrics_only_include_safe_stage_elapsed_and_nonnegative_integer_usage():
    metrics = safe_planner_metrics("ad_script", 37.291, {
        "prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180,
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100,
        "prompt": "PRIVATE_PROMPT", "reasoning_content": "PRIVATE_REASONING",
        "completion_tokens_details": {"reasoning_tokens": 60, "secret": "PRIVATE_DETAIL"}})
    assert metrics == {"stage": "ad_script", "elapsed_ms": 37291, "usage": {
        "prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180,
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100, "reasoning_tokens": 60}}
    assert "PRIVATE" not in json.dumps(metrics)


@pytest.mark.parametrize("elapsed", [None, True, -1, float("nan"), float("inf"), 10**1000, "PRIVATE_TIME"])
def test_invalid_metrics_cannot_leak_free_text_or_invalid_numbers(elapsed):
    assert safe_planner_metrics(["PRIVATE_STAGE"], elapsed, {
        "prompt_tokens": True, "completion_tokens": -10, "total_tokens": "PRIVATE_USAGE",
        "completion_tokens_details": {"reasoning_tokens": 1.5}}) == {
            "stage": "unknown", "elapsed_ms": None, "usage": {}}


def test_absent_usage_remains_unknown():
    assert safe_planner_metrics("storyboard", 0) == {"stage": "storyboard", "elapsed_ms": 0, "usage": {}}


@pytest.fixture
def scoped_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_skills, "SKILLS_ROOT", tmp_path)
    identifier = "ads-video-plan-review"
    root = tmp_path / identifier
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {identifier}\ndescription: Shared review rules\n---\nCOMMON_REVIEW_RULE\n")
    stage_resources = {}
    for stage in ("direction_review", "script_review", "alignment_review"):
        resource = f"references/{stage}.md"
        (root / resource).write_text(f"PRIVATE_TO_{stage}\n")
        stage_resources[stage] = ["SKILL.md", resource]
    registry = {"schema_version": 1, "skills": [{
        "id": identifier, "display_name": "Ads Video Plan Review", "version": "2.0.0",
        "stages": list(stage_resources), "mode": "model", "runtime_status": "active",
        "resources": ["SKILL.md"] + [paths[1] for paths in stage_resources.values()],
        "stage_resources": stage_resources}]}
    (tmp_path / "registry.json").write_text(json.dumps(registry))
    return root, registry


@pytest.mark.parametrize("stage", ["direction_review", "script_review", "alignment_review"])
def test_only_selected_review_instructions_are_sent_and_trace_is_honest(scoped_registry, stage):
    root, registry = scoped_registry
    bundle = runtime_skills.stage_skill_context(stage)["skills"][0]
    assert "COMMON_REVIEW_RULE" in bundle["content"]
    assert f"PRIVATE_TO_{stage}" in bundle["content"]
    for other in set(registry["skills"][0]["stages"]) - {stage}:
        assert f"PRIVATE_TO_{other}" not in bundle["content"]
    assert bundle["loaded_sections"] == ["SKILL.md", f"references/{stage}.md"]
    assert bundle["content_sha256"] == hashlib.sha256(bundle["content"].encode()).hexdigest()
    full = runtime_skills._bundle(registry["skills"][0])
    assert bundle["source_content_sha256"] == full["content_sha256"]
    assert all(row["sha256"] == hashlib.sha256((root / row["path"]).read_bytes()).hexdigest()
               for row in bundle["resources"])
    trace = runtime_skills.skill_trace(runtime_skills.stage_skill_context(stage))[0]
    assert trace["version"] == "2.0.0" and trace["loaded_sections"] == bundle["loaded_sections"]
    assert trace["source_content_sha256"] == bundle["source_content_sha256"]
    assert "PRIVATE_TO" not in json.dumps(trace)


def test_other_mode_edits_change_source_hash_without_claiming_sent_content_changed(scoped_registry):
    root, _ = scoped_registry
    before = runtime_skills.stage_skill_context("script_review")["skills"][0]
    (root / "references/alignment_review.md").write_text("Updated other mode\n")
    after = runtime_skills.stage_skill_context("script_review")["skills"][0]
    assert before["content_sha256"] == after["content_sha256"]
    assert before["source_content_sha256"] != after["source_content_sha256"]


@pytest.mark.parametrize("mapping", [
    [], {"invalid_stage": ["SKILL.md"]}, {"script_review": []},
    {"script_review": ["references/script_review.md"]},
    {"script_review": ["SKILL.md", "../private.md"]},
    {"script_review": ["SKILL.md", "SKILL.md"]},
    {"script_review": ["SKILL.md", ["nested"]]},
])
def test_invalid_stage_resource_mapping_fails_closed(scoped_registry, mapping):
    root, registry = scoped_registry
    registry["skills"][0]["stage_resources"] = mapping
    (root.parent / "registry.json").write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="分阶段资源"):
        runtime_skills.stage_skill_context("script_review")
