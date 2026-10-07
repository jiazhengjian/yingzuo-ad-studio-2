"""Who the ad is for is a fixed question, and the answer stays on the brief."""
from app.ad_form import intake_strategy_response
from app.audience import audience_question, known_audience_text, should_ask_audience
from app.planning import evidence_sources
from app.strategy import validate_brief


def _clarify(question_id, prompt):
    return {"kind": "clarify", "message": "再确认一件事", "questions": [
        {"id": question_id, "prompt": prompt, "options": [], "allow_custom": True, "allow_skip": True}]}


def test_a_stated_or_skipped_audience_is_not_asked_again():
    stated = {"ad_form": "brand_tvc", "cta_target": "ai_decide", "brief": "这款蛋糕卖给上班族"}
    assert should_ask_audience(stated) is False
    for brief in ("这款蛋糕放在下午茶场景", "女生主角", "这款蛋糕送给妈妈", "自己用"):
        assert should_ask_audience({"ad_form": "brand_tvc", "cta_target": "ai_decide", "brief": brief}) is True
    asked = intake_strategy_response(_clarify("product_info", "几个、多大？"), stated)
    assert all(item["id"] != "audience" for item in asked["questions"])
    project = {"ad_form": "brand_tvc", "cta_target": "ai_decide", "brief": "我想做一个蛋糕广告"}
    gates = [{"kind": "clarification", "status": "answered",
              "questions": [audience_question("蛋糕")],
              "answers": [{"question_id": "audience", "skipped": True}]}]
    assert should_ask_audience(project, gates=gates) is False
    historical = [{"kind": "clarification", "status": "answered",
                   "questions": [{"id": "audience_use", "prompt": "主要给谁看？",
                                  "options": [{"id": "styling", "label": "造型"}]}],
                   "answers": [{"question_id": "audience_use", "option_id": "styling", "text": None}]}]
    assert should_ask_audience(project, gates=historical) is False
    assert known_audience_text(historical) == "造型"
    assert known_audience_text(gates) is None


def test_unknown_form_saves_the_destination_for_the_next_round():
    project = {"ad_form": None, "cta_target": None, "brief": "我想做一个蛋糕广告"}
    opened = intake_strategy_response({"kind": "directions", "message": "三个方向"}, project)
    assert opened["kind"] == "clarify"
    assert [item["id"] for item in opened["questions"]] == ["ad_form", "highlight", "audience", "cta_target"]
    known = {"ad_form": "brand_tvc", "cta_target": None, "brief": "我想做一个蛋糕广告"}
    waiting = intake_strategy_response({"kind": "directions", "message": "三个方向"}, known)
    assert [item["id"] for item in waiting["questions"]] == ["highlight", "audience", "cta_target"]
    closed = [{"kind": "clarification", "status": "answered", "questions": [
        {"id": "highlight", "prompt": "记住哪一点？", "options": []},
        audience_question("蛋糕")],
        "answers": [
            {"question_id": "highlight", "skipped": True},
            {"question_id": "audience", "text": "送给妈妈，生日聚会"}]}]
    later = intake_strategy_response({"kind": "directions", "message": "三个方向"}, known, gates=closed)
    assert [item["id"] for item in later["questions"]] == ["cta_target"]


def test_an_audience_answer_is_kept_on_the_brief_and_as_a_fact():
    question = audience_question("蛋糕")
    answer = "送给妈妈，生日聚会"
    gates = [{"kind": "clarification", "status": "answered",
              "questions": [question],
              "answers": [{"question_id": "audience", "text": answer}]}]
    project = {"ad_form": "brand_tvc", "cta_target": "ai_decide", "brief": "我想做一个蛋糕广告，主打松软"}
    messages = [{"id": "m1", "role": "user", "content": question["prompt"] + "：" + answer}]
    raw = {"kind": "directions", "message": "三个方向", "brief": {
        "product_name": "蛋糕", "category": "food", "audience": "", "scenario": "",
        "creative_intent": {"use_context": "", "setting_requirement": "",
                            "reference_intent": "", "structure_preference": "auto"},
        "facts": [{"id": "f", "text": "蛋糕", "source_kind": "merchant", "source_id": "brief"}]}}
    result = intake_strategy_response(raw, project, messages=messages, gates=gates)
    brief = result["brief"]
    assert brief["audience"] == answer
    assert brief["scenario"] == answer
    assert brief["creative_intent"]["use_context"] == answer
    registry = evidence_sources(project, messages, gates)
    checked = validate_brief(brief, registry, set())
    assert checked.audience == answer
    fact = next(item for item in checked.facts if item.id == "known_audience")
    assert fact.text == answer and fact.source_kind == "merchant" and fact.source_id == "m1"
    skipped = [{"kind": "clarification", "status": "answered",
                "questions": [question],
                "answers": [{"question_id": "audience", "skipped": True}]}]
    quiet = intake_strategy_response(raw, project, messages=messages, gates=skipped)
    assert quiet["brief"]["audience"] == ""
    assert all(item["id"] != "known_audience" for item in quiet["brief"]["facts"])
