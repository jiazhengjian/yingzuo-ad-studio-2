"""Capacity repairs may simplify content while scoped and user locks remain."""
from copy import deepcopy

import pytest

from app.progressive_planning import (
    ProgressivePlanningError, canonical_director, capacity_repair_scope,
    validate_local_refinement,
)
from app.providers.gateway import ProviderError
from test_progressive_planning import (
    chosen_strategy, director_output, forbid_real_network, overburden_first_beat,
    pacing_review, progressive_gateway, reject_first_shot, simplify_first_beat,
)
from test_providers import settings, source, install_transport


def merge_first_two(value, *, retain_line=True):
    first, second = value["ad_script"]["beats"][:2]
    if retain_line:
        first["narration"] += second["narration"]
    first["timing"].update(visual_seconds=3,
        action_plan="省去重复观看，直接辨认同一瓶身与办公桌角的关系。")
    first["visual_role"] = "同一画面中辨认瓶身轮廓与桌角关系。"
    del value["ad_script"]["beats"][1]
    value["shots"][0]["description"] = "办公桌角，同一瓶子静置，办公者站在一旁；镜头保留瓶身轮廓与桌角关系，省去重复展示。"
    del value["shots"][1]


def test_compile_capacity_failure_can_merge_beats_and_still_reviews_final_plan(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    state.refinement_mutation = merge_first_two
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_review"]
    assert [beat["id"] for beat in plan["ad_script"]["beats"]] == ["b0", "b2"]
    assert len(plan["plan_review"]["alignment"]["shots"]) == 2
    assert plan["plan_review"]["ready"] is True
    scope = state.contexts[2]["repair_scope"]
    assert scope["allow_beat_restructure"] is True
    assert set(scope["beat_ids"]) == {"b0", "b1", "b2"}
    assert set(scope["shot_ids"]) == {"shot_0", "shot_1", "shot_2"}
    assert scope["allow_summary_update"] is True


def test_review_capacity_failure_can_rebalance_content_and_runs_full_review(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = pacing_review(10)
    state.refinement_mutation = merge_first_two
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    assert state.contexts[3]["repair_scope"]["allow_beat_restructure"] is True
    assert len(plan["ad_script"]["beats"]) == 2
    assert len(plan["plan_review"]["alignment"]["spans"]) == 2
    assert plan["plan_review"]["ready"] is True


def test_local_semantic_repair_cannot_delete_an_unaffected_beat(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    state.refinement_mutation = merge_first_two
    with pytest.raises(ProviderError, match="未能保留"):
        state.run()
    assert not state.contexts[3]["repair_scope"].get("allow_beat_restructure", False)
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


@pytest.mark.parametrize("retain_line", [True, False])
def test_capacity_merge_cannot_drop_user_fixed_speech(progressive_gateway, retain_line):
    state = progressive_gateway
    state.brief = "固定台词“看桌角。”，其他重复过程可以精简。"
    state.first_director_mutation = overburden_first_beat
    state.refinement_mutation = lambda value: merge_first_two(value, retain_line=retain_line)
    if retain_line:
        plan = state.run()
        assert "看桌角。" in plan["script"]
        assert plan["plan_review"]["ready"] is True
    else:
        with pytest.raises(ProviderError, match="已确认要求"):
            state.run()
        assert state.calls == ["ad_script", "storyboard", "plan_refinement"]


@pytest.mark.parametrize("rename_beats", [False, True])
def test_global_capacity_scope_still_rejects_estimate_only_changes(source, rename_beats):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 21
    before = canonical_director(raw, strategy).model_dump()
    after = deepcopy(before)
    after["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 3
    if rename_beats:
        for beat in after["ad_script"]["beats"]:
            beat["id"] += "_renamed"
        for shot in after["shots"]:
            for part in shot["coverage"]:
                part["beat_id"] += "_renamed"
    with pytest.raises(ProgressivePlanningError, match="只缩短了时间估计"):
        validate_local_refinement(before, after, capacity_repair_scope(before))


@pytest.mark.parametrize("missing_scope", ["beat_ids", "shot_ids"])
def test_restructure_flag_requires_every_original_beat_and_shot(source, missing_scope):
    strategy = chosen_strategy(source)
    before = canonical_director(director_output(strategy), strategy).model_dump()
    after = deepcopy(before)
    merge_first_two(after)
    scope = capacity_repair_scope(before)
    scope[missing_scope].pop()
    with pytest.raises(ValueError, match="全部内容和镜头"):
        validate_local_refinement(before, after, scope)


def test_refinement_requests_only_editable_fields_and_preserves_fixed_scene(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def refine(value):
        value["shots"][0]["description"] += "只观察静置瓶身，不作重复取放。"
        value["title"] = "另一个标题"
        value["visual_direction"]["scene"] = "改成海边沙滩"
        value["ad_script"]["core_message"] = "替换整条广告主旨"
    state.refinement_mutation = refine
    plan = state.run()
    request = state.contexts[3]
    properties = request["schema"]["oneOf"][1]["properties"]
    assert set(properties) == {"ad_script", "shots"}
    assert "core_message" not in request["schema"]["$defs"]["AdScript"]["properties"]
    assert plan["title"] == "桌角轮廓"
    assert plan["visual_direction"]["scene"] == "办公桌与办公椅"
    assert plan["ad_script"]["core_message"] == request["draft_storyboard"]["ad_script"]["core_message"]
    assert "只观察静置瓶身" in plan["segments"][0]["shots"][0]["description"]
    assert plan["plan_review"]["ready"] is True


def test_partial_refinement_payload_is_composed_before_complete_validation(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def refine(value):
        value.pop("title")
        value.pop("visual_direction")
        for field in ("core_message", "audience_takeaway", "product_role"):
            value["ad_script"].pop(field)
        value["shots"][0]["description"] += "省去重复取放，只保留静置观察。"
    state.refinement_mutation = refine
    assert state.run()["plan_review"]["ready"] is True
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]


def test_global_restructure_updates_derived_summary_and_preserves_fixed_globals(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    def refine(value):
        merge_first_two(value)
        value["ad_script"]["core_message"] = "同一画面中辨认瓶身与桌角的搭配，省去重复展示。"
        value["visual_direction"]["scene"] = "不同场景"
    state.refinement_mutation = refine
    plan = state.run()
    request = state.contexts[2]
    assert "core_message" in request["schema"]["$defs"]["AdScript"]["properties"]
    assert "省去重复展示" in plan["ad_script"]["core_message"]
    assert plan["visual_direction"]["scene"] == "办公桌与办公椅"
    assert plan["strategy"] == state.strategy.model_dump()


@pytest.mark.parametrize("location", ["root", "beat", "shot"])
def test_refinement_other_unknown_fields_remain_invalid(progressive_gateway, location):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    def refine(value):
        value["shots"][0]["description"] += "真实精简动作。"
        target = value if location == "root" else value["shots"][0] if location == "shot" else value["ad_script"]["beats"][0]
        target["unexpected_override"] = "不可接受"
    state.refinement_mutation = refine
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


def break_mapping(value):
    value["shots"][0]["evidence_ids"] = []


def fix_mapping(value):
    value["shots"][0]["evidence_ids"] = ["appearance"]


def test_known_mapping_error_gets_scoped_authored_repair_then_independent_review(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = break_mapping
    state.refinement_mutation = fix_mapping
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_review"]
    request = state.contexts[2]
    assert request["review_feedback"]["kind"] == "evidence_mapping"
    assert request["review_feedback"]["missing_mappings"] == [{"beat_id": "b0", "fact_id": "appearance"}]
    assert request["repair_scope"]["beat_ids"] == ["b0"]
    assert request["repair_scope"]["shot_ids"] == ["shot_0"]
    assert request["draft_storyboard"]["shots"][0]["evidence_ids"] == []
    assert plan["segments"][0]["shots"][0]["evidence_ids"] == ["appearance"]
    assert plan["plan_review"]["ready"] is True


def test_mapping_repair_uses_existing_budget_and_keeps_full_semantic_review(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = break_mapping
    state.first_review_mutation = reject_first_shot
    state.refinement_mutations = [fix_mapping,
        lambda value: value["shots"][0].update(description="办公者站定，只观察瓶身与桌角。")]
    assert state.run()["plan_review"]["ready"] is True
    assert state.calls.count("plan_refinement") == 2
    assert state.calls.count("plan_review") == 2


def test_mapping_repair_covers_shared_shot_without_expanding_to_neighbor_shots(progressive_gateway):
    state = progressive_gateway
    def broken(value):
        value["ad_script"]["beats"][1]["evidence_ids"] = []
        value["shots"][0]["coverage"].append({"beat_id": "b1", "weight": 1})
        # b1 also has its own shot; repairing the shared shot must not unlock it.
        break_mapping(value)
    state.first_director_mutation = broken
    state.refinement_mutation = fix_mapping
    plan = state.run()
    request = state.contexts[2]
    assert request["review_feedback"]["missing_mappings"] == [{"beat_id": "b0", "fact_id": "appearance"}]
    assert request["repair_scope"]["beat_ids"] == ["b0"]
    assert request["repair_scope"]["shot_ids"] == ["shot_0"]
    assert request["repair_scope"]["allow_existing_mapping_coverage"] is True
    assert plan["plan_review"]["ready"] is True
    assert plan["segments"][0]["shots"][1]["description"] == request["draft_storyboard"]["shots"][1]["description"]


@pytest.mark.parametrize("change", ["neighbor_beat", "new_shot", "changed_coverage"])
def test_shared_mapping_coverage_does_not_unlock_neighbors_or_new_shots(progressive_gateway, change):
    state = progressive_gateway
    def broken(value):
        value["ad_script"]["beats"][1]["evidence_ids"] = []
        value["shots"][0]["coverage"].append({"beat_id": "b1", "weight": 1})
        break_mapping(value)
    def refine(value):
        fix_mapping(value)
        if change == "neighbor_beat":
            value["ad_script"]["beats"][1]["visual_role"] = "改成无关的动作。"
        elif change == "new_shot":
            value["shots"][0]["id"] = "new_shared_shot"
        else:
            value["shots"][0]["coverage"][1]["weight"] = 2
    state.first_director_mutation = broken
    state.refinement_mutation = refine
    with pytest.raises(ProviderError, match="未能保留"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement"]


def test_mapping_error_may_not_keep_retrying_the_same_mapping(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = break_mapping
    state.refinement_mutation = lambda value: value["shots"][0].update(description="商品静置，办公者只看瓶身。")
    with pytest.raises(ProviderError, match="商品依据与镜头对应仍未完成"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement"]


@pytest.mark.parametrize("foreign", ["fact", "character"])
def test_mapping_error_never_masks_foreign_facts_or_characters(progressive_gateway, foreign):
    state = progressive_gateway
    def broken(value):
        break_mapping(value)
        field = "evidence_ids" if foreign == "fact" else "character_ids"
        value["shots"][1][field].append("foreign_identity")
    state.first_director_mutation = broken
    with pytest.raises(ProviderError):
        state.run()
    assert state.calls == ["ad_script", "storyboard"]


def test_mapping_repair_cannot_rewrite_unaffected_shots(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = break_mapping
    def refine(value):
        fix_mapping(value)
        value["shots"][2]["description"] = "镜头转到另一个场所。"
    state.refinement_mutation = refine
    with pytest.raises(ProviderError, match="未能保留"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement"]


def test_preserving_global_scene_does_not_hide_actual_shot_change_from_review(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    def refine(value):
        merge_first_two(value)
        value["shots"][0]["description"] = "改在陌生厨房，另一件商品放在灶台上。"
    def reject_changed_content(value):
        value["checks"][0]["passed"] = False
        value["checks"][0]["note"] = "实际镜头替换了已选商品与场景。"
        value["ready"] = False
    state.refinement_mutations = [refine, lambda value: None]
    state.first_review_mutation = reject_changed_content
    with pytest.raises(ProviderError):
        state.run()
    review = next(context for context in state.contexts if context["stage"] == "plan_review")
    assert review["final_plan"]["visual_direction"]["scene"] == "办公桌与办公椅"
    assert "陌生厨房" in review["final_plan"]["segments"][0]["shots"][0]["description"]
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_review", "plan_refinement"]


def lower_first_estimate_only(value):
    value["ad_script"]["beats"][0]["timing"].update(visual_seconds=3,
        action_plan="重新估计三秒足够，不改变实际动作。")


@pytest.mark.parametrize("trigger", ["compile", "review"])
def test_estimate_guard_uses_remaining_repair_and_reviews_actual_final_content(progressive_gateway, trigger):
    state = progressive_gateway
    if trigger == "compile":
        state.first_director_mutation = overburden_first_beat
    else:
        state.first_director_mutation = lambda value: value["ad_script"]["beats"][0]["timing"].update(visual_seconds=6)
        state.first_review_mutation = pacing_review(10)
    state.refinement_mutations = [lower_first_estimate_only, simplify_first_beat]
    plan = state.run()
    expected = ["ad_script", "storyboard"] + (["plan_review"] if trigger == "review" else [])
    assert state.calls == expected + ["plan_refinement", "plan_refinement", "plan_review"]
    requests = [context for context in state.contexts if context["stage"] == "plan_refinement"]
    assert [request["repair_attempt"] for request in requests] == [1, 2]
    feedback = requests[1]["review_feedback"]
    assert feedback["kind"] == "unsupported_timing_reduction"
    assert feedback["beats"][0]["beat_id"] == "b0"
    assert feedback["beats"][0]["baseline"]["visual_seconds"] == (21 if trigger == "compile" else 6)
    assert feedback["beats"][0]["candidate"]["visual_seconds"] == 3
    assert requests[1]["draft_storyboard"]["ad_script"]["beats"][0]["timing"]["visual_seconds"] == 3
    assert "final_plan" not in requests[1] and "final_execution" not in requests[1]
    assert "省去重复取放" in state.contexts[-1]["final_plan"]["segments"][0]["shots"][0]["description"]
    assert plan["plan_review"]["ready"] is True


@pytest.mark.parametrize("change_other_content", [False, True])
def test_second_estimate_guard_still_compares_original_before_and_stops(progressive_gateway, change_other_content):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    def second(value):
        # Keeping the rejected candidate's three seconds must not become a
        # valid unchanged estimate simply because it was sent back to author.
        if change_other_content:
            value["shots"][2]["description"] += "省去最后一次重复展示。"
    state.refinement_mutations = [lower_first_estimate_only, second]
    with pytest.raises(ProviderError, match="只缩短了时间估计") as caught:
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_refinement"]
    assert caught.value.repair_attempts == 2
    assert any("第1个内容段" in issue for issue in caught.value.issue_summary)
    assert all("b0" not in issue for issue in caught.value.issue_summary)


def test_guard_feedback_includes_joint_capacity_conflict_in_last_repair(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    def partial(value):
        lower_first_estimate_only(value)
        value["ad_script"]["beats"][1]["timing"].update(visual_seconds=21, speech_overlap=False)
        value["shots"][1]["description"] += "先完成取放，再开口。"
    def complete(value):
        simplify_first_beat(value)
        value["ad_script"]["beats"][1]["timing"].update(visual_seconds=1, speech_overlap=True)
        value["shots"][1]["description"] = "省去取放过程，同一瓶子保持静置，展示桌角关系。"
    state.refinement_mutations = [partial, complete]
    assert state.run()["plan_review"]["ready"] is True
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_refinement", "plan_review"]
    feedback = state.contexts[3]["review_feedback"]
    assert feedback["capacity_issue"] == "完整台词无法为对应画面留出时间，请减少内容节拍或重新策划"
    assert feedback["capacity"]["overrun_seconds"] > 0
    second = feedback["capacity"]["beats"][1]
    assert second["minimum_seconds"] == second["visual_seconds"] + second["speech_seconds"]
    assert feedback["previous_feedback"]["kind"] == "deterministic_timing"


@pytest.mark.parametrize("fatal", ["fact", "character", "fixed_speech", "coverage"])
def test_estimate_guard_cannot_hide_authored_contract_error_behind_capacity(progressive_gateway, fatal):
    state = progressive_gateway
    state.brief = "固定台词“看桌角。”"
    state.first_director_mutation = overburden_first_beat
    def invalid(value):
        lower_first_estimate_only(value)
        value["ad_script"]["beats"][2]["timing"]["visual_seconds"] = 25
        if fatal == "fact":
            value["shots"][1]["evidence_ids"].append("foreign_fact")
        elif fatal == "character":
            value["shots"][1]["character_ids"].append("foreign_character")
        elif fatal == "fixed_speech":
            value["ad_script"]["beats"][1]["narration"] = ""
        else:
            value["shots"][2]["coverage"] = [{"beat_id": "b1", "weight": 1}]
    state.refinement_mutation = invalid
    with pytest.raises(ProviderError, match="已确认要求"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_refinement"]


def test_estimate_guard_does_not_unlock_unaffected_content(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = lambda value: value["ad_script"]["beats"][0]["timing"].update(visual_seconds=6)
    state.first_review_mutation = reject_first_shot
    def invalid(value):
        lower_first_estimate_only(value)
        value["shots"][2]["description"] = "未受影响的末镜也被替换。"
    state.refinement_mutation = invalid
    with pytest.raises(ProviderError, match="未能保留"):
        state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement"]


def test_two_guard_repairs_cannot_skip_failed_independent_review(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = overburden_first_beat
    state.refinement_mutations = [lower_first_estimate_only, simplify_first_beat]
    state.first_review_mutation = reject_first_shot
    with pytest.raises(ProviderError, match="两轮自动调整") as caught:
        state.run()
    assert caught.value.repair_attempts == 2
    assert state.calls == ["ad_script", "storyboard", "plan_refinement", "plan_refinement", "plan_review"]
