"""Type playbooks, per-type speech caps, and non-blocking type-fit advice."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.ad_form_playbook import (
    ending_policy, resolve_playbook_form, review_finding_disposition, speech_profile,
    speech_profile_for_strategy, tagline_policy,
)
from app.ad_script import script_context, validate_script
from app.beat_timing import speech_ticks
from app.plan_review import validate_plan_review
from app.review_policy import REVIEW_POLICY_VERSION
from app.runtime_skills import stage_skill_context
from app.strategy import CreativeDirection, ProductBrief, build_strategy, select_rules
from test_plan_review import plan, review


PLAYBOOKS = {
    "brand_tvc": "ad-form-playbook-brand-tvc",
    "scene_story": "ad-form-playbook-scene-story",
    "presenter_pitch": "ad-form-playbook-presenter-pitch",
}
FULL = {
    "brand_tvc": "shared/ad-form-playbooks/brand-tvc.md",
    "scene_story": "shared/ad-form-playbooks/scene-story.md",
    "presenter_pitch": "shared/ad-form-playbooks/presenter-pitch.md",
}


def _ids(stage, form=None):
    kwargs = {"playbook_form": form} if form is not None else {}
    return [item["id"] for item in stage_skill_context(stage, **kwargs)["shared_references"]]


def _strategy(form, structure):
    brief = ProductBrief(product_name="玻璃瓶", category="general", audience="办公的人", scenario="书桌",
        ad_form=form, facts=[{"id": "shape", "text": "瓶身轮廓", "source_kind": "image", "source_id": "photo"}])
    direction = CreativeDirection(id="chosen", rule_id="appearance_reveal", label="看见瓶身",
        audience=brief.audience, scenario=brief.scenario, angle="用轮廓建立辨认",
        hook="先看到局部", development="再看到整瓶", payoff="停在瓶身", evidence_ids=["shape"],
        reason="原图只支持外观", production_notes=["不编功效"],
        film_concept={"structure": structure, "key_visual": "瓶身从局部到完整",
                      "memory_anchor": "同一瓶身轮廓", "sound_role": "短句补充外观"})
    return build_strategy(brief, [direction], direction.id, select_rules(brief))


def _script(narration):
    return {"core_message": "记住这只瓶的轮廓", "audience_takeaway": "桌上看见同一瓶身",
            "product_role": "瓶身轮廓承担辨认", "evidence_ids": ["shape"], "skill_version": "1.3.0",
            "beats": [{"id": "open", "purpose": "建立辨认", "message": "轮廓可以被看见",
                       "visual_role": "瓶身停在桌面", "narration": narration, "evidence_ids": ["shape"]}]}


def test_only_the_matching_playbook_is_injected():
    untyped = stage_skill_context("ad_script")
    assert [item["id"] for item in untyped["shared_references"]] == ["script-expression"]
    hashes = {}
    for form, identifier in PLAYBOOKS.items():
        loaded = stage_skill_context("ad_script", playbook_form=form)
        ids = [item["id"] for item in loaded["shared_references"]]
        assert ids == ["script-expression", identifier]
        assert all(item["path"] != path or item["id"] == identifier for item in loaded["shared_references"] for path in FULL.values())
        body = next(item["content"] for item in loaded["shared_references"] if item["id"] == identifier)
        assert "英雄定格" in body and "映作原创" in body
        hashes[form] = loaded["content_sha256"]
    assert len(set(hashes.values())) == 3
    assert _ids("strategy", "auto") == ["product-evidence", "ad-form-contract", "ad-form-playbook-index"]
    assert "ad-form-playbook-brand-tvc" not in _ids("ad_script", "auto")
    brand_copy = stage_skill_context("speech_draft", playbook_form="brand_tvc")
    assert [item["id"] for item in brand_copy["shared_references"]] == ["ad-form-playbook-brand-tvc"]
    assert stage_skill_context("speech_draft")["shared_references"] == []


def test_auto_uses_the_selected_structure_after_a_direction_exists():
    assert resolve_playbook_form("auto") == "auto"
    assert resolve_playbook_form("auto", "scene_action") == "scene_story"
    assert resolve_playbook_form("brand_tvc", "scene_action") == "brand_tvc"
    assert resolve_playbook_form(None, "presenter_explanation") is None
    loaded = stage_skill_context("storyboard", playbook_form=resolve_playbook_form("auto", "presenter_explanation"))
    assert [item["id"] for item in loaded["shared_references"]] == ["script-expression", "ad-form-playbook-presenter-pitch"]


def test_default_rates_and_legacy_ticks_stay_compatible(monkeypatch):
    assert speech_profile(None).characters_per_tenth == 29
    assert speech_profile("brand_tvc").characters_per_second == 3.0
    assert speech_profile("scene_story").max_beat_characters == 38
    assert speech_profile("presenter_pitch").characters_per_tenth == 48
    assert speech_ticks("字" * 15) == (15 * 100 + 28) // 29
    monkeypatch.setenv("AD_SPEECH_RATE_PRESENTER_PITCH", "5.0")
    monkeypatch.setenv("AD_BEAT_CHARS_PRESENTER_PITCH", "40")
    tuned = speech_profile("presenter_pitch")
    assert tuned.characters_per_tenth == 50 and tuned.max_beat_characters == 40
    monkeypatch.setenv("AD_SPEECH_RATE_BRAND_TVC", "9")
    assert speech_profile("brand_tvc").characters_per_tenth == 30


def test_validate_script_uses_the_type_cap_and_keeps_the_legacy_beat_limit():
    legacy = _strategy(None, "product_visual")
    assert speech_profile_for_strategy(legacy).max_beat_characters == 29
    assert validate_script(_script("看这只瓶的轮廓。"), legacy, 15)
    with pytest.raises(ValueError, match="单拍"):
        validate_script(_script("字" * 29 + "。"), legacy, 15)
    presenter = _strategy("presenter_pitch", "presenter_explanation")
    long_line = "你先看这一条边，它就是开合的位置，每天拿一次就放在手边，不用再找。"
    assert len(long_line) == 33
    assert validate_script(_script(long_line), presenter, 15)
    with pytest.raises(ValueError, match="单拍"):
        validate_script(_script("字" * 48 + "。"), presenter, 15)
    context = script_context(presenter, 15, skill_stage="ad_script")
    assert context["schedule_rules"]["characters_per_second"] == 4.8
    assert context["schedule_rules"]["max_beat_characters"] == 48
    assert any(item["id"] == "ad-form-playbook-presenter-pitch" for item in context["stage_skills"]["shared_references"])
    assert context["tagline_policy"]["enabled"] is False
    assert "不要说去哪里" in context["ending_policy"]["instruction"]
    assert "点下面" not in context["ending_policy"]["instruction"]
    from app.ad_form_playbook import ending_policy as ending
    assert "点下面链接看看" in ending("presenter_pitch", "link")["instruction"]
    assert "点下面" not in ending("presenter_pitch", "ai_decide")["instruction"]
    assert "英雄定格" in ending_policy("brand_tvc")["instruction"]
    assert "定格" in ending_policy("scene_story")["instruction"]


def test_tagline_stays_off_until_the_flag_is_set(monkeypatch):
    assert tagline_policy()["enabled"] is False
    assert "不写口号" in tagline_policy()["instruction"]
    monkeypatch.setenv("AD_TAGLINE_ENABLED", "true")
    enabled = tagline_policy()
    assert enabled["enabled"] is True and enabled["max_characters"] == 10
    assert "字卡" in enabled["instruction"]


def test_type_fit_is_recorded_advice_and_does_not_block(plan, review):
    assert review_finding_disposition("type_fit") == "suggestion"
    assert review_finding_disposition("missing_fact") == "blocking"
    raw = deepcopy(review)
    raw.update(policy_version=REVIEW_POLICY_VERSION, suggestions=[{
        "kind": "type_fit", "target": "/segments/0/shots/0/description",
        "quote": "手按已确认的风速键。",
        "note": "开场可以先给产品局部，再进入这一个操作。"}])
    bound = validate_plan_review(raw, plan)
    assert bound.ready is True
    assert bound.suggestions[0].kind == "type_fit"


def test_speech_ticks_follow_the_supplied_tenth():
    assert speech_ticks("字" * 10, 48) == (1000 + 47) // 48
    assert speech_ticks("字" * 10, 30) < speech_ticks("字" * 10, 29)
