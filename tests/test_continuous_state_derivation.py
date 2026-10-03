"""New-wire state references without weakening historical Plan validation."""
import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.directing import (ShotContinuity, derive_continuous_start_states,
                           storyboard_continuity_schema)
from app.providers.gateway import Gateway, ProviderError
from app.schemas import Plan
from app.strategy import build_strategy, select_rules
from ad_script_fixtures import CRITERIA, execution_check, with_action_timing
from test_directing_contract import directed_plan_data
from test_speech_spans import plan_data
from test_direction_selection import candidates_for, compiled_from_reservations, directed_review
from test_providers import settings, source, strategy_stage, install_transport, chat_output, request_context


def shots(plan):
    return [shot for segment in plan["segments"] for shot in segment["shots"]]


def test_wire_schema_permits_only_continuous_omission_without_changing_storage():
    canonical = ShotContinuity.model_json_schema()
    wire = storyboard_continuity_schema()
    assert "start_state" in canonical["required"]
    assert "start_state" not in wire["required"]
    assert wire["allOf"] == [{
        "if": {"properties": {"entry_transition": {"const": "continuous"}},
               "required": ["entry_transition"]},
        "else": {"required": ["start_state"]},
    }]
    assert wire["properties"]["start_state"]["type"] == "string"
    assert wire["properties"]["start_state"]["minLength"] == 1
    assert ShotContinuity.model_json_schema() == canonical
    wire["properties"]["start_state"]["minLength"] = 0
    assert storyboard_continuity_schema()["properties"]["start_state"]["minLength"] == 1


def test_new_continuous_omission_is_derived_without_mutating_raw_or_other_fields(directed_plan_data):
    raw = deepcopy(directed_plan_data)
    for shot in shots(raw)[1:]: shot["continuity"].pop("start_state")
    before = deepcopy(raw)
    with pytest.raises(ValidationError): Plan.model_validate(raw)
    complete = derive_continuous_start_states(raw)
    assert raw == before
    assert complete == directed_plan_data
    assert Plan.model_validate(complete, context={"require_directing_contract": True})
    for previous, current in zip(shots(complete), shots(complete)[1:]):
        assert current["continuity"]["start_state"] == previous["continuity"]["end_state"]


@pytest.mark.parametrize("transition", ["opening", "cut", "scene_change"])
def test_noncontinuous_states_remain_authored_and_required(directed_plan_data, transition):
    raw = deepcopy(directed_plan_data)
    continuity = shots(raw)[0 if transition == "opening" else 1]["continuity"]
    continuity.update(entry_transition=transition)
    continuity.pop("start_state")
    complete = derive_continuous_start_states(raw)
    assert complete == raw
    with pytest.raises(ValidationError, match="start_state"):
        Plan.model_validate(complete)


@pytest.mark.parametrize("omit", [False, True])
def test_first_shot_cannot_claim_a_continuous_predecessor(directed_plan_data, omit):
    raw = deepcopy(directed_plan_data)
    continuity = shots(raw)[0]["continuity"]
    continuity["entry_transition"] = "continuous"
    if omit: continuity.pop("start_state")
    complete = derive_continuous_start_states(raw)
    assert complete == raw
    with pytest.raises(ValidationError): Plan.model_validate(complete)


@pytest.mark.parametrize("authored", [None, "", " ", "商品已经被取走，原位置只剩空盘"])
def test_explicit_invalid_state_is_never_repaired_or_overwritten(directed_plan_data, authored):
    raw = deepcopy(directed_plan_data)
    shots(raw)[1]["continuity"]["start_state"] = authored
    complete = derive_continuous_start_states(raw)
    assert complete == raw
    assert shots(complete)[1]["continuity"]["start_state"] == authored
    with pytest.raises(ValidationError): Plan.model_validate(complete)


def test_authored_paraphrase_remains_rejected_instead_of_guessed_equivalent(directed_plan_data):
    raw = deepcopy(directed_plan_data)
    shots(raw)[1]["continuity"]["start_state"] = "同一完整蛋糕切块仍然静置在办公桌餐盘中央"
    complete = derive_continuous_start_states(raw)
    assert complete == raw
    with pytest.raises(ValidationError, match="逐字继承"):
        Plan.model_validate(complete)


def test_omitted_state_inherits_across_generation_boundary(directed_plan_data):
    raw = deepcopy(directed_plan_data)
    raw.pop("ad_script")
    raw.pop("script_review")
    second = deepcopy(raw["segments"][0])
    second.update(id="seg2", narration="", speech_spans=[])
    for shot in second["shots"]:
        shot["continuity"]["entry_transition"] = "continuous"
        shot["continuity"].pop("start_state")
    raw["segments"].append(second)
    complete = derive_continuous_start_states(raw)
    assert complete["segments"][1]["shots"][0]["continuity"]["start_state"] == raw["segments"][0]["shots"][-1]["continuity"]["end_state"]
    assert Plan.model_validate(complete)


def test_missing_predecessor_cannot_borrow_state_from_an_earlier_shot(directed_plan_data):
    raw = deepcopy(directed_plan_data)
    shots(raw)[1].pop("continuity")
    shots(raw)[2]["continuity"].pop("start_state")
    complete = derive_continuous_start_states(raw)
    assert complete == raw
    with pytest.raises(ValidationError): Plan.model_validate(complete)


def test_stored_plans_are_never_implicitly_upgraded_or_changed(directed_plan_data, plan_data):
    assert Plan.model_validate(plan_data).model_dump() == Plan.model_validate(deepcopy(plan_data)).model_dump()
    assert derive_continuous_start_states(plan_data) == plan_data
    assert derive_continuous_start_states(directed_plan_data) == directed_plan_data
    missing = deepcopy(directed_plan_data)
    shots(missing)[1]["continuity"].pop("start_state")
    for context in ({}, {"require_directing_contract": True}):
        with pytest.raises(ValidationError, match="start_state"):
            Plan.model_validate(missing, context=context)


@pytest.mark.parametrize("new_flow,mutation,accepted", [
    (True, "omitted", True), (True, "authored_drift", False),
    (True, "cut_omitted", False), (False, "omitted", False),
])
def test_gateway_derives_only_new_wire_omissions_without_extra_model_requests(
        settings, source, strategy_stage, install_transport, tmp_path, new_flow, mutation, accepted):
    settings.data_root = tmp_path / "data"
    settings.direction_selection_enabled = new_flow
    settings.planning_generation_strategy = "continuity_v1"
    candidates = candidates_for((None, None, None, source))
    strategy = build_strategy(candidates.brief, candidates.directions, "direction_1", select_rules(None), merchant_choice=True)
    seen, compiled = [], None

    def handle(request):
        nonlocal compiled
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        _, context = request_context(request)
        stage = context["stage"]
        seen.append(stage)
        if stage == "strategy":
            assert not new_flow
            return chat_output(strategy_stage)
        if stage == "ad_script":
            direction = context["selected_direction"]
            evidence = direction["evidence_ids"]
            result = {"core_message": direction["angle"], "audience_takeaway": "了解同一商品与桌面外观的关系",
                "product_role": "商品承担主体表达", "evidence_ids": evidence,
                "skill_version": context["runtime_skill"]["version"],
                "beats": [{"id": "one", "purpose": "呈现商品与桌面搭配", "message": "桌面上的商品外观",
                    "visual_role": "同一商品保持摆位，镜头缓慢靠近", "narration": "给桌面一点喜欢的样子。", "evidence_ids": evidence}]}
            return chat_output(with_action_timing(result) if new_flow else result)
        if stage == "script_review":
            result = {"script": context["draft_script"], "ready": True,
                "checks": [{"criterion": key, "passed": True, "note": "离线固定夹具"} for key in CRITERIA]}
            if new_flow: result["execution_check"] = execution_check()
            return chat_output(result)
        if stage == "storyboard":
            schema = context["schema"]["$defs"]["ShotContinuity"]
            assert ("start_state" not in schema["required"]) == new_flow
            compiled = compiled_from_reservations(context)
            continuity = shots(compiled)[1]["continuity"]
            if mutation == "authored_drift": continuity["start_state"] = "商品被拿走，桌面空了"
            else:
                continuity.pop("start_state")
                if mutation == "cut_omitted": continuity["entry_transition"] = "cut"
            return chat_output(compiled)
        assert accepted and stage == "alignment_review"
        reviewed_shots = [shot for segment in context["segments"] for shot in segment["shots"]]
        assert reviewed_shots[1]["continuity"]["start_state"] == reviewed_shots[0]["continuity"]["end_state"]
        # The raw model result was not changed when the complete Plan was built.
        assert "start_state" not in shots(compiled)[1]["continuity"]
        return chat_output(directed_review(compiled))

    install_transport(handle)
    async def run():
        gateway = Gateway(settings)
        project = {"brief": "蓝瓶商品", "duration_seconds": 20}
        if new_flow:
            return (await gateway.decide(project, [source], [], [], None, selected_strategy=strategy.model_dump()))["plan"]
        return await gateway.plan(project, [source], [])

    if accepted:
        result = asyncio.run(run())
        assert shots(result)[1]["continuity"]["start_state"] == shots(result)[0]["continuity"]["end_state"]
    else:
        with pytest.raises(ProviderError): asyncio.run(run())
    assert seen == ([] if new_flow else ["strategy"]) + ["ad_script", "script_review", "storyboard"] + (["alignment_review"] if accepted else [])
