"""Ending destination is asked once, then spoken only when the merchant chose it."""
from app.ad_form import ad_form_question, intake_strategy_response
from app.cta import apply_spoken_policy, cta_line, cta_question, should_ask_cta, spoken_cta_line, stated_cta
from app.progressive_planning import compile_director
from test_progressive_planning import chosen_strategy, director_output
from test_providers import source


def test_clarification_asks_where_to_send_the_viewer_unless_the_brief_already_says():
    project = {"ad_form": "presenter_pitch", "cta_target": None, "brief": "介绍这只吹风机"}
    raw = {"kind": "clarify", "message": "再确认一件事", "questions": [
        {"id": "audience", "prompt": "给谁看？", "options": [{"id": "self", "label": "自己用"}],
         "allow_custom": True, "allow_skip": True}]}
    result = intake_strategy_response(raw, project)
    assert [item["id"] for item in result["questions"]] == ["audience", "cta_target"]
    destination = result["questions"][1]
    assert destination["prompt"] == "看完视频后，希望观众去哪里？"
    assert [item["label"] for item in destination["options"]] == [
        "商品页 / 购买链接", "品牌页 / 产品详情", "私信 / 咨询购买", "到店 / 门店地址", "让 AI 决定"]
    assert should_ask_cta({"ad_form": "presenter_pitch", "cta_target": None, "brief": "结尾点链接购买"}) is False
    assert stated_cta("让他们私信我") == "message"
    assert stated_cta("到店看看") == "store"
    skipped = intake_strategy_response(raw, {**project, "brief": "看完点下面链接"})
    assert all(item["id"] != "cta_target" for item in skipped["questions"])
    assert cta_question()["allow_custom"] is True
    directions = intake_strategy_response(
        {"kind": "directions", "message": "三个方向"}, project)
    assert directions["kind"] == "clarify"
    assert [item["id"] for item in directions["questions"]] == ["cta_target"]


def test_open_endings_drop_guidance_and_an_explicit_target_is_required(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["ad_script"]["beats"][-1]["narration"] = "想要的话，点下面链接看看。"
    raw["video_flow"] = "先看见商品，再停住。"
    for shot in raw["shots"]:
        shot["product_visible"] = True
        shot["continuity"]["camera_move"] = "中景，机位固定"
    open_plan, _, _ = compile_director(raw, strategy, 20, "continuity_v1", [source["id"]], cta_target="ai_decide")
    open_line = open_plan["segments"][-1]["speech_spans"][-1]["text"]
    assert "点下面" not in open_line and "链接" not in open_line
    assert open_plan["segments"][-1]["speech_spans"][-1]["end"] <= 18 + 1e-6
    linked, _, _ = compile_director(raw, strategy, 20, "continuity_v1", [source["id"]], cta_target="link")
    assert "点下面链接看看" in linked["segments"][-1]["speech_spans"][-1]["text"]
    quiet = apply_spoken_policy({"beats": [{"id": "b0", "narration": "价格399。想要的话，点下面看看。"}]},
                                strategy, "none")
    assert "399" not in quiet["beats"][0]["narration"]
    assert "点下面" not in quiet["beats"][0]["narration"]
    priced = apply_spoken_policy({"beats": [{"id": "b0", "narration": "价格399。"}]}, strategy, "link")
    assert "399" not in priced["beats"][0]["narration"] or "点下面" in priced["beats"][-1]["narration"]


def test_each_ad_type_speaks_the_link_in_its_own_voice():
    presenter = spoken_cta_line("link", "presenter_pitch")
    brand = spoken_cta_line("link", "brand_tvc")
    story_voice = spoken_cta_line("link", "scene_story", {"beats": [{"narration": "她还在睡。"}]})
    story_line = spoken_cta_line("link", "scene_story", {"beats": [{"narration": "我还没醒。"}]})
    assert presenter != brand
    assert story_voice == story_line == ""
    assert "你" in presenter and "这款" in presenter and "点下面链接" in presenter
    assert "我" not in brand and "点下面链接" in brand
    assert "点下面" not in story_voice and "链接" not in story_line
    assert spoken_cta_line("link", "unknown-form") == cta_line("link")
    assert "399" not in presenter and "折" not in brand
