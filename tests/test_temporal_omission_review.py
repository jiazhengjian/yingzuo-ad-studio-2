"""Explicit model omission verdicts are required, conservative and historical."""
import json
from copy import deepcopy

import pytest

from app.av_review import review_transition_boundaries, validate_av_review
from app.plan_review import lower_failed_plan_verdicts, validate_plan_review
from app.progressive_planning import compile_director
from app.schemas import Plan
from test_plan_review import plan, review
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import settings, source


@pytest.fixture
def cut_before_continuous_after(source):
    strategy = chosen_strategy(source)
    draft = director_output(strategy)
    shot = draft["shots"][1]
    shot["description"] = "切点省略此前过程后，人物把一口食物送入口中，咀嚼咽下的过程省略，本镜结束按已咽下记录。"
    shot["continuity"].update(entry_transition="cut", start_state="人物尚未入口。",
        allowed_changes="入口后省略咀嚼吞咽，结束按已咽下记录。", end_state="人物已咽下。")
    draft["shots"][2]["continuity"].update(entry_transition="continuous", start_state="人物已咽下。")
    return compile_director(draft, strategy, 20, "continuity_v1", [source["id"]])[0]


def test_ready_true_downgrade_is_recorded_beside_the_model_body(cut_before_continuous_after, tmp_path):
    from app.config import Settings
    from app.plan_review import lower_failed_plan_verdicts, program_downgrade_reasons, structured_review_reasons
    from app.providers.gateway import _owned_check_reasons, save_failed_planner_output
    subject = cut_before_continuous_after
    checks = accepted_review(subject)
    shot = checks["alignment"]["shots"][1]
    shot["temporal_omission"] = "unsupported"
    shot_id = shot["shot_id"]
    before = deepcopy(checks)
    reasons = program_downgrade_reasons(checks, subject)
    assert checks == before and checks["ready"] is True
    assert any(item == "程序把审核总评从通过改成未通过" for item in reasons)
    assert any(shot_id in item and "unsupported" in item for item in reasons)
    lowered = lower_failed_plan_verdicts(checks, subject)
    combined = []
    for item in [*structured_review_reasons(lowered, subject), *reasons]:
        if item not in combined:
            combined.append(item)
    settings = Settings(data_root=tmp_path)
    save_failed_planner_output(settings, {
        "stage": "plan_review", "raw_output": checks,
        "validation_errors": [{"loc": ["check"], "type": "content_check", "msg": text}
                              for text in _owned_check_reasons(combined)],
    })
    saved = json.loads(next((tmp_path / "diagnostics").glob("*.json")).read_text())
    assert saved["raw_output"]["ready"] is True
    messages = [item.get("msg", "") for item in saved["validation_errors"]]
    assert any("程序把审核总评从通过改成未通过" in item for item in messages)
    assert any(shot_id in item and "时间省略" in item for item in messages)


@pytest.mark.parametrize("verdict", ["unsupported", "unclear"])
def test_cut_before_entry_cannot_override_failed_verdict_for_omission_after_entry(cut_before_continuous_after, verdict):
    subject = cut_before_continuous_after
    checks = accepted_review(subject)
    check = checks["alignment"]["shots"][1]
    check.update(temporal_omission=verdict,
        note="入镜cut只省略入口之前的过程，入口后吞咽没有对应切点，下一镜仍continuous。")
    # This is an explicit independent model verdict, not a keyword inference
    # by application code. All the model's boolean verdicts still say True.
    before = deepcopy(checks)
    assert checks["ready"] and checks["alignment"]["ready"]
    assert all(item["passed"] for item in checks["checks"] + checks["alignment"]["shots"])
    boundaries = review_transition_boundaries(subject)
    assert boundaries[1]["entry_transition"] == "cut"
    assert boundaries[1]["next_entry_transition"] == "continuous"
    lowered = lower_failed_plan_verdicts(checks, subject)
    assert checks == before
    assert lowered["alignment"]["shots"][1]["passed"] is False
    assert lowered["alignment"]["shots"][0] == before["alignment"]["shots"][0]
    assert lowered["alignment"]["ready"] is False and lowered["ready"] is False
    assert lower_failed_plan_verdicts(lowered, subject) == lowered
    assert validate_plan_review(lowered, subject).ready is False
    with pytest.raises(ValueError, match="时间省略缺少"):
        validate_plan_review(checks, subject)


@pytest.mark.parametrize("invalid", ["missing", None, "probably_supported", True])
def test_fresh_omission_verdict_is_required_and_not_filled_from_notes(plan, review, invalid):
    first = review["alignment"]["shots"][0]
    first["note"] = "没有省略，动作已经完成；说明不能代替结构化结论。"
    if invalid == "missing":
        first.pop("temporal_omission")
    else:
        first["temporal_omission"] = invalid
    before = deepcopy(review)
    assert lower_failed_plan_verdicts(review, plan) == before
    with pytest.raises(ValueError):
        validate_plan_review(review, plan)
    assert review == before


def test_bound_historical_and_standalone_av_reviews_remain_readable_without_omission_verdict(plan, review):
    bound = validate_plan_review(review, plan).model_dump()
    for check in bound["alignment"]["shots"]:
        check.pop("temporal_omission")
    before = deepcopy(bound)
    assert validate_plan_review(bound, plan).model_dump() == before
    assert Plan.model_validate({**plan.model_dump(), "plan_review": bound}).plan_review == before
    assert validate_av_review(bound["alignment"], plan).model_dump() == before["alignment"]
    assert lower_failed_plan_verdicts(bound, plan) == before
    with pytest.raises(ValueError, match="逐镜填写时间省略"):
        validate_plan_review(bound, plan, require_temporal_omission=True)


@pytest.mark.parametrize("verdict", ["no_omission", "supported_by_cut"])
def test_positive_model_omission_verdict_is_not_downgraded_by_note_keywords(plan, review, verdict):
    # This legacy bound record predates explicit cut evidence. New supported
    # opinions are exercised against actual cuts in test_omission_evidence.
    review = validate_plan_review(review, plan).model_dump()
    first = review["alignment"]["shots"][0]
    first["temporal_omission"] = verdict
    first["note"] = "核对过省略、未完成和切点问题；实际动作安排可成立。"
    assert lower_failed_plan_verdicts(review, plan) == review
    assert validate_plan_review(review, plan).ready is True


def test_supported_omission_never_promotes_an_explicit_failed_shot(plan, review):
    review = validate_plan_review(review, plan).model_dump()
    review["alignment"]["shots"][0].update(temporal_omission="supported_by_cut", passed=False,
        note="切点支持时间省略，但另有实际画面问题。")
    review["ready"] = review["alignment"]["ready"] = False
    assert lower_failed_plan_verdicts(review, plan) == review
    assert validate_plan_review(review, plan).ready is False


def test_transition_boundaries_preserve_actual_neighbors_across_generation_segments(source):
    strategy = chosen_strategy(source)
    subject = compile_director(director_output(strategy), strategy, 45,
                               "continuity_v1", [source["id"]])[0]
    before = deepcopy(subject)
    boundaries = review_transition_boundaries(subject)
    assert subject == before and len(subject["segments"]) == 2
    expected = [(segment["id"], shot) for segment in subject["segments"] for shot in segment["shots"]]
    assert len(boundaries) == len(expected)
    for index, item in enumerate(boundaries):
        assert (item["segment_id"], item["shot_id"]) == (expected[index][0], expected[index][1]["id"])
        assert item["entry_transition"] == expected[index][1]["continuity"]["entry_transition"]
        assert "description" not in item and "end_state" not in item
        if index + 1 < len(expected):
            assert item["next_shot"] == {"segment_id": expected[index + 1][0], "shot_id": expected[index + 1][1]["id"]}
            assert item["next_entry_transition"] == expected[index + 1][1]["continuity"]["entry_transition"]
        if index:
            assert item["previous_shot"] == {"segment_id": expected[index - 1][0], "shot_id": expected[index - 1][1]["id"]}
    assert boundaries[0]["previous_shot"] is None
    assert boundaries[-1]["next_shot"] is None and boundaries[-1]["next_entry_transition"] is None


def test_transition_boundaries_do_not_invent_missing_historical_transitions(plan):
    before = plan.model_dump()
    boundaries = review_transition_boundaries(plan)
    assert len(boundaries) == 2
    assert all(item["entry_transition"] is None and item["next_entry_transition"] is None for item in boundaries)
    assert plan.model_dump() == before
