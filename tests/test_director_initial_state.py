"""Initial state references must remove copying work, never authored conflicts."""
from copy import deepcopy

import httpx
import pytest

from app.directing import ShotContinuity, derive_continuous_start_states, storyboard_continuity_schema
from app.progressive_planning import compile_director, director_schema, refinement_schema
from app.schemas import Plan
from test_progressive_planning import (
    chosen_strategy, director_output, progressive_gateway, reject_first_shot,
)
from test_providers import settings, source, install_transport


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Initial-state tests must not contact a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


def continuity_schema(schema):
    return schema["$defs"]["ShotContinuity"]


def test_initial_contract_does_not_change_refinement_or_stored_state_requirements():
    canonical = ShotContinuity.model_json_schema()
    legacy_wire = storyboard_continuity_schema()
    initial = continuity_schema(director_schema(initial=True))
    refinement = continuity_schema(refinement_schema({"allow_summary_update": False}))
    # The initial-only branch forbids transcribing the previous state. Every
    # other transition still requires its own complete authored start state.
    branch = initial["allOf"][0]
    assert branch["if"] == {"properties": {"entry_transition": {"const": "continuous"}},
                            "required": ["entry_transition"]}
    assert branch["then"] == {"not": {"required": ["start_state"]}}
    assert branch["else"] == {"required": ["start_state"]}
    assert refinement == legacy_wire == continuity_schema(director_schema())
    assert "then" not in refinement["allOf"][0]
    assert "start_state" in canonical["required"]
    assert ShotContinuity.model_json_schema() == canonical


def test_actual_initial_and_refinement_requests_use_separate_state_contracts(progressive_gateway):
    state = progressive_gateway
    state.first_review_mutation = reject_first_shot
    # The initial response follows the new contract; refinement receives the
    # complete compiled states and may keep those explicit values unchanged.
    def omit_continuous_starts(value):
        for shot in value["shots"][1:]:
            shot["continuity"].pop("start_state")
    state.first_director_mutation = omit_continuous_starts
    plan = state.run()
    assert state.calls == ["ad_script", "storyboard", "plan_review", "plan_refinement", "plan_review"]
    initial, repair = state.contexts[1], state.contexts[3]
    assert "then" in continuity_schema(initial["schema"])["allOf"][0]
    assert "then" not in continuity_schema(repair["schema"])["allOf"][0]
    assert all("start_state" in shot["continuity"] for shot in repair["draft_storyboard"]["shots"])
    assert plan["plan_review"]["ready"] is True
    # Independent review sees the complete final states, including states
    # inherited by the server, rather than the incomplete provider response.
    for actual, reviewed in zip(plan["segments"], state.contexts[-1]["final_plan"]["segments"]):
        for shot, review_shot in zip(actual["shots"], reviewed["shots"]):
            for key in ("start_state", "end_state", "allowed_changes", "camera_move"):
                assert review_shot["continuity"][key] == shot["continuity"][key]


@pytest.mark.parametrize("duration", [20, 45])
def test_continuous_camera_changes_keep_complete_states_across_provider_groups(source, duration):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    physical_state = "同一蓝瓶直立在办公桌左前角，瓶盖完整，办公者坐在桌后，未发生取放。"
    cameras = ["正面中景缓慢移近", "保持轴线切至瓶盖近景", "保持轴线拉回桌角中景"]
    for index, shot in enumerate(raw["shots"]):
        shot["continuity"].update(start_state=physical_state, end_state=physical_state,
                                  camera_move=cameras[index])
        if index:
            shot["continuity"].pop("start_state")
    before = deepcopy(raw)
    expected_shots = deepcopy(raw["shots"])
    for shot in expected_shots[1:]:
        shot["continuity"]["start_state"] = physical_state
    plan, _, coordinated = compile_director(raw, strategy, duration, "continuity_v1", [source["id"]])
    assert raw == before
    assert coordinated["shots"] == expected_shots
    assert len(plan["segments"]) == (1 if duration == 20 else 2)
    assert sum(segment["duration_seconds"] for segment in plan["segments"]) == duration
    shots = [shot for segment in plan["segments"] for shot in segment["shots"]]
    assert [shot["continuity"]["camera_move"] for shot in shots] == cameras
    assert [shot["description"] for shot in shots] == [shot["description"] for shot in before["shots"]]
    for previous, current in zip(shots, shots[1:]):
        assert current["continuity"]["start_state"] == previous["continuity"]["end_state"] == physical_state
    if duration > 30:
        assert plan["segments"][1]["shots"][0]["continuity"]["entry_transition"] == "continuous"
        assert plan["segments"][1]["shots"][0]["continuity"]["start_state"] == plan["segments"][0]["shots"][-1]["continuity"]["end_state"]
    assert Plan.model_validate(plan, context={"require_directing_contract": True})


@pytest.mark.parametrize("explicit", ["同一件商品保持静止", "商品已被取走，桌面为空", "", None])
def test_provider_ignoring_initial_contract_cannot_have_conflict_removed(source, explicit):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["shots"][1]["continuity"]["start_state"] = explicit
    before = deepcopy(raw)
    derived = derive_continuous_start_states({"segments": [{"shots": raw["shots"]}]})
    assert derived["segments"][0]["shots"][1]["continuity"]["start_state"] == explicit
    with pytest.raises(ValueError):
        compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])
    assert raw == before


@pytest.mark.parametrize("transition", ["opening", "cut", "scene_change"])
def test_noncontinuous_missing_start_state_is_not_inherited(source, transition):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    index = 0 if transition == "opening" else 1
    raw["shots"][index]["continuity"].update(entry_transition=transition)
    raw["shots"][index]["continuity"].pop("start_state")
    before = deepcopy(raw)
    with pytest.raises(ValueError, match="start_state"):
        compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])
    assert raw == before


@pytest.mark.parametrize("omit", [True, False])
def test_first_shot_cannot_inherit_a_nonexistent_predecessor(source, omit):
    strategy = chosen_strategy(source)
    raw = director_output(strategy)
    raw["shots"][0]["continuity"]["entry_transition"] = "continuous"
    if omit:
        raw["shots"][0]["continuity"].pop("start_state")
    before = deepcopy(raw)
    with pytest.raises(ValueError):
        compile_director(raw, strategy, 20, "continuity_v1", [source["id"]])
    assert raw == before
