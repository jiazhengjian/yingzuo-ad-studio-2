"""Offline classification of sentences that can reach a failure card."""
import pytest
from pydantic import ValidationError

from app.merchant_cards import SYSTEM_CARD, failure_class, present_exception, present_text
from app.schemas import Plan


def test_system_recoverable_codes_are_not_merchant_actions():
    for code in ("planning_invalid_response", "planning_response_interrupted", "planning_request_failed",
                 "planning_review_incomplete"):
        assert failure_class("内部结构没有通过", code) == 1


@pytest.mark.parametrize("text,code", [
    ("还没有可用的商品图，已有方案仍保留。请重新上传商品图。", None),
    ("实测配音长于视频计划，请先精简文案再生成", "voice_too_long"),
    ("请先配置土豆 API Key", "planning_configuration_failed"),
    ("账户余额不足，请补充额度", None),
    ("内容未通过平台要求", None),
    ("图片生成失败", "image_generation_failed"),
])
def test_merchant_actions_keep_one_plain_sentence(text, code):
    assert failure_class(text, code) == 2
    sentence = present_text(text, code)
    assert "请重新策划" not in sentence
    assert "pydantic" not in sentence.lower()
    assert "validation" not in sentence.lower()
    assert "_" not in sentence


def test_technical_and_unknown_errors_use_the_system_sentence():
    for text in ("", "4 validation errors for CreativeOption", "speech_characters_per_tenth 无效",
                 "请重新策划", "Value error, 综合审核记录必须对应当前最终方案"):
        assert failure_class(text) == 3
        assert present_text(text) == SYSTEM_CARD
    try:
        Plan.model_validate({"language": "zh-CN"})
    except ValidationError as error:
        assert present_exception(error) == SYSTEM_CARD
    assert present_exception(RuntimeError("traceback")) == SYSTEM_CARD


def test_internal_check_wording_is_not_shown_to_the_merchant():
    for text in ("方案检查没通过", "这次分镜没有通过内容检查", "分镜校验没有通过", "硬门没有通过"):
        assert present_text(text) == SYSTEM_CARD
        assert present_exception(ValueError(text)) == SYSTEM_CARD


def test_plain_chinese_provider_text_is_not_replaced():
    text = "部分视频片段生成失败；已保留其他任务与素材，可重试失败片段。"
    assert failure_class(text) == 2
    assert present_text(text) == text
