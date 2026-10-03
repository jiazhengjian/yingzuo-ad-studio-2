"""Source-preserving image-observation corrections within the one existing review.

The mocked reviewer is explicit test data, not proof of image understanding or
advertising quality. No provider call, user asset or production DB is used.
"""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from app.ad_script import script_context
from app.db import Store
from app.direction_review import (direction_review_schema, reviewed_brief,
    reviewed_candidates, validate_direction_review)
from app.planner_runtime import planner_wire_context
from app.providers.gateway import Gateway
from app.strategy import DirectionCandidates, select_rules
from test_conversation_api import env, post_turn
from test_direction_selection import (approved_direction_review, candidates_for,
    isolated_skill_loader, select)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def deny(*args, **kwargs):
        pytest.fail("Image-observation review tests cannot call a real provider")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)


def original_and_correction(env):
    raw = candidates_for(env).model_dump()
    raw["brief"]["facts"][0]["text"] = "金属瓶身轮廓"
    raw["brief"]["facts"].append({"id": "merchant_name", "text": "透明玻璃瓶",
        "source_kind": "merchant", "source_id": "brief"})
    original = DirectionCandidates.model_validate(raw)
    review = approved_direction_review([d.model_dump() for d in original.directions])
    review["image_observation_corrections"] = [{"fact_id": "appearance", "text": "浅色瓶身轮廓"}]
    return original, review


def test_review_can_correct_only_an_existing_image_observation_without_mutating_input(env):
    original, raw = original_and_correction(env)
    before = original.model_dump()
    review = validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    brief = reviewed_brief(review, original).model_dump()
    expected = deepcopy(before["brief"])
    expected["facts"][0]["text"] = "浅色瓶身轮廓"
    assert brief == expected
    result = reviewed_candidates(review, original, select_rules(None)).model_dump()
    assert result["brief"] == expected
    assert result["direction_review"]["image_observation_corrections"] == raw["image_observation_corrections"]
    assert result["rules_used"] == before["rules_used"]
    assert result["recommended_direction_id"] == before["recommended_direction_id"]
    assert original.model_dump() == before
    result["brief"]["facts"][0]["text"] = "later external change"
    assert original.model_dump() == before


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("mutation", ["unknown_id", "merchant_id", "duplicate_id"])
def test_observation_corrections_cannot_invent_or_redirect_facts_even_on_negative_review(env, ready, mutation):
    original, raw = original_and_correction(env)
    before = original.model_dump()
    if not ready:
        raw["ready"] = False
        raw["checks"][0]["claim_support_passed"] = False
    if mutation == "unknown_id":
        raw["image_observation_corrections"][0]["fact_id"] = "new_feature"
    elif mutation == "merchant_id":
        raw["image_observation_corrections"][0]["fact_id"] = "merchant_name"
    else:
        raw["image_observation_corrections"] *= 2
    with pytest.raises(ValueError):
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    assert original.model_dump() == before


@pytest.mark.parametrize("extra", ["id", "source_kind", "source_id", "facts", "creative_intent"])
def test_correction_schema_has_no_channel_for_changing_sources_or_other_brief_fields(env, extra):
    original, raw = original_and_correction(env)
    raw["image_observation_corrections"][0][extra] = "invented"
    with pytest.raises(ValidationError):
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)


@pytest.mark.parametrize("text", [None, "", " \n\t "])
def test_correction_cannot_delete_fact_text(env, text):
    original, raw = original_and_correction(env)
    raw["image_observation_corrections"][0]["text"] = text
    with pytest.raises(ValidationError):
        validate_direction_review(raw, original, select_rules(None), product_reference_count=1)


def test_existing_reviews_default_to_no_observation_changes(env):
    original = candidates_for(env)
    raw = approved_direction_review([d.model_dump() for d in original.directions])
    review = validate_direction_review(raw, original, select_rules(None), product_reference_count=1)
    assert reviewed_brief(review, original) == original.brief
    assert "image_observation_corrections" not in review.model_dump()
    schema = direction_review_schema(8)
    assert "image_observation_corrections" not in schema["required"]
    assert schema["properties"]["image_observation_corrections"]["default"] == []
    assert schema["properties"]["image_observation_corrections"]["maxItems"] == 12
    assert set(schema["$defs"]["ImageObservationCorrection"]["properties"]) == {"fact_id", "text"}
    assert schema["$defs"]["ImageObservationCorrection"]["additionalProperties"] is False


def test_same_two_text_requests_publish_corrected_fact_and_carry_it_to_selected_script(env, monkeypatch, isolated_skill_loader):
    _, app, pid, asset = env
    store = app.state.store
    original, review_raw = original_and_correction(env)
    before = original.model_dump()
    job = post_turn(env, content="为我的透明玻璃瓶提供三个广告方向").json()["job"]
    frozen = store.get_job(job["id"])["payload"]["frozen"]
    calls, checkpoints = [], []
    async def reply(self, system, context, originals, max_tokens=7000):
        calls.append(context["stage"])
        if context["stage"] == "strategy":
            return {"kind": "directions", "message": "初稿", "brief": before["brief"],
                "directions": before["directions"]}
        assert context["stage"] == "direction_review"
        assert context["immutable_brief"] == before["brief"]
        assert len(originals) == 1 and originals[0]["id"] == asset["id"]
        return review_raw
    monkeypatch.setattr(Gateway, "_planner_json", reply)
    result = asyncio.run(Gateway(app.state.settings).decide(frozen["project"], frozen["assets"],
        frozen["messages"], frozen["gates"], None,
        on_checkpoint=lambda checkpoint: checkpoints.append(deepcopy(checkpoint))))
    assert calls == ["strategy", "direction_review"]
    assert result["direction_candidates"]["brief"]["facts"][0]["text"] == "浅色瓶身轮廓"
    assert result["planning_brief"] == result["direction_candidates"]["brief"]
    assert original.model_dump() == before
    checkpoint_text = json.dumps(checkpoints, ensure_ascii=False)
    assert "金属瓶身轮廓" in checkpoint_text and "浅色瓶身轮廓" in checkpoint_text
    assert store.claim_job(job["id"])
    store.finish_decision(job["id"], result)
    gate = store.pending_gate(pid, "direction")
    continuation = select(env, gate).json()["job"]
    reopened = Store(store.db_path, store.media_root)
    saved = reopened.get_job(job["id"])["payload"]["direction_candidates"]
    assert saved["direction_review"]["image_observation_corrections"] == review_raw["image_observation_corrections"]
    selected = reopened.get_job(continuation["id"])["payload"]["selected_strategy"]
    script = planner_wire_context({**script_context(selected, 20), "stage": "ad_script"})
    assert script["creative_strategy"]["brief"] == result["planning_brief"]
    assert selected["brief"]["facts"][1] == before["brief"]["facts"][1]
    assert store.get_project(pid)["plan"] is None
    assert [a["kind"] for a in store.project_detail(pid)["assets"]] == ["product"]
