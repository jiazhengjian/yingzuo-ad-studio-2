"""Exact evidence checks report every fact error without rewriting the brief."""
from copy import deepcopy

import pytest

from app.strategy import BriefEvidenceError, ProductBrief, validate_brief


def brief(*facts):
    return ProductBrief(product_name="吹风机", category="appliance", facts=[
        {"id": f"fact_{index}", "text": text, "source_kind": kind, "source_id": source}
        for index, (text, source, kind) in enumerate(facts)])


def test_one_repair_receives_all_source_and_quote_errors_without_mutation():
    sources = {"message_features_12345": "风速、温度控制、护发功能",
        "message_audience_67890": "主要给专业造型师看"}
    candidate = brief(
        ("风速、温度控制、护发功能", "message_features_1234", "merchant"),
        ("我主要给专业造型师看", "message_audience_6789", "merchant"),
        ("我希望突出护发功能", "message_features_12345", "merchant"),
        ("银灰色机身", "generated-scene", "image"))
    original, original_sources = candidate.model_dump(), deepcopy(sources)
    with pytest.raises(BriefEvidenceError, match="不存在的商家来源") as error:
        validate_brief(candidate, sources, {"original-product"})
    issues = error.value.issues
    assert [(issue["loc"], issue["type"]) for issue in issues] == [
        (["brief", "facts", 0, "source_id"], "merchant_source_missing"),
        (["brief", "facts", 1, "source_id"], "merchant_source_missing"),
        (["brief", "facts", 1, "text"], "merchant_quote_mismatch"),
        (["brief", "facts", 2, "text"], "merchant_quote_mismatch"),
        (["brief", "facts", 3, "source_id"], "image_source_missing")]
    assert issues[0]["matching_source_ids"] == ["message_features_12345"]
    assert issues[1]["matching_source_ids"] == []
    assert issues[3]["source_text"] == sources["message_features_12345"]
    assert issues[4]["allowed_asset_ids"] == ["original-product"]
    assert candidate.model_dump() == original and sources == original_sources


def test_missing_source_is_rejected_even_when_quote_occurs_elsewhere():
    candidate = brief(("护发功能", "typo-id", "merchant"))
    with pytest.raises(BriefEvidenceError, match="不存在的商家来源") as error:
        validate_brief(candidate, {"real-id": "风速、温度控制、护发功能"}, set())
    assert len(error.value.issues) == 1
    assert error.value.issues[0]["matching_source_ids"] == ["real-id"]
    assert candidate.facts[0].source_id == "typo-id"


def test_quote_in_another_source_cannot_satisfy_a_named_source():
    sources = {"selected": "仅讨论外形", "other": "支持温度控制"}
    with pytest.raises(BriefEvidenceError, match="原文子串") as error:
        validate_brief(brief(("支持温度控制", "selected", "merchant")), sources, set())
    issue, = error.value.issues
    assert issue["loc"] == ["brief", "facts", 0, "text"]
    assert issue["source_text"] == "仅讨论外形"
    assert issue["matching_source_ids"] == ["other"]


@pytest.mark.parametrize("quote", ["我希望突出护发功能", "有护发功能", "护发的功能"])
def test_extra_words_cannot_be_treated_as_an_exact_quote(quote):
    with pytest.raises(BriefEvidenceError, match="原文子串") as error:
        validate_brief(brief((quote, "original", "merchant")),
            {"original": "希望突出护发功能"}, set())
    assert error.value.issues[0]["source_text"] == "希望突出护发功能"


@pytest.mark.parametrize("source", [
    "不能宣称可以杀菌。", "请勿说可以杀菌。", "可以杀菌尚未验证。",
    "假设可以杀菌。", "据说可以杀菌，尚需测试。",
])
def test_qualifier_omission_still_rejected_alongside_later_fact_errors(source):
    candidate = brief(("可以杀菌", "original", "merchant"),
        ("额外性能", "missing", "merchant"))
    with pytest.raises(BriefEvidenceError, match="省略了原句的否定或不确定限定") as error:
        validate_brief(candidate, {"original": source}, set())
    assert [issue["type"] for issue in error.value.issues] == [
        "merchant_qualifier_missing", "merchant_source_missing", "merchant_quote_mismatch"]
    assert error.value.issues[0]["source_text"] == source


def test_preserved_qualifiers_and_separate_affirmative_occurrence_still_pass():
    candidate = brief(("不能宣称可以杀菌", "negative", "merchant"),
        ("可以杀菌", "repeated", "merchant"), ("银灰色机身", "original-image", "image"))
    sources = {"negative": "未经验证，不能宣称可以杀菌。",
        "repeated": "旧款不能宣称可以杀菌。本款检测说明写明可以杀菌。"}
    # Include every qualifier from the chosen containing sentence.
    candidate.facts[0].text = "未经验证，不能宣称可以杀菌"
    result = validate_brief(candidate, sources, {"original-image"})
    assert isinstance(result, ProductBrief)
    assert result.model_dump() == candidate.model_dump()
    assert validate_brief(candidate.model_dump(), sources, {"original-image"}) == candidate


def test_image_membership_never_falls_back_to_merchant_registry():
    with pytest.raises(BriefEvidenceError, match="不存在的商品原图") as error:
        validate_brief(brief(("银色机身", "generated-image", "image")),
            {"generated-image": "银色机身"}, {"original-image"})
    issue, = error.value.issues
    assert issue["type"] == "image_source_missing"
    assert issue["allowed_asset_ids"] == ["original-image"]
    assert "source_text" not in issue


def test_non_string_registry_value_cannot_authorize_a_source_or_quote():
    with pytest.raises(BriefEvidenceError) as error:
        validate_brief(brief(("护发功能", "broken", "merchant")),
            {"broken": {"text": "护发功能"}, "valid": "其他资料"}, set())
    assert len(error.value.issues) == 2
    assert error.value.issues[0]["allowed_source_ids"] == ["valid"]


def test_general_exception_message_never_echoes_model_identifiers_or_source_text():
    candidate = brief(("PRIVATE_INVENTED_QUOTE", "PRIVATE_MODEL_ID", "merchant"),
        ("PRIVATE_EXTRA_WORD", "real", "merchant"))
    candidate.facts[0].id = "PRIVATE_FACT_ID"
    with pytest.raises(BriefEvidenceError) as error:
        validate_brief(candidate, {"real": "PRIVATE_AUTHORED_SOURCE"}, set())
    # Repair feedback may carry an exact authoritative source. Public errors
    # and ordinary exception logging must only contain fixed owned messages.
    assert error.value.issues[-1]["source_text"] == "PRIVATE_AUTHORED_SOURCE"
    assert "PRIVATE" not in str(error.value) and "PRIVATE" not in repr(error.value)
    assert isinstance(error.value, ValueError)
