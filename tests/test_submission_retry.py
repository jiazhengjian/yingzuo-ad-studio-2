"""Unpaid provider refusals retry inside the job. No live provider calls."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.merchant_cards import AUTH_CARD, BALANCE_CARD, SYSTEM_CARD
from app.providers import gateway as gateway_module
from app.providers.gateway import Gateway, ProviderError, ProviderResultError, SubmissionError
from test_failed_video_retry import prepared, run
from test_pipeline import fake_media, setup
from test_providers import SECRET, install_transport, plan, prepared_original, settings, source


def _video_settings(tmp_path, delays):
    return SimpleNamespace(tudou_api_key=SECRET, deepseek_api_key=SECRET, data_root=tmp_path,
                           video_model="wan3.0", image_model="gpt-image-2-all",
                           submission_retry_delays=delays, poll_seconds=0, provider_timeout_seconds=2)


class ScriptedVideoGateway(Gateway):
    """Real unpaid-retry POST, with the rest of the provider replaced by fixtures."""

    def __init__(self, settings, store, plan):
        super().__init__(settings)
        self.store, self.plan_data = store, plan
        self.submissions, self.polls, self.downloads, self.voices = [], [], 0, 0

    def prepare_video_request(self, segment):
        prompt = "冻结测试"
        return {"model": "wan3.0", "prompt": prompt, "duration": segment["duration_seconds"],
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "input_sha256": self._video_input_hash(segment), "skill_trace": []}

    async def submit_video(self, segment, assets, idempotency_key, *, prepared_request=None):
        self.submissions.append((idempotency_key, prepared_request))
        task_id = await self._submit("/v1/videos/generations", {"model": "wan3.0", "prompt": "冻结测试"})
        receipt = {"task_id": task_id, "provider": "tudou", "model": "wan3.0"}
        if self._submission_refusals:
            receipt["submission_refusals"] = list(self._submission_refusals)
        return receipt

    async def poll_video(self, task_id):
        self.polls.append(task_id)
        return {"status": "succeeded", "download_url": "https://fixture.invalid/" + task_id}

    async def download_video(self, url, destination):
        self.downloads += 1
        Path(destination).write_bytes(b"TEST_FAKE_VIDEO")

    async def synthesize_voice(self, text, destination, *, target_duration_seconds=None):
        self.voices += 1
        Path(destination).write_bytes(b"TEST_FAKE_AUDIO")
        return {"duration_seconds": 1, "provider": "EXPLICIT_TEST_FAKE", "captions": [
            {"start": 0, "end": .5, "text": "展示商品。"}, {"start": .5, "end": 1, "text": "了解详情。"}]}


def _install(monkeypatch, handler):
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        assert kwargs.get("trust_env") is False
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(gateway_module.httpx, "AsyncClient", client)


def _diagnostics(tmp_path):
    folder = tmp_path / "diagnostics"
    return [json.loads(path.read_text()) for path in folder.glob("*.json")] if folder.exists() else []


def test_transient_4xx_then_success_continues_without_a_second_submission(prepared, fake_media, monkeypatch, tmp_path):
    store, project, plan, _, job = prepared
    posts = []

    def handle(request):
        posts.append(request.content)
        if len(posts) == 1:
            return httpx.Response(400, text="上游繁忙 upstream overloaded https://cdn.example/secret " + SECRET)
        return httpx.Response(200, json={"data": {"id": "task-" + str(len(posts))}})

    _install(monkeypatch, handle)
    gateway = ScriptedVideoGateway(_video_settings(tmp_path, (0,)), store, plan)
    result = run(prepared, gateway)
    assert result["status"] == "ready"
    assert not result.get("error")
    assert result.get("progress", {}).get("retry_action") is None
    assert result["submission_count"] == 2
    assert len(gateway.submissions) == 2
    assert len(posts) == 3 and posts[0] == posts[1] == posts[2]
    labels = [event["label"] for event in store.project_detail(project["id"])["events"]]
    assert labels.count("视频平台繁忙，正在自动重新提交") == 1
    first = store.get_step(job["id"], "video:s0")
    second = store.get_step(job["id"], "video:s1")
    assert first["state"] == "downloaded" and first["task_id"] == "task-2"
    assert second["task_id"] == "task-3"
    refusal = first["result"]["submission_refusals"][0]
    assert refusal["status"] == 400 and refusal["refusal"] == "transient"
    assert "https://" not in refusal["excerpt"] and SECRET not in refusal["excerpt"]
    assert "upstream overloaded" in refusal["excerpt"]
    saved = _diagnostics(tmp_path)
    assert any(item["status"] == 400 and item["route"] == "video" and SECRET not in item["excerpt"]
               and "https://" not in item["excerpt"] for item in saved)
    assert "重试失败片段" not in json.dumps(result, ensure_ascii=False)


def test_balance_refusal_is_a_merchant_sentence_and_is_not_reposted(prepared, fake_media, monkeypatch, tmp_path):
    store, _, plan, _, job = prepared
    posts = []

    def handle(request):
        posts.append(1)
        return httpx.Response(402, text="insufficient balance https://pay.example/topup " + SECRET)

    _install(monkeypatch, handle)
    result = run(prepared, ScriptedVideoGateway(_video_settings(tmp_path, (0, 0, 0)), store, plan))
    assert result["status"] == "failed"
    assert result["error"] == BALANCE_CARD
    assert result["progress"]["error_code"] == "provider_balance"
    assert result["progress"]["retryable"] is False
    assert result["progress"].get("retry_action") is None
    assert len(posts) == 1 and result["submission_count"] == 1
    step = store.get_step(job["id"], "video:s0")
    assert step["state"] == "rejected" and step["task_id"] is None
    assert step["result"]["provider_status"] == 402
    assert SECRET not in step["result"]["provider_excerpt"] and "https://" not in step["result"]["provider_excerpt"]
    assert "重试失败片段" not in result["error"]


def test_timeout_stays_unknown_and_is_not_reposted(prepared, fake_media, monkeypatch, tmp_path):
    store, project, plan, _, job = prepared
    posts = []

    def handle(request):
        posts.append(1)
        raise httpx.ReadTimeout(SECRET, request=request)

    _install(monkeypatch, handle)
    result = run(prepared, ScriptedVideoGateway(_video_settings(tmp_path, (0, 0, 0)), store, plan))
    assert result["status"] == "submitted_unknown"
    assert result["phase"] == "needs_reconcile"
    assert len(posts) == 1 and result["submission_count"] == 1
    step = store.get_step(job["id"], "video:s0")
    assert step["state"] == "submitted_unknown" and step["task_id"] is None
    labels = [event["label"] for event in store.project_detail(project["id"])["events"]]
    assert "视频平台繁忙，正在自动重新提交" not in labels
    assert not _diagnostics(tmp_path)


def test_permanent_parameter_refusal_is_the_system_sentence(prepared, fake_media, monkeypatch, tmp_path):
    store, _, plan, _, job = prepared
    posts = []

    def handle(request):
        posts.append(1)
        return httpx.Response(400, text="invalid parameter: duration")

    _install(monkeypatch, handle)
    result = run(prepared, ScriptedVideoGateway(_video_settings(tmp_path, (0, 0, 0)), store, plan))
    assert result["status"] == "failed" and result["error"] == SYSTEM_CARD
    assert result["progress"]["error_code"] == "provider_request_rejected"
    assert result["progress"]["retryable"] is False and result["progress"].get("retry_action") is None
    assert len(posts) == 1
    assert store.get_step(job["id"], "video:s0")["result"]["provider_status"] == 400
    assert "重试失败片段" not in result["error"]


def test_auth_refusal_is_not_retried(settings, install_transport):
    install_transport(lambda request: httpx.Response(401, text="bad key " + SECRET))
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(Gateway(settings)._submit("/v1/videos/generations", {"prompt": "x"}))
    assert exc.value.uncertain is False
    assert exc.value.error_code == "provider_auth" and str(exc.value) == AUTH_CARD
    assert SECRET not in str(exc.value)


def _connect_refused(request):
    raise httpx.ConnectError(SECRET, request=request)


@pytest.mark.parametrize("handler,uncertain", [
    (lambda request: httpx.Response(500, text="upstream " + SECRET), True),
    (lambda request: httpx.Response(200, json={"data": {}}), True),
    (lambda request: httpx.Response(429, json={"id": "task-already", "error": "busy"}), True),
    (_connect_refused, False),
])
def test_server_error_missing_task_and_task_id_on_4xx_are_not_reposted(settings, install_transport, handler, uncertain):
    settings.submission_retry_delays = (0, 0, 0)
    posts = []

    def wrapped(request):
        posts.append(request.url.path)
        return handler(request)

    install_transport(wrapped)
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(Gateway(settings)._submit("/v1/videos/generations", {"prompt": "same"}))
    assert exc.value.uncertain is uncertain and len(posts) == 1
    assert SECRET not in str(exc.value)


def test_transient_status_retries_three_times_then_records_the_system_sentence(settings, install_transport, monkeypatch, tmp_path):
    settings.submission_retry_delays = (20, 60, 120)
    settings.data_root = tmp_path
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gateway_module.asyncio, "sleep", fake_sleep)
    notices = []
    posts = []

    def handle(request):
        posts.append(request.content)
        return httpx.Response(429, text="rate limit https://queue.example/wait")

    install_transport(handle)
    gateway = Gateway(settings)
    gateway._submission_busy_notice = lambda: notices.append("视频平台繁忙，正在自动重新提交")
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(gateway._submit("/v1/videos/generations", {"prompt": "frozen"}))
    assert len(posts) == 4 and len(set(posts)) == 1
    assert slept == [20, 60, 120] and notices == ["视频平台繁忙，正在自动重新提交"] * 3
    assert exc.value.uncertain is False and str(exc.value) == SYSTEM_CARD
    assert exc.value.error_code == "provider_request_rejected"
    assert sorted(item["attempt"] for item in _diagnostics(tmp_path)) == [1, 2, 3, 4]


def test_image_submit_shares_the_unpaid_retry_and_balance_stop(settings, install_transport, source, plan, tmp_path,
                                                               prepared_original, monkeypatch):
    settings.submission_retry_delays = (0,)
    settings.data_root = tmp_path
    folder = tmp_path / "designs"
    posts = []

    def handle(request):
        if request.method != "POST":
            return httpx.Response(200, json={"data": {"status": "completed", "result": {"images": [
                {"url": "https://cdn.example/scene.png"}]}}})
        posts.append(request.content)
        if len(posts) == 1:
            return httpx.Response(422, text="queue overloaded https://cdn.example/img " + SECRET)
        return httpx.Response(200, json={"data": {"id": "image-task"}})

    install_transport(handle)

    async def download(url, path):
        Image.new("RGB", (8, 8), "blue").save(path, "PNG")

    gateway = Gateway(settings)
    monkeypatch.setattr(gateway, "download_video", download)
    notices = []
    gateway._submission_busy_notice = lambda: notices.append("图片平台繁忙，正在自动重新提交")
    result = asyncio.run(gateway.create_designs({}, [source], plan, folder))
    assert result[0]["metadata"]["task_id"] == "image-task"
    assert len(posts) == 2 and posts[0] == posts[1] and notices == ["图片平台繁忙，正在自动重新提交"]
    receipt = json.loads((folder / "scene_task.json").read_text())
    assert receipt["submission_refusals"][0]["status"] == 422
    assert SECRET not in json.dumps(receipt["submission_refusals"]) and "https://" not in receipt["submission_refusals"][0]["excerpt"]

    balance_posts = []

    def balance(request):
        balance_posts.append(1)
        return httpx.Response(402, text="quota https://pay.example " + SECRET)

    install_transport(balance)
    balance_gateway = Gateway(settings)
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(balance_gateway.create_designs({}, [source], plan, tmp_path / "balance"))
    assert exc.value.error_code == "provider_balance" and str(exc.value) == BALANCE_CARD
    with pytest.raises(ProviderResultError) as again:
        asyncio.run(balance_gateway.create_designs({}, [source], plan, tmp_path / "balance"))
    assert again.value.error_code == "image_generation_failed"
    assert len(balance_posts) == 1
    assert isinstance(exc.value, ProviderError) and SECRET not in str(exc.value)
