"""Offline planning contracts; these do not verify semantics or generated audio."""
from copy import deepcopy

import pytest

from app.ad_script import AD_EXPRESSION_VERSION, CHECK_CRITERIA
from app.schemas import AgentDecision, Plan, Segment, SpeechSpan, shot_character_limit
from app.strategy import CreativeDirection, ProductBrief, build_strategy, select_rules


@pytest.fixture
def plan_data():
    brief = ProductBrief(product_name="蛋糕切块", category="food", audience="办公人群", scenario="办公室下午茶",
        facts=[{"id": "layers", "text": "多层切面", "source_kind": "image", "source_id": "photo"}],
        unknowns=["配料与口味"], assumptions=[])
    direction = CreativeDirection(id="afternoon", rule_id="sensory_detail", label="下午茶里的层次", audience="办公人群",
        scenario="办公室下午茶", angle="用多层外观建立下午茶选择的吸引力", hook="蛋糕进入办公桌边",
        development="切面支撑外观吸引力", payoff="回到办公室下午茶选择", evidence_ids=["layers"],
        reason="照片支持切面外观", production_notes=["不把口味当成事实"])
    strategy = build_strategy(brief, [direction], direction.id, select_rules(brief)).model_dump()
    text = "下午茶，给自己选块蛋糕。"
    script = {"core_message": direction.angle, "audience_takeaway": "下午茶可以选择蛋糕切块",
        "product_role": "蛋糕切块承担下午茶选择的吸引力", "evidence_ids": ["layers"],
        "skill_version": AD_EXPRESSION_VERSION, "beats": [
            {"id": "choice", "purpose": "给出下午茶选择", "message": "蛋糕切块适合进入下午茶场景",
             "visual_role": "蛋糕进入办公桌，随后展示多层切面", "narration": text, "evidence_ids": ["layers"]},
            {"id": "ending", "purpose": "留出商品展示时间", "message": "完成下午茶画面的自然收束",
             "visual_role": "保持蛋糕与桌面关系", "narration": "", "evidence_ids": []}]}
    segment = {"id": "seg1", "duration_seconds": 10, "purpose": "展示下午茶选择", "prompt": "商品广告画面",
        "narration": text, "reference_asset_ids": ["photo"], "shots": [
            {"id": "desk", "start": 0, "end": 3, "beat_id": "choice", "description": "办公桌旁放着蛋糕",
             "purpose": "建立下午茶场景", "evidence_ids": [], "narration": ""},
            {"id": "detail", "start": 3, "end": 7, "beat_id": "choice", "description": "多层切面近景",
             "purpose": "支撑商品选择", "evidence_ids": ["layers"], "narration": ""},
            {"id": "rest", "start": 7, "end": 10, "beat_id": "ending", "description": "停留在蛋糕与桌面的中景",
             "purpose": "自然收束", "evidence_ids": [], "narration": ""}],
        "speech_spans": [{"id": "voice1", "beat_id": "choice", "text": text, "start": 0, "end": 7,
            "shot_ids": ["desk", "detail"], "anchors": [
                {"text": "下午茶，", "start": 0, "end": 3, "shot_ids": ["desk"], "visual_evidence": "办公桌上的蛋糕建立下午茶场景"},
                {"text": "给自己选块蛋糕。", "start": 3, "end": 7, "shot_ids": ["detail"], "visual_evidence": "蛋糕的多层切面支撑这项具体选择"}]}]}
    return {"title": "下午茶选择", "script": text, "audio_strategy": "native", "segments": [segment],
        "facts": {"name": brief.product_name, "claims": [f.text for f in brief.facts]}, "strategy": strategy,
        "ad_script": script, "script_review": {"script": deepcopy(script), "ready": True,
            "checks": [{"criterion": criterion, "passed": True, "note": "离线计划结构测试夹具"} for criterion in CHECK_CRITERIA]}}


def test_complete_speech_can_cross_continuous_shots_within_one_beat(plan_data):
    segment = plan_data["segments"][0]
    assert len(segment["narration"]) > shot_character_limit(0, 3)
    plan = Plan.model_validate(plan_data, context={"require_shot_narration": True})
    assert plan.segments[0].speech_spans[0].shot_ids == ["desk", "detail"]
    assert all(shot.narration == "" for shot in plan.segments[0].shots)
    assert plan.script_review.ready


def test_anchor_can_span_cuts_and_pauses_are_allowed(plan_data):
    segment = plan_data["segments"][0]
    span = segment["speech_spans"][0]
    span["anchors"] = [{"text": span["text"], "start": 0.5, "end": 6.5, "shot_ids": ["desk", "detail"],
                        "visual_evidence": "办公桌与蛋糕切面共同支撑下午茶选择"}]
    assert Segment.model_validate(segment).speech_spans[0].anchors[0].start == 0.5


@pytest.mark.parametrize("invalid", ["missing_id", "duplicate_id", "shot_narration", "segment_narration",
    "unknown_shot", "reversed_shots", "duplicate_shot", "different_beat", "no_overlap", "uncovered_time",
    "empty_speech", "incomplete_sentence", "over_budget", "out_of_segment", "duplicate_span", "duplicate_beat"])
def test_invalid_speech_or_shot_binding_is_rejected(plan_data, invalid):
    segment = plan_data["segments"][0]
    span = segment["speech_spans"][0]
    if invalid == "missing_id": segment["shots"][0]["id"] = ""
    elif invalid == "duplicate_id": segment["shots"][1]["id"] = "desk"
    elif invalid == "shot_narration": segment["shots"][0]["narration"] = span["text"]
    elif invalid == "segment_narration": segment["narration"] += "啊"
    elif invalid == "unknown_shot": span["shot_ids"] = ["missing"]
    elif invalid == "reversed_shots": span["shot_ids"].reverse()
    elif invalid == "duplicate_shot": span["shot_ids"] = ["desk", "desk"]
    elif invalid == "different_beat": segment["shots"][1]["beat_id"] = "ending"
    elif invalid == "no_overlap":
        segment["shots"][2]["beat_id"] = "choice"
        span["shot_ids"].append("rest")
    elif invalid == "uncovered_time": span["shot_ids"] = ["desk"]
    elif invalid == "empty_speech": span["text"] = " "
    elif invalid == "incomplete_sentence": span["text"] = "下午茶，"
    elif invalid == "over_budget": span["text"] = "字" * 21
    elif invalid == "out_of_segment": span["end"] = 10.1
    else:
        duplicate = deepcopy(span)
        if invalid == "duplicate_beat": duplicate["id"] = "voice2"
        segment["speech_spans"].append(duplicate)
        segment["narration"] += duplicate["text"]
    with pytest.raises(ValueError): Segment.model_validate(segment)


@pytest.mark.parametrize("invalid", ["missing_text", "extra_text", "reordered_text", "empty_evidence", "empty_text",
    "unknown_shot", "outside_span_shot", "uncovered_time", "no_overlap", "reversed_time", "overlap", "outside_span"])
def test_anchor_text_time_and_visual_references_are_required(plan_data, invalid):
    segment = plan_data["segments"][0]
    anchors = segment["speech_spans"][0]["anchors"]
    if invalid == "missing_text": anchors[0]["text"] = "下午"
    elif invalid == "extra_text": anchors[0]["text"] += "下午茶，"
    elif invalid == "reordered_text": anchors[0]["text"], anchors[1]["text"] = anchors[1]["text"], anchors[0]["text"]
    elif invalid == "empty_evidence": anchors[0]["visual_evidence"] = "   "
    elif invalid == "empty_text": anchors[0]["text"] = " "
    elif invalid == "unknown_shot": anchors[0]["shot_ids"] = ["missing"]
    elif invalid == "outside_span_shot": anchors[1]["shot_ids"] = ["rest"]
    elif invalid == "uncovered_time": anchors[1]["start"] = 2.9
    elif invalid == "no_overlap": anchors[0]["shot_ids"] = ["detail"]
    elif invalid == "reversed_time": anchors[0]["end"] = 0
    elif invalid == "overlap": anchors[1]["start"] = 2
    elif invalid == "outside_span": anchors[1]["end"] = 7.1
    with pytest.raises(ValueError): Segment.model_validate(segment)


def test_nonconsecutive_shots_cannot_be_hidden_by_anchor_references(plan_data):
    segment = plan_data["segments"][0]
    span = segment["speech_spans"][0]
    segment["shots"][2]["beat_id"] = "choice"
    span.update(end=10, shot_ids=["desk", "rest"])
    span["anchors"] = [{"text": span["text"], "start": 0, "end": 10, "shot_ids": ["desk", "rest"], "visual_evidence": "桌上的蛋糕"}]
    with pytest.raises(ValueError, match="连续排列"): Segment.model_validate(segment)


def test_spans_must_be_ordered_and_cannot_overlap(plan_data):
    segment = plan_data["segments"][0]
    text = "留下片刻。"
    other = {"id": "voice2", "beat_id": "ending", "text": text, "start": 7, "end": 10,
        "shot_ids": ["rest"], "anchors": [{"text": text, "start": 7, "end": 10,
            "shot_ids": ["rest"], "visual_evidence": "桌上的蛋糕中景停留"}]}
    segment["speech_spans"].append(other)
    segment["narration"] += text
    assert Segment.model_validate(segment)
    segment["speech_spans"].reverse()
    segment["narration"] = "".join(span["text"] for span in segment["speech_spans"])
    with pytest.raises(ValueError, match="按顺序排列且不重叠"): Segment.model_validate(segment)


def test_silent_visual_segment_uses_empty_spans(plan_data):
    segment = plan_data["segments"][0]
    segment["speech_spans"] = []
    segment["narration"] = ""
    assert Segment.model_validate(segment).speech_spans == []
    segment["narration"] = "请听。"
    with pytest.raises(ValueError): Segment.model_validate(segment)


def test_same_complete_beat_cannot_reappear_in_another_generation_segment(plan_data):
    other = deepcopy(plan_data["segments"][0])
    other["id"] = "seg2"
    plan_data["segments"].append(other)
    plan_data["script"] *= 2
    with pytest.raises(ValueError, match="只能出现在一个生成段"): Plan.model_validate(plan_data)


def test_one_sentence_cannot_be_split_across_independent_generation_segments(plan_data):
    first = plan_data["segments"][0]
    second = deepcopy(first)
    second["id"] = "seg2"
    plan_data["segments"].append(second)
    for segment, text in zip((first, second), ("下午茶", "，给自己选块蛋糕。")):
        span = segment["speech_spans"][0]
        span["text"] = segment["narration"] = text
        span["anchors"] = [{"text": text, "start": 0, "end": 7, "shot_ids": span["shot_ids"],
                            "visual_evidence": "桌面上的蛋糕切块"}]
    assert "".join(segment["narration"] for segment in plan_data["segments"]) == plan_data["script"]
    with pytest.raises(ValueError, match="只能出现在一个生成段"): Plan.model_validate(plan_data)


@pytest.mark.parametrize("seconds,characters,accepted", [(3, 8, True), (3, 9, False), (7, 20, True), (7, 21, False), (10, 29, True), (10, 30, False)])
def test_conservative_rate_applies_to_entire_speech_window(seconds, characters, accepted):
    text = "字" * (characters - 1) + "。"
    value = {"id": "speech", "beat_id": "beat", "text": text, "start": 0, "end": seconds,
        "shot_ids": ["shot"], "anchors": [{"text": text, "start": 0, "end": seconds,
            "shot_ids": ["shot"], "visual_evidence": "测试画面中的商品"}]}
    if accepted:
        assert SpeechSpan.model_validate(value).text == text
    else:
        with pytest.raises(ValueError, match="超过对应朗读时段"):
            SpeechSpan.model_validate(value, context={"allow_script_repair": True})


def test_partial_or_rewritten_line_cannot_replace_reviewed_whole_sentence(plan_data):
    segment = plan_data["segments"][0]
    span = segment["speech_spans"][0]
    span["text"] = "选块蛋糕。"
    span["anchors"] = [{"text": span["text"], "start": 0, "end": 7, "shot_ids": span["shot_ids"], "visual_evidence": "桌上的蛋糕切块"}]
    segment["narration"] = span["text"]
    with pytest.raises(ValueError, match="完整台词"): Plan.model_validate(plan_data)


@pytest.mark.parametrize("invalid", ["unknown_fact", "missing_fact", "wrong_beat_order"])
def test_ad_script_keeps_fact_and_beat_order_checks(plan_data, invalid):
    segment = plan_data["segments"][0]
    if invalid == "unknown_fact": segment["shots"][0]["evidence_ids"] = ["unknown"]
    elif invalid == "missing_fact": segment["shots"][1]["evidence_ids"] = []
    else: plan_data["ad_script"]["beats"].reverse()
    with pytest.raises(ValueError): Plan.model_validate(plan_data)


def test_review_still_binds_exact_script_and_manual_edit_can_clear_review(plan_data):
    plan_data["ad_script"]["core_message"] = "用蛋糕切块建立新的下午茶选择表达"
    with pytest.raises(ValueError, match="审查记录必须对应当前定稿"): Plan.model_validate(plan_data)
    plan_data["script_review"] = None
    assert Plan.model_validate(plan_data).script_review is None
    plan_data["ad_script"] = None
    assert Plan.model_validate(plan_data).ad_script is None


@pytest.mark.parametrize("explicit_audio_strategy", [True, False])
def test_independent_speech_plan_cannot_fall_back_to_voiceover_after_manual_edit(plan_data, explicit_audio_strategy):
    # Clearing reviewed metadata is allowed; it must not change how approved
    # speech windows are rendered or silently send this plan to external TTS.
    for field in ("strategy", "ad_script", "script_review"):
        plan_data[field] = None
    if explicit_audio_strategy:
        plan_data["audio_strategy"] = "voiceover"
    else:
        plan_data.pop("audio_strategy")
    with pytest.raises(ValueError):
        Plan.model_validate(plan_data)


def test_historical_plan_without_spans_keeps_shot_narration_contract(plan_data):
    segment = plan_data["segments"][0]
    segment.pop("speech_spans")
    segment["shots"] = [segment["shots"][0], segment["shots"][2]]
    segment["shots"][0].update(end=7, narration=segment["narration"], evidence_ids=["layers"])
    for shot in segment["shots"]: shot.pop("id")
    assert Plan.model_validate(plan_data, context={"require_shot_narration": True}).segments[0].speech_spans is None
    segment["shots"][0]["narration"] = ""
    with pytest.raises(ValueError, match="一个镜头中说出"): Plan.model_validate(plan_data)


def test_native_creative_alternatives_preserve_speech_timing(plan_data):
    for field in ("strategy", "ad_script", "script_review"): plan_data[field] = None
    segment = plan_data["segments"][0]
    options = [{"id": f"alt{i}", "label": f"备选{i}", "description": "不同构图", "segment": deepcopy(segment)} for i in range(2)]
    value = {"kind": "plan", "message": "请看方案", "plan": plan_data, "creative_options": options}
    assert AgentDecision.model_validate(value, context={"require_shot_narration": True})
    options[0]["segment"]["speech_spans"][0]["start"] = 0.1
    options[0]["segment"]["speech_spans"][0]["anchors"][0]["start"] = 0.1
    with pytest.raises(ValueError, match="保留首段完整旁白"): AgentDecision.model_validate(value)
