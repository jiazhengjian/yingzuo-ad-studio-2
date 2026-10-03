"""Pure final AV review contract tests; no settings, state or providers."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.av_review import AVReview, validate_av_review
from app.schemas import Plan


@pytest.fixture
def plan():
    segments = []
    for index, words in enumerate(["三档风速自己选。", "用完挂起来。"]):
        segment_id, beat_id = f"seg_{index}", f"beat_{index}"
        segments.append({
            "id": segment_id, "duration_seconds": 10, "purpose": "呈现已知操作",
            "prompt": "商品操作计划", "narration": words, "reference_asset_ids": ["product"],
            "shots": [{"id": "shot_1", "start": 0, "end": 10,
                       "description": "手按已确认的风速键。" if index == 0 else "用挂环挂起同一吹风机。",
                       "narration": "", "purpose": "演示操作", "beat_id": beat_id}],
            # Span IDs are intentionally local to each segment.
            "speech_spans": [{"id": "speech_1", "beat_id": beat_id, "text": words,
                              "start": 0, "end": 10, "shot_ids": ["shot_1"],
                              "anchors": [{"text": words, "start": 0, "end": 10,
                                           "shot_ids": ["shot_1"], "visual_evidence": "编排器声称已支持这句台词"}]}],
        })
    return Plan.model_validate({
        "title": "使用与收纳", "script": "三档风速自己选。用完挂起来。",
        "segments": segments, "audio_strategy": "native",
        "facts": {"name": "吹风机", "claims": ["三档风速", "自带挂环，可挂起收纳"]},
    })


@pytest.fixture
def review():
    return {"ready": True, "ad_expression_passed": True,
            "summary": "旁白说明已知三档风速，后段画面完成挂起动作，使用与收纳在计划中连贯。",
            "spans": [
                {"segment_id": "seg_0", "span_id": "speech_1", "passed": True,
                 "note": "三档由确认事实和台词说明，按键画面只演示切换动作。"},
                {"segment_id": "seg_1", "span_id": "speech_1", "passed": True,
                 "note": "用完挂起来对应本时段实际挂环挂置动作。"},
            ]}


def test_review_matches_every_actual_span_by_segment_and_id_without_mutating_plan(plan, review):
    before = plan.model_dump()
    result = validate_av_review(review, plan)
    assert result.ready and result.model_dump() == review
    assert validate_av_review(review, before) == result
    assert plan.model_dump() == before
    assert "av_review" not in plan.model_dump()


@pytest.mark.parametrize("invalid", ["missing", "extra", "wrong_segment", "wrong_span", "duplicate"])
def test_review_rejects_incomplete_or_foreign_span_coverage(plan, review, invalid):
    if invalid == "missing":
        review["spans"].pop()
    elif invalid == "extra":
        review["spans"].append({**review["spans"][0], "span_id": "unplanned"})
    elif invalid == "wrong_segment":
        review["spans"][0]["segment_id"] = "other_segment"
    elif invalid == "wrong_span":
        review["spans"][0]["span_id"] = "other_span"
    else:
        review["spans"][1] = deepcopy(review["spans"][0])
    with pytest.raises(ValueError):
        validate_av_review(review, plan)


@pytest.mark.parametrize("ad_passed,span_passed", [(True, True), (True, False), (False, True), (False, False)])
def test_ready_is_exact_conjunction_of_whole_ad_and_each_span(plan, review, ad_passed, span_passed):
    review["ad_expression_passed"] = ad_passed
    review["spans"][1]["passed"] = span_passed
    review["ready"] = ad_passed and span_passed
    assert validate_av_review(review, plan).ready == review["ready"]
    review["ready"] = not review["ready"]
    with pytest.raises(ValidationError, match="结论必须"):
        AVReview.model_validate(review)


@pytest.mark.parametrize("field", ["ready", "ad_expression_passed", "passed"])
@pytest.mark.parametrize("coerced", ["true", 1, 0])
def test_boolean_verdicts_are_strict(review, field, coerced):
    target = review["spans"][0] if field == "passed" else review
    target[field] = coerced
    with pytest.raises(ValidationError):
        AVReview.model_validate(review)


@pytest.mark.parametrize("field,limit", [("summary", 240), ("note", 180)])
@pytest.mark.parametrize("invalid", ["blank", "overlong"])
def test_review_requires_bounded_nonblank_evidence_notes(review, field, limit, invalid):
    target = review["spans"][0] if field == "note" else review
    target[field] = "  " if invalid == "blank" else "字" * (limit + 1)
    with pytest.raises(ValidationError):
        AVReview.model_validate(review)


@pytest.mark.parametrize("level", ["review", "span"])
def test_review_cannot_return_rewritten_content_or_extra_fields(review, level):
    target = review if level == "review" else review["spans"][0]
    target["rewritten_script"] = "无来源的新台词。"
    with pytest.raises(ValidationError):
        AVReview.model_validate(review)


def test_alignment_failure_is_recordable_even_when_planner_claims_support(plan, review):
    value = plan.model_dump()
    value["segments"][1]["shots"][0]["description"] = "风嘴外观静态特写，没有挂环或挂起动作。"
    value["segments"][1]["speech_spans"][0]["anchors"][0]["visual_evidence"] = "已经充分证明用后挂起"
    review["ready"] = False
    review["spans"][1].update(passed=False, note="用完挂起来的时段只有风嘴特写，没有挂起动作或挂置状态。")
    result = validate_av_review(review, value)
    assert not result.ready
    # This tests storage of an independent failure, not an automatic semantic
    # detector. A model must read the actual shot description to find it.
    assert not result.spans[1].passed


def test_empty_span_review_only_matches_plan_without_independent_speech_spans(plan, review):
    review["spans"] = []
    with pytest.raises(ValueError, match="全部旁白时段"):
        validate_av_review(review, plan)
    legacy = plan.model_dump()
    for segment in legacy["segments"]:
        segment["speech_spans"] = None
    assert validate_av_review(review, legacy).ready
