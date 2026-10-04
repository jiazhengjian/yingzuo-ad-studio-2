"""Sparse edits keep the original scope, final compile and independent review."""
import asyncio
from copy import deepcopy

import pytest

from app.progressive_planning import (canonical_director, capacity_repair_scope, compose_refinement,
    refinement_schema, validate_local_refinement)
from app.refinement_patch import compose_sparse_refinement, sparse_refinement_schema
from app.providers.gateway import Gateway, ProviderError
from app.schemas import Plan
from test_progressive_planning import (chosen_strategy, director_output, forbid_real_network,
    install_transport, progressive_gateway, reject_first_shot, pacing_review, settings, source)
from test_capacity_restructure import merge_first_two, overburden_first_beat


@pytest.fixture
def local_draft(source):
    strategy = chosen_strategy(source)
    before = canonical_director(director_output(strategy), strategy).model_dump()
    scope = {"beat_ids": ["b0"], "shot_ids": ["shot_0"], "allow_summary_update": False,
        "adjacent_state_beat_ids": ["b1"], "adjacent_state_shot_ids": ["shot_1"]}
    return strategy, before, scope


def patch_response(patch):
    def replace(value):
        value.clear()
        value["patch"] = deepcopy(patch)
    return replace


def canonical_patch(patch, local_draft):
    strategy, before, scope = local_draft
    composed = compose_sparse_refinement({"patch": patch}, before, scope)
    return canonical_director(compose_refinement(composed, before, scope), strategy).model_dump()


def test_partial_fields_preserve_all_other_values_and_never_mutate_inputs(local_draft):
    _, before, scope = local_draft
    patch = {"beats": [{"id": "b0", "set": {"narration": "看看瓶身。"}}],
        "shots": [{"id": "shot_0", "set": {"description": "同一瓶子静置桌角，留出轮廓辨认时间。"}}]}
    original, original_patch = deepcopy(before), deepcopy(patch)
    result = canonical_patch(patch, local_draft)
    validate_local_refinement(before, result, scope)
    expected = deepcopy(before)
    expected["ad_script"]["beats"][0]["narration"] = "看看瓶身。"
    expected["shots"][0]["description"] = patch["shots"][0]["set"]["description"]
    assert result == expected
    assert before == original and patch == original_patch


@pytest.mark.parametrize("patch", [
    {}, {"beats": []}, {"shots": None},
    {"beats": [{"id": "b0", "set": {}}]},
    {"beats": [{"id": "unknown", "set": {"narration": "换句。"}}]},
    {"beats": [{"id": "b2", "set": {"narration": "换句。"}}]},
    {"beats": [{"id": "b0", "set": {"narration": "换句。"}}, {"id": "b0", "set": {"narration": "重复。"}}]},
    {"shots": [{"id": "new_shot", "set": {"description": "新镜头"}}]},
    {"shots": [{"id": "shot_2", "set": {"description": "越界"}}]},
    {"shots": [{"id": "shot_0", "set": {"id": "renamed"}}]},
    {"shots": [{"id": "shot_0", "set": {"unknown": "不可丢弃"}}]},
    {"shots": [{"id": "shot_0", "set": {"continuity": {"unknown": "不可丢弃"}}}]},
    {"beats": [{"id": "b0", "set": {"narration": None}}]},
    {"beats": [{"id": "b0", "set": {"timing": None}}]},
    {"beats": [{"id": "b0", "set": {"timing": {"start_state": None}}}]},
    {"shots": [{"id": "shot_0", "set": {"coverage": [None]}}]},
    {"summary": {"core_message": "没有权限"}},
    {"production": {"speech_mode": "none"}},
])
def test_unknown_duplicate_out_of_scope_and_null_edits_are_rejected(local_draft, patch):
    _, before, scope = local_draft
    original = deepcopy(before), deepcopy(patch)
    with pytest.raises(ValueError):
        compose_sparse_refinement({"patch": patch}, before, scope)
    assert (before, patch) == original


def test_sparse_and_complete_formats_cannot_be_mixed(local_draft):
    _, before, scope = local_draft
    with pytest.raises(ValueError):
        compose_sparse_refinement({"patch": {"shots": []}, "ad_script": before["ad_script"]}, before, scope)


@pytest.mark.parametrize("fields", [
    {"narration": 123}, {"evidence_ids": ["foreign_fact"]},
    {"evidence_ids": ["appearance", "appearance"]}, {"timing": {"visual_seconds": -1}},
    {"timing": {"speech_overlap": "yes"}},
])
def test_composition_is_not_a_replacement_for_strict_canonical_validation(local_draft, fields):
    with pytest.raises(ValueError):
        canonical_patch({"beats": [{"id": "b0", "set": fields}]}, local_draft)


def test_adjacent_states_can_change_without_unlocking_neighbor_content(local_draft):
    _, before, scope = local_draft
    patch = {"beats": [{"id": "b1", "set": {"timing": {"start_state": "商品已经在桌角。"}}}],
        "shots": [{"id": "shot_1", "set": {"continuity": {"start_state": "商品已经在桌角。"}}}]}
    revised = canonical_patch(patch, local_draft)
    validate_local_refinement(before, revised, scope)
    for bad in ({"beats": [{"id": "b1", "set": {"narration": "更改邻拍。"}}]},
                {"shots": [{"id": "shot_1", "set": {"description": "更改邻镜动作。"}}]},
                {"shots": [{"id": "shot_1", "set": {"continuity": {"camera_move": "新机位"}}}]}):
        with pytest.raises(ValueError):
            compose_sparse_refinement({"patch": bad}, before, scope)


def test_allowed_summary_and_list_replacements_do_not_merge_or_invent_members(local_draft):
    _, before, scope = local_draft
    scope = {**scope, "allow_summary_update": True}
    patch = {"summary": {"core_message": "在同一桌角辨认轮廓。"},
        "shots": [{"id": "shot_0", "set": {"character_ids": [], "evidence_ids": []}}]}
    result = compose_sparse_refinement({"patch": patch}, before, scope)
    assert result["shots"][0]["character_ids"] == []
    assert result["shots"][0]["evidence_ids"] == []
    assert result["ad_script"]["core_message"] == patch["summary"]["core_message"]
    assert result["ad_script"]["audience_takeaway"] == before["ad_script"]["audience_takeaway"]


def test_schema_keeps_original_complete_branch_and_typed_partial_scope(local_draft):
    _, before, scope = local_draft
    full = refinement_schema(scope)
    snapshot = deepcopy(full)
    schema = sparse_refinement_schema(full, before, scope)
    assert schema["oneOf"][1] == {k: v for k, v in full.items() if k != "$defs"}
    assert schema["$defs"] == full["$defs"]
    assert full == snapshot
    patch = schema["oneOf"][0]["properties"]["patch"]
    assert "summary" not in patch["properties"]
    neighbor = patch["properties"]["shots"]["items"]["oneOf"][1]
    assert neighbor["properties"]["id"]["enum"] == ["shot_1"]
    assert set(neighbor["properties"]["set"]["properties"]) == {"continuity"}
    assert set(neighbor["properties"]["set"]["properties"]["continuity"]["properties"]) == {"start_state", "end_state"}
    # Referenced leaf contracts are the original typed fields, not shortened
    # text-only replacements that lose enums, lengths or numeric bounds.
    def check_references(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = schema
                for part in value["$ref"].removeprefix("#/").split("/"):
                    target = target[part]
                assert isinstance(target, dict)
            for item in value.values():
                check_references(item)
        elif isinstance(value, list):
            for item in value:
                check_references(item)
    check_references(schema)
    global_scope = capacity_repair_scope(before)
    global_schema = sparse_refinement_schema(refinement_schema(global_scope), before, global_scope)
    assert "summary" in global_schema["oneOf"][0]["properties"]["patch"]["properties"]


def test_actual_gateway_sparse_response_gets_full_compile_and_complete_review(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    description = "同一瓶子静置办公桌角，省去重复取放，办公者只观察瓶身。"
    state.refinement_mutation = patch_response({"shots": [{"id": "shot_0", "set": {"description": description}}]})
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    repair, review = state.contexts[-2:]
    assert repair["refinement_output_format"] == "sparse_or_full"
    assert repair["refinement_output_version"] >= 1
    assert {"patch"} == set(repair["schema"]["oneOf"][0]["properties"])
    assert review["final_plan"]["segments"][0]["shots"][0]["description"] == description
    assert len(plan["plan_review"]["alignment"]["shots"]) == 3
    assert len(plan["plan_review"]["alignment"]["spans"]) == 3
    assert plan["segments"][0]["shots"][1]["description"] == repair["draft_storyboard"]["shots"][1]["description"]
    assert Plan.model_validate(plan).plan_review["ready"]


def test_sparse_edits_cannot_change_fixed_speech(progressive_gateway):
    state = progressive_gateway
    state.brief = "固定台词“看轮廓。”，其他内容可调整。"
    state.first_review_mutation = reject_first_shot
    state.refinement_mutation = patch_response({"beats": [{"id": "b0", "set": {"narration": "换说法。"}}]})
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


def test_estimate_only_sparse_edits_keep_existing_bounded_repair_and_fail(progressive_gateway):
    state = progressive_gateway
    # Make visual work, rather than the unchanged speech duration, determine
    # the beat's minimum capacity so this really reduces the required window.
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0]["timing"].update(visual_seconds=4)
    state.first_review_mutation = reject_first_shot
    state.refinement_mutations = [patch_response({"beats": [{"id": "b0", "set": {"timing": {"visual_seconds": seconds}}}]})
                                  for seconds in (0.1, 0.2)]
    plan = state.run()
    assert plan["segments"]
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_refinement"]
    assert state.contexts[-1]["review_feedback"]["kind"] == "unsupported_timing_reduction"
    assert state.contexts[-1]["draft_storyboard"]["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 0.1


def test_local_full_branch_can_replace_affected_shots_without_rewriting_others(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def split_shot(value):
        first, second = deepcopy(value["shots"][0]), deepcopy(value["shots"][0])
        first.update(id="replacement_a", description="保持商品直立，先看清瓶身整体轮廓。")
        second.update(id="replacement_b", description="同一商品保持原位，继续靠近辨认瓶身边缘。")
        second["continuity"]["entry_transition"] = "continuous"
        value["shots"][:1] = [first, second]
    state.refinement_mutation = split_shot
    plan = state.run()
    repair = state.contexts[3]
    assert repair["refinement_output_format"] == "sparse_or_full"
    assert not repair["repair_scope"].get("allow_beat_restructure", False)
    shots = [shot for segment in plan["segments"] for shot in segment["shots"]]
    assert [shot["id"] for shot in shots] == ["replacement_a", "replacement_b", "shot_1", "shot_2"]
    assert shots[-1]["description"] == repair["draft_storyboard"]["shots"][-1]["description"]
    assert len(plan["plan_review"]["alignment"]["shots"]) == 4
    assert plan["plan_review"]["ready"]


def test_full_candidate_changed_identity_keeps_remaining_estimate_repair(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0]["timing"].update(visual_seconds=4)
    state.first_review_mutation = reject_first_shot
    def first_full(value):
        value["shots"][0]["id"] = "replacement_shot"
        value["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 0.1
    def second_full(value):
        assert value["shots"][0]["id"] == "replacement_shot"
        value["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 4
        value["shots"][0]["description"] = "省去重复取放，同一瓶子静置桌角，办公者只观察瓶身。"
    state.refinement_mutations = [first_full, second_full]
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_refinement", "plan_review"]
    first, second = state.contexts[3:5]
    assert first["refinement_output_format"] == "sparse_or_full"
    assert "refinement_output_format" not in second
    assert set(second["schema"]["properties"]) == {"ad_script", "shots"}
    assert second["review_feedback"]["kind"] == "unsupported_timing_reduction"
    assert second["repair_scope"] == first["repair_scope"]
    assert second["repair_scope"]["shot_ids"] == ["shot_0"]
    assert second["draft_storyboard"]["shots"][0]["id"] == "replacement_shot"
    shots = plan["segments"][0]["shots"]
    assert shots[0]["id"] == "replacement_shot"
    assert shots[1]["description"] == first["draft_storyboard"]["shots"][1]["description"]
    assert plan["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 4
    assert Plan.model_validate(plan).plan_review["ready"]
    assert len(plan["plan_review"]["alignment"]["shots"]) == len(shots)


def test_patches_apply_to_current_candidate_but_validate_against_original_scope(local_draft):
    _, before, scope = local_draft
    current = compose_sparse_refinement({"patch": {"beats": [{"id": "b0", "set": {"narration": "保留本次已改台词。"}}]}}, before, scope)
    after = compose_sparse_refinement({"patch": {"shots": [{"id": "shot_0", "set": {"description": "简化本镜实际动作。"}}]}}, current, scope)
    assert after["ad_script"]["beats"][0]["narration"] == "保留本次已改台词。"
    validate_local_refinement(before, after, scope)
    assert before["ad_script"]["beats"][0]["narration"] == "看轮廓。"


def test_capacity_restructure_still_offers_and_accepts_complete_output(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    state.refinement_mutation = merge_first_two
    plan = state.run()
    repair = state.contexts[2]
    assert repair["repair_scope"]["allow_beat_restructure"]
    assert repair["refinement_output_format"] == "sparse_or_full"
    assert set(repair["schema"]["oneOf"][1]["properties"]) == {"ad_script", "shots"}
    assert len(plan["ad_script"]["beats"]) == 2 and plan["plan_review"]["ready"]


def test_global_patch_changes_only_existing_fields_and_keeps_identity_order(local_draft):
    strategy, before, _ = local_draft
    scope = capacity_repair_scope(before)
    # Reversed patch rows are addressed by ID and cannot reorder the film.
    patch = {"beats": [{"id": "b2", "set": {"narration": "留在桌角。"}},
                       {"id": "b0", "set": {"narration": "看清瓶身。"}}],
             "shots": [{"id": "shot_2", "set": {"description": "保持同一商品的完整轮廓，停留辨认。"}}]}
    after = compose_sparse_refinement({"patch": patch}, before, scope)
    canonical = canonical_director(compose_refinement(after, before, scope), strategy).model_dump()
    validate_local_refinement(before, canonical, scope)
    assert [b["id"] for b in canonical["ad_script"]["beats"]] == [b["id"] for b in before["ad_script"]["beats"]]
    assert [s["id"] for s in canonical["shots"]] == [s["id"] for s in before["shots"]]
    assert canonical["ad_script"]["beats"][1] == before["ad_script"]["beats"][1]
    assert canonical["shots"][0] == before["shots"][0]


@pytest.mark.parametrize("patch", [
    {"beats": [{"id": "new_beat", "set": {"narration": "新内容。"}}]},
    {"shots": [{"id": "new_shot", "set": {"description": "新镜头。"}}]},
    {"beats": [{"id": "b0", "delete": True}]},
    {"shots": [{"id": "shot_0", "set": None}]},
    {"beats": [{"id": "b0", "set": {"id": "b2"}}]},
    {"shots": [{"id": "shot_0", "set": {"description": "第一版。"}},
               {"id": "shot_0", "set": {"description": "重复版。"}}]},
])
def test_global_restructure_permission_does_not_make_patch_a_structure_editor(local_draft, patch):
    _, before, _ = local_draft
    with pytest.raises(ValueError):
        compose_sparse_refinement({"patch": patch}, before, capacity_repair_scope(before))


def test_review_capacity_failure_can_use_sparse_timing_change_and_full_review(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = pacing_review(10)
    state.refinement_mutation = patch_response({"beats": [{"id": "b0", "set": {"timing": {"visual_seconds": 10}}}]})
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    repair = state.contexts[3]
    assert repair["repair_scope"]["allow_beat_restructure"] is True
    assert repair["refinement_output_version"] == 2
    assert repair["refinement_output_format"] == "sparse_or_full"
    assert [beat["id"] for beat in plan["ad_script"]["beats"]] == ["b0", "b1", "b2"]
    assert [shot["id"] for segment in plan["segments"] for shot in segment["shots"]] == ["shot_0", "shot_1", "shot_2"]
    assert plan["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 10
    assert sum(segment["duration_seconds"] for segment in plan["segments"]) == state.duration
    assert Plan.model_validate(plan).plan_review["ready"]
    assert len(plan["plan_review"]["alignment"]["shots"]) == 3


def test_global_patch_still_cannot_change_fixed_original_speech(progressive_gateway):
    state = progressive_gateway
    state.brief = "固定台词“看轮廓。”，其他内容可调整。"
    state.first_review_mutation = pacing_review(10)
    state.refinement_mutation = patch_response({"beats": [{"id": "b0", "set": {"narration": "改写了。"}}]})
    with pytest.raises(ProviderError):
        state.run()
    assert state.contexts[-1]["repair_scope"]["allow_beat_restructure"] is True
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


def test_global_patch_cannot_deliver_over_capacity_or_gain_more_repairs(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = pacing_review(10)
    state.refinement_mutations = [patch_response({"beats": [{"id": "b0", "set": {"timing": {"visual_seconds": seconds}}}]})
                                  for seconds in (21, 22)]
    plan = state.run()
    assert plan["segments"]
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_refinement"]
    assert all(context["repair_scope"]["allow_beat_restructure"] for context in state.contexts if context["stage"] == "plan_refinement")
    assert state.contexts[-1]["review_feedback"]["kind"] == "deterministic_timing"


def test_sparse_contract_version_invalidates_only_refinement_replay(progressive_gateway, settings, source, monkeypatch):
    import app.progressive_planning as progressive
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    state.refinement_mutation = patch_response({"shots": [{"id": "shot_0", "set": {"description": "省去重复取放，只观察瓶身。"}}]})
    checkpoint = None
    def save(value):
        nonlocal checkpoint
        checkpoint = deepcopy(value)
    def run(saved=None):
        return asyncio.run(Gateway(settings).decide({"brief": state.brief, "duration_seconds": state.duration},
            [source], state.conversation, [], state.current_plan, selected_strategy=state.strategy.model_dump(),
            planning_checkpoint=saved, on_checkpoint=save))
    assert run()["plan"]["plan_review"]["ready"]
    frozen, calls = deepcopy(checkpoint), list(state.calls)
    monkeypatch.setattr(progressive, "REFINEMENT_OUTPUT_VERSION", progressive.REFINEMENT_OUTPUT_VERSION + 1)
    result = run(checkpoint)
    assert result["plan"]["plan_review"]["ready"]
    assert len(state.calls) > len(calls)
    assert checkpoint != frozen
