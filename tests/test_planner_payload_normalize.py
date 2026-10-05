"""Planner payloads are normalized before schema validation. Offline."""
from copy import deepcopy

from app.content_draft import ContentDraft
from app.planner_recovery import normalize_model_payload
from app.planning import StrategyDecision
from app.speech_draft import SpeechDraft


def _strategy():
    return {
        "kind": "plan", "message": "已整理外观方向，请核对镜头计划。", "questions": [],
        "brief": {"product_name": "蓝瓶", "category": "general", "audience": "关注商品外观的人",
                  "scenario": "商品展示", "facts": [{"id": "fact_blue", "text": "蓝瓶",
                       "source_kind": "merchant", "source_id": "brief"}],
                  "unknowns": ["功能与材质未确认"], "assumptions": []},
        "directions": [{"id": "appearance", "rule_id": "appearance_reveal", "label": "外观揭示",
                        "audience": "关注商品外观的人", "scenario": "商品展示", "angle": "看清蓝瓶的外观",
                        "hook": "先展示商品整体轮廓", "development": "由整体进入局部细节",
                        "payoff": "回到完整商品", "evidence_ids": ["fact_blue"],
                        "reason": "现有资料支持外观展示", "production_notes": ["不演示未确认功能"]}],
        "selected_direction_id": "appearance",
    }


def test_duplicate_key_visual_merges_when_canonical_is_empty_and_unknowns_drop():
    raw = deepcopy(_strategy())
    raw["unexpected"] = "drop me"
    raw["key_visual"] = "不要写进每一条方向"
    direction = raw["directions"][0]
    direction["also_unknown"] = {"nested": True}
    direction["key_visual"] = "中段这一下看清瓶身。"
    direction["film_concept"] = {"structure": "product_visual", "key_visual": ""}
    normalized = normalize_model_payload(StrategyDecision, raw)
    assert "unexpected" not in normalized and "key_visual" not in normalized
    assert "key_visual" not in normalized["directions"][0]
    assert "also_unknown" not in normalized["directions"][0]
    assert normalized["directions"][0]["film_concept"]["key_visual"] == "中段这一下看清瓶身。"
    StrategyDecision.model_validate(normalized)


def test_a_different_duplicate_keeps_the_canonical_sentence():
    raw = deepcopy(_strategy())
    direction = raw["directions"][0]
    direction["film_concept"] = {"structure": "product_visual", "key_visual": "画面甲在中段出现。"}
    direction["key_visual"] = "画面乙在中段出现。"
    normalized = normalize_model_payload(StrategyDecision, raw)
    concept = normalized["directions"][0]["film_concept"]
    assert concept["key_visual"] == "画面甲在中段出现。"
    assert "key_visual" not in normalized["directions"][0]
    StrategyDecision.model_validate(normalized)


def test_identical_duplicate_is_dropped_and_a_missing_parent_is_not_invented():
    raw = deepcopy(_strategy())
    raw["directions"][0]["key_visual"] = "中段这一下看清瓶身。"
    normalized = normalize_model_payload(StrategyDecision, raw)
    assert "film_concept" not in normalized["directions"][0]
    assert "key_visual" not in normalized["directions"][0]
    StrategyDecision.model_validate(normalized)
    raw["directions"][0]["film_concept"] = {"structure": "product_visual", "key_visual": "中段这一下看清瓶身。"}
    raw["directions"][0]["key_visual"] = "中段这一下看清瓶身。"
    normalized = normalize_model_payload(StrategyDecision, raw)
    assert "key_visual" not in normalized["directions"][0]
    assert normalized["directions"][0]["film_concept"]["key_visual"] == "中段这一下看清瓶身。"


def test_content_and_speech_drafts_drop_unknown_fields():
    content = normalize_model_payload(ContentDraft, {"extra": 1, "beats": [{
        "id": "b", "narration": "看轮廓。", "visual_role": "瓶身直立", "evidence_ids": [], "purpose": "多余"}]})
    assert "extra" not in content and "purpose" not in content["beats"][0]
    speech = normalize_model_payload(SpeechDraft, {"narration": "看轮廓。", "note": "多余"})
    assert speech == {"narration": "看轮廓。"}
    SpeechDraft.model_validate(speech)
