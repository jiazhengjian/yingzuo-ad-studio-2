"""Visual review provenance and verdicts; no keyword-based semantic classifier."""
from copy import deepcopy

import pytest

from app.av_review import AVReview, lower_failed_av_verdicts, validate_av_review
from app.plan_review import (
    lower_failed_plan_verdicts, plan_review_fingerprint, plan_review_schema, validate_plan_review,
)
from app.review_recovery import preserve_review_rejections
from app.schemas import Plan
from test_plan_review import plan, review
from test_omission_evidence import cut_plan
from test_progressive_planning import (
    accepted_review, forbid_real_network, install_transport, progressive_gateway, settings, source,
)


def visual_row(review):
    return review["alignment"]["shots"][0]["visual_execution"]


def test_combined_schema_requires_concise_visual_check_but_legacy_av_does_not():
    schema = plan_review_schema()
    shot = schema["$defs"]["AVShotCheck"]
    assert "visual_execution" in shot["required"]
    assert shot["properties"]["visual_execution"] == {"$ref": "#/$defs/AVVisualExecution"}
    visual = schema["$defs"]["AVVisualExecution"]
    assert set(visual["required"]) == {"verdict", "source", "quote", "explanation"}
    assert visual["additionalProperties"] is False
    assert set(visual["properties"]["verdict"]["enum"]) == {"clear", "unclear", "conflict"}
    assert set(visual["properties"]["source"]["enum"]) == {"description", "camera_move"}
    legacy = AVReview.model_json_schema()["$defs"]["AVShotCheck"]
    assert "visual_execution" not in legacy["required"]


@pytest.mark.parametrize("missing", ["field", "null", "verdict", "source", "quote", "explanation"])
def test_fresh_review_requires_actual_visual_check_and_never_fills_it(plan, review, missing):
    shot = review["alignment"]["shots"][0]
    if missing == "field":
        shot.pop("visual_execution")
    elif missing == "null":
        shot["visual_execution"] = None
    else:
        shot["visual_execution"].pop(missing)
    before = deepcopy(review)
    lowered = lower_failed_plan_verdicts(review, plan)
    assert lowered == before and review == before
    with pytest.raises(ValueError):
        validate_plan_review(lowered, plan)


@pytest.mark.parametrize("mutation", ["invented_quote", "other_shot", "purpose_source", "no_camera",
    "empty_quote", "empty_explanation", "long_quote", "long_explanation", "numeric_quote", "unknown_verdict"])
def test_visual_evidence_cannot_borrow_or_invent_the_current_shot(plan, review, mutation):
    row = visual_row(review)
    if mutation == "invented_quote":
        row["quote"] = "镜头从侧面回到明确的左右分屏。"
    elif mutation == "other_shot":
        row["quote"] = plan.segments[1].shots[0].description
    elif mutation == "purpose_source":
        row.update(source="purpose", quote=plan.segments[0].shots[0].purpose)
    elif mutation == "no_camera":
        row["source"] = "camera_move"
    elif mutation == "empty_quote":
        row["quote"] = " "
    elif mutation == "empty_explanation":
        row["explanation"] = " "
    elif mutation == "long_quote":
        row["quote"] = "字" * 241
    elif mutation == "long_explanation":
        row["explanation"] = "字" * 181
    elif mutation == "numeric_quote":
        row["quote"] = 7
    else:
        row["verdict"] = "probably"
    if mutation in ("invented_quote", "other_shot", "no_camera", "long_quote"):
        checked = validate_plan_review(lower_failed_plan_verdicts(review, plan), plan)
        visual = checked.alignment.shots[0].visual_execution
        shot = plan.segments[0].shots[0]
        owned = shot.description
        if shot.continuity is not None and shot.continuity.camera_move:
            owned += shot.continuity.camera_move
        assert visual.quote in owned
        return
    with pytest.raises(ValueError):
        validate_plan_review(lower_failed_plan_verdicts(review, plan), plan)


def test_camera_move_is_checked_at_current_shot_continuity(cut_plan):
    value = deepcopy(cut_plan)
    shot = value["segments"][0]["shots"][0]
    shot["continuity"]["camera_move"] = "摄影机沿桌边平移观察静置商品。"
    review = accepted_review(value)
    visual_row(review).update(source="camera_move", quote="摄影机沿桌边平移观察静置商品。",
        explanation="摄影机平移，商品保持原位，画面如何变化有明确描述。")
    assert validate_plan_review(review, value).ready
    # A valid description quote cannot be mislabeled as camera_move.
    visual_row(review)["quote"] = shot["description"]
    checked = validate_plan_review(review, value)
    assert checked.alignment.shots[0].visual_execution.source == "description"
    assert checked.alignment.shots[0].visual_execution.quote == shot["description"]


@pytest.mark.parametrize("description", [
    "单一画面中，摄影机由侧面缓慢绕到四分之三正面，商品始终保持同一位置。",
    "左右分屏并置：左侧固定正面，右侧固定侧面；右半画面从右边缘滑入后，两边一起停留。",
    "切至商品侧面中景，摄影机锁定，保持单一侧面视图。",
])
def test_explicit_single_composite_and_cut_views_can_carry_clear_verdict(plan, review, description):
    # These are authored reviewer fixtures, not a claim that Python can judge
    # composition semantics. No view-specific vocabulary is denied.
    value = plan.model_dump()
    value["segments"][0]["shots"][0]["description"] = description
    visual_row(review).update(quote=description,
        explanation="离线审核夹具：原稿已明确实际视图及其形成方法。")
    assert validate_plan_review(review, value).ready


@pytest.mark.parametrize("verdict", ["unclear", "conflict"])
def test_negative_visual_execution_lowers_shot_and_totals_without_rewriting(plan, review, verdict):
    visual_row(review).update(verdict=verdict, explanation="视图变化的依据不明确或互相冲突。")
    before, original_plan = deepcopy(review), plan.model_dump()
    with pytest.raises(ValueError, match="对应镜头不能"):
        validate_plan_review(review, plan)
    lowered = lower_failed_plan_verdicts(review, plan)
    assert lowered["ready"] is lowered["alignment"]["ready"] is False
    assert lowered["alignment"]["shots"][0]["passed"] is False
    assert lowered["alignment"]["shots"][1] == before["alignment"]["shots"][1]
    assert visual_row(lowered) == visual_row(before)
    assert lower_failed_plan_verdicts(lowered, plan) == lowered
    assert not validate_plan_review(lowered, plan).ready
    assert review == before and plan.model_dump() == original_plan


def test_clear_visual_execution_never_promotes_an_existing_shot_failure(plan, review):
    review["alignment"]["shots"][0]["passed"] = False
    lowered = lower_failed_plan_verdicts(review, plan)
    assert visual_row(lowered)["verdict"] == "clear"
    assert lowered["alignment"]["shots"][0]["passed"] is False
    assert not validate_plan_review(lowered, plan).ready


def test_historical_bound_review_omits_new_field_without_changing_binding(plan, review):
    bound = validate_plan_review(review, plan).model_dump()
    for row in bound["alignment"]["shots"]:
        row.pop("visual_execution")
    before = deepcopy(bound)
    assert validate_plan_review(bound, plan).model_dump() == before
    assert bound["reviewed_plan_sha256"] == plan_review_fingerprint(plan)
    assert Plan.model_validate({**plan.model_dump(), "plan_review": bound}).plan_review == before
    with pytest.raises(ValueError, match="须逐镜填写"):
        validate_plan_review(bound, plan, require_visual_execution=True)


def test_legacy_av_route_does_not_require_or_invent_visual_execution(plan, review):
    raw = deepcopy(review["alignment"])
    for row in raw["shots"]:
        row.pop("visual_execution")
    assert validate_av_review(raw, plan).model_dump() == raw
    assert lower_failed_av_verdicts(raw, plan) == raw


@pytest.mark.parametrize("replacement", ["clear", "conflict", "missing"])
def test_format_repair_cannot_erase_or_relabel_unclear_visual_execution(plan, review, replacement):
    visual_row(review)["verdict"] = "unclear"
    previous = lower_failed_plan_verdicts(review, plan)
    corrected = deepcopy(previous)
    if replacement == "missing":
        corrected["alignment"]["shots"][0].pop("visual_execution")
    else:
        visual_row(corrected)["verdict"] = replacement
    # Even retaining the boolean rejection cannot erase its explicit reason.
    with pytest.raises(ValueError, match="已有失败依据"):
        preserve_review_rejections(previous, corrected, plan)
    assert preserve_review_rejections(previous, previous, plan) == previous


def test_visual_format_correction_stays_on_same_plan_and_existing_one_call_allowance(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = lambda review: review["alignment"]["shots"][0].pop("visual_execution")
    plan = state.run()
    assert plan["plan_review"]["ready"]
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_review"]
    initial, correction = state.contexts[-2:]
    assert correction["task_kind"] == "plan_review_response_repair"
    assert initial["final_plan"] == correction["final_plan"]
    assert correction["validation_feedback"]
