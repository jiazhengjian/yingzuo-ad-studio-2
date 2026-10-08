"""Conversation and creative gates with explicit offline provider doubles only."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from app.db import Store
from app.pipeline import Pipeline
from app.providers.gateway import Gateway, ProviderError
from app.schemas import AgentDecision, Plan
from app import media
from ad_script_fixtures import stage_outputs, assert_script_saved, script_stages
from test_providers import assert_legacy_plan_wire_schema, assert_strategy_wire_schema


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    async def denied(*args, **kwargs):
        pytest.fail("Offline conversation test attempted real HTTP instead of its explicit supplier fixture")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


QUESTION = {"id": "channel", "prompt": "主要在哪里展示这条广告？", "options": [
    {"id": "douyin", "label": "抖音商品页"}, {"id": "wechat", "label": "微信朋友圈"}], "allow_custom": True, "allow_skip": True}
CLARIFY = {"kind": "clarify", "message": "确认展示渠道后，我再安排开场。", "questions": [QUESTION]}
REPLY = {"kind": "reply", "message": "当前还没有提交视频任务。"}


def native_decision(decision):
    """Explicit fixture for the new model-output contract, not a runtime fallback."""
    decision = copy.deepcopy(decision)
    decision["plan"]["audio_strategy"] = "native"
    for segment in decision["plan"]["segments"]:
        for shot in segment["shots"]:
            shot["narration"] = "看看商品细节。" if shot["end"] - shot["start"] >= 3 else ""
        segment["narration"] = "".join(s["narration"] for s in segment["shots"])
    decision["plan"]["script"] = "".join(s["narration"] for s in decision["plan"]["segments"])
    for option in decision["creative_options"]:
        option["segment"]["shots"] = copy.deepcopy(decision["plan"]["segments"][0]["shots"])
        option["segment"]["narration"] = decision["plan"]["segments"][0]["narration"]
    return decision


def staged_responses(project, decision):
    """Real adapter fixtures only; keep legacy OfflineGateway fixtures unchanged."""
    brief = {"product_name": "蓝色玻璃瓶", "category": "general", "audience": "关注外观的顾客",
             "scenario": "商品展示", "facts": [{"id": "fact_visual", "text": "蓝色玻璃瓶",
                 "source_kind": "merchant", "source_id": "brief"}],
             "unknowns": ["功效未确认"], "assumptions": ["采用克制的外观展示"]}
    assert brief["facts"][0]["text"] in project["brief"]
    direction = {"id": "appearance", "rule_id": "appearance_reveal", "label": "由轮廓到细节",
                 "audience": brief["audience"], "scenario": brief["scenario"],
                 "angle": "看清商品外观", "hook": "从商品轮廓开始", "development": "推进到瓶身细节",
                 "payoff": "回到完整商品", "evidence_ids": ["fact_visual"],
                 "reason": "现有资料支持外观展示", "production_notes": ["不演示未确认功效"]}
    stage = {"kind": "plan", "message": decision["message"], "brief": brief,
             "directions": [direction], "selected_direction_id": direction["id"]}
    compiled = native_decision(decision)["plan"]
    for segment in compiled["segments"]:
        for shot in segment["shots"]:
            shot.update(purpose="展示可确认的商品外观", evidence_ids=["fact_visual"])
    return stage, compiled


def assert_staged_plan(result, stage, compiled):
    expected = script_stages(stage, compiled)[2]
    expected["facts"] = {"name": stage["brief"]["product_name"],
                         "claims": [fact["text"] for fact in stage["brief"]["facts"]]}
    expected.pop("strategy", None)
    expected.pop("ad_script", None)
    expected.pop("script_review", None)
    actual = copy.deepcopy(result["plan"])
    actual.pop("ad_script", None)
    actual.pop("script_review", None)
    actual.pop("review_status", None)
    actual.pop("review_status_note", None)
    for segment in actual.get("segments") or []:
        if isinstance(segment, dict):
            segment.pop("speech_characters_per_tenth", None)
    strategy = actual.pop("strategy")
    assert actual == expected
    bound = {**stage["brief"], "ad_form": strategy["brief"].get("ad_form")}
    assert strategy["brief"] == bound
    assert strategy["directions"] == stage["directions"]
    assert strategy["selected_direction_id"] == stage["selected_direction_id"]
    assert result["planning_brief"] == bound
    assert result["creative_options"] == []


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "studio.sqlite", tmp_path / "media")
    project = store.create_project("蓝色玻璃瓶，展示外形，不承诺功效", "product", ad_form="brand_tvc")
    store.add_message(project["id"], "user", project["brief"])
    path = store.media_root / "product.png"
    Image.new("RGB", (16, 16), "blue").save(path)
    asset = store.add_asset(project["id"], "product", "原始商品", path, "image/png", metadata={"generated": False})
    plan = {"title": "蓝瓶广告", "script": "看看商品细节。了解更多信息。", "language": "zh-CN",
            "aspect_ratio": "9:16", "audio_strategy": "voiceover", "facts": {"name": "蓝色玻璃瓶", "claims": []},
            "segments": [{"id": f"s{i}", "duration_seconds": 10, "purpose": "展示外形", "prompt": f"第{i+1}段完整镜头说明", "narration": "",
                          "shots": [{"start": 0, "end": 4, "description": "全景", "narration": ""}, {"start": 4, "end": 10, "description": "细节", "narration": ""}],
                          "reference_asset_ids": [asset["id"]]} for i in range(2)]}
    options = [{"id": f"alternative{i}", "label": f"开场方向{i}", "description": f"从细节{i}开始",
                "segment": {**copy.deepcopy(plan["segments"][0]), "prompt": f"备选{i}完整镜头说明"}} for i in (1, 2)]
    decision = {"kind": "plan", "message": "方案已准备好，请确认后制作参考图。", "plan": plan, "creative_options": options}
    settings = SimpleNamespace(tudou_api_key="TEST_ONLY", deepseek_api_key="TEST_ONLY", planner_model="deepseek-flash", image_model="gpt-image-2-all",
                               ffmpeg_path="TEST_ONLY", tts_voice="TEST_ONLY", video_model="TEST_ONLY", max_video_submissions=2,
                               max_active_runs=1, poll_seconds=.01, provider_timeout_seconds=2)
    return store, store.get_project(project["id"]), asset, decision, settings


class OfflineGateway:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.contexts = []
        self.design_calls = 0
        self.voice_calls = 0
        self.calls = []
        self.submissions = []
        self.on_decide = None

    async def decide(self, project, assets, messages, gates, current_plan, **kwargs):
        self.contexts.append(copy.deepcopy({"project": project, "assets": assets, "messages": messages,
                                            "gates": gates, "current_plan": current_plan}))
        if self.on_decide:
            self.on_decide()
        return copy.deepcopy(self.decisions.pop(0))

    async def create_designs(self, project, assets, plan, folder, cancelled=None):
        assert not cancelled or not cancelled()
        self.design_calls += 1
        self.calls.append("scene")
        result = []
        for kind in ("scene",):
            path = folder / f"{kind}.png"
            Image.new("RGB", (16, 16), "white").save(path)
            result.append({"source_path": path, "kind": kind, "name": f"TEST_{kind}",
                           "metadata": {"generated": True, "provider_url": f"https://fixture.invalid/{kind}.png"}})
        return result

    async def synthesize_voice(self, text, path, *, target_duration_seconds=None):
        self.voice_calls += 1
        self.calls.append(("voice", target_duration_seconds))
        path.write_bytes(b"EXPLICIT_TEST_AUDIO")
        return {"duration_seconds": 2, "provider": "EXPLICIT_TEST", "captions": [{"start": 0, "end": 2, "text": text}]}

    async def submit_video(self, segment, assets, idempotency_key):
        self.calls.append("video")
        self.submissions.append({"segment": copy.deepcopy(segment), "assets": copy.deepcopy(assets), "key": idempotency_key})
        return {"task_id": "task_" + segment["id"], "provider": "EXPLICIT_TEST"}

    async def poll_video(self, task_id):
        return {"status": "succeeded", "download_url": "https://fixture.invalid/video.mp4"}

    async def download_video(self, url, path):
        path.write_bytes(b"EXPLICIT_TEST_VIDEO")


@pytest.fixture
def fake_media(monkeypatch):
    def probe(path, *args, **kwargs):
        assert Path(path).is_file()
        video = Path(path).suffix == ".mp4"
        return {"duration_seconds": 10 if video else 2, "video_duration_seconds": 10 if video else None,
                "has_video": video, "has_audio": True, "frames": 240 if video else None, "fps": 24, "width": 720, "height": 1280}
    def compose(manifest, paths, destination, *args, **kwargs):
        assert all(Path(p).exists() for p in paths.values())
        destination.write_bytes(b"EXPLICIT_TEST_COMPOSITE")
        return {"duration_seconds": manifest["total_frames"] / 24, "frames": manifest["total_frames"], "has_video": True, "has_audio": True, "fps": 24, "width": 720, "height": 1280}
    monkeypatch.setattr(media, "probe", probe)
    monkeypatch.setattr(media, "compose", compose)


def turn(store, pid, request_id, content="", **kwargs):
    return store.create_turn(pid, {"client_turn_id": request_id, "expected_context_version": store.get_project(pid)["context_version"],
                                   "content": content, **kwargs})


def execute(store, settings, gateway, job):
    return asyncio.run(Pipeline(store, gateway, settings).execute(job["id"]))


def test_clarification_releases_worker_and_records_only_actual_steps(setup):
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([CLARIFY, REPLY])
    job = execute(store, settings, gateway, turn(store, project["id"], "clarify"))
    assert job["status"] == "awaiting_input" and job["lease_until"] == 0
    assert not store.active_jobs() and job["submission_count"] == 0
    assert gateway.design_calls == gateway.voice_calls == 0 and not gateway.submissions
    gate = store.pending_gate(project["id"], "clarification")
    assert gate["questions"][0]["id"] == QUESTION["id"]
    events = store.events_after(project["id"])["events"]
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    assert any(e["label"] == "正在读取需求与商品素材" for e in events)
    assert any(e["label"] == "正在判断是否需要补充信息" for e in events)
    assert events[-1]["kind"] == "gate" and events[-1]["state"] == "waiting"
    another = store.create_project("另一个问题", "product")
    second = execute(store, settings, gateway, turn(store, another["id"], "reply", "能做什么？"))
    assert second["status"] == "ready"


def test_answered_question_context_reaches_plan_decision(setup):
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([CLARIFY, decision])
    execute(store, settings, gateway, turn(store, project["id"], "initial"))
    gate = store.pending_gate(project["id"])
    job = turn(store, project["id"], "answers", reply_to_gate_id=gate["id"], answers=[{"question_id": "channel", "option_id": "douyin"}])
    result = execute(store, settings, gateway, job)
    assert result["status"] == "ready" and result["phase"] == "plan_ready"
    assert gateway.contexts[-1]["gates"][-1]["status"] == "answered"
    assert any("抖音商品页" in m["content"] for m in gateway.contexts[-1]["messages"])
    expected = AgentDecision.model_validate(decision).model_dump()
    assert result["payload"]["creative_options"] == expected["creative_options"]
    saved = store.get_project(project["id"])["plan"]
    saved.pop("review_status", None)
    saved.pop("review_status_note", None)
    assert saved == expected["plan"]
    assert not gateway.submissions


def test_reply_preserves_current_plan_without_regeneration(setup):
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([decision, REPLY])
    execute(store, settings, gateway, turn(store, project["id"], "plan"))
    before = store.get_project(project["id"])
    result = execute(store, settings, gateway, turn(store, project["id"], "meta", "为什么还没有视频？"))
    after = store.get_project(project["id"])
    assert result["phase"] == "reply_ready" and after["plan"] == before["plan"] and after["revision"] == before["revision"]
    assert gateway.contexts[-1]["current_plan"] == before["plan"]
    assert not gateway.submissions and gateway.design_calls == gateway.voice_calls == 0


def test_stale_context_cannot_apply_late_decision(setup):
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([decision])
    job = turn(store, project["id"], "frozen")
    gateway.on_decide = lambda: store.add_message(project["id"], "user", "新的事实，不允许旧结果覆盖")
    result = execute(store, settings, gateway, job)
    assert result["status"] == "needs_review"
    assert store.get_project(project["id"])["plan"] is None and store.pending_gate(project["id"]) is None
    assert len(gateway.contexts[0]["messages"]) < len(store.project_detail(project["id"])["messages"])


def test_invalid_decision_has_no_fallback_gate(setup):
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([{"kind": "clarify", "message": "TEST_PRIVATE_RAW", "questions": []}])
    result = execute(store, settings, gateway, turn(store, project["id"], "bad"))
    assert result["status"] == "failed" and "TEST_PRIVATE_RAW" not in result["error"]
    assert store.pending_gate(project["id"]) is None and store.get_project(project["id"])["plan"] is None
    assert not gateway.submissions


def pause_with_creative(setup, gateway):
    store, project, asset, decision, settings = setup
    execute(store, settings, gateway, turn(store, project["id"], "plan"))
    p = store.get_project(project["id"])
    job = store.create_job(p["id"], "generate", p["revision"], {"pause_for_creative": True})
    result = execute(store, settings, gateway, job)
    assert result["status"] == "awaiting_input", result.get("error")
    assert result["submission_count"] == 0 and result["lease_until"] == 0
    assert not store.active_jobs() and not gateway.submissions
    assert gateway.design_calls == 1 and gateway.voice_calls == 0
    assert len(result["payload"]["design_asset_ids"]) == 1
    assert "audio_asset_id" not in result["payload"]
    return result, store.pending_gate(p["id"], "creative")


@pytest.mark.parametrize("selection", ["current"])
def test_creative_pause_and_same_job_resume_reuses_images_and_voice(setup, fake_media, selection):
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([decision])
    waiting, gate = pause_with_creative(setup, gateway)
    assert len(gate["options"]) == 1
    assert execute(store, settings, gateway, waiting)["status"] == "awaiting_input"
    body = {"gate_id": gate["id"], "accepted_revision": waiting["revision"], "option_id": selection, "client_turn_id": "confirm"}
    queued = store.confirm_creative(project["id"], body)
    assert queued["id"] == waiting["id"] and queued["revision"] == waiting["revision"] + 1
    reopened = Store(store.db_path, store.media_root)
    result = execute(reopened, settings, gateway, queued)
    assert result["status"] == "ready", result.get("error")
    assert gateway.design_calls == gateway.voice_calls == 1 and len(gateway.submissions) == 2
    selected_prompt = next(o["segment"]["prompt"] for o in gate["options"] if o["id"] == selection)
    assert gateway.submissions[0]["segment"]["prompt"] == selected_prompt
    assert set(waiting["payload"]["design_asset_ids"]).issubset(gateway.submissions[0]["segment"]["reference_asset_ids"])
    assert result["payload"]["audio_asset_id"]
    assert gateway.calls == ["scene", ("voice", 20), "video", "video"]
    repeated = store.confirm_creative(project["id"], body)
    execute(store, settings, gateway, repeated)
    assert repeated["id"] == waiting["id"] and len(gateway.submissions) == 2


def test_creative_note_returns_to_planner_without_video_submission(setup, fake_media):
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([decision, decision])
    waiting, gate = pause_with_creative(setup, gateway)
    job = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "note": "开场改得更安静，不增加功效", "client_turn_id": "creative-note"})
    assert job["kind"] == "plan" and job["id"] != waiting["id"]
    assert store.get_job(waiting["id"])["status"] == "canceled"
    result = execute(store, settings, gateway, job)
    assert result["status"] == "ready" and result["phase"] == "plan_ready"
    answered = gateway.contexts[-1]["gates"][-1]
    assert answered["selected_option_id"] == "current" and "更安静" in answered["note"]
    feedback = gateway.contexts[-1]["messages"][-1]["content"]
    assert feedback == "图片修改意见：开场改得更安静，不增加功效"
    assert gateway.design_calls == 1 and gateway.voice_calls == 0 and not gateway.submissions


def test_image_confirmation_does_not_require_old_creative_alternatives(setup, fake_media):
    store, project, asset, decision, settings = setup
    p = store.update_plan(project["id"], decision["plan"])
    gateway = OfflineGateway([])
    job = store.create_job(p["id"], "generate", p["revision"], {"pause_for_creative": True})
    result = execute(store, settings, gateway, job)
    assert result["status"] == "awaiting_input"
    assert store.pending_gate(p["id"])["options"][0]["id"] == "current"
    assert gateway.design_calls == 1 and gateway.voice_calls == 0 and not gateway.submissions


@pytest.mark.parametrize("failure", ["probe", "provider"])
def test_long_voice_fails_after_images_and_cannot_resubmit_same_script(setup, fake_media, monkeypatch, failure):
    store, project, _, decision, settings = setup
    gateway = OfflineGateway([decision])
    waiting, gate = pause_with_creative(setup, gateway)
    scene_ids = waiting["payload"]["design_asset_ids"]
    if failure == "probe":
        probe = media.probe
        def long_probe(path, *args, **kwargs):
            result = probe(path, *args, **kwargs)
            if Path(path).suffix == ".wav":
                result["duration_seconds"] = 25
            return result
        monkeypatch.setattr(media, "probe", long_probe)
    else:
        async def long_voice(text, path, *, target_duration_seconds=None):
            gateway.voice_calls += 1
            error = ValueError("配音文案过长，请精简")
            error.error_code = "voice_too_long"
            raise error
        gateway.synthesize_voice = long_voice
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "confirm-long"})
    result = execute(store, settings, gateway, queued)
    assert result["status"] == "failed" and result["progress"]["error_code"] == "voice_too_long"
    assert result["progress"]["retryable"] is False and result["submission_count"] == 0
    assert result["payload"]["design_asset_ids"] == scene_ids
    assert all(Path(store.get_asset(aid)["path"]).exists() for aid in scene_ids)
    assert gateway.design_calls == gateway.voice_calls == 1 and not gateway.submissions
    # Even an internal caller bypassing the API retry prohibition cannot rerun the same narration.
    repeated = execute(store, settings, gateway, result)
    assert repeated["status"] == "failed" and repeated["progress"]["error_code"] == "voice_too_long"
    assert gateway.design_calls == gateway.voice_calls == 1 and not gateway.submissions


@pytest.mark.parametrize("duration", [10, 15, 60])
def test_image_confirmation_precedes_voice_for_selected_duration(setup, fake_media, duration):
    store, _, _, decision, settings = setup
    project = store.create_project("TEST selected duration", "product", duration_seconds=duration, ad_form="brand_tvc")
    path = store.media_root / f"product_{duration}.png"
    Image.new("RGB", (16, 16), "blue").save(path)
    original = store.add_asset(project["id"], "product", "TEST original", path, "image/png")
    durations = [10] * (duration // 10) + ([duration % 10] if duration % 10 else [])
    first = decision["plan"]["segments"][0]
    decision["plan"]["segments"] = [{**copy.deepcopy(first), "id": f"s{i}", "duration_seconds": seconds,
        "shots": [{"start": 0, "end": seconds, "description": f"TEST shot {i + 1}"}],
        "reference_asset_ids": [original["id"]]} for i, seconds in enumerate(durations)]
    for option in decision["creative_options"]:
        option["segment"] = {**copy.deepcopy(decision["plan"]["segments"][0]), "prompt": option["segment"]["prompt"]}
    settings.max_video_submissions = 6
    gateway = OfflineGateway([decision])
    waiting, gate = pause_with_creative((store, project, original, decision, settings), gateway)
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": f"confirm-{duration}"})
    result = execute(store, settings, gateway, queued)
    assert result["status"] == "ready", result.get("error")
    assert gateway.calls == ["scene", ("voice", duration), *(["video"] * len(durations))]
    assert result["submission_count"] == result["payload"]["video_submission_budget"] == len(durations)
    assert result["payload"]["manifest"]["total_frames"] == duration * 24


def test_historical_generated_product_is_excluded_when_resuming_saved_scene(setup, fake_media):
    store, project, original, decision, settings = setup
    gateway = OfflineGateway([decision])
    waiting, gate = pause_with_creative(setup, gateway)
    path = store.media_root / "TEST_OLD_GENERATED_PRODUCT.png"
    Image.new("RGB", (16, 16), "red").save(path)
    old = store.add_asset(project["id"], "product", "TEST old generated product", path, "image/png", metadata={"generated": True})
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "confirm-old"})
    payload = queued["payload"]
    payload["design_asset_ids"].append(old["id"])
    for segment in payload["plan"]["segments"]:
        segment["reference_asset_ids"].append(old["id"])
    store.update_job(queued["id"], payload=payload)
    result = execute(store, settings, gateway, queued)
    assert result["status"] == "ready", result.get("error")
    assert gateway.design_calls == 1
    assert result["payload"]["design_asset_ids"] == waiting["payload"]["design_asset_ids"]
    for submission in gateway.submissions:
        ids = [a["id"] for a in submission["assets"]]
        assert original["id"] in ids and old["id"] not in ids


@pytest.mark.parametrize("has_audio", [True, False])
def test_native_generation_uses_provider_audio_without_tts_or_estimated_captions(setup, fake_media, monkeypatch, has_audio):
    store, project, _, decision, settings = setup
    decision = native_decision(decision)
    gateway = OfflineGateway([decision])
    waiting, gate = pause_with_creative((store, project, setup[2], decision, settings), gateway)
    probe = media.probe
    def native_probe(path, *args, **kwargs):
        result = probe(path, *args, **kwargs)
        result["has_audio"] = has_audio
        return result
    monkeypatch.setattr(media, "probe", native_probe)
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "confirm-native"})
    result = execute(store, settings, gateway, queued)
    assert gateway.voice_calls == 0 and gateway.calls == ["scene", "video", "video"]
    assert all(s["segment"]["audio_strategy"] == "native" and s["segment"]["narration"] for s in gateway.submissions)
    assert result["payload"]["design_asset_ids"] == waiting["payload"]["design_asset_ids"]
    if has_audio:
        assert result["status"] == "ready", result.get("error")
        manifest = result["payload"]["manifest"]
        assert manifest["audio_strategy"] == "native" and manifest["audio_asset_id"] is None
        assert manifest["captions"] == [] and manifest["caption_alignment"] == "native_audio_untranscribed"
        assert manifest["audio_alignment_status"] == "not_verified"
        assert manifest["expected_speech_timeline"] == [
            {"start_frame": 0, "end_frame": 96, "segment_id": "s0", "shot_index": 0, "visual_description": "全景", "narration": "看看商品细节。"},
            {"start_frame": 96, "end_frame": 240, "segment_id": "s0", "shot_index": 1, "visual_description": "细节", "narration": "看看商品细节。"},
            {"start_frame": 240, "end_frame": 336, "segment_id": "s1", "shot_index": 0, "visual_description": "全景", "narration": "看看商品细节。"},
            {"start_frame": 336, "end_frame": 480, "segment_id": "s1", "shot_index": 1, "visual_description": "细节", "narration": "看看商品细节。"},
        ]
        assert manifest["manual_review_required"] is True
        assert "audio_asset_id" not in result["payload"]
    else:
        assert result["status"] == "failed" and "没有原生音轨" in result["error"]
        assert store.get_project(project["id"])["current_render_id"] is None


def test_scene_remains_publicly_associated_after_image_confirmation_changes_revision(setup, fake_media):
    store, project, _, decision, settings = setup
    gateway = OfflineGateway([decision])
    waiting, gate = pause_with_creative(setup, gateway)
    scene_ids = waiting["payload"]["design_asset_ids"]
    assert waiting["progress"]["design_asset_ids"] == scene_ids
    assert all(store.get_asset(aid)["metadata"]["job_id"] == waiting["id"] for aid in scene_ids)
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "confirm-scene-reference"})
    assert queued["revision"] != store.get_asset(scene_ids[0])["revision"]
    result = execute(store, settings, gateway, queued)
    assert result["progress"]["design_asset_ids"] == scene_ids and result["status"] == "ready"


def test_expected_speech_timeline_keeps_short_tail_and_silent_shot(setup, fake_media):
    store, _, _, decision, settings = setup
    project = store.create_project("TEST fifteen-second native scene", "product", duration_seconds=15, ad_form="brand_tvc")
    path = store.media_root / "TEST_NATIVE_ORIGINAL.png"
    Image.new("RGB", (16, 16), "blue").save(path)
    original = store.add_asset(project["id"], "product", "TEST original", path, "image/png")
    for segment in decision["plan"]["segments"]:
        segment["reference_asset_ids"] = [original["id"]]
    decision["plan"]["segments"][1].update(duration_seconds=5, shots=[
        {"start": 0, "end": 2, "description": "瓶身细节", "narration": "看细节"},
        {"start": 2, "end": 5, "description": "缓慢收束", "narration": ""},
    ])
    decision = native_decision(decision)
    tail = decision["plan"]["segments"][1]
    tail["shots"][0]["narration"] = "看细节"
    tail["shots"][1]["narration"] = ""
    tail["narration"] = "看细节"
    decision["plan"]["script"] = "".join(s["narration"] for s in decision["plan"]["segments"])
    for option in decision["creative_options"]:
        option["segment"]["reference_asset_ids"] = [original["id"]]
    gateway = OfflineGateway([decision])
    waiting, gate = pause_with_creative((store, project, original, decision, settings), gateway)
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": waiting["revision"],
        "option_id": "current", "client_turn_id": "confirm-short-tail"})
    result = execute(store, settings, gateway, queued)
    assert result["status"] == "ready", result.get("error")
    manifest = result["payload"]["manifest"]
    assert manifest["expected_speech_timeline"][-2:] == [
        {"start_frame": 240, "end_frame": 288, "segment_id": "s1", "shot_index": 0, "visual_description": "瓶身细节", "narration": "看细节"},
        {"start_frame": 288, "end_frame": 360, "segment_id": "s1", "shot_index": 1, "visual_description": "缓慢收束", "narration": ""},
    ]
    assert manifest["total_frames"] == 360 and manifest["audio_alignment_status"] == "not_verified"
    assert manifest["captions"] == [] and gateway.voice_calls == 0


@pytest.mark.parametrize("kind", ["clarify", "plan", "reply"])
def test_real_decision_adapter_uses_full_context_and_strict_split(setup, monkeypatch, kind):
    store, project, asset, plan_decision, settings = setup
    plan_decision = native_decision(plan_decision)
    real_client = httpx.AsyncClient
    strategy, compiled = staged_responses(project, plan_decision)
    outputs = stage_outputs(strategy, compiled)
    outputs["strategy"] = {"clarify": CLARIFY, "plan": strategy, "reply": REPLY}[kind]
    messages = [{"role": "user", "content": f"已确认信息{i}"} for i in range(30)]
    gates = [{"id": "gate_answered", "kind": "clarification", "status": "answered", "questions": [QUESTION],
              "answers": [{"question_id": "channel", "option_id": "douyin"}]}]
    calls = []
    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        contents = payload["messages"][1]["content"]
        context = json.loads(contents if isinstance(contents, str) else contents[0]["text"])
        assert payload["thinking"] == {"type": "enabled" if context["stage"] in ("strategy", "ad_script", "script_review", "alignment_review") else "disabled"}
        if context["stage"] == "strategy":
            assert contents[1]["image_url"]["url"].startswith("data:image/png;base64,")
            assert context["conversation"] == messages and context["gates"] == gates
            assert context["current_plan"] == plan_decision["plan"]
            assert_strategy_wire_schema(context["schema"])
        elif context["stage"] == "storyboard":
            assert kind == "plan"
            assert_legacy_plan_wire_schema(context["schema"])
            assert context["selected_direction"] == strategy["directions"][0]
            assert context["approved_ad_script"] == outputs["script_review"]["script"]
        output = outputs[context["stage"]]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(output), "reasoning_content": "TEST_PRIVATE_REASONING"}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handle), **kw))
    legacy_plan = {**plan_decision["plan"], "cta": "TEST_RETIRED_TAIL_TEXT"}
    result = asyncio.run(Gateway(settings).decide(project, [asset], messages, gates, legacy_plan))
    assert result["kind"] == kind and len(calls) == (5 if kind == "plan" else 1)
    assert "TEST_PRIVATE_REASONING" not in json.dumps(result)
    if kind == "plan":
        assert_staged_plan(result, strategy, compiled)
        assert_script_saved(result["plan"], outputs["ad_script"], outputs["script_review"])
    assert legacy_plan["cta"] == "TEST_RETIRED_TAIL_TEXT"


def test_decision_adapter_rejects_invented_references_without_retry(setup, monkeypatch):
    store, project, asset, decision, settings = setup
    strategy, compiled = staged_responses(project, decision)
    outputs = stage_outputs(strategy, compiled)
    outputs["storyboard"]["segments"][0]["reference_asset_ids"] = ["TEST_PRIVATE_UNKNOWN"]
    real_client, calls = httpx.AsyncClient, []
    def handle(request):
        calls.append(request)
        assert len(calls) <= 4, "Invalid original references must fail without a repair request"
        contents = json.loads(request.content)["messages"][1]["content"]
        context = json.loads(contents if isinstance(contents, str) else contents[0]["text"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(outputs[context["stage"]])}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).decide(project, [asset], [], [], None))
    assert "TEST_PRIVATE_UNKNOWN" not in str(exc.value) and len(calls) == 4


@pytest.mark.parametrize("duration", [10, 15, 60])
def test_real_decision_adapter_honors_selected_duration(setup, monkeypatch, duration):
    store, project, asset, decision, settings = setup
    durations = [10] * (duration // 10) + ([duration % 10] if duration % 10 else [])
    first = decision["plan"]["segments"][0]
    decision["plan"]["segments"] = [{**copy.deepcopy(first), "id": f"s{i}", "duration_seconds": seconds,
        "shots": [{"start": 0, "end": seconds, "description": "完整镜头"}]} for i, seconds in enumerate(durations)]
    strategy, compiled = staged_responses(project, decision)
    outputs = stage_outputs(strategy, compiled)
    real_client, calls = httpx.AsyncClient, []
    def handle(request):
        calls.append(request)
        contents = json.loads(request.content)["messages"][1]["content"]
        context = json.loads(contents if isinstance(contents, str) else contents[0]["text"])
        if context["stage"] != "alignment_review":
            assert context["duration_seconds"] == duration
        if context["stage"] in ("strategy", "storyboard"):
            assert context["segment_durations"] == durations
        if context["stage"] == "storyboard":
            assert "script" not in context["schema"]["properties"]
            assert "narration" not in context["schema"]["$defs"]["Segment"]["properties"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(outputs[context["stage"]])}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handle), **kw))
    result = asyncio.run(Gateway(settings).decide({**project, "duration_seconds": duration}, [asset], [], [], None))
    assert_staged_plan(result, strategy, compiled)
    assert len(calls) == 5
    assert [segment["duration_seconds"] for segment in result["plan"]["segments"]] == durations


def test_conversation_plan_wrong_duration_does_not_persist_or_generate(setup):
    store, project, asset, decision, settings = setup
    decision["plan"]["segments"] = decision["plan"]["segments"][:1]
    gateway = OfflineGateway([decision])
    result = execute(store, settings, gateway, turn(store, project["id"], "wrong-duration"))
    assert result["status"] == "failed" and "所选时长不一致" in result["error"]
    assert store.get_project(project["id"])["plan"] is None and store.pending_gate(project["id"]) is None
    assert gateway.design_calls == gateway.voice_calls == 0 and not gateway.submissions


@pytest.mark.parametrize("brief,response_decision,status,phase", [
    ("你好", {"kind": "reply", "message": "你好！想为哪件商品制作广告？可以先说说需求。"}, "ready", "reply_ready"),
    ("我想推广一只蓝色玻璃瓶，还没有准备图片", CLARIFY, "awaiting_input", "awaiting_input"),
])
def test_text_only_conversation_uses_real_gateway_without_requiring_images(
        setup, monkeypatch, brief, response_decision, status, phase):
    store, _, _, _, settings = setup
    project = store.create_project(brief, "product")
    real_client, calls = httpx.AsyncClient, []
    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        assert "问候、进度、能力、原因等普通问题直接简短答复，不改方案" in system
        assert "纯问候一两句自然回应，只了解想推广什么，不列问卷或技术规格" in system
        assert "先正常聊需求，在创作需要锁定外观时用一句自然回复请上传商品图" in system
        assert "不为上传图片造选择题" in system
        assert "无原图只能reply或clarify" in system
        contents = payload["messages"][1]["content"]
        assert len(contents) == 1 and contents[0]["type"] == "text"
        context = json.loads(contents[0]["text"])
        assert context["assets"] == [] and context["current_plan"] is None
        assert context["stage"] == "strategy"
        assert_strategy_wire_schema(context["schema"])
        assert context["allowed_decisions"] == context["schema"]["properties"]["kind"]["enum"]
        assert set(context["allowed_decisions"]) == {"clarify", "reply"}
        assert context["conversation"][-1] == {"role": "user", "content": brief}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response_decision)}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handle), **kw))
    result = execute(store, settings, Gateway(settings), turn(store, project["id"], "text-only", brief))
    assert result["status"] == status and result["phase"] == phase, result.get("error")
    assert result["lease_until"] == 0 and result["submission_count"] == 0 and len(calls) == 1
    detail = store.project_detail(project["id"])
    assert detail["assets"] == [] and store.get_project(project["id"])["plan"] is None
    assert any(m["role"] == "assistant" and m["content"] == response_decision["message"] for m in detail["messages"])
    assert bool(store.pending_gate(project["id"])) == (response_decision["kind"] == "clarify")


def test_text_only_conversation_rejects_plan_without_original_image(setup, monkeypatch):
    store, _, _, decision, settings = setup
    # Keep the product assertion separate from the missing-image qualifier so
    # the intended failure is the image prerequisite, not citation validation.
    project = store.create_project("蓝色玻璃瓶。帮我决定全部内容，但还没有商品图", "product")
    strategy, _ = staged_responses(project, decision)
    assert all(fact["source_kind"] == "merchant" for fact in strategy["brief"]["facts"])
    real_client, calls = httpx.AsyncClient, []
    def handle(request):
        calls.append(request)
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert len(calls) == 1, "No original image means no storyboard compilation"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(strategy)}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handle), **kw))
    result = execute(store, settings, Gateway(settings), turn(store, project["id"], "no-image-plan", project["brief"]))
    assert result["status"] == "failed" and "重新上传商品图" in result["error"]
    assert result["submission_count"] == 0 and result["lease_until"] == 0 and len(calls) == 1
    assert store.get_project(project["id"])["plan"] is None and store.pending_gate(project["id"]) is None
    assert store.project_detail(project["id"])["assets"] == []


@pytest.mark.parametrize("query_failures", [1, 3])
def test_partial_design_receipts_resume_before_creative_gate_without_duplicate_images(setup, fake_media, monkeypatch, query_failures):
    import io
    import socket
    from app.providers import gateway as provider_module
    store, project, asset, decision, settings = setup
    gateway = OfflineGateway([decision])
    execute(store, settings, gateway, turn(store, project["id"], "plan-partial"))
    p = store.get_project(project["id"])
    job = store.create_job(p["id"], "generate", p["revision"], {"pause_for_creative": True})
    # Exercise the real receipt/download implementation with only HTTP transport replaced.
    settings.planner_retry_delay_seconds = 0
    real = Gateway(settings)
    async def original_reference(asset):
        # Upload transport has its own adapter tests; this fixture isolates image receipts.
        assert asset["id"] == original_asset_id
        return "https://fixture.invalid/original.png"
    original_asset_id = asset["id"]
    real._original_reference_url = original_reference
    gateway.create_designs = real.create_designs
    real_client, posts = httpx.AsyncClient, []
    failed_scene_query = 0
    png = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(png, "PNG")
    monkeypatch.setattr(provider_module.socket, "getaddrinfo", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    def handle(request):
        nonlocal failed_scene_query
        if request.method == "POST":
            assert request.url.path == "/v1/images/generations/async"
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"id": "task_scene"}})
        if request.url.path.startswith("/v1/tasks/"):
            scene = request.url.path.endswith("task_scene")
            if scene and failed_scene_query < query_failures:
                failed_scene_query += 1
                raise httpx.ReadError("EXPLICIT_TEST_TEMPORARY_QUERY_FAILURE", request=request)
            name = "scene" if scene else "product"
            return httpx.Response(200, json={"data": {"status": "completed", "result": {
                "images": [{"url": [f"https://fixture.invalid/{name}.png"]}]}}})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=png.getvalue())
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handle), **kw))
    partial = execute(store, settings, gateway, job)
    assert partial["status"] == ("failed" if query_failures == 3 else "awaiting_input")
    assert len(posts) == 1 and not gateway.submissions
    # The real compiler was used even though no image asset has been downloaded.
    usage = next(item for item in store.project_detail(project["id"])["jobs"] if item["id"] == job["id"])["media_skill_usage"]
    assert len(usage) == 1 and usage[0]["stage"] == "asset_prompts" and usage[0]["mode"] == "compiler"
    folder = Path(partial["payload"]["designs_directory"])
    assert not (folder / "product.png").exists()
    assert json.loads((folder / "scene_task.json").read_text())["task_id"] == "task_scene"
    if query_failures == 3:
        assert store.pending_gate(project["id"]) is None
        resumed = execute(Store(store.db_path, store.media_root), settings, gateway, partial)
    else:
        resumed = partial  # A temporary query failure needs no user recovery.
    assert resumed["status"] == "awaiting_input" and resumed["submission_count"] == 0
    assert gateway.voice_calls == 0 and len(posts) == 1 and not gateway.submissions
    assert len(resumed["payload"]["design_asset_ids"]) == 1
    gate = store.pending_gate(project["id"], "creative")
    queued = store.confirm_creative(project["id"], {"gate_id": gate["id"], "accepted_revision": resumed["revision"],
        "option_id": "current", "client_turn_id": "confirm-partial"})
    result = execute(store, settings, gateway, queued)
    assert result["status"] == "ready" and len(posts) == 1 and gateway.voice_calls == 1 and len(gateway.submissions) == 2


def test_decision_prompt_does_not_offer_unverified_product_claims(setup, monkeypatch):
    store, project, asset, decision, settings = setup
    real_client = httpx.AsyncClient
    calls = []
    def handle(request):
        calls.append(request)
        system = json.loads(request.content)["messages"][0]["content"]
        assert "不预填未知的功效或参数供用户认领" in system
        assert "questions仅提供可选的实际答案" in system
        assert "自由输入由统一输入框承担" in system
        assert "不要给自填、我来补充、其他、让AI决定等伪选项" in system
        assert "没有证据就不要选择功效演示或对比证明" in system
        assert "原图只有外观时可以做外观" in system
        assert "text必须是对应原文中连续出现的准确摘录，不能改写、拼接或增加事实" in system
        assert "1至3个真正不同的directions" in system
        assert "候选的rule_id各不相同" in system and "不能只是换标题或运镜" in system
        assert "资料受限时允许只给一个方向" in system
        assert "没有新增确认门、营销报告、独立结尾字卡或强制购买引导" in system
        assert "不承诺高转化、自然音色或已验证声画同步" in system
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(CLARIFY)}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handle), **kw))
    result = asyncio.run(Gateway(settings).decide(project, [asset], [], [], None))
    assert result["kind"] == "clarify" and len(calls) == 1
