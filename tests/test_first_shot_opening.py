"""New director boundary labels, without repairing authored film semantics."""
from copy import deepcopy

import pytest

from app.plan_review import lower_failed_plan_verdicts, validate_plan_review
from app.progressive_planning import canonical_director, compile_director
from app.schemas import Plan
from test_progressive_planning import (
    accepted_review, chosen_strategy, director_output, forbid_real_network,
    progressive_gateway,
)
from test_providers import settings, source, install_transport


def compile_draft(draft, strategy, source, locked=()):
    return compile_director(draft, strategy, 20, "continuity_v1", [source["id"]], locked)


@pytest.mark.parametrize("transition", ["cut", "scene_change"])
def test_only_new_film_first_entry_label_is_owned_by_the_compiler(source, transition):
    strategy = chosen_strategy(source)
    original = director_output(strategy)
    # Content boundary and physical starting state stay authored. Neither
    # changes the fact that the first shot begins this film.
    original["ad_script"]["beats"][0]["boundary_before"] = "scene_change"
    raw = deepcopy(original)
    raw["shots"][0]["continuity"]["entry_transition"] = transition
    before = deepcopy(raw)
    expected = compile_draft(original, strategy, source)
    actual = compile_draft(raw, strategy, source)
    assert actual == expected
    assert raw == before
    assert actual[0]["segments"][0]["shots"][0]["continuity"]["entry_transition"] == "opening"
    assert "plan_review" not in actual[0]


@pytest.mark.parametrize("transition", ["cut", "scene_change"])
@pytest.mark.parametrize("state", ["missing", None, "", " ", 123])
def test_missing_hard_cut_start_state_is_derived_and_does_not_fail(source, transition, state):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    first = raw["shots"][0]["continuity"]
    first["entry_transition"] = transition
    if state == "missing":
        first.pop("start_state")
    else:
        first["start_state"] = state
    before = deepcopy(raw)
    plan, _, _ = compile_draft(raw, strategy, source)
    assert raw == before
    opening = plan["segments"][0]["shots"][0]["continuity"]
    assert opening["entry_transition"] == "opening"
    assert isinstance(opening["start_state"], str) and opening["start_state"].strip()


@pytest.mark.parametrize("transition", ["continuous", "invented", None])
def test_first_continuous_or_unknown_entry_is_not_normalized(source, transition):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["shots"][0]["continuity"]["entry_transition"] = transition
    before = deepcopy(raw)
    with pytest.raises(ValueError):
        compile_draft(raw, strategy, source)
    assert raw == before


@pytest.mark.parametrize("problem,match", [
    ("later_opening", "只有全片第一镜"),
    ("later_scene_change", "场景转换只能"),
    ("state_drift", "逐字继承"),
    ("foreign_fact", "已有商品事实"),
    ("foreign_character", "已确认的出镜角色"),
    ("over_capacity", "完整台词无法为对应画面留出时间"),
    ("fixed_speech", "用户明确固定的台词"),
])
def test_first_label_normalization_does_not_bypass_other_contracts(source, problem, match):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["shots"][0]["continuity"]["entry_transition"] = "scene_change"
    second = raw["shots"][1]["continuity"]
    locked = ()
    if problem == "later_opening":
        second["entry_transition"] = "opening"
    elif problem == "later_scene_change":
        second["entry_transition"] = "scene_change"
    elif problem == "state_drift":
        second["start_state"] = "商品已经被吃掉，原位只剩空盘。"
    elif problem == "foreign_fact":
        raw["shots"][0]["evidence_ids"] = ["invented_fact"]
    elif problem == "foreign_character":
        raw["shots"][0]["character_ids"] = ["char_other"]
    elif problem == "over_capacity":
        raw["ad_script"]["beats"][0]["timing"]["visual_seconds"] = 25
    else:
        locked = ("用户逐字固定的另一句。",)
    before = deepcopy(raw)
    with pytest.raises(ValueError, match=match):
        compile_draft(raw, strategy, source, locked)
    assert raw == before


def test_opening_normalization_keeps_omission_evidence_for_independent_review(source):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    first = raw["shots"][0]
    first["continuity"]["entry_transition"] = "scene_change"
    first["description"] = "同一镜头内人物刚把食物入口，立即按已吞咽完成记录；没有后续切点。"
    first["continuity"]["entry_reason"] = "作者声称本镜开始前的切换可以省略本镜内的吞咽。"
    before = deepcopy(raw)
    canonical = canonical_director(raw, strategy).model_dump()
    assert canonical["shots"][0]["description"] == first["description"]
    assert canonical["shots"][0]["continuity"]["entry_reason"] == first["continuity"]["entry_reason"]
    assert raw == before
    plan = compile_draft(raw, strategy, source)[0]
    assert "plan_review" not in plan
    review = accepted_review(plan)
    review["alignment"]["shots"][0].update(temporal_omission="unsupported",
        note="首镜opening不能省略镜内入口之后的吞咽；实际没有对应切点。")
    checked = validate_plan_review(lower_failed_plan_verdicts(review, plan), plan)
    assert checked.ready is False
    assert checked.alignment.shots[0].passed is False


@pytest.mark.parametrize("transition", ["cut", "scene_change"])
def test_existing_stored_plan_still_rejects_nonopening_first_shot(source, transition):
    strategy = chosen_strategy(source)
    plan = compile_draft(director_output(strategy), strategy, source)[0]
    plan["segments"][0]["shots"][0]["continuity"]["entry_transition"] = transition
    with pytest.raises(ValueError, match="全片第一镜"):
        Plan.model_validate(plan)


def test_new_first_scene_change_reaches_independent_review_without_extra_call(progressive_gateway):
    state = progressive_gateway
    state.first_director_mutation = lambda draft: draft["shots"][0]["continuity"].update(entry_transition="scene_change")
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    first = state.contexts[-1]["final_plan"]["segments"][0]["shots"][0]
    assert first["continuity"]["entry_transition"] == "opening"
    assert plan["plan_review"]["ready"] is True
