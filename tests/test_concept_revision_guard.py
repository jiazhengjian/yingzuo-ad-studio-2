"""Offline checks that local copy edits cannot silently replace a chosen film.

Fixtures deliberately stop at the script boundary. They verify authorization
and persistence contracts, not the semantic quality of generated advertising.
"""
import asyncio
from copy import deepcopy

import httpx
import pytest

from app.planning import concept_revision_permissions
from app.providers.gateway import Gateway, ProviderError
from app.strategy import DirectionCandidates
from test_conversation_api import env, post_turn
from test_direction_selection import candidates_for, isolated_skill_loader, select


@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch):
    async def deny(*args, **kwargs):
        pytest.fail("Concept-revision tests must not call a real supplier")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)


def accepted_candidate(env, *, ad_form=None):
    raw = candidates_for(env).model_dump()
    if ad_form is not None:
        raw["brief"]["ad_form"] = ad_form
    raw["brief"]["creative_intent"] = {
        "use_context": "办公桌用品的选择",
        "setting_requirement": "深色影棚",
        "reference_intent": "已确认参考片以产品本身组织画面，保持纯产品视觉主导",
        "structure_preference": "product_visual",
    }
    for direction in raw["directions"]:
        direction["film_concept"]["structure"] = "product_visual"
    return DirectionCandidates.model_validate(raw)


def revision_for(candidate):
    selected = candidate.directions[0].model_dump()
    return {"kind": "plan", "message": "按本轮要求修改", "brief": candidate.brief.model_dump(),
            "directions": [selected], "selected_direction_id": selected["id"]}


@pytest.mark.parametrize("message,expected", [
    ("台词短些", set()),
    ("背景改成海边", {"setting_requirement"}),
    ("使用场景改成家里", {"use_context"}),
    ("目标人群改成学生", {"use_context"}),
    ("参考片换成生活方式广告，影片结构改成生活方式", {"reference_intent"}),
    ("背景改成海边，使用场景改成家里", {"setting_requirement", "use_context"}),
    ("不要更换参考片，只缩短台词", set()),
    ("不用修改背景，只缩短台词", set()),
    ("别改影片结构", set()),
    ("参考不改，只把台词缩短", set()),
    ("影片结构不改，背景也不改", set()),
    ('台词改成“换个参考，生活多一种可能”', set()),
    ('台词改成“参考片换成新的，影片结构改成生活方式”', set()),
    ('台词改成"参考片换成新的，影片结构改成生活方式"', set()),
    ('台词改成「参考片换成新的，影片结构改成生活方式」', set()),
    ('台词改成“参考片换成新的”，背景改成海边', {"setting_requirement"}),
])
def test_only_direct_requests_authorize_specific_intent_changes(message, expected):
    assert concept_revision_permissions(message) == expected


@pytest.mark.parametrize("changes,structure", [
    ({"reference_intent": ""}, None),
    ({"setting_requirement": ""}, None),
    ({"use_context": ""}, None),
    ({"structure_preference": "auto"}, None),
    ({"structure_preference": "auto"}, "lifestyle"),
    ({"reference_intent": "", "setting_requirement": "", "structure_preference": "auto"}, "lifestyle"),
])
def test_copy_revision_cannot_clear_intent_or_change_structure_before_script(
        env, monkeypatch, isolated_skill_loader, changes, structure):
    candidate = accepted_candidate(env, ad_form="brand_tvc")
    raw = revision_for(candidate)
    raw["brief"]["creative_intent"].update(changes)
    if structure:
        raw["directions"][0]["film_concept"]["structure"] = structure
    assert_rejected_before_script(env, monkeypatch, candidate, raw, "台词短些", set())


def assert_rejected_before_script(env, monkeypatch, candidate, raw, message, permissions):
    current = {"strategy": candidate.select("direction_0").model_dump()}
    before = deepcopy(current)
    calls = []
    async def reply(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        assert context["stage"] == "strategy", "unauthorized revision reached ad_script"
        assert context["existing_creative_intent"] == candidate.brief.creative_intent.model_dump()
        assert set(context["concept_revision_permissions"]) == permissions
        return deepcopy(raw)
    monkeypatch.setattr(Gateway, "_planner_json", reply)
    project = {**env[1].state.store.get_project(env[2]), "ad_form": candidate.brief.ad_form}
    with pytest.raises(ProviderError, match="策划回复未通过"):
        asyncio.run(Gateway(env[1].state.settings).decide(project, [env[3]],
            [{"role": "user", "content": message}], [], current))
    assert calls == ["strategy", "strategy"]
    assert current == before


@pytest.mark.parametrize("message,changes,permissions", [
    ("背景改成海边", {"setting_requirement": "海边", "reference_intent": ""}, {"setting_requirement"}),
    ("使用场景改成家里", {"use_context": "家庭日常使用", "setting_requirement": "家里"}, {"use_context"}),
    ("使用场景改成家里", {"use_context": "家庭日常使用", "reference_intent": ""}, {"use_context"}),
])
def test_authorized_one_field_change_does_not_release_other_constraints(
        env, monkeypatch, isolated_skill_loader, message, changes, permissions):
    candidate = accepted_candidate(env, ad_form="brand_tvc")
    raw = revision_for(candidate)
    raw["brief"]["creative_intent"].update(changes)
    assert_rejected_before_script(env, monkeypatch, candidate, raw, message, permissions)


class ReachedScript(BaseException):
    """Stop an explicitly mocked successful revision before further authoring."""


@pytest.mark.parametrize("message,changes,structure,permissions", [
    ("台词短些", {}, "product_visual", set()),
    ("背景改成海边", {"setting_requirement": "海边"}, "product_visual", {"setting_requirement"}),
    ("使用场景改成家里", {"use_context": "家庭日常使用"}, "product_visual", {"use_context"}),
    ("参考片换成生活方式广告，影片结构改成生活方式",
     {"reference_intent": "参考生活方式广告，以日常生活组织画面", "structure_preference": "lifestyle"},
     "lifestyle", {"reference_intent"}),
])
def test_authorized_revision_reaches_script_with_only_requested_intent_changed(
        env, monkeypatch, isolated_skill_loader, message, changes, structure, permissions):
    candidate = accepted_candidate(env, ad_form="brand_tvc")
    raw = revision_for(candidate)
    raw["brief"]["creative_intent"].update(changes)
    raw["directions"][0]["film_concept"]["structure"] = structure
    current = {"strategy": candidate.select("direction_0").model_dump()}
    before = deepcopy(current)
    calls = []
    async def reply(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        if context["stage"] == "strategy":
            assert set(context["concept_revision_permissions"]) == permissions
            assert context["existing_creative_intent"] == candidate.brief.creative_intent.model_dump()
            return deepcopy(raw)
        assert context["stage"] == "ad_script"
        assert context["creative_strategy"]["brief"]["creative_intent"] == raw["brief"]["creative_intent"]
        assert context["selected_direction"]["film_concept"]["structure"] == structure
        assert context["creative_strategy"]["brief"]["ad_form"] == "brand_tvc"
        raise ReachedScript
    monkeypatch.setattr(Gateway, "_planner_json", reply)
    project = {**env[1].state.store.get_project(env[2]), "ad_form": "brand_tvc"}
    with pytest.raises(ReachedScript):
        asyncio.run(Gateway(env[1].state.settings).decide(project, [env[3]],
            [{"role": "user", "content": message}], [], current))
    assert calls == ["strategy", "ad_script"]
    assert current == before


def test_reference_change_still_cannot_cross_saved_ad_form(
        env, monkeypatch, isolated_skill_loader):
    candidate = accepted_candidate(env, ad_form="brand_tvc")
    raw = revision_for(candidate)
    raw["brief"]["creative_intent"].update(reference_intent="改用人物讲解的参考", structure_preference="auto")
    raw["directions"][0]["film_concept"]["structure"] = "presenter_explanation"
    assert_rejected_before_script(env, monkeypatch, candidate, raw,
        "更换参考片，修改影片结构", {"reference_intent"})


def test_first_selection_without_plan_retains_source_intent_in_next_frozen_turn(
        env, monkeypatch, isolated_skill_loader):
    store = env[1].state.store
    candidate = accepted_candidate(env)
    response = post_turn(env, content="帮我策划三个方向")
    assert response.status_code == 200
    source_id = response.json()["job"]["id"]
    assert store.claim_job(source_id)
    store.finish_decision(source_id, {"kind": "directions", "message": "请选择一个方向",
        "direction_candidates": candidate.model_dump()})
    gate = store.pending_gate(env[2], "direction")
    selection = select(env, gate, direction_id="direction_0")
    assert selection.status_code == 200
    selected_job_id = selection.json()["job"]["id"]
    # The first script never succeeded. A later normal edit must still inherit
    # the authored source intent instead of treating missing plan as consent.
    store.update_job(selected_job_id, status="failed", phase="planning_invalid_response", error="OFFLINE_FIXTURE")
    assert store.get_project(env[2])["plan"] is None
    source_before = deepcopy(store.get_job(source_id))
    selected_before = deepcopy(store.get_job(selected_job_id))
    gates_before = deepcopy(store.project_detail(env[2])["gates"])
    assert all("creative_intent" not in gate for gate in gates_before)

    response = post_turn(env, content="台词短些", client_turn_id="revision-after-first-script-failed")
    assert response.status_code == 200
    frozen = store.get_job(response.json()["job"]["id"])["payload"]["frozen"]
    inherited_gate = next(g for g in frozen["gates"] if g["id"] == gate["id"])
    assert inherited_gate["creative_intent"] == candidate.brief.creative_intent.model_dump()
    assert frozen["project"]["plan"] is None
    assert store.get_job(source_id) == source_before
    assert store.get_job(selected_job_id) == selected_before
    assert store.project_detail(env[2])["gates"] == gates_before

    calls = []
    raw = revision_for(candidate)
    raw["brief"]["creative_intent"]["reference_intent"] = ""
    async def reply(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        assert context["stage"] == "strategy", "gate-based revision reached ad_script after erasing intent"
        assert context["existing_creative_intent"] == candidate.brief.creative_intent.model_dump()
        assert context["concept_revision_permissions"] == []
        return deepcopy(raw)
    monkeypatch.setattr(Gateway, "_planner_json", reply)
    with pytest.raises(ProviderError, match="策划回复未通过"):
        asyncio.run(Gateway(env[1].state.settings).decide(frozen["project"], frozen["assets"],
            frozen["messages"], frozen["gates"], None))
    assert calls == ["strategy", "strategy"]
    assert store.get_job(source_id) == source_before
    assert store.project_detail(env[2])["gates"] == gates_before
