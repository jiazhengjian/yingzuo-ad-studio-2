"""Combined review contracts and offline reference/confirmation lifecycle."""
from copy import deepcopy

import pytest

from app.ad_script import CHECK_CRITERIA
from app.plan_review import (
    PlanReview, lower_failed_plan_verdicts, plan_review_fingerprint,
    plan_review_schema, validate_plan_review,
)
from app.schemas import Plan


@pytest.fixture
def plan():
    segments = []
    for index, words in enumerate(["三档风速自己选。", "用完挂起来。"]):
        segments.append({
            "id": f"seg_{index}", "duration_seconds": 10, "purpose": "呈现已知操作",
            "prompt": "商品操作计划", "narration": words, "reference_asset_ids": ["product"],
            "shots": [{"id": "shot_1", "start": 0, "end": 10,
                "description": "手按已确认的风速键。" if index == 0 else "用挂环挂起同一吹风机。",
                "narration": "", "purpose": "演示操作", "beat_id": f"beat_{index}"}],
            "speech_spans": [{"id": "speech_1", "beat_id": f"beat_{index}", "text": words,
                "start": 0, "end": 10, "shot_ids": ["shot_1"], "anchors": [
                    {"text": words, "start": 0, "end": 10, "shot_ids": ["shot_1"],
                     "visual_evidence": "编排器提供的待审画面说明"}]}],
        })
    return Plan.model_validate({"title": "使用与收纳", "script": "三档风速自己选。用完挂起来。",
        "segments": segments, "audio_strategy": "native",
        "facts": {"name": "吹风机", "claims": ["三档风速", "自带挂环，可挂起收纳"]}})


@pytest.fixture
def review(plan):
    return {
        "ready": True,
        "checks": [{"criterion": criterion, "passed": True,
                    "note": "最终台词与操作画面共同说明已知风速选择和挂起方式。"}
                   for criterion in CHECK_CRITERIA],
        "execution_check": {"action_capacity": True, "state_continuity": True,
                            "note": "按键与挂起动作有完整窗口，前后保持同一商品状态。"},
        "alignment": {
            "ready": True, "ad_expression_passed": True,
            "summary": "最终计划依次呈现风速选择和用后挂起。",
            "spans": [{"segment_id": segment.id, "span_id": span.id, "passed": True,
                       "speaker_visibility": "not_applicable",
                       "note": "完整台词与当前操作画面相容。"}
                      for segment in plan.segments for span in segment.speech_spans],
            "shots": [{"segment_id": segment.id, "shot_id": shot.id, "passed": True,
                "visual_execution": {"verdict": "clear", "source": "description",
                    "quote": shot.description[:240], "explanation": "离线夹具明确审查当前镜头，实际画面与摄影变化相容。"},
                "temporal_omission": "no_omission",
                "note": "实际操作和停留位于对应时窗。", "pacing": {
                    "action_seconds_required": 2, "action_timing_passed": True,
                    "cut_necessity_passed": True, "note": "动作加辨认估计两秒；段首没有实际切镜。"}}
                for segment in plan.segments for shot in segment.shots],
        },
    }


def test_wire_schema_requires_complete_combined_review_without_copy_or_server_hash():
    schema = plan_review_schema()
    assert set(schema["properties"]) == {"ready", "checks", "execution_check", "alignment", "policy_version", "suggestions"}
    assert set(schema["required"]) == set(schema["properties"])
    assert "shots" in schema["$defs"]["AVReview"]["required"]
    assert "pacing" in schema["$defs"]["AVShotCheck"]["required"]
    assert "speaker_visibility" in schema["$defs"]["AVSpanCheck"]["required"]
    visibility = schema["$defs"]["AVSpanCheck"]["properties"]["speaker_visibility"]
    assert visibility["type"] == "string"
    assert set(visibility["enum"]) == {"visible_speaking", "off_screen", "mixed", "unclear", "not_applicable"}
    assert "anyOf" not in visibility and "default" not in visibility
    shot = schema["$defs"]["AVShotCheck"]
    assert "temporal_omission" in shot["required"]
    omission = shot["properties"]["temporal_omission"]
    assert omission["type"] == "string"
    assert set(omission["enum"]) == {"no_omission", "supported_by_cut", "unsupported", "unclear"}
    assert "anyOf" not in omission and "default" not in omission
    assert schema["additionalProperties"] is False


def test_review_binds_normalized_final_plan_without_rewriting_or_legacy_passes(plan, review):
    before, raw = plan.model_dump(), deepcopy(review)
    result = validate_plan_review(review, plan)
    assert result.ready and result.reviewed_plan_sha256 == plan_review_fingerprint(plan)
    assert result.reviewed_reference_asset_ids == {segment.id: segment.reference_asset_ids for segment in plan.segments}
    assert result == validate_plan_review(review, before)
    assert plan.model_dump() == before and review == raw
    stored = {**before, "plan_review": result.model_dump()}
    parsed = Plan.model_validate(stored)
    assert parsed.plan_review == result.model_dump()
    assert parsed.script_review is None and parsed.alignment_review is None
    assert "script" not in parsed.plan_review
    assert validate_plan_review(parsed.plan_review, parsed) == result


def test_old_plan_can_still_omit_all_new_review_fields(plan):
    assert "plan_review" not in plan.model_dump()
    assert Plan.model_validate(plan.model_dump()) == plan


def test_stored_review_requires_server_binding(plan, review):
    with pytest.raises(ValueError, match="服务端绑定"):
        Plan.model_validate({**plan.model_dump(), "plan_review": review})


@pytest.mark.parametrize("change", ["script", "shot", "anchor", "prompt", "fact", "reference", "title"])
def test_changed_plan_cannot_reuse_bound_review_even_if_ids_stay_identical(plan, review, change):
    bound = validate_plan_review(review, plan).model_dump()
    value = plan.model_dump()
    if change == "script":
        words = "三档风速按需选。"
        segment = value["segments"][0]
        value["script"] = words + value["segments"][1]["narration"]
        segment["narration"] = segment["speech_spans"][0]["text"] = words
        segment["speech_spans"][0]["anchors"][0]["text"] = words
    elif change == "shot":
        value["segments"][0]["shots"][0]["description"] = "只拍风嘴外观，没有按键操作。"
    elif change == "anchor":
        value["segments"][0]["speech_spans"][0]["anchors"][0]["start"] = 1
    elif change == "prompt":
        value["segments"][0]["prompt"] = "另一种画面说明"
    elif change == "fact":
        value["facts"]["claims"].append("另一条商家事实")
    elif change == "reference":
        value["segments"][0]["reference_asset_ids"] = ["other_product"]
    else:
        value["title"] = "另一版使用与收纳"
    Plan.model_validate(value)  # The content edit itself remains structurally valid.
    with pytest.raises(ValueError, match="当前最终方案"):
        validate_plan_review(bound, value)
    with pytest.raises(ValueError, match="当前最终方案"):
        Plan.model_validate({**value, "plan_review": bound})


def test_review_and_trace_metadata_do_not_change_subject_identity(plan, review):
    value = plan.model_dump()
    bound = validate_plan_review(review, value)
    value.update(plan_review=bound.model_dump(),
                 alignment_review={"historical_note": "仅保留历史记录"},
                 skill_trace=[{"stage": "plan_review", "attempt": 1}])
    assert plan_review_fingerprint(value) == bound.reviewed_plan_sha256
    assert Plan.model_validate(value).plan_review == bound.model_dump()


def test_later_scene_and_character_references_preserve_original_review_without_rebinding(plan, review):
    value = plan.model_dump()
    bound = validate_plan_review(review, value).model_dump()
    value["plan_review"] = deepcopy(bound)
    for segment in value["segments"]:
        segment["reference_asset_ids"].extend(["scene_fixture", "character_fixture"])
    before = deepcopy(value)
    assert Plan.model_validate(value).plan_review == bound
    assert validate_plan_review(bound, value).model_dump() == bound
    assert plan_review_fingerprint(value) == bound["reviewed_plan_sha256"]
    assert value == before
    # Replaced/regenerated supplements are outside the text-plan review. Their
    # existence, ownership and approval belong to the existing picture gate.
    for segment in value["segments"]:
        segment["reference_asset_ids"] = ["product", "replacement_scene", "replacement_character"]
    assert Plan.model_validate(value).plan_review == bound
    for segment in value["segments"]:
        segment["reference_asset_ids"] = ["product"]
    assert Plan.model_validate(value).plan_review == bound


@pytest.mark.parametrize("change", ["replace", "drop", "reorder", "prepend", "insert", "snapshot_missing",
    "snapshot_shorter", "snapshot_extended", "snapshot_foreign_segment", "snapshot_missing_segment"])
def test_appended_media_cannot_mask_an_original_reference_or_binding_change(plan, review, change):
    value = plan.model_dump()
    for segment in value["segments"]:
        segment["reference_asset_ids"] = ["product_front", "product_back"]
    bound = validate_plan_review(review, value).model_dump()
    value["plan_review"] = deepcopy(bound)
    for segment in value["segments"]:
        segment["reference_asset_ids"].append("scene_fixture")
    references = value["segments"][0]["reference_asset_ids"]
    if change == "replace":
        references[0] = "other_product"
    elif change == "drop":
        references.pop(0)
    elif change == "reorder":
        references[:2] = reversed(references[:2])
    elif change == "prepend":
        references.insert(0, "other_product")
    elif change == "insert":
        references.insert(1, "other_product")
    elif change == "snapshot_missing":
        value["plan_review"].pop("reviewed_reference_asset_ids")
    else:
        snapshot = value["plan_review"]["reviewed_reference_asset_ids"]
        segment_id = value["segments"][0]["id"]
        if change == "snapshot_shorter":
            snapshot[segment_id].pop()
        elif change == "snapshot_extended":
            snapshot[segment_id].append("scene_fixture")
        elif change == "snapshot_foreign_segment":
            snapshot["foreign_segment"] = snapshot.pop(segment_id)
        else:
            snapshot.pop(segment_id)
    with pytest.raises(ValueError):
        Plan.model_validate(value)


@pytest.mark.parametrize("field", ["reviewed_reference_asset_ids", "reviewed_plan_sha256"])
def test_partial_binding_is_rejected_instead_of_derived_from_the_current_plan(plan, review, field):
    bound = validate_plan_review(review, plan).model_dump()
    bound.pop(field)
    with pytest.raises(ValueError, match="同时保留"):
        validate_plan_review(bound, plan)


def test_reference_additions_do_not_exempt_changed_content_from_reapproval(plan, review):
    value = plan.model_dump()
    value["plan_review"] = validate_plan_review(review, value).model_dump()
    for segment in value["segments"]:
        segment["reference_asset_ids"].append("scene_fixture")
    value["segments"][0]["shots"][0]["description"] = "只拍商品底部，没有原计划的按键操作。"
    with pytest.raises(ValueError, match="当前最终方案"):
        Plan.model_validate(value)


def test_reviewed_plan_survives_real_offline_design_resume_and_current_picture_confirmation(tmp_path, plan, review):
    """Exercise the actual Store normalization and Pipeline validation, offline.

    All media are tiny local fixtures; an object without provider methods is
    enough to resume saved designs. This test never creates a supplier client.
    """
    import asyncio

    from PIL import Image

    from app.db import Store
    from app.pipeline import Pipeline, validate_plan, video_submission_spec

    store = Store(tmp_path / "lifecycle.sqlite3", tmp_path / "media")
    project = store.create_project("离线确认图片生命周期", "product", duration_seconds=20)
    product_path = store.media_root / "product_fixture.png"
    Image.new("RGB", (8, 8), "white").save(product_path)
    product = store.add_asset(project["id"], "product", "商品原图夹具", product_path, "image/png")
    value = plan.model_dump()
    for segment in value["segments"]:
        segment["reference_asset_ids"] = [product["id"]]
    bound = validate_plan_review(review, value).model_dump()
    value["plan_review"] = bound
    project = store.update_plan(project["id"], value)
    job = store.create_job(project["id"], "generate", project["revision"], {"pause_for_creative": True})
    assert store.claim_job(job["id"])

    scene_path = store.media_root / "scene_fixture.png"
    Image.new("RGB", (8, 8), "gray").save(scene_path)
    scene = store.add_asset(project["id"], "scene", "已保存场景夹具", scene_path, "image/png", metadata={"generated": True})
    payload = store.get_job(job["id"])["payload"]
    payload["design_asset_ids"] = [scene["id"]]
    store.update_job(job["id"], payload=payload)

    pipeline = Pipeline(store, object(), None)
    prepared = asyncio.run(pipeline._designs(store.get_job(job["id"]), value))
    assert all(segment["reference_asset_ids"] == [product["id"], scene["id"]] for segment in prepared["segments"])
    assert validate_plan(prepared, 20)["plan_review"] == bound
    gate = store.create_creative_gate(job["id"], [{"id": "current", "label": "保持当前图片",
        "description": "确认离线场景图夹具", "segment": prepared["segments"][0]}], "确认图片")
    confirmed = store.confirm_creative(project["id"], {"client_turn_id": "confirm-fixture",
        "gate_id": gate["id"], "accepted_revision": project["revision"], "option_id": "current"})
    public_plan = store.get_project(project["id"])["plan"]
    run_plan = confirmed["payload"]["plan"]
    assert all(segment["reference_asset_ids"] == [product["id"]] for segment in public_plan["segments"])
    assert all(segment["reference_asset_ids"] == [product["id"], scene["id"]] for segment in run_plan["segments"])
    assert Plan.model_validate(public_plan).plan_review == bound
    assert validate_plan(run_plan, 20)["plan_review"] == bound
    assert confirmed["payload"]["creative_confirmed"] and confirmed["status"] == "queued"
    assert store.claim_job(confirmed["id"])
    resumed = asyncio.run(pipeline._designs(store.get_job(confirmed["id"]), run_plan))
    assert validate_plan(resumed, 20)["plan_review"] == bound
    spec = video_submission_spec(resumed, resumed["segments"][0], [product, scene])
    assert spec["reference_asset_ids"] == [product["id"], scene["id"]]
    assert [item["role"] for item in spec["reference_roles"]] == ["product", "scene"]
    assert resumed["plan_review"] == bound and "reference_roles" not in resumed["segments"][0]


@pytest.mark.parametrize("failure", ["content", "action_capacity", "state_continuity", "ad", "span", "shot"])
def test_independent_failures_remain_recordable_and_ready_is_exact_conjunction(plan, review, failure):
    if failure == "content":
        review["checks"][0]["passed"] = False
    elif failure in {"action_capacity", "state_continuity"}:
        review["execution_check"][failure] = False
    else:
        review["alignment"]["ready"] = False
        if failure == "ad":
            review["alignment"]["ad_expression_passed"] = False
        else:
            review["alignment"]["spans" if failure == "span" else "shots"][0]["passed"] = False
    with pytest.raises(ValueError, match="综合审核结论"):
        validate_plan_review(review, plan)
    review["ready"] = False
    result = validate_plan_review(review, plan)
    assert not result.ready
    stored = Plan.model_validate({**plan.model_dump(), "plan_review": result.model_dump()})
    assert stored.plan_review["ready"] is False


@pytest.mark.parametrize("invalid", ["missing_check", "duplicate_check", "missing_execution", "missing_alignment",
    "missing_shot", "foreign_shot", "duplicate_shot", "missing_span", "foreign_span", "duplicate_span",
    "missing_pacing", "false_pacing", "action_overrun", "rewritten_script", "blank_note", "long_note"])
def test_combined_review_retains_all_content_coverage_and_action_guards(plan, review, invalid):
    alignment = review["alignment"]
    if invalid == "missing_check":
        review["checks"].pop()
    elif invalid == "duplicate_check":
        review["checks"][1] = deepcopy(review["checks"][0])
    elif invalid == "missing_execution":
        review.pop("execution_check")
    elif invalid == "missing_alignment":
        review.pop("alignment")
    elif invalid == "missing_shot":
        alignment["shots"].pop()
    elif invalid == "foreign_shot":
        alignment["shots"][0]["segment_id"] = "other_segment"
    elif invalid == "duplicate_shot":
        alignment["shots"][1] = deepcopy(alignment["shots"][0])
    elif invalid == "missing_span":
        alignment["spans"].pop()
    elif invalid == "foreign_span":
        alignment["spans"][0]["segment_id"] = "other_segment"
    elif invalid == "duplicate_span":
        alignment["spans"][1] = deepcopy(alignment["spans"][0])
    elif invalid == "missing_pacing":
        alignment["shots"][0].pop("pacing")
    elif invalid == "false_pacing":
        alignment["shots"][0]["pacing"]["cut_necessity_passed"] = False
    elif invalid == "action_overrun":
        alignment["shots"][0]["pacing"]["action_seconds_required"] = 10.1
    elif invalid == "rewritten_script":
        review["script"] = "未经审核的新脚本"
    elif invalid == "blank_note":
        review["execution_check"]["note"] = " "
    else:
        review["checks"][0]["note"] = "字" * 181
    with pytest.raises(ValueError):
        validate_plan_review(review, plan)


@pytest.mark.parametrize("field", ["ready", "content", "action_capacity", "state_continuity"])
@pytest.mark.parametrize("value", ["false", 0, None])
def test_new_verdicts_are_strict_booleans_and_false_like_is_never_promoted(plan, review, field, value):
    target, name = (review, "ready") if field == "ready" else (
        (review["checks"][0], "passed") if field == "content" else (review["execution_check"], field))
    target[name] = value
    assert lower_failed_plan_verdicts(review, plan) == review
    with pytest.raises(ValueError):
        validate_plan_review(review, plan)


@pytest.mark.parametrize("failure", ["content", "action_capacity", "state_continuity", "span", "pacing", "overrun"])
def test_lowering_only_propagates_explicit_failures_and_preserves_original_response(plan, review, failure):
    if failure == "content":
        review["checks"][0]["passed"] = False
    elif failure in {"action_capacity", "state_continuity"}:
        review["execution_check"][failure] = False
    elif failure == "span":
        review["alignment"]["spans"][0]["passed"] = False
    else:
        pacing = review["alignment"]["shots"][0]["pacing"]
        if failure == "pacing":
            pacing["cut_necessity_passed"] = False
        else:
            pacing["action_seconds_required"] = 10.1
    before = deepcopy(review)
    lowered = lower_failed_plan_verdicts(review, plan)
    assert review == before and lowered["ready"] is False
    assert lower_failed_plan_verdicts(lowered, plan) == lowered
    assert not validate_plan_review(lowered, plan).ready
    if failure in {"pacing", "overrun"}:
        assert lowered["alignment"]["ready"] is False
        assert lowered["alignment"]["shots"][0]["passed"] is False
        assert lowered["alignment"]["shots"][1] == before["alignment"]["shots"][1]


def test_lowering_does_not_invent_missing_checks_or_fix_foreign_identities(plan, review):
    review["checks"][0]["passed"] = False
    review.pop("execution_check")
    review["alignment"]["spans"][0]["span_id"] = "other_span"
    lowered = lower_failed_plan_verdicts(review, plan)
    assert lowered == {**review, "ready": False}
    with pytest.raises(ValueError):
        validate_plan_review(lowered, plan)


@pytest.mark.parametrize("level", ["combined", "alignment"])
def test_false_verdict_with_all_children_passing_is_rejected_never_upgraded(plan, review, level):
    (review if level == "combined" else review["alignment"])["ready"] = False
    lowered = lower_failed_plan_verdicts(review, plan)
    assert lowered["ready"] is False
    with pytest.raises(ValueError):
        validate_plan_review(lowered, plan)


def test_server_stamps_do_not_invalidate_a_saved_review(plan, review):
    from test_plan_segment_normalization import _presenter_plan, _ready_review
    value = _presenter_plan()
    for segment in value["segments"]:
        segment.pop("speech_characters_per_tenth", None)
    bound = validate_plan_review(_ready_review(value), value).model_dump()
    value["plan_review"] = deepcopy(bound)
    value["segments"][0]["speech_characters_per_tenth"] = 48
    value["segments"][0]["reference_roles"] = [{"asset_id": "photo", "role": "product"}]
    parsed = Plan.model_validate(value)
    assert parsed.plan_review["reviewed_plan_sha256"] == bound["reviewed_plan_sha256"]
    assert parsed.segments[0].speech_characters_per_tenth == 48
    assert plan.segments and review["ready"]


def test_legacy_fingerprint_that_included_the_speech_rate_is_rebound(plan, review):
    from app.plan_review import _fingerprint_validated_plan, _validated_review_subject
    from test_plan_segment_normalization import _presenter_plan, _ready_review
    value = _presenter_plan()
    bound = validate_plan_review(_ready_review(value), value).model_dump()
    subject = _validated_review_subject(value)
    legacy = _fingerprint_validated_plan(subject, bound["reviewed_reference_asset_ids"], include_server_fields=True)
    assert legacy != bound["reviewed_plan_sha256"]
    bound["reviewed_plan_sha256"] = legacy
    value["plan_review"] = bound
    parsed = Plan.model_validate(value)
    assert parsed.plan_review["reviewed_plan_sha256"] != legacy
    assert parsed.plan_review["reviewed_plan_sha256"] == validate_plan_review(
        _ready_review(value), value).reviewed_plan_sha256
    assert plan.segments and review["ready"]


def test_a_script_change_still_rejects_a_review_when_the_speech_rate_is_stamped(plan, review):
    value = plan.model_dump()
    value["segments"][0]["speech_characters_per_tenth"] = 48
    bound = validate_plan_review(review, value).model_dump()
    words = "三档风速按需选。"
    value["script"] = words + value["segments"][1]["narration"]
    segment = value["segments"][0]
    segment["narration"] = segment["speech_spans"][0]["text"] = words
    segment["speech_spans"][0]["anchors"][0]["text"] = words
    value["plan_review"] = bound
    with pytest.raises(ValueError, match="当前最终方案"):
        Plan.model_validate(value)


def test_pydantic_instances_are_revalidated_after_mutation(plan, review):
    parsed = PlanReview.model_validate(review)
    parsed.checks[0].passed = False
    with pytest.raises(ValueError):
        validate_plan_review(parsed, plan)
