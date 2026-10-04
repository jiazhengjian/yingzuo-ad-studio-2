"""Editorial advice is localized metadata, never a replacement for a gate."""
from copy import deepcopy

import pytest

from app.plan_review import (PlanReview, lower_failed_plan_verdicts,
                             plan_review_fingerprint, plan_review_schema,
                             validate_plan_review)
from app.progressive_planning import compile_director
from app.planner_runtime import planner_model_context
from app.review_policy import REVIEW_POLICY_VERSION, review_suggestion_targets
from app.review_recovery import preserve_review_rejections
from app.schemas import Plan
from test_plan_review import plan, review
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import settings, source


def advice(**changes):
    value = {"kind": "shot_variety", "target": "/segments/0/shots/0/description",
             "quote": "手按已确认的风速键。", "note": "可选择补充近景，使按键细节更清楚。"}
    value.update(changes)
    return value


def graded(raw, suggestions=None):
    return {**deepcopy(raw), "policy_version": REVIEW_POLICY_VERSION,
            "suggestions": [] if suggestions is None else deepcopy(suggestions)}


def legacy(raw):
    value = deepcopy(raw)
    value.pop("policy_version", None)
    value.pop("suggestions", None)
    return value


def test_wire_requires_explicit_policy_and_bounded_advice_for_current_text(plan):
    schema = plan_review_schema(plan)
    assert {"policy_version", "suggestions"} <= set(schema["required"])
    assert schema["properties"]["policy_version"]["const"] == 1
    assert schema["properties"]["policy_version"]["type"] == "integer"
    assert "anyOf" not in schema["properties"]["policy_version"]
    suggestions = schema["properties"]["suggestions"]
    assert suggestions["type"] == "array" and suggestions["maxItems"] == 6
    definition = schema["$defs"]["ReviewSuggestion"]
    assert set(definition["required"]) == {"kind", "target", "quote", "note"}
    assert definition["additionalProperties"] is False
    assert set(definition["properties"]["kind"]["enum"]) == {
        "ordinary_wording", "expression_quality", "optional_pacing", "shot_variety", "type_fit", "shot_craft",
        "line_focus"}
    actual = review_suggestion_targets(plan)
    assert set(definition["properties"]["target"]["enum"]) == set(actual)
    assert "/segments/0/shots/0/description" in actual
    assert "/segments/0/shots/0/narration" not in actual  # Empty fields cannot be quoted.
    assert not any("facts" in target or "plan_review" in target for target in actual)
    assert "不代表艺术质量满分" in schema["properties"]["ready"]["description"]


def test_old_bound_review_stays_byte_equivalent_and_cannot_impersonate_live_policy(plan, review):
    old = legacy(review)
    bound = validate_plan_review(old, plan).model_dump()
    assert "suggestions" not in bound and "policy_version" not in bound
    stored = {**plan.model_dump(), "plan_review": deepcopy(bound)}
    assert Plan.model_validate(stored).plan_review == bound
    assert validate_plan_review(bound, plan).model_dump() == bound
    with pytest.raises(ValueError, match="审核分级"):
        validate_plan_review(bound, plan, require_review_policy=True)
    # Old unbound adapters also keep their old contract; only the explicit
    # live boundary requires metadata, without manufacturing an empty list.
    assert validate_plan_review(old, plan).ready
    with pytest.raises(ValueError, match="审核分级"):
        validate_plan_review(old, plan, require_review_policy=True)
    assert old == legacy(review)


@pytest.mark.parametrize("version", [True, False, "1", 1.0, 0, 2, -1])
def test_policy_version_is_the_exact_supported_integer(review, version):
    with pytest.raises(ValueError, match="版本"):
        PlanReview.model_validate({**graded(review), "policy_version": version})


@pytest.mark.parametrize("change", ["missing_policy", "missing_suggestions", "null_policy", "null_suggestions"])
def test_policy_metadata_is_a_pair(review, change):
    raw = graded(review)
    field = "policy_version" if change.endswith("policy") else "suggestions"
    if change.startswith("missing"):
        raw.pop(field)
    else:
        raw[field] = None
    with pytest.raises(ValueError, match="同时保留"):
        PlanReview.model_validate(raw)


def test_advice_keeps_same_subject_and_all_verdicts_without_rewriting(plan, review):
    raw = graded(review, [advice()])
    before = deepcopy(raw), plan.model_dump()
    parsed = validate_plan_review(raw, plan, require_review_policy=True)
    assert parsed.ready
    assert parsed.suggestions[0].model_dump() == advice()
    assert parsed.reviewed_plan_sha256 == plan_review_fingerprint(plan)
    # Attaching advice never changes the content hash or implies edits occurred.
    assert parsed.reviewed_plan_sha256 == validate_plan_review(legacy(review), plan).reviewed_plan_sha256
    assert parsed.checks == PlanReview.model_validate(review).checks
    stored = {**plan.model_dump(), "plan_review": parsed.model_dump()}
    assert Plan.model_validate(stored).plan_review == parsed.model_dump()
    assert (raw, plan.model_dump()) == before


@pytest.mark.parametrize("changes", [
    {"kind": "factual_support"}, {"kind": "action_capacity"},
    {"target": "/facts/name", "quote": "吹风机"},
    {"target": "/segments/0/shots/0/end", "quote": "10"},
    {"target": "/segments/0/shots/0/../description"},
    {"target": "/segments/00/shots/0/description"},
    {"target": "/segments/-1/shots/0/description"},
    {"target": "/segments/9/shots/0/description"},
    {"target": "/ad_script/beats/0/narration"},
    {"target": "/segments/0/shots/0/narration"},
    {"target": "/segments/1/shots/0/description"},  # A real path, wrong quoted field.
    {"quote": "按下风速键"}, {"quote": "手按风速键。"},
    {"quote": "手按已确认的风速键。 "}, {"quote": "  "},
    {"quote": ""}, {"quote": None}, {"quote": 123},
    {"quote": "字" * 241}, {"note": " "}, {"note": "字" * 301},
    {"passed": True}, {"fixed": True},
])
def test_advice_cannot_claim_arbitrary_issues_or_unlocated_rewrites(plan, review, changes):
    raw = graded(review, [advice(**changes)])
    before = deepcopy(raw)
    with pytest.raises(ValueError):
        validate_plan_review(raw, plan, require_review_policy=True)
    assert raw == before


def test_advice_is_bounded_and_duplicate_rewording_is_not_a_new_suggestion(plan, review):
    with pytest.raises(ValueError):
        validate_plan_review(graded(review, [advice()] * 7), plan)
    with pytest.raises(ValueError, match="不能重复"):
        validate_plan_review(graded(review, [advice(), advice(note="另一句相同位置的建议。")]), plan)


def test_beat_and_shot_pointers_resolve_current_compiled_content(source):
    strategy = chosen_strategy(source)
    current = compile_director(director_output(strategy), strategy, 20,
                               "continuity_v1", [source["id"]])[0]
    targets = review_suggestion_targets(current)
    chosen = ["/ad_script/beats/0/narration", "/ad_script/beats/1/visual_role",
              "/ad_script/beats/2/timing/action_plan",
              "/segments/0/shots/0/continuity/camera_move"]
    raw = graded(accepted_review(current), [
        advice(kind="expression_quality", target=target, quote=targets[target]) for target in chosen])
    assert validate_plan_review(raw, current, require_review_policy=True).ready
    assert review_suggestion_targets(Plan.model_validate(current)) == targets
    assert all(suggestion["quote"] == targets[suggestion["target"]] for suggestion in raw["suggestions"])


def test_advice_targets_exist_in_the_actual_compacted_model_input(source):
    strategy = chosen_strategy(source)
    current = compile_director(director_output(strategy), strategy, 20,
                               "continuity_v1", [source["id"]])[0]
    before = deepcopy(current)
    payload = planner_model_context({"stage": "plan_review", "final_plan": current,
                                     "schema": plan_review_schema(current)})
    assert "script" not in payload["final_plan"]
    targets = payload["schema"]["$defs"]["ReviewSuggestion"]["properties"]["target"]["enum"]
    assert "/script" not in targets
    assert set(targets) == set(review_suggestion_targets(payload["final_plan"]))
    assert current == before


@pytest.mark.parametrize("failure", ["content", "execution", "span", "shot", "timing", "cut"])
def test_advice_never_promotes_any_existing_explicit_failure(plan, review, failure):
    raw = graded(review, [advice()])
    raw["ready"] = False
    if failure == "content":
        raw["checks"][4].update(passed=False, note="台词实际含义不完整，必须阻断。")
    elif failure == "execution":
        raw["execution_check"].update(action_capacity=False, note="实际动作超出当前容量。")
    else:
        raw["alignment"]["ready"] = False
        if failure == "span":
            raw["alignment"]["spans"][0].update(passed=False, note="实际说话时段不相容。")
        else:
            shot = raw["alignment"]["shots"][0]
            shot.update(passed=False, note="实际镜头执行存在阻断。")
            if failure == "timing":
                shot["pacing"].update(action_timing_passed=False, note="镜头实际时长不够。")
            elif failure == "cut":
                shot["pacing"].update(cut_necessity_passed=False, note="换镜破坏必要连续动作。")
    lowered = lower_failed_plan_verdicts(raw, plan)
    assert not validate_plan_review(lowered, plan, require_review_policy=True).ready
    corrected = graded(review, [advice(kind="ordinary_wording", note="仅作为可选建议，不应返修。")])
    with pytest.raises(ValueError, match="已有失败"):
        preserve_review_rejections(raw, corrected, plan)


def test_numeric_overrun_and_missing_coverage_cannot_be_hidden_in_advice(plan, review):
    raw = graded(review, [advice(kind="optional_pacing")])
    raw["alignment"]["shots"][0]["pacing"]["action_seconds_required"] = 11
    checked = validate_plan_review(lower_failed_plan_verdicts(raw, plan), plan, require_review_policy=True)
    assert not checked.ready
    assert checked.alignment.shots[0].pacing.action_seconds_required == 11
    assert checked.suggestions[0].model_dump() == advice(kind="optional_pacing")
    incomplete = graded(review, [advice()])
    incomplete["alignment"]["shots"].pop()
    with pytest.raises(ValueError):
        validate_plan_review(lower_failed_plan_verdicts(incomplete, plan), plan, require_review_policy=True)
