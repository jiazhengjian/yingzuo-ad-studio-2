"""Offline planner-provider isolation, actual wire requests and replay guards.

Every credential is synthetic. App imports and dotenv tests use temporary
settings so this file can run without reading the developer's .env or data.
"""
import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import importlib
import io
import json
import logging
import socket
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from app import config
from app.config import Settings
from app.planner_transport import transport_options
from app.planning_session import PlanningSession, fingerprint
from app.providers.gateway import Gateway, ProviderError
from app.schemas import Plan


DEEPSEEK_KEY = "TEST_DEEPSEEK_ACCOUNT_ONLY"
TUDOU_KEY = "TEST_TUDOU_ACCOUNT_ONLY"


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Planner transport regression attempted a real socket")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_root=tmp_path / "data", planner_provider="tudou",
        planner_model="gpt-6-sol", deepseek_api_key=DEEPSEEK_KEY,
        tudou_api_key=TUDOU_KEY, ffmpeg_path="not-invoked")


@pytest.fixture
def transport(monkeypatch):
    client = httpx.AsyncClient

    def install(respond):
        def isolated_client(**kwargs):
            assert kwargs.get("trust_env") is False
            return client(transport=httpx.MockTransport(respond), **kwargs)
        monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    return install


def completion(content, *, finish_reason="stop", **extra):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish_reason,
        "message": {"content": json.dumps(content, ensure_ascii=False)}}], **extra})


@pytest.mark.parametrize("stage,effort,budget", [
    ("strategy", "low", 65536),
    ("direction_review", "high", 65536),
    ("ad_script", "low", 24000),
    ("storyboard", "high", 32768),
    ("plan_review", "medium", 32768),
    ("plan_refinement", "medium", 32768),
])
def test_tudou_actual_request_keeps_image_json_and_stage_budget(
        settings, tmp_path, transport, stage, effort, budget):
    image_path = tmp_path / "synthetic-product.png"
    Image.new("RGB", (8, 8), "blue").save(image_path)
    source = {"id": "synthetic-product", "path": str(image_path), "mime": "image/png"}
    context = {"stage": stage, "stage_skills": {"skills": []}, "merchant_request": "看清蓝瓶"}
    before = deepcopy(context)
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://api.ai-tudou.net/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {TUDOU_KEY}"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-6-sol"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["stream"] is False
        assert payload["reasoning_effort"] == effort
        assert payload["max_completion_tokens"] == budget
        assert "thinking" not in payload and "max_tokens" not in payload
        assert DEEPSEEK_KEY not in request.content.decode()
        assert payload["messages"][0] == {"role": "system", "content": "Return source-bound JSON"}
        content = payload["messages"][1]["content"]
        assert json.loads(content[0]["text"]) == before
        assert content[1]["type"] == "image_url"
        prefix, encoded = content[1]["image_url"]["url"].split(",", 1)
        assert prefix == "data:image/png;base64"
        assert base64.b64decode(encoded) == image_path.read_bytes()
        return completion({"checked": True})

    transport(respond)
    assert asyncio.run(Gateway(settings)._planner_json(
        "Return source-bound JSON", context, [source], max_tokens=5500)) == {"checked": True}
    assert len(requests) == 1 and context == before


def test_settings_without_provider_keep_legacy_deepseek_transport(transport):
    legacy = SimpleNamespace(planner_model="deepseek-flash", deepseek_api_key=DEEPSEEK_KEY,
        tudou_api_key=TUDOU_KEY)

    def respond(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == f"Bearer {DEEPSEEK_KEY}"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-flash"
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "high"
        assert payload["max_tokens"] == 65536
        assert "max_completion_tokens" not in payload
        return completion({"checked": True})

    transport(respond)
    assert asyncio.run(Gateway(legacy)._planner_json("JSON", {
        "stage": "direction_review", "stage_skills": {"skills": []}}, [])) == {"checked": True}


@pytest.mark.parametrize("provider,model,stage,initial,expected", [
    ("tudou", "gpt-6-sol", "plan_review", "high", "medium"),
    ("tudou", "gpt-6-sol", None, "high", "high"),
    ("tudou", "gpt-6-sol", "plan_review", "low", "low"),
    ("tudou", "gpt-6-sol", "storyboard", "high", "high"),
    ("tudou", "gpt-6-sol", "plan_refinement", "high", "medium"),
    ("tudou", "gpt-6-sol", "plan_refinement", "low", "low"),
    ("tudou", "gpt-6-sol", "direction_review", "high", "high"),
    ("tudou", "another-model", "plan_review", "high", "high"),
    ("tudou", "another-model", "plan_refinement", "high", "high"),
    ("deepseek", "gpt-6-sol", "plan_review", "high", "high"),
    ("deepseek", "gpt-6-sol", "plan_refinement", "high", "high"),
])
def test_sol_review_and_refinement_profile_has_no_other_scope(settings, provider, model, stage, initial, expected):
    before = {"reasoning_effort": initial, "max_tokens": 32768, "thinking": {"type": "enabled"}}
    options = deepcopy(before)
    adjusted = transport_options(replace(settings, planner_provider=provider, planner_model=model), options, stage=stage)
    assert adjusted["reasoning_effort"] == expected
    assert adjusted["max_completion_tokens" if provider == "tudou" else "max_tokens"] == 32768
    assert options == before


@pytest.mark.parametrize("initial_profile", ["current_medium", "historical_high"])
@pytest.mark.parametrize("target_stage", ["plan_review", "plan_refinement"])
def test_actual_request_and_checkpoint_share_medium_profile_without_replaying_high(
        settings, transport, tmp_path, monkeypatch, initial_profile, target_stage):
    from test_progressive_planning import chosen_strategy, director_output, accepted_review, reject_first_shot
    # A local synthetic asset supplies the ordinary source-byte fingerprint.
    source_path = tmp_path / "source.png"
    Image.new("RGB", (8, 8), "blue").save(source_path)
    source = {"id": "source", "name": "离线蓝瓶", "path": str(source_path), "mime": "image/png", "kind": "product", "metadata": {}}
    strategy = chosen_strategy(source)
    project = {"id": "synthetic_project", "brief": "蓝瓶商品", "duration_seconds": 20,
               "revision": 0, "context_version": 1}
    requests, signatures, checkpoints = [], [], []
    original_call = PlanningSession.call

    async def captured_call(self, stage, signature, perform, **kwargs):
        signatures.append((stage, deepcopy(signature)))
        return await original_call(self, stage, signature, perform, **kwargs)
    monkeypatch.setattr(PlanningSession, "call", captured_call)

    def respond(request):
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        requests.append((context["stage"], payload))
        if context["stage"] == "ad_script":
            return completion(director_output(strategy)["ad_script"])
        if context["stage"] == "storyboard":
            return completion(director_output(strategy))
        if context["stage"] == "plan_refinement":
            refined = deepcopy(context["draft_storyboard"])
            refined["shots"][0]["description"] += "仅保留静置辨认。"
            return completion(refined)
        assert context["stage"] == "plan_review"
        review = accepted_review(context["final_plan"])
        if target_stage == "plan_refinement" and sum(stage == "plan_review" for stage, _ in requests) == 1:
            reject_first_shot(review)
        return completion(review)
    transport(respond)
    if initial_profile == "historical_high":
        monkeypatch.setattr("app.providers.gateway.transport_options",
            lambda selected_settings, options, *, stage=None: transport_options(selected_settings, options,
                stage=None if stage == target_stage else stage))

    def run(checkpoint=None):
        return asyncio.run(Gateway(settings).decide(project, [source], [], [], None,
            selected_strategy=strategy.model_dump(), planning_checkpoint=checkpoint,
            on_checkpoint=lambda value: checkpoints.append(deepcopy(value))))

    result = run()
    assert result["kind"] == "plan" and result["plan"]["plan_review"]["ready"] is True
    expected_stages = ["ad_script", "storyboard", "plan_review"]
    if target_stage == "plan_refinement":
        expected_stages += ["plan_refinement", "plan_review"]
    assert [stage for stage, _ in requests] == expected_stages
    target_index = expected_stages.index(target_stage)
    expected_effort = "high" if initial_profile == "historical_high" else "medium"
    assert requests[target_index][1]["reasoning_effort"] == signatures[target_index][1]["options"]["reasoning_effort"] == expected_effort
    assert requests[target_index][1]["max_completion_tokens"] == signatures[target_index][1]["options"]["max_completion_tokens"] == 32768
    assert requests[1][1]["reasoning_effort"] == signatures[1][1]["options"]["reasoning_effort"] == "high"
    checkpoint = deepcopy(checkpoints[-1])
    prior_signatures = deepcopy(signatures)
    monkeypatch.setattr("app.providers.gateway.transport_options", transport_options)
    count = len(expected_stages)
    if initial_profile == "historical_high":
        resumed = run(checkpoint)
        assert resumed["kind"] == "plan" and resumed["plan"]["plan_review"]["ready"] is True
        assert [stage for stage, _ in requests[count:]][0] == target_stage
    else:
        assert run(checkpoint) == result
        assert len(requests) == count
    # Every unchanged earlier stage replays, including an earlier rejected review.
    assert signatures[count:count + target_index] == prior_signatures[:target_index]
    replayed_target = signatures[count + target_index]
    assert replayed_target[0] == target_stage
    assert replayed_target[1]["options"]["reasoning_effort"] == "medium"
    if initial_profile == "historical_high":
        assert checkpoint["entries"][target_index]["input_hash"] != fingerprint(replayed_target[1])


@pytest.mark.parametrize("effort", ["high", "medium"])
def test_storyboard_effort_matches_wire_and_replay_identity(
        settings, transport, tmp_path, monkeypatch, effort):
    from test_progressive_planning import chosen_strategy, director_output, accepted_review
    source_path = tmp_path / "source.png"
    Image.new("RGB", (8, 8), "blue").save(source_path)
    source = {"id": "source", "name": "离线蓝瓶", "path": str(source_path),
              "mime": "image/png", "kind": "product", "metadata": {}}
    strategy = chosen_strategy(source)
    project = {"id": "synthetic_project", "brief": "蓝瓶商品", "duration_seconds": 20,
               "revision": 0, "context_version": 1}
    requests, signatures, checkpoints = [], [], []
    original_call = PlanningSession.call

    async def captured_call(self, stage, signature, perform, **kwargs):
        signatures.append((stage, deepcopy(signature)))
        return await original_call(self, stage, signature, perform, **kwargs)
    monkeypatch.setattr(PlanningSession, "call", captured_call)

    def respond(request):
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][1]["content"][0]["text"])
        requests.append((context, payload))
        if context["stage"] == "ad_script":
            return completion(director_output(strategy)["ad_script"])
        if context["stage"] == "storyboard":
            return completion(director_output(strategy))
        assert context["stage"] == "plan_review"
        return completion(accepted_review(context["final_plan"]))
    transport(respond)

    def run(selected_effort, checkpoint=None):
        return asyncio.run(Gateway(replace(settings, planner_storyboard_effort=selected_effort)).decide(
            project, [source], [], [], None, selected_strategy=strategy.model_dump(),
            planning_checkpoint=checkpoint,
            on_checkpoint=lambda value: checkpoints.append(deepcopy(value))))

    result = run(effort)
    assert result["kind"] == "plan" and result["plan"]["plan_review"]["ready"] is True
    assert [context["stage"] for context, _ in requests] == ["ad_script", "storyboard", "plan_review"]
    assert [payload["reasoning_effort"] for _, payload in requests] == ["low", effort, "medium"]
    for (context, payload), (_, signature) in zip(requests, signatures):
        assert signature["options"]["reasoning_effort"] == payload["reasoning_effort"]
        assert signature["options"]["max_completion_tokens"] == payload["max_completion_tokens"]
        # Actual method text remains on the wire; server provenance remains in
        # identity even after wire-only bookkeeping is removed.
        canonical = signature["context"]["stage_skills"]
        assert canonical["content_sha256"]
        assert "content_sha256" not in context["stage_skills"]
        assert [item["content"] for item in context["stage_skills"]["skills"]] == [
            item["content"] for item in canonical["skills"]]
    assert requests[1][1]["max_completion_tokens"] == 32768
    checkpoint = deepcopy(checkpoints[-1])
    assert run(effort, checkpoint) == result
    assert len(requests) == 3
    # The draft can replay across a board-only effort change. The board must
    # reject the old response before sending any new paid request.
    signatures.clear()
    before = len(requests)
    resumed = run("medium" if effort == "high" else "high", checkpoint)
    assert resumed["kind"] == "plan" and resumed["plan"]["plan_review"]["ready"] is True
    assert [stage for stage, _ in signatures] == ["ad_script", "storyboard", "plan_review"]
    assert fingerprint(signatures[0][1]) == checkpoint["entries"][0]["input_hash"]
    assert fingerprint(signatures[1][1]) != checkpoint["entries"][1]["input_hash"]
    assert [context["stage"] for context, _ in requests[before:]] == ["storyboard", "plan_review"]


def test_storyboard_effort_does_not_change_legacy_wire_profile(settings, transport):
    def respond(request):
        payload = json.loads(request.content)
        assert payload["reasoning_effort"] == "low"
        assert payload["max_completion_tokens"] == 5500
        return completion({"checked": True})
    transport(respond)
    assert asyncio.run(Gateway(replace(settings, planner_storyboard_effort="medium"))._planner_json(
        "JSON", {"stage": "storyboard"}, [], max_tokens=5500)) == {"checked": True}


def test_copy_fitting_uses_selected_account_and_preserves_shots(settings, transport):
    plan = Plan.model_validate({"title": "蓝瓶", "script": "看清细节。", "language": "zh-CN",
        "aspect_ratio": "9:16", "audio_strategy": "voiceover", "facts": {"name": "蓝瓶", "claims": []},
        "segments": [{"id": f"s{i}", "duration_seconds": 10, "purpose": "展示",
            "prompt": "蓝瓶的外观", "shots": [{"start": 0, "end": 10, "description": "外观展示"}],
            "reference_asset_ids": ["synthetic-product"]} for i in range(2)]}).model_dump()
    plan["script"] = "只使用已经确认的商品资料来介绍这只蓝瓶。" * 10
    before = deepcopy(plan)
    requests = []

    def respond(request):
        requests.append(request)
        assert str(request.url) == "https://api.ai-tudou.net/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {TUDOU_KEY}"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-6-sol"
        assert payload["max_completion_tokens"] == 4096
        assert payload["reasoning_effort"] == "low"
        assert "max_tokens" not in payload and "thinking" not in payload
        assert payload["response_format"] == {"type": "json_object"}
        return completion({"script": "看清蓝瓶的细节。"})

    transport(respond)
    result = asyncio.run(Gateway(settings)._fit_script(plan, 20, {}))
    assert result["script"] == "看清蓝瓶的细节。"
    assert result["segments"] == before["segments"]
    assert plan == before and len(requests) == 1


@pytest.mark.parametrize("provider,deepseek_key,tudou_key,configured", [
    ("tudou", "", TUDOU_KEY, True),
    ("tudou", DEEPSEEK_KEY, "", False),
    ("deepseek", DEEPSEEK_KEY, "", True),
    ("deepseek", "", TUDOU_KEY, False),
])
def test_health_and_plan_authorization_follow_selected_provider(
        settings, monkeypatch, provider, deepseek_key, tudou_key, configured):
    selected = replace(settings, planner_provider=provider,
        planner_model="gpt-6-sol" if provider == "tudou" else "deepseek-flash",
        deepseek_api_key=deepseek_key, tudou_api_key=tudou_key)
    # main creates an application on import. Keep that import isolated too.
    with monkeypatch.context() as guarded:
        guarded.setattr(Settings, "from_env", classmethod(lambda cls: selected))
        main = importlib.import_module("app.main")
    app = main.create_app(selected, run_worker=False)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.json()["configured"]["planner"] is configured
        assert health.json()["capabilities"]["planner_provider"] == provider
        assert health.json()["capabilities"]["planner_model"] == selected.planner_model
        assert selected.planner_model in health.json()["providers"]["planner"]
        assert DEEPSEEK_KEY not in health.text and TUDOU_KEY not in health.text
        project = client.post("/api/projects", json={"brief": "介绍蓝瓶", "ad_form": "auto"}).json()
        picture = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(picture, "PNG")
        upload = client.post(f"/api/projects/{project['id']}/assets",
            files={"file": ("synthetic.png", picture.getvalue(), "image/png")})
        assert upload.status_code == 201
        result = client.post(f"/api/projects/{project['id']}/plan", json={})
        assert result.status_code == (200 if configured else 409)
        assert bool(app.state.store.active_jobs()) is configured
        if configured:
            assert result.json()["job"]["kind"] == "plan"
            assert result.json()["job"]["status"] == "queued"
        assert DEEPSEEK_KEY not in result.text and TUDOU_KEY not in result.text


@pytest.fixture
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    for key in ("PLANNER_PROVIDER", "PLANNER_MODEL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY",
                "TUDOU_API_KEY", "AD_DATA_ROOT", "TTS_VOICE", "JOINT_PLANNING_ENABLED",
                "PLANNER_STORYBOARD_EFFORT", "PLANNER_DIRECTION_REVIEW_EFFORT", "PLANNER_STAGE_ROUTES",
                "PLANNER_FALLBACK_PROVIDER", "PLANNER_FALLBACK_MODEL", "PLANNER_FALLBACK_ON",
                "PLANNER_STREAM", "PLANNER_REQUEST_DEADLINE_SECONDS", "PLANNER_MAX_REFINEMENTS",
                "PLANNER_REFINEMENT_FORMAT", "PLANNER_SPECULATE_RECOMMENDED"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


@pytest.mark.parametrize("environment,provider,model", [
    ({}, "deepseek", "deepseek-flash"),
    ({"DEEPSEEK_MODEL": "legacy-model"}, "deepseek", "legacy-model"),
    ({"PLANNER_PROVIDER": "tudou", "DEEPSEEK_MODEL": "legacy-model"}, "tudou", "gpt-6-sol"),
    ({"PLANNER_PROVIDER": " TuDou ", "PLANNER_MODEL": " explicit-model ",
      "DEEPSEEK_MODEL": "legacy-model"}, "tudou", "explicit-model"),
    ({"PLANNER_MODEL": "explicit-model", "DEEPSEEK_MODEL": "legacy-model"}, "deepseek", "explicit-model"),
])
def test_environment_selection_is_explicit_and_legacy_compatible(
        isolated_environment, monkeypatch, environment, provider, model):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    configured = Settings.from_env()
    assert configured.planner_provider == provider and configured.planner_model == model
    assert configured.data_root == isolated_environment / "data"
    assert not configured.deepseek_api_key and not configured.tudou_api_key


@pytest.mark.parametrize("environment,joint,effort", [
    ({}, False, "high"),
    ({"JOINT_PLANNING_ENABLED": " true ", "PLANNER_STORYBOARD_EFFORT": " Medium "}, True, "medium"),
    ({"JOINT_PLANNING_ENABLED": "FALSE", "PLANNER_STORYBOARD_EFFORT": "low"}, False, "low"),
])
def test_experimental_planning_profiles_require_explicit_configuration(
        isolated_environment, monkeypatch, environment, joint, effort):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    configured = Settings.from_env()
    assert configured.joint_planning_enabled is joint
    assert configured.planner_storyboard_effort == effort


@pytest.mark.parametrize("key,value", [
    ("JOINT_PLANNING_ENABLED", "yes"),
    ("PLANNER_STORYBOARD_EFFORT", "automatic"),
])
def test_invalid_planning_profiles_fail_before_provider_use(
        isolated_environment, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        Settings.from_env()


def test_local_dotenv_precedence_uses_only_isolated_file(isolated_environment, monkeypatch):
    monkeypatch.setenv("PLANNER_PROVIDER", "deepseek")
    monkeypatch.setenv("PLANNER_MODEL", "shell-model")
    (isolated_environment / ".env").write_text(
        "PLANNER_PROVIDER=tudou\nPLANNER_MODEL=local-model\nTUDOU_API_KEY=TEST_LOCAL_KEY\n")
    configured = Settings.from_env()
    assert (configured.planner_provider, configured.planner_model) == ("tudou", "local-model")
    assert configured.tudou_api_key == "TEST_LOCAL_KEY" and not configured.deepseek_api_key


def test_unknown_provider_cannot_fall_back_to_deepseek(isolated_environment, monkeypatch):
    monkeypatch.setenv("PLANNER_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="PLANNER_PROVIDER"):
        Settings.from_env()


def test_tudou_truncation_reports_actual_budget_without_private_response(
        settings, transport, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {
            "content": "PRIVATE_PARTIAL_PLAN", "reasoning_content": "PRIVATE_REASONING"}}],
            "usage": {"completion_tokens": 65536, "private": "PRIVATE_USAGE",
                "completion_tokens_details": {"reasoning_tokens": 64000}}})

    transport(respond)
    with caplog.at_level(logging.WARNING, logger="app.providers.gateway"):
        with pytest.raises(ProviderError, match="长度上限") as error:
            asyncio.run(Gateway(settings)._planner_json("PRIVATE_PROMPT", {
                "stage": "direction_review", "stage_skills": {"skills": []}}, []))
    assert len(requests) == 1
    record = next(r.getMessage() for r in caplog.records
        if r.getMessage().startswith("Planner incomplete response: "))
    detail = json.loads(record.split(": ", 1)[1])
    assert detail["stage"] == "direction_review" and detail["max_tokens"] == 65536
    assert detail["usage"]["reasoning_tokens"] == 64000
    exposed = caplog.text + str(error.value)
    assert not any(secret in exposed for secret in ("PRIVATE", DEEPSEEK_KEY, TUDOU_KEY))
    assert not (settings.data_root / "diagnostics").exists()


@pytest.mark.parametrize("initial_provider,next_provider", [("deepseek", "tudou"), ("tudou", "deepseek")])
def test_provider_change_rejects_completed_checkpoint_even_with_same_model(
        settings, transport, initial_provider, next_provider):
    # Keep the synthetic model name identical to exercise provider isolation,
    # independently of the existing model-name fingerprint check.
    initial = replace(settings, planner_provider=initial_provider)
    calls, checkpoints = [], []

    def respond(request):
        calls.append(request)
        return completion({"kind": "reply", "message": "请补充商品信息。"})

    transport(respond)
    project = {"id": "project_offline", "brief": "你好", "route": "product", "revision": 0,
        "context_version": 1, "duration_seconds": 20, "ad_form": "auto"}
    messages = [{"id": "user_offline", "role": "user", "content": "你好"}]
    result = asyncio.run(Gateway(initial).decide(project, [], messages, [], None,
        on_checkpoint=lambda value: checkpoints.append(deepcopy(value))))
    assert result["kind"] == "reply" and len(calls) == 1
    checkpoint = deepcopy(checkpoints[-1])
    replay = asyncio.run(Gateway(initial).decide(project, [], messages, [], None,
        planning_checkpoint=checkpoint))
    assert replay == result and len(calls) == 1
    changed = asyncio.run(Gateway(replace(initial, planner_provider=next_provider)).decide(
        project, [], messages, [], None, planning_checkpoint=checkpoint,
        on_checkpoint=lambda value: checkpoints.append(deepcopy(value))))
    assert changed["kind"] == "reply" and len(calls) == 2
    assert checkpoints[-1]["entries"][0]["input_hash"] != checkpoint["entries"][0]["input_hash"]
