"""Corrections receive the current subject without superseded execution drafts."""
from copy import deepcopy

from app.planner_runtime import planner_wire_context


def direction():
    return {"id": "selected", "story_summary": "They share a product and leave together.",
        "attraction_mechanism": "Someone remembered their friend's preference.",
        "angle": "A thoughtful choice", "evidence_ids": ["appearance"],
        "production": {"characters": [{"id": "char_friend"}], "speaker_id": "char_friend"},
        "hook": "Earlier execution", "development": "Superseded action order",
        "payoff": "Earlier end action", "production_notes": ["Old detailed sequence"],
        "reason": "Author's justification"}


def test_refinement_keeps_current_content_feedback_and_user_requirements():
    context = {"stage": "plan_refinement", "selected_direction": direction(),
        "draft_script": {"beats": [{"id": "discarded_initial"}]},
        "draft_capacity": {"overrun_seconds": 12}, "draft_capacity_basis": "Old estimate",
        "draft_validation_issues": ["Initial issue"],
        "draft_storyboard": {"ad_script": {"beats": [{"id": "current"}]}, "shots": [{"id": "current_shot"}]},
        "review_feedback": {"issue": "Current shot needs more time"},
        "repair_scope": {"shot_ids": ["current_shot"]},
        "conversation": [{"role": "user", "content": "Keep my exact line and ending."}],
        "fixed_speech": ["My exact line."], "duration_seconds": 20,
        "brief": {"facts": [{"id": "appearance", "text": "One blue bottle"}]}}
    before = deepcopy(context)
    wire = planner_wire_context(context)
    assert context == before
    assert not {"draft_script", "draft_capacity", "draft_capacity_basis", "draft_validation_issues"} & wire.keys()
    for field in ("draft_storyboard", "review_feedback", "repair_scope", "conversation", "fixed_speech", "duration_seconds", "brief"):
        assert wire[field] == context[field]
    for field in ("id", "story_summary", "attraction_mechanism", "angle", "evidence_ids", "production"):
        assert wire["selected_direction"][field] == context["selected_direction"][field]


def test_initial_director_still_receives_content_draft_and_capacity():
    context = {"stage": "storyboard", "selected_direction": direction(),
        "draft_script": {"beats": [{"id": "initial"}]}, "draft_capacity": {"overrun_seconds": 12}}
    wire = planner_wire_context(context)
    assert wire["draft_script"] == context["draft_script"]
    assert wire["draft_capacity"] == context["draft_capacity"]


def test_review_preserves_actual_plan_and_only_selected_story():
    actual = {"production": {"speaker_id": "char_friend"}, "script": "Exact words.",
        "ad_script": {"beats": [{"id": "final_beat"}]},
        "segments": [{"shots": [{"id": "final_shot", "description": "Actual action"}],
                      "speech_spans": [{"text": "Exact words.", "start": 1, "end": 4}]}],
        "facts": {"claims": ["Verified fact"]}}
    context = {"stage": "plan_review", "final_plan": {**deepcopy(actual),
        "strategy": {"selected_direction_id": "selected", "brief": {"facts": ["source"]},
                     "directions": [direction(), {"id": "unselected"}]}},
        "execution_windows": [{"visual_start": 0, "visual_end": 1}]}
    before = deepcopy(context)
    wire = planner_wire_context(context)
    assert context == before
    for field, value in actual.items():
        assert wire["final_plan"][field] == value
    assert wire["execution_windows"] == context["execution_windows"]
    assert [d["id"] for d in wire["final_plan"]["strategy"]["directions"]] == ["selected"]
    assert wire["final_plan"]["strategy"]["brief"] == {"facts": ["source"]}


def test_legacy_direction_keeps_its_only_narrative():
    legacy = {"id": "old", "hook": "Start", "development": "Continue", "payoff": "Finish"}
    context = {"stage": "ad_script", "selected_direction": legacy}
    assert planner_wire_context(context) == context


def test_authors_keep_all_merchant_corrections_without_repeating_old_assistant_proposals():
    conversation = [{"role": "user", "content": "Keep the two characters."},
        {"role": "assistant", "content": "Old proposed sequence to be corrected."},
        {"role": "user", "content": "Simplify the actions but keep the ending."}]
    for stage in ("ad_script", "storyboard", "plan_refinement", "plan_review"):
        context = {"stage": stage, "conversation": deepcopy(conversation), "selected_direction": direction()}
        wire = planner_wire_context(context)
        assert wire["conversation"] == [conversation[0], conversation[2]]
        assert wire["selected_direction"]["story_summary"] == direction()["story_summary"]
        assert context["conversation"] == conversation
    # The intent/clarification stage still needs full dialogue to resolve replies
    # such as "use your previous suggestion" before passing on the requirement.
    strategy = {"stage": "strategy", "conversation": conversation}
    assert planner_wire_context(strategy)["conversation"] == conversation


def test_refinement_receives_compiled_clocks_without_duplicate_authored_plan():
    draft = {"shots": [{"id": "s1", "description": "The current action."}]}
    segment = {"id": "seg_1", "duration_seconds": 20,
        "shots": [{"id": "s1", "description": "The current action.", "start": 0, "end": 20, "beat_ids": ["b1"]}],
        "speech_spans": [{"text": "Exact words", "start": 4, "end": 10, "shot_ids": ["s1"]}]}
    context = {"stage": "plan_refinement", "draft_storyboard": draft,
        "final_plan": {"ad_script": {"beats": ["duplicate"]}, "segments": [segment]}}
    wire = planner_wire_context(context)
    assert wire["draft_storyboard"] == draft
    assert "final_plan" not in wire
    actual = wire["final_execution"][0]
    assert actual["duration_seconds"] == 20
    assert actual["shots"] == [{"id": "s1", "start": 0, "end": 20, "beat_ids": ["b1"]}]
    assert actual["speech_spans"] == segment["speech_spans"]
    assert context["final_plan"]["ad_script"] == {"beats": ["duplicate"]}
