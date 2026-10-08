"""The existing three stages receive one shared method; no semantic claims."""
from copy import deepcopy
import pytest

from app.plan_review import lower_failed_plan_verdicts
from app.review_recovery import preserve_review_rejections
from app.runtime_skills import stage_skill_context
from test_progressive_planning import progressive_gateway, forbid_real_network
from test_providers import settings, source, install_transport
from test_character_voice_continuation import continuation_plan, continuation_review


def test_same_shared_method_reaches_three_actual_requests_and_provenance(progressive_gateway):
    state = progressive_gateway
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    methods = []
    for context in state.contexts:
        shared = context["stage_skills"]["shared_references"]
        assert [item["id"] for item in shared] == ["script-expression"]
        methods.append(shared[0])
        assert context["speech_requirements"]["speech_mode"] == "voiceover"
    assert methods[0] == methods[1] == methods[2]
    assert plan["plan_review"]["ready"]
    for stage in ("strategy", "direction_review"):
        assert "script-expression" not in [item["id"] for item in stage_skill_context(stage)["shared_references"]]
    for stage in ("legacy_storyboard", "script_review"):
        with pytest.raises(ValueError, match="尚未绑定"):
            stage_skill_context(stage)


def test_final_review_sees_actual_lines_and_pictures_not_claimed_speech_purpose(progressive_gateway):
    state = progressive_gateway
    marker = "作者声称这是有效教学，不是判断证据"
    state.draft_mutation = lambda draft: draft["beats"][0].update(purpose=marker, message=marker)
    state.run()
    final = state.contexts[-1]["final_plan"]
    assert marker not in str(final)
    assert all("narration" in beat and "visual_role" in beat for beat in final["ad_script"]["beats"])
    assert all("description" in shot for segment in final["segments"] for shot in segment["shots"])


@pytest.mark.parametrize("replacement", ["visible_speaking", "off_screen", "missing"])
def test_continuation_format_repair_cannot_erase_unclear_shot(source, replacement):
    plan = continuation_plan(source)
    previous = continuation_review(plan)
    check = previous["alignment"]["spans"][0]
    check["shot_visibility"][1]["visibility"] = "unclear"
    check["speaker_visibility"] = "unclear"
    corrected = lower_failed_plan_verdicts(previous, plan)
    shot = corrected["alignment"]["spans"][0]["shot_visibility"][1]
    if replacement == "missing":
        corrected["alignment"]["spans"][0]["shot_visibility"].pop()
    else:
        shot["visibility"] = replacement
    with pytest.raises(ValueError, match="已有失败依据"):
        preserve_review_rejections(previous, corrected, plan)


def test_legitimate_offscreen_is_not_a_negative_opinion_to_preserve(source):
    plan = continuation_plan(source)
    previous = continuation_review(plan)
    corrected = deepcopy(previous)
    corrected["alignment"]["spans"][0]["note"] = "格式补正保留同一人和完整逐镜结论。"
    assert preserve_review_rejections(previous, corrected, plan) == corrected
