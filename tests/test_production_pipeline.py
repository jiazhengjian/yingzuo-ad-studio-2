"""Offline cast-reference confirmation and durable reuse; no generated-film QA."""
import asyncio
import copy
import hashlib
import json
import socket
from types import SimpleNamespace

import pytest

from app.db import Store
from app.pipeline import Pipeline, validate_plan, video_submission_spec
from app.schemas import Plan
from test_pipeline import FakeGateway, PNG, fake_media
from test_speech_spans import plan_data


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Cast workflow verification must not contact a provider")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def cast_setup(tmp_path, plan_data):
    store = Store(tmp_path / "studio.sqlite", tmp_path / "media")
    project = store.create_project("多层切面蛋糕，办公室下午茶", "product", duration_seconds=10)
    path = store.media_root / "product.png"
    path.write_bytes(PNG)
    product = store.add_asset(project["id"], "product", "原图", path, "image/png")
    plan = copy.deepcopy(plan_data)
    plan["production"] = {"version": "1.0.0", "product_constraints": [{"text": "保持多层切面", "evidence_ids": ["layers"]}],
        "characters": [{"id": "char_worker", "name": "白领", "description": "浅灰衬衫"},
                       {"id": "char_coworker", "name": "同事", "description": "深色针织衫"}],
        "speech_mode": "voiceover", "speaker_id": "", "voice_description": "自然中文旁白"}
    plan["segments"][0]["reference_asset_ids"] = [product["id"]]
    plan = Plan.model_validate(plan).model_dump()
    project = store.update_plan(project["id"], plan)
    settings = SimpleNamespace(ffmpeg_path="TEST_ONLY", max_video_submissions=1, max_active_runs=1,
        poll_seconds=.01, provider_timeout_seconds=1, tts_voice="TEST_ONLY", video_model="TEST_ONLY", presenter_verified=False)
    # A caller cannot bypass picture confirmation for a new cast-bearing plan.
    job = store.create_job(project["id"], "generate", project["revision"], {"pause_for_creative": False})
    assert job["payload"]["pause_for_creative"] is True
    return store, project, plan, settings, job


class CastGateway(FakeGateway):
    def __init__(self, store, plan, partial_first=False):
        super().__init__(store, plan)
        self.design_calls = 0
        self.image_writes = 0
        self.partial_first = partial_first
        self.requests = []

    async def create_designs(self, project, assets, plan, folder, cancelled=None):
        self.design_calls += 1
        results = []
        roles = [("scene", None), *[("character", person) for person in reversed(plan["production"]["characters"])]]
        if self.partial_first and self.design_calls == 1:
            roles = roles[:1]
        for kind, person in roles:
            path = folder / ((person["id"] if person else "scene") + ".png")
            if not path.exists():
                self.image_writes += 1
                path.write_bytes(PNG)
            metadata = {"provider_url": "https://fixture.invalid/reference.png"}
            if person:
                metadata.update(character_id=person["id"], character_name=person["name"], view_layout="four_view")
            results.append({"kind": kind, "name": person["name"] if person else "场景", "source_path": path, "metadata": metadata})
        return results

    async def submit_video(self, spec, assets, key):
        self.requests.append({"spec": copy.deepcopy(spec), "assets": copy.deepcopy(assets)})
        return await super().submit_video(spec, assets, key)


class FrozenCastGateway(CastGateway):
    failing = True

    def prepare_video_request(self, spec):
        prompt = json.dumps(spec, sort_keys=True, ensure_ascii=False)
        return {"model": "CAST_TEST_MODEL", "duration": spec["duration_seconds"], "prompt": prompt,
                "input_sha256": hashlib.sha256(prompt.encode()).hexdigest()}

    async def submit_video(self, spec, assets, key, *, prepared_request):
        assert prepared_request["input_sha256"] == hashlib.sha256(
            json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return await super().submit_video(spec, assets, key)

    async def poll_video(self, task_id):
        if self.failing:
            return {"status": "failed", "error_code": "provider_video_failed"}
        return await super().poll_video(task_id)


def execute(setup, gateway, job=None):
    store, _, _, settings, original = setup
    return asyncio.run(Pipeline(store, gateway, settings).execute((job or original)["id"]))


def confirm(setup, waiting):
    store, project, *_ = setup
    gate = store.pending_gate(project["id"], "creative")
    return store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "confirm-cast"})


def test_all_character_sheets_share_image_confirmation_and_actual_video_reference_order(cast_setup, fake_media):
    store, _, plan, _, _ = cast_setup
    gateway = CastGateway(store, plan)
    waiting = execute(cast_setup, gateway)
    assert waiting["status"] == "awaiting_input", waiting.get("error")
    assert gateway.image_writes == 3 and gateway.submissions == []
    assert len(waiting["payload"]["design_asset_ids"]) == 3
    assert waiting["progress"]["design_asset_ids"] == waiting["payload"]["design_asset_ids"]
    result = execute(cast_setup, gateway, confirm(cast_setup, waiting))
    assert result["status"] == "ready", result.get("error")
    assert gateway.design_calls == 1 and gateway.image_writes == 3
    request = gateway.requests[0]
    assert [asset["kind"] for asset in request["assets"]] == ["product", "scene", "character", "character"]
    assert [asset["metadata"].get("character_id") for asset in request["assets"][2:]] == ["char_worker", "char_coworker"]
    assert request["spec"]["production"] == plan["production"]
    assert request["spec"]["reference_roles"] == [{"index": 1, "role": "product"}, {"index": 2, "role": "scene"},
        {"index": 3, "role": "character", "character_id": "char_worker", "name": "白领"},
        {"index": 4, "role": "character", "character_id": "char_coworker", "name": "同事"}]
    assert request["spec"]["reference_asset_ids"] == [asset["id"] for asset in request["assets"]]


def test_partial_character_set_never_opens_confirmation_or_submits_video_and_recovers_saved_scene(cast_setup, fake_media):
    store, project, plan, *_ = cast_setup
    gateway = CastGateway(store, plan, partial_first=True)
    failed = execute(cast_setup, gateway)
    assert failed["status"] == "failed" and not gateway.submissions
    assert store.pending_gate(project["id"], "creative") is None
    assert len(failed["payload"]["design_asset_ids"]) == 1
    scene_id = failed["payload"]["design_asset_ids"][0]
    waiting = execute(cast_setup, gateway, failed)
    assert waiting["status"] == "awaiting_input", waiting.get("error")
    assert scene_id in waiting["payload"]["design_asset_ids"]
    assert gateway.image_writes == 3, "the original scene file was reused, not recreated"
    assert not gateway.submissions


@pytest.mark.parametrize("broken", ["missing", "changed", "wrong_layout"])
def test_image_confirmation_rechecks_character_identity_and_bytes(cast_setup, fake_media, broken):
    store, _, plan, *_ = cast_setup
    gateway = CastGateway(store, plan)
    waiting = execute(cast_setup, gateway)
    character_id = waiting["payload"]["design_asset_ids"][1]
    character = store.get_asset(character_id)
    if broken == "missing":
        payload = waiting["payload"]
        payload["design_asset_ids"].remove(character_id)
        store.update_job(waiting["id"], payload=payload)
    elif broken == "changed":
        from pathlib import Path
        Path(character["path"]).write_bytes(b"changed")
    else:
        from app.db import encoded
        with store._connection(True) as c:
            metadata = {**character["metadata"], "view_layout": "single_view"}
            c.execute("UPDATE assets SET metadata=? WHERE id=?", (encoded(metadata), character_id))
    with pytest.raises(ValueError, match="角色"):
        confirm(cast_setup, store.get_job(waiting["id"]))
    assert not gateway.submissions


def test_empty_script_requires_an_explicit_no_speech_production_decision(cast_setup):
    _, _, plan, *_ = cast_setup
    silent = copy.deepcopy(plan)
    silent.update(script="", ad_script=None, script_review=None)
    silent["production"].update(speech_mode="none", voice_description="")
    for segment in silent["segments"]:
        segment.update(narration="", speech_spans=[])
    assert validate_plan(silent, 10)["script"] == ""
    silent["production"]["speech_mode"] = "voiceover"
    with pytest.raises(ValueError, match="台词"):
        validate_plan(silent, 10)
    silent.pop("production")
    with pytest.raises(ValueError, match="台词"):
        validate_plan(silent, 10)


def test_legacy_submission_spec_does_not_invent_a_production_decision():
    asset = {"id": "original", "kind": "product", "metadata": {}}
    plan = {"audio_strategy": "native"}
    spec = video_submission_spec(plan, {"reference_asset_ids": [asset["id"]]}, [asset])
    assert "production" not in spec
    assert spec["reference_roles"] == [{"index": 1, "role": "product"}]


@pytest.mark.parametrize("changed_character", [False, True])
def test_failed_cast_video_retry_keeps_exact_frozen_spec_and_reference_bytes(cast_setup, fake_media, monkeypatch, changed_character):
    from pathlib import Path
    from app.db import encoded
    store, project, plan, *_ = cast_setup
    gateway = FrozenCastGateway(store, plan)
    waiting = execute(cast_setup, gateway)
    failed = execute(cast_setup, gateway, confirm(cast_setup, waiting))
    assert failed["status"] == "failed"
    segment_id = plan["segments"][0]["id"]
    old_snapshot = store.get_step(failed["id"], "video:" + segment_id)["snapshot"]
    child = store.create_failed_video_retry(project["id"], failed["id"])
    assert child["payload"]["frozen_video_specs"][segment_id] == old_snapshot["segment"]
    assert child["payload"]["frozen_video_requests"][segment_id] == old_snapshot["video_request"]
    monkeypatch.setattr(gateway, "prepare_video_request", lambda *_: pytest.fail("An explicit retry must not recompile"))
    gateway.failing = False
    if changed_character:
        character = store.get_asset(child["payload"]["design_asset_ids"][1])
        changed = PNG + b"CHANGED"
        Path(character["path"]).write_bytes(changed)
        # Even an updated valid media digest must not replace confirmed input bytes.
        metadata = {**character["metadata"], "sha256": hashlib.sha256(changed).hexdigest()}
        with store._connection(True) as c:
            c.execute("UPDATE assets SET metadata=? WHERE id=?", (encoded(metadata), character["id"]))
    result = execute(cast_setup, gateway, child)
    assert gateway.design_calls == 1 and gateway.image_writes == 3
    if changed_character:
        assert result["status"] == "failed" and len(gateway.submissions) == 1
        assert store.get_step(child["id"], "video:" + segment_id) is None
    else:
        assert result["status"] == "ready" and len(gateway.submissions) == 2
        snapshot = store.get_step(child["id"], "video:" + segment_id)["snapshot"]
        assert snapshot["segment"] == old_snapshot["segment"]
        assert snapshot["video_request"] == old_snapshot["video_request"]


@pytest.mark.parametrize("failure", ["failed", "rejected", "unknown"])
def test_real_image_adapter_registers_each_completion_before_later_failure(cast_setup, fake_media, monkeypatch, failure):
    from PIL import Image
    from app.providers.gateway import Gateway, SubmissionError
    store, project, _, settings, job = cast_setup
    settings.image_model = "TEST_ONLY_IMAGE"
    gateway, submitted = Gateway(settings), []

    async def original(asset):
        return "https://fixture.invalid/original.png"

    async def submit(route, payload):
        assert route == "/v1/images/generations/async", "No video request may precede image confirmation"
        saved_ids = store.get_job(job["id"])["payload"].get("design_asset_ids", [])
        assert len(saved_ids) == len(submitted), "Every preceding completion must already be durable and visible"
        submitted.append(payload)
        if len(submitted) == 3 and failure != "failed":
            raise SubmissionError("TEST_ONLY_IMAGE_FAILURE", uncertain=failure == "unknown")
        return "image-" + str(len(submitted))

    async def task(task_id):
        if task_id == "image-3":
            return {"status": "failed"}
        return {"status": "completed", "result": {"images": [{"url": "https://fixture.invalid/" + task_id + ".png"}]}}

    async def download(url, path):
        Image.new("RGB", (8, 8), "blue").save(path, "PNG")

    monkeypatch.setattr(gateway, "_original_reference_url", original)
    monkeypatch.setattr(gateway, "_submit", submit)
    monkeypatch.setattr(gateway, "_task", task)
    monkeypatch.setattr(gateway, "download_video", download)
    result = execute(cast_setup, gateway)
    assert result["status"] == ("submitted_unknown" if failure == "unknown" else "failed")
    saved_ids = result["payload"]["design_asset_ids"]
    assert len(saved_ids) == 2 and result["progress"]["design_asset_ids"] == saved_ids
    assets = [store.get_asset(aid) for aid in saved_ids]
    assert [asset["kind"] for asset in assets] == ["scene", "character"]
    assert assets[1]["metadata"]["character_id"] == "char_worker"
    assert store.pending_gate(project["id"], "creative") is None
    if failure != "unknown":
        assert result["progress"]["retryable"] is False
        assert result["progress"]["error_code"] == "image_generation_failed"
        retried = execute(cast_setup, gateway, result)
        assert retried["status"] == "failed" and retried["payload"]["design_asset_ids"] == saved_ids
    assert len(submitted) == 3, "Terminal or unknown image receipts must not create another paid request"
