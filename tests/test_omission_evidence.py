"""Cut support must locate real adjacent pictures, not an author's assurance."""
from copy import deepcopy

import pytest

from app.av_review import validate_av_review
from app.plan_review import (lower_failed_plan_verdicts, plan_review_fingerprint,
                             plan_review_schema, validate_plan_review)
from app.progressive_planning import compile_director
from app.review_recovery import preserve_review_rejections
from app.schemas import Plan
from test_plan_review import plan, review
from test_progressive_planning import accepted_review, chosen_strategy, director_output
from test_providers import settings, source


@pytest.fixture
def cut_plan(source):
    strategy = chosen_strategy(source)
    draft = director_output(strategy)
    draft["shots"][0]["description"] = "办公者握住桌上瓶盖，开始旋开。"
    draft["shots"][0]["continuity"]["end_state"] = "办公者正旋开瓶盖。"
    draft["shots"][1]["description"] = "稍后，瓶盖已放在瓶旁，办公者手离开瓶口。"
    draft["shots"][1]["continuity"].update(entry_transition="cut",
        start_state="瓶盖已取下放在瓶旁。", end_state="瓶盖已取下放在瓶旁。")
    draft["shots"][2]["continuity"].update(start_state="瓶盖已取下放在瓶旁。",
        end_state="瓶盖已取下放在瓶旁。")
    return compile_director(draft, strategy, 20, "continuity_v1", [source["id"]])[0]


def support(subject, *, row=1, boundary="entry"):
    checks = accepted_review(subject)
    ordered = [shot for segment in subject["segments"] for shot in segment["shots"]]
    before = row - 1 if boundary == "entry" else row
    checks["alignment"]["shots"][row].update(temporal_omission="supported_by_cut",
        temporal_omission_evidence={"boundary": boundary, "omitted_action": "将瓶盖继续旋开并放到瓶旁。",
            "before_description_quote": ordered[before]["description"],
            "after_description_quote": ordered[before + 1]["description"]})
    return checks


@pytest.mark.parametrize("row,boundary", [(1, "entry"), (0, "exit")])
def test_supported_cut_binds_actual_neighbors_without_rewriting_plan(cut_plan, row, boundary):
    raw = support(cut_plan, row=row, boundary=boundary)
    originals = deepcopy(cut_plan), deepcopy(raw)
    checked = validate_plan_review(raw, cut_plan)
    assert checked.ready
    assert checked.reviewed_plan_sha256 == plan_review_fingerprint(cut_plan)
    assert validate_plan_review(checked, cut_plan) == checked
    assert (cut_plan, raw) == originals


def test_fresh_support_requires_evidence_even_when_shot_already_failed(cut_plan):
    raw = support(cut_plan)
    raw["alignment"]["shots"][1].pop("temporal_omission_evidence")
    raw["alignment"]["shots"][1]["passed"] = False
    lowered = lower_failed_plan_verdicts(raw, cut_plan)
    assert lowered["ready"] is False
    with pytest.raises(ValueError, match="supported_by_cut须提供"):
        validate_plan_review(lowered, cut_plan)


@pytest.mark.parametrize("mutation", ["first_entry", "last_exit", "continuous", "wrong_boundary",
    "wrong_before_quote", "wrong_after_quote", "purpose_quote", "extra_identity"])
def test_support_cannot_borrow_missing_wrong_or_unquoted_cut(cut_plan, mutation):
    raw = support(cut_plan)
    check = raw["alignment"]["shots"][1]
    evidence = check["temporal_omission_evidence"]
    if mutation == "first_entry":
        raw["alignment"]["shots"][0].update(temporal_omission="supported_by_cut",
            temporal_omission_evidence=deepcopy(evidence))
    elif mutation == "last_exit":
        raw["alignment"]["shots"][-1].update(temporal_omission="supported_by_cut",
            temporal_omission_evidence={**evidence, "boundary": "exit"})
    elif mutation == "continuous":
        shots = cut_plan["segments"][0]["shots"]
        shots[1]["continuity"].update(entry_transition="continuous",
            start_state=shots[0]["continuity"]["end_state"])
    elif mutation == "wrong_boundary":
        evidence["boundary"] = "exit"
    elif mutation == "wrong_before_quote":
        evidence["before_description_quote"] = "作者没有写过的前态"
    elif mutation == "wrong_after_quote":
        evidence["after_description_quote"] = "作者没有写过的后态"
    elif mutation == "purpose_quote":
        evidence["before_description_quote"] = cut_plan["segments"][0]["shots"][0]["purpose"]
    else:
        evidence["shot_id"] = "a_different_shot"
    originals = deepcopy(raw), deepcopy(cut_plan)
    if mutation == "continuous":
        with pytest.raises(ValueError):
            validate_plan_review(raw, cut_plan)
        assert (raw, cut_plan) == originals
        return
    checked = validate_plan_review(raw, cut_plan)
    assert (raw, cut_plan) == originals
    assert checked.ready
    if mutation == "extra_identity":
        assert checked.alignment.shots[1].temporal_omission == "supported_by_cut"
    elif mutation == "first_entry":
        assert checked.alignment.shots[0].temporal_omission == "no_omission"
    elif mutation == "last_exit":
        assert checked.alignment.shots[-1].temporal_omission == "no_omission"
    else:
        assert checked.alignment.shots[1].temporal_omission == "no_omission"


def test_support_cannot_use_a_cut_from_another_generation_segment(source):
    strategy = chosen_strategy(source)
    draft = director_output(strategy)
    draft["shots"][1]["continuity"]["entry_transition"] = "cut"
    subject = compile_director(draft, strategy, 45, "continuity_v1", [source["id"]])[0]
    assert len(subject["segments"]) == 2
    raw = support(subject)
    assert validate_plan_review(raw, subject).ready
    # The last shot uses the actual preceding continuous boundary, not the
    # earlier cut, even when it is stored in another generation segment.
    evidence = raw["alignment"]["shots"][1].pop("temporal_omission_evidence")
    raw["alignment"]["shots"][1]["temporal_omission"] = "no_omission"
    raw["alignment"]["shots"][-1].update(temporal_omission="supported_by_cut",
        temporal_omission_evidence=evidence)
    checked = validate_plan_review(raw, subject)
    assert checked.alignment.shots[-1].temporal_omission == "no_omission"


def test_real_cut_across_segments_uses_segment_and_local_shot_identity(source):
    strategy = chosen_strategy(source)
    draft = director_output(strategy)
    draft["shots"][2]["continuity"]["entry_transition"] = "cut"
    subject = compile_director(draft, strategy, 45, "continuity_v1", [source["id"]])[0]
    assert len(subject["segments"]) == 2
    final_segment = subject["segments"][-1]
    previous_id = final_segment["shots"][0]["id"]
    repeated_id = subject["segments"][0]["shots"][0]["id"]
    final_segment["shots"][0]["id"] = repeated_id
    for span in final_segment["speech_spans"]:
        span["shot_ids"] = [repeated_id if value == previous_id else value for value in span["shot_ids"]]
        for anchor in span["anchors"]:
            anchor["shot_ids"] = [repeated_id if value == previous_id else value for value in anchor["shot_ids"]]
    raw = support(subject, row=2)
    assert validate_plan_review(raw, subject).ready
    # The same local ID in the first segment does not change which cut is
    # referenced by the last segment's row.
    raw["alignment"]["shots"][2]["temporal_omission_evidence"]["boundary"] = "exit"
    checked = validate_plan_review(raw, subject)
    assert checked.alignment.shots[2].temporal_omission == "no_omission"


def test_no_omission_needs_no_new_field_and_retains_plan_fingerprint(plan, review):
    before = deepcopy(review), plan.model_dump(), plan_review_fingerprint(plan)
    checked = validate_plan_review(review, plan)
    assert all("temporal_omission_evidence" not in row for row in checked.model_dump()["alignment"]["shots"])
    assert (review, plan.model_dump(), checked.reviewed_plan_sha256) == before


def test_bound_history_missing_cut_evidence_stays_readable_but_explicit_recheck_requires_it(cut_plan):
    bound = validate_plan_review(support(cut_plan), cut_plan).model_dump()
    bound["alignment"]["shots"][1].pop("temporal_omission_evidence")
    original = deepcopy(bound)
    assert validate_plan_review(bound, cut_plan).model_dump() == original
    assert Plan.model_validate({**cut_plan, "plan_review": bound}).plan_review == original
    assert validate_av_review(bound["alignment"], cut_plan).model_dump() == bound["alignment"]
    with pytest.raises(ValueError, match="supported_by_cut须提供"):
        validate_plan_review(bound, cut_plan, require_temporal_omission_evidence=True)
    assert bound == original


@pytest.mark.parametrize("negative", ["unsupported", "unclear"])
def test_format_repair_cannot_upgrade_negative_omission_with_new_evidence(cut_plan, negative):
    corrected = support(cut_plan)
    previous = deepcopy(corrected)
    previous["alignment"]["shots"][1].pop("temporal_omission_evidence")
    previous["alignment"]["shots"][1]["temporal_omission"] = negative
    corrected = lower_failed_plan_verdicts(corrected, cut_plan)
    corrected["ready"] = corrected["alignment"]["ready"] = False
    corrected["alignment"]["shots"][1]["passed"] = False
    with pytest.raises(ValueError, match="已有失败依据"):
        preserve_review_rejections(previous, corrected, cut_plan)


def test_missing_positive_evidence_can_be_completed_without_inventing_a_failure(cut_plan):
    corrected = support(cut_plan)
    previous = deepcopy(corrected)
    previous["alignment"]["shots"][1].pop("temporal_omission_evidence")
    result = preserve_review_rejections(previous, corrected, cut_plan)
    assert validate_plan_review(result, cut_plan).ready
    assert "temporal_omission_evidence" not in previous["alignment"]["shots"][1]


@pytest.mark.parametrize("verdict", ["no_omission", "unsupported", "unclear", None])
def test_cut_support_object_does_not_belong_to_another_verdict(cut_plan, verdict):
    raw = support(cut_plan)
    raw["alignment"]["shots"][1]["temporal_omission"] = verdict
    with pytest.raises(ValueError):
        validate_plan_review(lower_failed_plan_verdicts(raw, cut_plan), cut_plan)


def test_wire_only_requires_evidence_when_claiming_supported_cut():
    schema = plan_review_schema()
    shot = schema["$defs"]["AVShotCheck"]
    assert "temporal_omission_evidence" not in shot["required"]
    conditional = shot["allOf"][0]
    assert conditional["if"]["properties"]["temporal_omission"] == {"const": "supported_by_cut"}
    assert conditional["then"]["required"] == ["temporal_omission_evidence"]
    evidence = schema["$defs"]["AVTemporalOmissionEvidence"]
    assert set(evidence["required"]) == {"boundary", "omitted_action", "before_description_quote", "after_description_quote"}
