"""Offline advertising-content contracts, independent of API keys and production DB."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ad_script import (AD_EXPRESSION_VERSION, AdScript, ScriptReview, CHECK_CRITERIA, load_ad_expression_skill,
                           script_context, script_narration, validate_script)
from app.strategy import ProductBrief, CreativeDirection, build_strategy, select_rules


@pytest.fixture
def strategy():
    brief = ProductBrief(product_name="蛋糕切块", category="food", audience="办公人群", scenario="办公室下午茶",
        facts=[{"id": "layers", "text": "多层切面", "source_kind": "image", "source_id": "photo"}],
        unknowns=["配料、口味、价格"], assumptions=[])
    direction = CreativeDirection(id="afternoon", rule_id="sensory_detail", label="下午茶里的层次", audience="办公人群",
        scenario="办公室下午茶", angle="用多层外观建立下午茶选择的吸引力", hook="蛋糕进入办公桌边",
        development="切面支撑外观吸引力", payoff="回到办公室下午茶选择", evidence_ids=["layers"],
        reason="照片支持切面外观", production_notes=["不把口味与成分当事实"])
    return build_strategy(brief, [direction], direction.id, select_rules(brief))


@pytest.fixture
def script():
    return {"core_message": "用多层外观建立下午茶选择的吸引力", "audience_takeaway": "下午茶可以选一块外观有层次的蛋糕",
            "product_role": "蛋糕切块的多层切面承担视觉吸引力", "evidence_ids": ["layers"],
            "skill_version": load_ad_expression_skill()["version"],
            "beats": [
                {"id": "opening", "purpose": "引出下午茶选择", "message": "给办公室下午茶一个具体选项",
                 "visual_role": "蛋糕进入办公桌", "narration": "下午茶，选块蛋糕。", "evidence_ids": []},
                {"id": "product", "purpose": "建立商品吸引力", "message": "多层切面的外观值得留意",
                 "visual_role": "切面支持外观表达", "narration": "这层次，看着就抢眼。", "evidence_ids": ["layers"]}]}


def review_for(script):
    return {"script": deepcopy(script), "ready": True,
            "checks": [{"criterion": c, "passed": True, "note": "离线结构夹具通过"} for c in CHECK_CRITERIA]}


def test_actual_skill_resources_have_reproducible_hash_and_defensive_copy():
    bundle = load_ad_expression_skill()
    assert bundle["id"] == "ad_expression" and bundle["sources"]
    assert bundle["content_sha256"] == hashlib.sha256(bundle["content"].encode()).hexdigest()
    assert "淋面" in bundle["content"] and len(bundle["content"]) > 1000
    bundle["sources"].clear()
    bundle["content"] = "tampered"
    assert load_ad_expression_skill()["sources"] and load_ad_expression_skill()["content"] != "tampered"


def test_valid_script_preserves_fact_sources_and_allows_nonspoken_beat(strategy, script):
    before = deepcopy(script)
    result = validate_script(script, strategy, 20)
    assert result.model_dump() == before
    silent = deepcopy(script)
    silent["beats"][0]["narration"] = ""
    assert script_narration(validate_script(silent, strategy, 20)) == silent["beats"][1]["narration"]
    assert script == before


@pytest.mark.parametrize("invalid", ["unknown", "missing_union", "missing_selected", "duplicate_beat", "duplicate_fact", "unsupported_extra"])
def test_script_fact_graph_is_validated_before_compilation(strategy, script, invalid):
    value = deepcopy(script)
    if invalid == "unknown":
        value["evidence_ids"] = value["beats"][1]["evidence_ids"] = ["invented"]
    elif invalid == "missing_union": value["beats"][1]["evidence_ids"] = []
    elif invalid == "missing_selected":
        from app.strategy import StrategyRecord
        value["evidence_ids"] = value["beats"][1]["evidence_ids"] = ["new_fact"]
        expanded = strategy.model_dump()
        expanded["brief"]["facts"].append({"id": "new_fact", "text": "纸盒包装", "source_kind": "merchant", "source_id": "brief"})
        strategy = StrategyRecord.model_validate(expanded)
    elif invalid == "duplicate_beat": value["beats"][1]["id"] = value["beats"][0]["id"]
    elif invalid == "duplicate_fact": value["beats"][1]["evidence_ids"] *= 2
    else: value["invented_claim"] = "TEST_PRIVATE_UNSUPPORTED"
    with pytest.raises(ValueError):
        validate_script(value, strategy, 20, final=False)


@pytest.mark.parametrize("ending", ["，", ",", "、", "：", ":", "；", ";", "，\"", "，”"])
def test_draft_may_be_repaired_but_final_incomplete_line_is_rejected(strategy, script, ending):
    script["beats"][0]["narration"] = f"这块蛋糕{ending}  "
    assert validate_script(script, strategy, 20, final=False)
    with pytest.raises(ValueError, match="完整表达"):
        validate_script(script, strategy, 20)


def test_whole_lines_must_fit_ordered_segments_not_just_total_budget(strategy, script):
    first = script["beats"][0]
    script["beats"] = [{**deepcopy(first), "id": f"beat_{i}", "narration": "字" * (size - 1) + "。", "evidence_ids": ["layers"]}
                       for i, size in enumerate([20, 20, 12])]
    assert len(script_narration(script)) == 52 < 58
    assert validate_script(script, strategy, 20, final=False)
    with pytest.raises(ValueError, match="按顺序无法放入"):
        validate_script(script, strategy, 20)
    assert validate_script(script, strategy, 30)


def test_short_tail_does_not_force_voice_or_overfill_budget(strategy, script):
    script["beats"][0]["narration"] = "字" * 27 + "。"
    script["beats"][1]["narration"] = "字" * 13 + "。"
    assert validate_script(script, strategy, 15)
    script["beats"][1]["narration"] += "啊"
    with pytest.raises(ValueError):
        validate_script(script, strategy, 15)
    script["beats"][1]["narration"] = ""
    assert validate_script(script, strategy, 11)


@pytest.mark.parametrize("duration", [9, 61, 20.0, True])
def test_invalid_duration_does_not_enter_script_stage(strategy, script, duration):
    with pytest.raises(ValueError): validate_script(script, strategy, duration)
    with pytest.raises(ValueError): script_context(strategy, duration)


@pytest.mark.parametrize("version", ["historical-v0", "1.1.0", "1.2.0"])
def test_historical_script_version_can_load_but_new_generation_requires_current(strategy, script, version):
    script["skill_version"] = version
    assert AdScript.model_validate(script).skill_version == version
    assert validate_script(script, strategy, 20, require_current_version=False)
    with pytest.raises(ValueError, match="当前"):
        validate_script(script, strategy, 20)


@pytest.mark.parametrize("version", ["1.1.0", "1.2.0"])
def test_saved_legacy_plan_retains_script_version_and_content_after_rule_upgrade(strategy, script, version):
    from app.schemas import Plan
    script["skill_version"] = version
    before = deepcopy(script)
    plan = {"title": "历史方案", "script": script_narration(script), "audio_strategy": "native",
            "facts": {"name": strategy.brief.product_name, "claims": [fact.text for fact in strategy.brief.facts]},
            "strategy": strategy.model_dump(), "ad_script": script, "script_review": review_for(script),
            "segments": [{"id": f"s{index}", "duration_seconds": 10, "purpose": beat["purpose"],
                "prompt": "保持历史商品与办公室场景", "narration": beat["narration"], "reference_asset_ids": ["photo"],
                "shots": [{"id": f"shot{index}", "start": 0, "end": 10, "description": beat["visual_role"],
                           "purpose": beat["purpose"], "beat_id": beat["id"], "evidence_ids": beat["evidence_ids"],
                           "narration": beat["narration"]}]} for index, beat in enumerate(script["beats"])]}
    saved = Plan.model_validate(plan).model_dump()
    assert saved["ad_script"] == before and saved["script_review"]["script"] == before
    assert saved["script"] == script_narration(before)
    assert [shot["description"] for segment in saved["segments"] for shot in segment["shots"]] == [b["visual_role"] for b in before["beats"]]
    assert script == before


def test_runtime_source_identity_is_explicit_local_snapshot_and_defensively_copied():
    bundle = load_ad_expression_skill()
    snapshot = bundle["local_source_snapshots"][0]
    assert snapshot["kind"] == "user_supplied_local_snapshot"
    assert snapshot["remote_url"] is None and snapshot["commit"] is None
    assert "local-snapshot:" + snapshot["id"] in bundle["sources"]
    assert snapshot["license"] == "MIT"
    assert snapshot["files"] and all(not Path(item["path"]).is_absolute() and ".." not in Path(item["path"]).parts for item in snapshot["files"])
    assert all(len(item["sha256"]) == 64 and all(c in "0123456789abcdef" for c in item["sha256"]) for item in snapshot["files"])
    serialized = json.dumps(snapshot)
    assert "/Users/" not in serialized and "github.com" not in serialized
    snapshot["files"][0]["sha256"] = "tampered"
    assert load_ad_expression_skill()["local_source_snapshots"][0]["files"][0]["sha256"] != "tampered"


def test_director_method_resource_is_included_in_actual_runtime_hash():
    root = Path(__file__).parents[1] / "app" / "legacy_skills" / "ad_expression"
    manifest = json.loads((root / "manifest.json").read_text())
    assert "references/director_methods.md" in manifest["resources"]
    loaded = "\n\n".join((root / name).read_text() for name in manifest["resources"])
    bundle = load_ad_expression_skill()
    assert bundle["content"] == loaded
    assert bundle["content_sha256"] == hashlib.sha256(loaded.encode()).hexdigest()


def test_current_version_retains_four_information_beats_within_legacy_time_budget(strategy, script):
    assert AD_EXPRESSION_VERSION == "1.3.0"
    template = script["beats"][1]
    script["beats"] = [
        {**deepcopy(template), "id": f"task_{index}", "narration": "字" * (size - 1) + "。"}
        for index, size in enumerate([14, 14, 14, 14])
    ]
    # There is no lower target or sentence-count gate. This proves only that
    # complete text can fit the engineering time budget, not ad quality or
    # measured audio duration. Each pair needs 4.9 + 4.9 seconds.
    assert len(script_narration(script)) == 56
    assert len(validate_script(script, strategy, 20).beats) == 4
    script["beats"][0]["narration"] += "啊啊！"
    with pytest.raises(ValueError, match="台词超过"):
        validate_script(script, strategy, 20)


def test_existing_58_character_hard_cap_still_allows_two_whole_29_character_lines(strategy, script):
    for beat in script["beats"]:
        beat["narration"] = "字" * 28 + "。"
    assert len(script_narration(script)) == 58
    assert validate_script(script, strategy, 20)


@pytest.mark.parametrize("sizes,feasible", [([23, 15, 14, 0], False), ([23, 13, 12, 0], True), ([14, 15, 14, 15], False)])
def test_review_context_and_new_final_use_same_director_time_allocation(strategy, script, sizes, feasible):
    from app.planning import expression_schedule
    template = script["beats"][1]
    script["beats"] = [{**deepcopy(template), "id": f"beat_{index}",
        "narration": "字" * (size - 1) + "。" if size else ""}
        for index, size in enumerate(sizes)]
    # The previous text-only contract accepts all three examples. New drafts
    # must remain available to the existing review instead of adding requests.
    assert len(script_narration(script)) <= 58
    assert validate_script(script, strategy, 20, final=False)
    assert validate_script(script, strategy, 20, require_current_version=False)
    context = script_context(strategy, 20, script=script)
    assert context["schedule_feasible"] is feasible
    assert [item["character_count"] for item in context["draft_beat_timing"]] == sizes
    if sizes == [23, 15, 14, 0]:
        assert len(script_narration(script)) == 52
        assert [item["minimum_seconds"] for item in context["draft_beat_timing"]] == [8.0, 5.2, 4.9, 1.0]
    if feasible:
        validated = validate_script(script, strategy, 20)
        assert expression_schedule(validated, [10, 10])
    else:
        with pytest.raises(ValueError, match="无法为对应画面留出时间"):
            validate_script(script, strategy, 20)
        with pytest.raises(ValueError):
            expression_schedule(AdScript.model_validate(script), [10, 10])


def test_schedule_constraints_are_available_before_draft_and_tiny_spoken_beats_need_one_second(strategy, script):
    context = script_context(strategy, 20)
    assert "schedule_feasible" not in context and "draft_beat_timing" not in context
    assert context["schedule_rules"]["minimum_beat_seconds"] == 1
    assert context["schedule_rules"]["time_quantum_seconds"] == 0.1
    assert context["schedule_rules"]["max_beats_per_segment"] == 6
    script["beats"][0]["narration"] = "好。"
    script["beats"][1]["narration"] = ""
    review_context = script_context(strategy, 20, script=script)
    assert review_context["schedule_feasible"] is True
    assert [item["minimum_seconds"] for item in review_context["draft_beat_timing"]] == [1.0, 1.0]


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "ready_false", "ready_true_failed", "coerced_bool"])
def test_review_requires_six_distinct_checks_and_matching_verdict(script, invalid):
    value = review_for(script)
    if invalid == "missing": value["checks"].pop()
    elif invalid == "duplicate": value["checks"][1] = deepcopy(value["checks"][0])
    elif invalid == "ready_false": value["ready"] = False
    elif invalid == "ready_true_failed": value["checks"][0]["passed"] = False
    else: value["ready"] = "true"
    with pytest.raises(ValidationError): ScriptReview.model_validate(value)


def test_valid_failed_review_is_recordable_but_not_a_ready_verdict(script):
    value = review_for(script)
    value["ready"] = False
    value["checks"][2].update(passed=False, note="所需的口味资料未确认")
    assert not ScriptReview.model_validate(value).ready


def test_context_loads_runtime_rules_and_carries_draft_before_camera_timings(strategy, script):
    value = script_context(strategy, 15, script=script)
    assert value["duration_seconds"] == 15 and value["segment_narration_limits"] == [29, 14]
    assert value["max_characters"] == 43
    assert "suggested_total_characters" not in value
    assert "工程硬上限" in value["speech_budget_basis"] and "真实音频" in value["speech_budget_basis"]
    assert value["draft_script"] == script
    assert value["runtime_skill"] == load_ad_expression_skill()
    assert "shot_slots" not in value and "shots" not in value


@pytest.mark.parametrize("duration,accepted", [(10, False), (20, True)])
def test_silent_beats_still_consume_one_of_six_shots(strategy, script, duration, accepted):
    template = script["beats"][1]
    script["beats"] = [{**deepcopy(template), "id": f"beat_{index}", "narration": "蛋糕。" if index == 6 else ""}
                       for index in range(7)]
    if accepted:
        assert validate_script(script, strategy, duration)
    else:
        with pytest.raises(ValueError, match="按顺序无法放入"):
            validate_script(script, strategy, duration)


def test_silent_shots_and_complete_speech_share_segment_capacity(strategy, script):
    template = script["beats"][1]
    script["beats"] = [{**deepcopy(template), "id": f"beat_{index}", "narration": "" if index < 6 else "字" * 19 + "。"}
                       for index in range(8)]
    assert len(script_narration(script)) == 40 < 58
    with pytest.raises(ValueError, match="按顺序无法放入"):
        validate_script(script, strategy, 20)
    assert validate_script(script, strategy, 30)


def test_beat_id_has_same_limit_as_shot_reference(script):
    script["beats"][0]["id"] = "b" * 81
    with pytest.raises(ValidationError):
        AdScript.model_validate(script)


def test_actual_generic_hairdryer_copy_is_a_semantic_review_case_not_a_lexical_verdict():
    path = Path(__file__).with_name("fixtures") / "ad_expression_cases.json"
    pack = json.loads(path.read_text(encoding="utf-8"))
    baseline = next(item for item in pack["baselines"]
                    if item["id"] == "historical_hairdryer_generic_storage_copy")
    assert baseline["kind"] == "historical_actual"
    assert baseline["narrations"] == ["出门前那几分钟，台面上总有个顺手的位置。", "每天都要拿，收进柜子反而麻烦。"]
    assert set(baseline["expected_semantic_failures"]) == {"product_relevance", "audience_relevance"}
    assert "expected_diagnostics" not in baseline
    for case in pack["cases"]:
        if case["id"].startswith("hairdryer_"):
            assert baseline["id"] in case["baseline_ids"]
    # Runtime method examples remain cross-category; the actual bad copy is
    # evaluation data, not a fixed prompt template for this product.
    assert all(line not in load_ad_expression_skill()["content"] for line in baseline["narrations"])


def test_semantically_failed_core_can_be_reported_with_valid_text_and_fact_budgets(strategy, script):
    script["beats"][0]["narration"] = "出门前那几分钟，台面上总有个顺手的位置。"
    script["beats"][1]["narration"] = "每天都要拿，收进柜子反而麻烦。"
    # Pure validation intentionally does not pretend to establish semantics.
    assert validate_script(script, strategy, 20)
    value = review_for(script)
    value["ready"] = False
    for check in value["checks"]:
        if check["criterion"] in {"product_relevance", "audience_relevance"}:
            check.update(passed=False, note="实际话语只谈常用与收柜麻烦，没有建立商品特点与关注理由的关系")
    assert not ScriptReview.model_validate(value).ready
