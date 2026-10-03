"""A provider cannot opt into legacy review exemptions by returning a binding."""
from copy import deepcopy

import pytest

from app.plan_review import validate_plan_review
from app.progressive_planning import compile_director
from app.providers import ProviderError
from test_progressive_planning import (
    director_output, forbid_real_network, install_transport, progressive_gateway,
    settings, source,
)


@pytest.mark.parametrize("correction_complete", [True, False])
def test_provider_bound_legacy_review_must_complete_visual_checks_before_delivery(
        progressive_gateway, source, correction_complete):
    state = progressive_gateway
    original_subject = compile_director(director_output(state.strategy), state.strategy,
        state.duration, "continuity_v1", [source["id"]])[0]
    returned_records = []

    def copied_legacy_record(raw):
        bound = validate_plan_review(raw, original_subject).model_dump()
        for shot in bound["alignment"]["shots"]:
            shot.pop("visual_execution")
        # This really is readable historical data with a matching binding;
        # the new provider boundary, rather than a bad hash, must reject it.
        historical = validate_plan_review(bound, original_subject)
        assert historical.ready
        assert all(shot.visual_execution is None for shot in historical.alignment.shots)
        raw.clear()
        raw.update(bound)
        returned_records.append(deepcopy(raw))

    state.first_review_mutation = copied_legacy_record
    if correction_complete:
        plan = state.run()
        assert plan["plan_review"]["ready"]
        assert all(shot["visual_execution"]["verdict"] == "clear"
                   for shot in plan["plan_review"]["alignment"]["shots"])
        assert plan["plan_review"]["reviewed_plan_sha256"] == returned_records[0]["reviewed_plan_sha256"]
    else:
        state.second_review_mutation = copied_legacy_record
        with pytest.raises(ProviderError, match="完整检查尚未完成"):
            state.run()
        assert len(returned_records) == 2

    # Never deliver the first apparently ready review and never regenerate
    # content: use only the existing one-call review-format repair allowance.
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_review"]
    initial, correction = state.contexts[-2:]
    assert correction["task_kind"] == "plan_review_response_repair"
    assert correction["final_plan"] == initial["final_plan"]
    assert correction["previous_review"] == returned_records[0]
    assert any("视图" in issue["message"] for issue in correction["validation_feedback"])
    assert all("visual_execution" not in shot for shot in returned_records[0]["alignment"]["shots"])
