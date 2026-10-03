"""An unchanged non-lean v1 stage remains reusable after lean drafting is added."""
import asyncio
from copy import deepcopy

import pytest

from app.planning_session import PlanningSession, fingerprint
from app.providers.gateway import Gateway
from test_progressive_planning import (
    forbid_real_network, install_transport, progressive_gateway, settings, source,
)


class BeforeNextStage(BaseException):
    pass


def test_saved_non_lean_v1_draft_replays_before_only_unfinished_stages(
        progressive_gateway, settings, source, monkeypatch):
    state = progressive_gateway
    settings.content_draft_enabled = False
    settings.joint_planning_enabled = False
    saved = None
    old_signature = None
    original_call = PlanningSession.call

    def save(value):
        nonlocal saved
        saved = deepcopy(value)

    async def legacy_v1_call(session, stage, signature, perform, **callbacks):
        nonlocal old_signature
        if stage != "ad_script":
            raise BeforeNextStage()
        # Pin the prior wire projection version independently of today's
        # constant. Method content, user input and all other signature fields
        # are intentionally unchanged in this migration case.
        old_signature = deepcopy(signature)
        old_signature["wire_context_version"] = 1
        return await original_call(session, stage, old_signature, perform, **callbacks)

    def run(checkpoint=None):
        return asyncio.run(Gateway(settings).decide(
            {"brief": state.brief, "duration_seconds": state.duration}, [source],
            state.conversation, [], state.current_plan,
            selected_strategy=state.strategy.model_dump(),
            planning_checkpoint=checkpoint, on_checkpoint=save))

    with monkeypatch.context() as patch:
        patch.setattr(PlanningSession, "call", legacy_v1_call)
        with pytest.raises(BeforeNextStage):
            run()

    assert saved["request_count"] == 1
    assert len(saved["entries"]) == 1
    assert saved["entries"][0]["state"] == "ready"
    assert saved["entries"][0]["input_hash"] == fingerprint(old_signature)
    assert "draft_format" not in old_signature["context"]
    original_entry = deepcopy(saved["entries"][0])
    assert state.calls == ["ad_script"]

    result = run(saved)
    assert result["plan"]["plan_review"]["ready"] is True
    assert state.calls == ["ad_script", "storyboard", "plan_review"]
    assert saved["request_count"] == 3
    assert saved["entries"][0] == original_entry
    assert all(entry["state"] == "ready" for entry in saved["entries"])
