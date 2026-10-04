"""Offline protocol/security tests. No real keys or paid provider calls."""
import asyncio
import copy
import io
import json
import re
import socket
import traceback
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from app.providers import gateway as module
from app.providers import ProviderError
from app.providers.gateway import Gateway, SubmissionError, atomic_json, public_url
from app.media import MediaCanceled
from app.schemas import Plan
from ad_script_fixtures import script_stages, stage_outputs, assert_script_saved, compile_script_plan, alignment_review


SECRET = "TEST_ONLY_SECRET_DO_NOT_EXPOSE"


@pytest.fixture
def settings():
    return SimpleNamespace(tudou_api_key=SECRET, deepseek_api_key=SECRET,
                           planner_model="deepseek-flash", image_model="gpt-image-2-all", video_model="wan3.0",
                           poll_seconds=0, provider_timeout_seconds=2, tts_voice="Tingting", ffmpeg_path="not-called",
                           submission_retry_delays=())


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.png"
    Image.new("RGB", (8, 8), "blue").save(path)
    return {"id": "original-product", "kind": "product", "name": "用户图", "path": str(path), "mime": "image/png"}


@pytest.fixture
def plan(source):
    return Plan.model_validate({"title": "蓝瓶商品广告", "script": "看看商品细节。了解商品详情。", "language": "zh-CN",
            "aspect_ratio": "9:16", "audio_strategy": "native", "facts": {"name": "蓝瓶", "claims": []},
            "segments": [{"id": f"s{i}", "duration_seconds": 10, "purpose": "展示", "prompt": "蓝瓶与相同台面的连续展示",
                          "narration": ["看看商品细节。", "了解商品详情。"][i],
                          "shots": [{"start": 0, "end": 4, "description": "商品全景", "narration": ["看看商品细节。", "了解商品详情。"][i],
                                     "purpose": "建立商品认知", "evidence_ids": ["fact_blue"]},
                                    {"start": 4, "end": 10, "description": "细节特写", "narration": "",
                                     "purpose": "看清商品外观", "evidence_ids": ["fact_blue"]}],
                          "reference_asset_ids": [source["id"]]} for i in range(2)]}).model_dump()


@pytest.fixture
def strategy_stage():
    return {
        "kind": "plan", "message": "已整理外观方向，请核对镜头计划。", "questions": [],
        "brief": {"product_name": "蓝瓶", "category": "general", "audience": "关注商品外观的人",
                  "scenario": "商品展示", "facts": [{"id": "fact_blue", "text": "蓝瓶",
                       "source_kind": "merchant", "source_id": "brief"}],
                  "unknowns": ["功能与材质未确认"], "assumptions": []},
        "directions": [{"id": "appearance", "rule_id": "appearance_reveal", "label": "外观揭示",
                        "audience": "关注商品外观的人", "scenario": "商品展示", "angle": "看清蓝瓶的外观",
                        "hook": "先展示商品整体轮廓", "development": "由整体进入局部细节",
                        "payoff": "回到完整商品", "evidence_ids": ["fact_blue"],
                        "reason": "现有资料支持外观展示", "production_notes": ["不演示未确认功能"]}],
        "selected_direction_id": "appearance",
    }


def chat_output(value):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]})


def request_context(request):
    payload = json.loads(request.content)
    content = payload["messages"][1]["content"]
    return payload, json.loads(content[0]["text"] if isinstance(content, list) else content)


def assert_strategy_wire_schema(schema):
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) == {"kind", "message"}
    assert {"kind", "message", "questions", "brief", "directions", "selected_direction_id"} <= set(schema["properties"])
    assert schema["properties"]["kind"]["type"] == "string"
    assert set(schema["properties"]["kind"]["enum"]) <= {"reply", "clarify", "plan", "directions"}
    assert schema["properties"]["questions"]["items"] == {"$ref": "#/$defs/Question"}
    assert "title" not in schema  # Display annotations are not the wire contract.


def assert_legacy_plan_wire_schema(schema):
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"title", "segments", "audio_strategy"}
    assert schema["properties"]["title"]["type"] == "string"
    assert schema["properties"]["segments"]["items"] == {"$ref": "#/$defs/Segment"}
    assert schema["properties"]["audio_strategy"] == {"type": "string", "const": "native"}
    assert {"kind", "facts", "script", "production", "strategy"}.isdisjoint(schema["properties"])
    assert "speech_spans" in schema["$defs"]["Segment"]["required"]
    assert "title" not in schema  # The actual title property above remains intact.


def assert_skill_wire_matches_source(wire, loaded):
    """Audit real source bytes on the server while keeping provenance off wire."""
    from hashlib import sha256
    from app.runtime_skills import SKILLS_ROOT

    assert wire["stage"] == loaded["stage"]
    assert set(wire) == {"stage", "skills", "shared_references"}
    for collection in ("skills", "shared_references"):
        for sent, original in zip(wire[collection], loaded[collection], strict=True):
            assert sent == {key: original[key] for key in ("id", "content")}
            assert sha256(sent["content"].encode()).hexdigest() == original["content_sha256"]
            assert original["version"]
            if collection == "shared_references":
                path = SKILLS_ROOT / original["path"]
                assert path.is_file() and path.read_text() == sent["content"]
                assert sha256(path.read_bytes()).hexdigest() == original["content_sha256"]
            else:
                documents = {}
                for resource in original["resources"]:
                    path = SKILLS_ROOT / original["id"] / resource["path"]
                    assert path.is_file()
                    assert sha256(path.read_bytes()).hexdigest() == resource["sha256"]
                    documents[resource["path"]] = path.read_text()
                source = "\n\n".join(f"<!-- {path} -->\n{content}" for path, content in documents.items())
                assert sha256(source.encode()).hexdigest() == original["source_content_sha256"]
                assert sent["content"] == "\n\n".join(
                    f"<!-- {path} -->\n{documents[path]}" for path in original["loaded_sections"])
                assert original["consumed_resources"] == [
                    resource for path in original["loaded_sections"]
                    for resource in original["resources"] if resource["path"] == path]


def expected_facts(strategy_stage):
    brief = strategy_stage["brief"]
    return {"name": brief["product_name"], "claims": [fact["text"] for fact in brief["facts"]]}


def assert_compiled_plan(result, original, strategy_stage):
    """Compiler-owned timing and speech must match the reserved reviewed content."""
    expected = script_stages(strategy_stage, original)[2]
    expected["facts"] = expected_facts(strategy_stage)
    actual = copy.deepcopy(result)
    strategy = actual.pop("strategy")
    expected.pop("strategy", None)
    actual.pop("ad_script", None)
    actual.pop("script_review", None)
    expected.pop("ad_script", None)
    expected.pop("script_review", None)
    assert actual == expected
    assert strategy["brief"] == strategy_stage["brief"]
    assert strategy["directions"] == strategy_stage["directions"]
    assert strategy["selected_direction_id"] == strategy_stage["selected_direction_id"]


@pytest.fixture
def install_transport(monkeypatch):
    real_client = httpx.AsyncClient
    def install(handler):
        transport = httpx.MockTransport(handler)
        def client(*args, **kwargs):
            assert kwargs.get("trust_env") is False
            return real_client(*args, transport=transport, **kwargs)
        monkeypatch.setattr(module.httpx, "AsyncClient", client)
    return install


@pytest.fixture
def public_dns(monkeypatch):
    def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)


@pytest.fixture
def prepared_original(monkeypatch):
    """Isolate video protocol tests; reference byte checks are tested separately."""
    async def prepared(self, asset):
        assert asset["kind"] == "product" and not asset.get("metadata", {}).get("generated")
        return "https://canva.ai-tudou.net/library/verified-original.png"
    monkeypatch.setattr(Gateway, "_original_reference_url", prepared)
    async def scene_reference(self, asset):
        return asset["metadata"]["provider_url"]
    monkeypatch.setattr(Gateway, "_scene_reference_url", scene_reference)


def assert_redacted(exc):
    assert isinstance(exc, ProviderError)
    assert exc.safe_to_display is True
    assert SECRET not in str(exc)
    assert SECRET not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("body", [{"data": {"id": "task_1"}}, {"data": {"task_id": "task_1"}}, {"id": "task_1"}])
def test_official_task_receipt_variants(settings, install_transport, body):
    install_transport(lambda request: httpx.Response(200, json=body))
    assert asyncio.run(Gateway(settings)._submit("/v1/videos/generations", {})) == "task_1"


@pytest.mark.parametrize("status,uncertain", [(401, False), (403, False), (429, False), (500, True), (302, True)])
def test_submission_errors_never_expose_provider_body(settings, install_transport, status, uncertain):
    install_transport(lambda request: httpx.Response(status, text=SECRET))
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(Gateway(settings)._submit("/v1/videos/generations", {}))
    assert exc.value.uncertain is uncertain
    assert_redacted(exc.value)


@pytest.mark.parametrize("exception,uncertain", [(httpx.ConnectError, False), (httpx.ReadTimeout, True), (httpx.WriteError, True)])
def test_transport_failure_is_classified_and_exception_chain_redacted(settings, install_transport, exception, uncertain):
    def handle(request):
        raise exception(SECRET, request=request)
    install_transport(handle)
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(Gateway(settings)._submit("/v1/videos/generations", {}))
    assert exc.value.uncertain is uncertain
    assert_redacted(exc.value)


def test_visual_json_plan_uses_stage_specific_reasoning(settings, install_transport, source, plan, strategy_stage):
    outputs = stage_outputs(strategy_stage, plan)
    stages = []
    def handle(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        data, context = request_context(request)
        stages.append(context["stage"])
        if context["stage"] in ("ad_script", "script_review"):
            assert data["thinking"] == {"type": "enabled"}
            assert data["reasoning_effort"] == "high" and data["max_tokens"] == 24000
        elif context["stage"] in ("strategy", "alignment_review"):
            assert data["thinking"] == {"type": "enabled"}
            expected_budget = 18000 if context["stage"] == "strategy" else 32768
            assert data["reasoning_effort"] == "low" and data["max_tokens"] == expected_budget
        else:
            assert data["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in data
        assert data["response_format"] == {"type": "json_object"}
        assert "JSON" in data["messages"][0]["content"]
        if context["stage"] == "strategy":
            assert_strategy_wire_schema(context["schema"])
            assert data["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        elif context["stage"] == "storyboard":
            assert_legacy_plan_wire_schema(context["schema"])
            assert "cta" not in context["schema"]["properties"]
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    result = asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert stages == ["strategy", "ad_script", "script_review", "storyboard", "alignment_review"]
    assert_compiled_plan(result, plan, strategy_stage)
    assert_script_saved(result, outputs["ad_script"], outputs["script_review"])


@pytest.mark.parametrize("failed_stage", ["strategy", "ad_script", "script_review", "storyboard", "alignment_review"])
@pytest.mark.parametrize("body", [SECRET, {"choices": [{"finish_reason": "length", "message": {"content": SECRET}}]},
                                    {"choices": [{"finish_reason": "stop", "message": {"content": '{"title":"' + SECRET + '"}'}}]},
                                    {"choices": None}])
def test_bad_plan_response_is_redacted(settings, install_transport, source, plan, body, failed_stage, strategy_stage):
    outputs = stage_outputs(strategy_stage, plan)
    stages = []
    def handle(request):
        _, context = request_context(request)
        stages.append(context["stage"])
        if context["stage"] == failed_stage:
            return httpx.Response(200, json=body)
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert_redacted(exc.value)
    order = list(outputs)
    assert stages == order[:order.index(failed_stage) + 1]


@pytest.mark.parametrize("kind", ["reply", "clarify"])
def test_strategy_reply_and_clarification_use_one_request_without_storyboard(settings, install_transport, kind):
    questions = [{"id": "audience", "prompt": "主要给谁看？",
                  "options": [{"id": "gift", "label": "送礼的人"}, {"id": "self", "label": "自己使用的人"}]}] if kind == "clarify" else []
    stage = {"kind": kind, "message": "先聊聊想推广什么商品。", "questions": questions}
    calls = []
    def handle(request):
        calls.append(request)
        payload, context = request_context(request)
        assert len(calls) == 1 and context["stage"] == "strategy"
        assert_strategy_wire_schema(context["schema"])
        assert payload["thinking"] == {"type": "enabled"}
        assert payload["reasoning_effort"] == "low" and payload["max_tokens"] == 18000
        assert payload["response_format"] == {"type": "json_object"}
        return chat_output(stage)
    install_transport(handle)
    result = asyncio.run(Gateway(settings).decide({"brief": "你好"}, [], [], [], None))
    assert result["kind"] == kind and result["message"] == stage["message"]
    assert len(result["questions"]) == len(questions) and result["plan"] is None
    assert result["creative_options"] == [] and len(calls) == 1


@pytest.mark.parametrize("invalid", ["invented_quote", "unknown_merchant", "assistant_source", "unknown_image"])
def test_strategy_facts_require_real_sources_before_compilation(settings, install_transport, source, strategy_stage, invalid):
    stage = copy.deepcopy(strategy_stage)
    fact = stage["brief"]["facts"][0]
    if invalid == "invented_quote":
        fact["text"] = "低温护发"
    elif invalid == "unknown_merchant":
        fact["source_id"] = "missing-message"
    elif invalid == "assistant_source":
        fact["source_id"] = "assistant-message"
    else:
        fact.update(source_kind="image", source_id="invented-asset")
    calls = []
    def handle(request):
        calls.append(request)
        assert len(calls) == 1, "Invalid provenance must not reach storyboard compilation"
        return chat_output(stage)
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).decide({"brief": "蓝瓶商品"}, [source],
                    [{"id": "assistant-message", "role": "assistant", "content": "蓝瓶"}], [], None))
    assert_redacted(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize("with_id", [True, False])
def test_stage_merchant_message_and_original_image_sources_are_retained(settings, install_transport, source, plan, strategy_stage, with_id):
    stage = copy.deepcopy(strategy_stage)
    message = {"role": "user", "content": "瓶身呈蓝色，请展示外观。"}
    if with_id:
        message["id"] = "merchant-note"
    stage["brief"]["facts"] = [
        {"id": "fact_blue", "text": "瓶身呈蓝色", "source_kind": "merchant", "source_id": "merchant-note" if with_id else "message_1"},
        {"id": "fact_outline", "text": "画面呈蓝色", "source_kind": "image", "source_id": source["id"]}]
    stage["directions"][0]["evidence_ids"] = ["fact_blue", "fact_outline"]
    compiled = copy.deepcopy(plan)
    compiled["segments"][0]["shots"][1]["evidence_ids"] = ["fact_outline"]
    outputs = stage_outputs(stage, compiled)
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        if context["stage"] == "strategy":
            assert context["evidence_sources"]["brief"] == "蓝瓶商品"
            assert context["evidence_sources"]["merchant-note" if with_id else "message_1"] == message["content"]
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    result = asyncio.run(Gateway(settings).decide({"brief": "蓝瓶商品"}, [source], [message], [], None))
    assert_compiled_plan(result["plan"], compiled, stage)
    assert result["creative_options"] == [] and len(calls) == 5


def test_compiler_cannot_replace_server_owned_facts_or_strategy(settings, install_transport, source, plan, strategy_stage):
    outputs = stage_outputs(strategy_stage, plan)
    outputs["storyboard"].update(facts={"name": SECRET, "claims": ["模型编造的商品功效"]},
                                 strategy={"selected_direction_id": "model_override", "private": SECRET},
                                 ad_script={"private": SECRET}, script_review={"private": SECRET})
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        if context["stage"] == "storyboard":
            assert not ({"facts", "strategy", "ad_script", "script_review"} & context["schema"]["properties"].keys())
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    decision = asyncio.run(Gateway(settings).decide({"brief": "蓝瓶商品"}, [source], [], [], None))
    assert_compiled_plan(decision["plan"], plan, strategy_stage)
    assert_script_saved(decision["plan"], outputs["ad_script"], outputs["script_review"])
    assert SECRET not in json.dumps(decision) and len(calls) == 5


@pytest.mark.parametrize("invalid", ["empty_purpose", "unknown_evidence", "uncovered_evidence"])
def test_compiled_shots_require_purpose_and_selected_evidence(settings, install_transport, source, plan, strategy_stage, invalid):
    outputs = stage_outputs(strategy_stage, plan)
    broken = outputs["storyboard"]
    if invalid == "empty_purpose":
        broken["segments"][0]["shots"][0]["purpose"] = ""
    elif invalid == "unknown_evidence":
        broken["segments"][0]["shots"][0]["evidence_ids"] = ["invented-fact"]
    else:
        for segment in broken["segments"]:
            for shot in segment["shots"]:
                shot["evidence_ids"] = []
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        assert len(calls) <= 4, "Invalid evidence cannot be repaired by changing the approved script"
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert_redacted(exc.value)
    assert len(calls) == 4


@pytest.mark.parametrize("body", [SECRET, [SECRET], {"data": SECRET}])
def test_bad_task_response_is_redacted(settings, install_transport, body):
    install_transport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(Gateway(settings)._task("task_known"))
    assert_redacted(exc.value)


def test_image_unknown_submission_never_posts_again(settings, install_transport, source, plan, tmp_path, prepared_original):
    calls = []
    def handle(request):
        calls.append(request)
        data = json.loads(request.content)
        assert request.url.path == "/v1/images/generations/async"
        assert data["model"] == "gpt-image-2-all" and data["quality"] == "medium" and data["resolution"] == "1k"
        assert data["images"][0].startswith("data:image/png;base64,")
        raise httpx.ReadTimeout(SECRET, request=request)
    install_transport(handle)
    gateway = Gateway(settings)
    folder = tmp_path / "designs"
    for _ in range(2):
        with pytest.raises(SubmissionError) as exc:
            asyncio.run(gateway.create_designs({}, [source], plan, folder))
        assert exc.value.uncertain
        assert_redacted(exc.value)
    assert len(calls) == 1
    assert json.loads((folder / "scene_task.json").read_text())["state"] == "submitted_unknown"
    assert not (folder / "product_task.json").exists()


@pytest.mark.parametrize("receipt", ["{broken", "{}", '{"state":"submitted"}'])
def test_damaged_image_receipt_does_not_post(settings, install_transport, source, plan, tmp_path, receipt, prepared_original):
    install_transport(lambda request: pytest.fail("Existing unknown receipt must never cause a new request"))
    (tmp_path / "scene_task.json").write_text(receipt)
    with pytest.raises(SubmissionError) as exc:
        asyncio.run(Gateway(settings).create_designs({}, [source], plan, tmp_path))
    assert exc.value.uncertain


def test_image_receipts_resume_poll_and_download_without_recreating(settings, install_transport, public_dns, source, plan, tmp_path, prepared_original):
    folder = tmp_path / "designs"
    folder.mkdir()
    atomic_json(folder / "product_task.json", {"state": "submitted", "task_id": "task_product"})
    legacy_receipt = (folder / "product_task.json").read_bytes()
    atomic_json(folder / "scene_task.json", {"state": "submitted", "task_id": "task_scene"})
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG")
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.host == "api.ai-tudou.net":
            assert request.url.path == "/v1/tasks/task_scene"
            return httpx.Response(200, json={"data": {"status": "completed", "result": {"images": [{"url": ["https://cdn.example/scene.png"], "expires_at": 2000000000}]}}})
        assert request.url.host == "93.184.216.34"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=buffer.getvalue())
    install_transport(handle)
    gateway = Gateway(settings)
    result = asyncio.run(gateway.create_designs({}, [source], plan, folder))
    assert len(calls) == 2 and len(result) == 1 and result[0]["kind"] == "scene"
    assert result[0]["metadata"]["task_id"] == "task_scene"
    assert result[0]["metadata"]["expires_at"] == 2000000000
    assert result[0]["metadata"]["source_asset_ids"] == [source["id"]]
    assert "design_request" not in result[0]["metadata"], "Legacy image receipts must not claim today's scene requirements"
    assert (folder / "scene.png").is_file() and not (folder / "product.png").exists()
    assert (folder / "product_task.json").read_bytes() == legacy_receipt
    asyncio.run(gateway.create_designs({}, [source], plan, folder))
    assert len(calls) == 2


def test_cancel_after_image_poll_preserves_receipt_and_stops_next_post(settings, install_transport, source, plan, tmp_path, prepared_original):
    calls = []
    canceled = False
    def handle(request):
        nonlocal canceled
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"id": "task_scene"}})
        canceled = True
        return httpx.Response(200, json={"data": {"status": "processing"}})
    install_transport(handle)
    with pytest.raises(MediaCanceled):
        asyncio.run(Gateway(settings).create_designs({}, [source], plan, tmp_path, cancelled=lambda: canceled))
    receipt = json.loads((tmp_path / "scene_task.json").read_text())
    assert receipt["task_id"] == "task_scene" and receipt["state"] == "submitted"
    assert [r.method for r in calls] == ["POST", "GET"]


def test_cancel_before_image_post_has_no_receipt(settings, install_transport, source, plan, tmp_path):
    install_transport(lambda request: pytest.fail("Canceled design must not submit"))
    with pytest.raises(MediaCanceled):
        asyncio.run(Gateway(settings).create_designs({}, [source], plan, tmp_path, cancelled=lambda: True))
    assert not (tmp_path / "scene_task.json").exists()


def test_original_upload_scene_only_and_video_reference_preserve_product_bytes(settings, install_transport, public_dns, source, plan, tmp_path):
    import base64
    import hashlib
    original = Path(source["path"]).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    uploaded_url = f"https://canva.ai-tudou.net/library/users/test/materials/product-{digest}.png"
    scene_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(scene_bytes, "PNG")
    posts, gets = [], []
    def handle(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            posts.append((str(request.url), payload))
            if request.url.host == "canva.ai-tudou.net":
                assert request.url.path == "/api/materials/upload"
                assert request.headers["authorization"] == "Bearer " + SECRET
                assert base64.b64decode(payload["data"].split(",", 1)[1]) == original
                assert payload["filename"] == f"product-{digest}.png"
                return httpx.Response(200, json={"success": True, "url": uploaded_url.replace("https://canva.ai-tudou.net", "")})
            assert request.url.host == "api.ai-tudou.net"
            if request.url.path == "/v1/images/generations/async":
                assert "空场景" in payload["prompt"] and "不出现商品" in payload["prompt"]
                assert base64.b64decode(payload["images"][0].split(",", 1)[1]) == original
                return httpx.Response(200, json={"data": {"id": "scene_only"}})
            assert request.url.path == "/v1/videos/generations"
            assert payload["images"] == [uploaded_url, "https://cdn.example/scene.png"]
            assert "用户上传的商品原图" in payload["prompt"]
            return httpx.Response(200, json={"data": {"id": "native_video"}})
        gets.append(str(request.url))
        if request.url.host == "api.ai-tudou.net":
            assert request.url.path == "/v1/tasks/scene_only"
            return httpx.Response(200, json={"data": {"status": "completed", "result": {"images": [{"url": ["https://cdn.example/scene.png"]}]}}})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=original if request.headers["host"] == "canva.ai-tudou.net" else scene_bytes.getvalue())
    install_transport(handle)
    gateway = Gateway(settings)
    folder = tmp_path / "designs"
    result = asyncio.run(gateway.create_designs({}, [source], plan, folder))
    assert len(result) == 1 and result[0]["kind"] == "scene" and Path(result[0]["source_path"]).is_file()
    assert not (folder / "product.png").exists() and not (folder / "product_task.json").exists()
    scene = {**result[0], "id": "scene"}
    before_posts, before_gets = len(posts), len(gets)
    asyncio.run(gateway.create_designs({}, [source], plan, folder))
    assert len(posts) == before_posts  # Resume never regenerates the scene.
    assert len(gets) == before_gets + 1  # Cached product URL is checked again.
    legacy_product = {"id": "old-product", "kind": "product", "metadata": {"generated": True, "provider_url": "https://cdn.example/redrawn.png"}}
    spec = {**plan["segments"][0], "audio_strategy": "native", "reference_asset_ids": [legacy_product["id"], source["id"], "scene"]}
    asyncio.run(gateway.submit_video(spec, [legacy_product, source, scene], "offline"))
    assert sum(url.endswith("/v1/images/generations/async") for url, _ in posts) == 1
    assert sum(url.endswith("/api/materials/upload") for url, _ in posts) == 1
    assert Path(source["path"]).read_bytes() == original
    receipt = json.loads((Path(source["path"]).parent / ".provider_refs" / f"{digest}.json").read_text())
    assert receipt["state"] == "verified" and receipt["sha256"] == digest and SECRET not in json.dumps(receipt)


@pytest.mark.parametrize("url", ["https://third-party.example/" + SECRET, "http://canva.ai-tudou.net/library/file.png", "/api/private/file.png"])
def test_original_upload_rejects_unapproved_or_invalid_urls(settings, install_transport, source, url):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.host == "canva.ai-tudou.net"
        return httpx.Response(200, json={"success": True, "url": url})
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings)._original_reference_url(source))
    assert_redacted(exc.value)
    assert len(calls) == 1


def test_original_upload_byte_mismatch_never_becomes_verified(settings, install_transport, public_dns, source):
    import hashlib
    digest = hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()
    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"success": True, "url": f"/library/product-{digest}.png"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"WRONG_IMAGE")
    install_transport(handle)
    with pytest.raises(ProviderError, match="字节不一致"):
        asyncio.run(Gateway(settings)._original_reference_url(source))
    receipt = json.loads((Path(source["path"]).parent / ".provider_refs" / f"{digest}.json").read_text())
    assert receipt["state"] != "verified" and "url" not in receipt


def test_wan_uses_two_ordered_elements_and_complete_shots(settings, install_transport, public_dns, plan, source, prepared_original):
    segment = {**plan["segments"][0], "reference_asset_ids": ["scene", "product"], "audio_strategy": "native"}
    assets = [{**source, "id": "product"}, {"id": "scene", "kind": "scene", "metadata": {"provider_url": "https://cdn.example/scene.png"}}]
    def handle(request):
        data = json.loads(request.content)
        assert request.url.path == "/v1/videos/generations"
        assert data["images"] == ["https://canva.ai-tudou.net/library/verified-original.png", "https://cdn.example/scene.png"]
        assert data["duration"] == 10 and data["aspect_ratio"] == "9:16" and data["resolution"] == "720p"
        assert re.search(r'0(?:\.0)?–4(?:\.0)?秒：画面：商品全景；该时段唯一台词："看看商品细节。"', data["prompt"])
        assert re.search(r"4(?:\.0)?–10(?:\.0)?秒：画面：细节特写；该时段唯一台词：不说话", data["prompt"])
        assert "不要朗读秒数、镜头说明、运镜" in data["prompt"] and "不另行重复朗读" in data["prompt"]
        assert "自然亲切的成年女性普通话画外音" in data["prompt"] and "不得吞字、增改台词" in data["prompt"]
        assert "看看商品细节。" in data["prompt"] and "成片将使用统一中文画外音" not in data["prompt"]
        assert not {"generate_audio", "audio", "multi_shot"} & data.keys()
        return httpx.Response(200, json={"data": {"task_id": "task_video"}})
    install_transport(handle)
    result = asyncio.run(Gateway(settings).submit_video(segment, assets, "local-receipt-only"))
    assert result["task_id"] == "task_video"


@pytest.mark.parametrize("url", ["http://example.com/file", "https://user:pass@example.com/file", "https://example.com:8443/file", "https://example.com:bad/file"])
def test_download_rejects_unsafe_urls_without_network(url):
    with pytest.raises(ValueError):
        asyncio.run(public_url(url))


def test_download_rechecks_redirect_and_blocks_private_dns(settings, install_transport, monkeypatch, tmp_path):
    calls = []
    def resolve(host, port, **kwargs):
        ip = "127.0.0.1" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://internal.example/private"})
    install_transport(handle)
    with pytest.raises(ValueError):
        asyncio.run(Gateway(settings).download_video("https://cdn.example/file", tmp_path / "video.mp4"))
    assert len(calls) == 1
    assert not (tmp_path / "video.mp4").exists()


def test_download_pins_validated_ip_with_original_tls_and_no_auth(settings, install_transport, public_dns, tmp_path):
    def handle(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "cdn.example"
        assert request.extensions["sni_hostname"] == "cdn.example"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"TEST_MEDIA")
    install_transport(handle)
    destination = tmp_path / "video.mp4"
    asyncio.run(Gateway(settings).download_video("https://cdn.example/video.mp4?token=signed", destination))
    assert destination.read_bytes() == b"TEST_MEDIA"
    assert not destination.with_suffix(".mp4.part").exists()


def test_partial_download_cleanup_and_error_redaction(settings, install_transport, public_dns, tmp_path):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError(SECRET)
    install_transport(lambda request: httpx.Response(200, stream=BrokenStream()))
    path = tmp_path / "video.mp4"
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(Gateway(settings).download_video("https://cdn.example/file", path))
    assert_redacted(exc.value)
    assert not path.exists() and not path.with_suffix(".mp4.part").exists()


def test_only_adapter_authored_errors_are_safe_to_display():
    assert ProviderError("脱敏后的错误").safe_to_display is True
    assert SubmissionError("脱敏后的提交错误", uncertain=True).safe_to_display is True
    for raw in (RuntimeError(SECRET), httpx.ReadError(SECRET), ValueError(SECRET)):
        assert getattr(raw, "safe_to_display", False) is False


def test_planner_json_schema_matches_variable_duration_and_local_shot_timeline():
    from app.schemas import Plan
    schema = Plan.model_json_schema()
    assert "cta" not in schema["properties"] and "cta" not in schema["required"]
    segment = schema["$defs"]["Segment"]["properties"]
    assert segment["duration_seconds"]["minimum"] == 1 and segment["duration_seconds"]["maximum"] == 30
    assert segment["duration_seconds"]["type"] == "integer"
    assert "duration_seconds" in schema["$defs"]["Segment"]["required"]
    assert "局部0至duration_seconds秒" in segment["shots"]["description"]
    assert schema["properties"]["segments"]["minItems"] == 1 and schema["properties"]["segments"]["maxItems"] == 6
    assert schema["properties"]["script"]["maxLength"] == 360
    shot = schema["$defs"]["Shot"]["properties"]
    assert shot["start"]["minimum"] == 0 and shot["start"]["exclusiveMaximum"] == 30
    assert shot["end"]["exclusiveMinimum"] == 0 and shot["end"]["maximum"] == 30


def test_validated_plan_preserves_script_durations_and_reference_ids(settings, install_transport, source, plan, strategy_stage):
    before = copy.deepcopy(plan)
    outputs = stage_outputs(strategy_stage, plan)
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        if context["stage"] == "storyboard":
            schema = context["schema"]
            assert schema["$defs"]["Segment"]["properties"]["duration_seconds"]["maximum"] == 10
            assert "strategy" not in schema["required"] and "facts" not in schema["required"]
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    result = asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert_compiled_plan(result, before, strategy_stage)
    assert plan == before and len(calls) == 5


def test_legacy_plan_cta_is_ignored_without_changing_natural_voiceover(plan):
    from app.schemas import Plan
    legacy = {**plan, "cta": "TEST_RETIRED_TAIL_TEXT"}
    result = Plan.model_validate(legacy).model_dump()
    assert "cta" not in result and result == plan
    assert result["script"] == "看看商品细节。了解商品详情。"
    assert legacy["cta"] == "TEST_RETIRED_TAIL_TEXT"


def test_script_is_reviewed_before_shots_and_only_approved_copy_is_compiled(settings, install_transport, source, plan, strategy_stage):
    stage = copy.deepcopy(strategy_stage)
    stage["brief"].update(audience="重视桌面配色的人", scenario="办公桌陈列")
    stage["brief"]["facts"].append({"id": "fact_unused", "text": "包装为纸盒", "source_kind": "merchant", "source_id": "brief"})
    unselected = {**copy.deepcopy(stage["directions"][0]), "id": "packaging", "rule_id": "everyday_context", "label": "未选择的包装方向",
                  "angle": "未选择方向不应参与审稿", "development": "打开纸盒摆在书桌上", "payoff": "纸盒与蓝瓶一起停稳", "evidence_ids": ["fact_unused"]}
    stage["directions"].append(unselected)
    outputs = stage_outputs(stage, plan)
    words = ["给桌面一点蓝。", "", "一只蓝瓶，自成风景。", ""]
    for beat, word in zip(outputs["script_review"]["script"]["beats"], words):
        beat["narration"] = word
    outputs["storyboard"] = compile_script_plan(outputs["script_review"]["script"], outputs["storyboard"])
    outputs["alignment_review"] = alignment_review(outputs["storyboard"])
    calls, progress = [], []
    def handle(request):
        payload, context = request_context(request)
        calls.append(context["stage"])
        if context["stage"] in ("ad_script", "script_review"):
            assert "未选择" not in json.dumps(context, ensure_ascii=False)
            assert "shot_slots" not in context, "Ad expression must be reviewed before slots exist"
        if context["stage"] == "storyboard":
            assert outputs["script_review"]["script"] == context["approved_ad_script"]
            assert progress[-1] == "storyboarding"
        elif context["stage"] == "alignment_review":
            assert progress[-1] == "checking_alignment"
            assert context["segments"][0]["shots"][0]["description"] == outputs["storyboard"]["segments"][0]["shots"][0]["description"]
            assert all("purpose" not in shot for segment in context["segments"] for shot in segment["shots"])
            assert all("visual_evidence" not in anchor for segment in context["segments"] for span in segment["speech_spans"] for anchor in span["anchors"])
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    result = asyncio.run(Gateway(settings).decide({"brief": "蓝瓶商品，包装为纸盒", "duration_seconds": 20},
                          [source], [], [], None, on_progress=progress.append))["plan"]
    assert result["script"] == "".join(words)
    assert_script_saved(result, outputs["script_review"]["script"], outputs["script_review"])
    assert calls == ["strategy", "ad_script", "script_review", "storyboard", "alignment_review"]


@pytest.mark.parametrize("failure", ["transport", "http", "invalid_json", "extra_fields", "overlong", "missing_check", "not_ready", "failed_check"])
def test_ad_script_review_failure_stops_before_storyboard_with_bounded_transport_retry(settings, install_transport, source, plan, strategy_stage, failure):
    before = copy.deepcopy(plan)
    outputs = stage_outputs(strategy_stage, plan)
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        assert context["stage"] != "storyboard"
        if context["stage"] != "script_review":
            return chat_output(outputs[context["stage"]])
        if failure == "transport":
            raise httpx.ReadTimeout(SECRET, request=request)
        if failure == "http":
            return httpx.Response(503, text=SECRET)
        if failure == "invalid_json":
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": SECRET}}]})
        review = copy.deepcopy(outputs["script_review"])
        if failure == "extra_fields": review["facts"] = {"name": SECRET}
        if failure == "overlong": review["script"]["beats"][0]["narration"] = SECRET * 10
        if failure == "missing_check": review["checks"].pop()
        if failure == "not_ready": review["ready"] = False
        if failure == "failed_check": review["checks"][0]["passed"] = False
        return chat_output(review)
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).decide({"brief": "蓝瓶商品"}, [source], [], [], None))
    assert_redacted(exc.value)
    assert plan == before and calls == ["strategy", "ad_script"] + ["script_review"] * (3 if failure in ("transport", "http") else 1)


@pytest.mark.parametrize("failure", ["expression_rejected", "span_rejected", "missing_span", "unknown_span", "inconsistent_verdict"])
def test_final_alignment_review_blocks_unapproved_plan_without_retry(settings, install_transport, source, plan, strategy_stage, failure):
    outputs = stage_outputs(strategy_stage, plan)
    review = outputs["alignment_review"]
    if failure == "expression_rejected":
        review.update(ready=False, ad_expression_passed=False)
    elif failure == "span_rejected":
        review["ready"] = False
        review["spans"][0]["passed"] = False
    elif failure == "missing_span":
        review["spans"].pop()
    elif failure == "unknown_span":
        review["spans"][0]["span_id"] = "unrelated-span"
    else:
        review["ready"] = False
    review["summary"] = SECRET
    review["spans"][0]["note"] = SECRET
    before, calls = copy.deepcopy(plan), []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        assert len(calls) <= 5, "Rejected final alignment must never start a repair loop or media generation"
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert_redacted(exc.value)
    assert calls == ["strategy", "ad_script", "script_review", "storyboard", "alignment_review"]
    assert plan == before


@pytest.mark.parametrize("method", ["plan", "decide"])
def test_draft_copy_can_be_completed_by_single_review_before_storyboard(settings, install_transport, source, plan, method, strategy_stage):
    outputs = stage_outputs(strategy_stage, plan)
    outputs["ad_script"]["beats"][0]["narration"] = "这只蓝瓶，"
    before = copy.deepcopy(outputs)
    calls = []
    def handle(request):
        payload, context = request_context(request)
        calls.append(context["stage"])
        assert request.url.host == "api.deepseek.com" and len(calls) <= 5
        if context["stage"] == "script_review":
            assert context["draft_script"] == outputs["ad_script"]
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    gateway = Gateway(settings)
    if method == "plan":
        result = asyncio.run(gateway.plan({"brief": "蓝瓶商品"}, [source], []))
    else:
        result = asyncio.run(gateway.decide({"brief": "蓝瓶商品"}, [source], [], [], None))["plan"]
    assert_compiled_plan(result, plan, strategy_stage)
    assert_script_saved(result, outputs["script_review"]["script"], outputs["script_review"])
    assert outputs == before and len(calls) == 5


@pytest.mark.parametrize("invalid", ["unknown_evidence", "wrong_order", "different_words", "missing_beat", "split_sentence"])
def test_storyboard_cannot_change_reviewed_script(settings, install_transport, source, plan, strategy_stage, invalid):
    outputs = stage_outputs(strategy_stage, plan)
    compiled = outputs["storyboard"]
    first, second = compiled["segments"][0]["shots"]
    if invalid == "unknown_evidence": first["evidence_ids"] = ["invented"]
    elif invalid == "wrong_order": first["beat_id"], second["beat_id"] = second["beat_id"], first["beat_id"]
    elif invalid == "different_words":
        span = compiled["segments"][0]["speech_spans"][0]
        span["text"] = span["anchors"][0]["text"] = "全新卖点。"
    elif invalid == "missing_beat": second["beat_id"] = first["beat_id"]
    else:
        span = compiled["segments"][0]["speech_spans"][0]
        span["text"] = span["anchors"][0]["text"] = "看看商品"
        split = copy.deepcopy(span)
        split.update(id="split-half", text="细节。", beat_id=second["beat_id"], shot_ids=[second["id"]])
        split["anchors"][0].update(text="细节。", shot_ids=[second["id"]])
        compiled["segments"][0]["speech_spans"].append(split)
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        assert len(calls) <= 4, "Do not rewrite approved content to repair compiler mistakes"
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert_redacted(exc.value)
    assert len(calls) == 4


@pytest.mark.parametrize("invalid", ["missing_spans", "shifted_speech", "shifted_group", "legacy_shot_words"])
def test_director_cannot_replace_reserved_groups_or_speech_windows(settings, install_transport, source, plan, strategy_stage, invalid):
    outputs = stage_outputs(strategy_stage, plan)
    segment = outputs["storyboard"]["segments"][0]
    if invalid == "missing_spans":
        segment.pop("speech_spans")
    elif invalid == "shifted_speech":
        span = segment["speech_spans"][0]
        span["start"] += .1
        span["end"] += .1
        span["anchors"][0]["start"] += .1
        span["anchors"][0]["end"] += .1
    elif invalid == "shifted_group":
        segment["shots"][0]["end"] += .2
        segment["shots"][1]["start"] += .2
    else:
        segment["shots"][0]["narration"] = segment["narration"]
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        assert context["stage"] != "alignment_review", "Structurally invalid schedules must fail before further model calls"
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(ProviderError):
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert calls == ["strategy", "ad_script", "script_review", "storyboard"]


@pytest.mark.parametrize("duration,durations", [(10, [10]), (15, [10, 5]), (60, [10] * 6)])
def test_planner_uses_selected_total_and_exact_segments(settings, install_transport, source, plan, duration, durations, strategy_stage):
    result_plan = copy.deepcopy(plan)
    result_plan["segments"] = [{**copy.deepcopy(plan["segments"][0]), "id": f"s{i}", "duration_seconds": seconds,
        "shots": [{"start": 0, "end": seconds, "description": "完整展示", "narration": plan["segments"][0]["narration"],
                   "purpose": "看清商品外观", "evidence_ids": ["fact_blue"]}]} for i, seconds in enumerate(durations)]
    result_plan["script"] = "".join(s["narration"] for s in result_plan["segments"])
    outputs = stage_outputs(strategy_stage, result_plan)
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        if context["stage"] != "alignment_review":
            assert context["duration_seconds"] == duration
        if context["stage"] in ("strategy", "storyboard"):
            assert context["segment_durations"] == durations
        if context["stage"] == "storyboard":
            assert "script" not in context["schema"]["properties"]
            assert "narration" not in context["schema"]["$defs"]["Segment"]["properties"]
            assert context["schema"]["properties"]["audio_strategy"]["const"] == "native"
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    result = asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品", "duration_seconds": duration}, [source], []))
    assert_compiled_plan(result, result_plan, strategy_stage)
    assert len(calls) == 5


def test_planner_rejects_valid_plan_for_wrong_selected_duration(settings, install_transport, source, plan, strategy_stage):
    outputs = stage_outputs(strategy_stage, plan)
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(ProviderError, match="所选视频时长不一致"):
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品", "duration_seconds": 15}, [source], []))
    assert len(calls) == 4 and [s["duration_seconds"] for s in plan["segments"]] == [10, 10]


@pytest.mark.parametrize("durations", [[5, 10], [10, 5, 10], [9], [10] * 7])
def test_plan_rejects_noncanonical_or_out_of_range_segment_lengths(plan, durations):
    import copy
    from pydantic import ValidationError
    from app.schemas import Plan
    value = copy.deepcopy(plan)
    value["segments"] = [{**copy.deepcopy(plan["segments"][0]), "id": f"s{i}", "duration_seconds": seconds,
        "shots": [{"start": 0, "end": seconds, "description": "完整展示", "narration": plan["segments"][0]["narration"]}]} for i, seconds in enumerate(durations)]
    with pytest.raises(ValidationError):
        Plan.model_validate(value)


def test_short_tail_submits_supported_ten_seconds_with_hold_instruction(settings, install_transport, monkeypatch, plan, source, prepared_original):
    from app.providers import gateway as module
    async def public(url):
        return "93.184.216.34"
    monkeypatch.setattr(module, "public_url", public)
    segment = {**plan["segments"][0], "duration_seconds": 5,
        "shots": [{"start": 0, "end": 5, "description": "完整展示后停稳"}], "reference_asset_ids": ["product", "scene"]}
    assets = [{**source, "id": "product"}, {"id": "scene", "kind": "scene", "metadata": {"provider_url": "https://cdn.example/scene.png"}}]
    calls = []
    def handle(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["duration"] == 10
        assert "前5秒完成本段全部主体动作与内容" in payload["prompt"]
        assert "第5–10秒保持结尾画面" in payload["prompt"] and "合成只使用前5秒" in payload["prompt"]
        assert "不生成屏幕文字、字幕、标价、CTA" in payload["prompt"]
        return httpx.Response(200, json={"data": {"id": "short_tail"}})
    install_transport(handle)
    assert asyncio.run(Gateway(settings).submit_video(segment, assets, "offline"))["task_id"] == "short_tail"
    assert len(calls) == 1 and segment["duration_seconds"] == 5


def test_schema_failure_has_safe_public_message_without_rewriting(settings, install_transport, source, plan, strategy_stage):
    outputs = stage_outputs(strategy_stage, plan)
    broken = outputs["storyboard"]
    broken["title"] = SECRET * 30
    broken["segments"][0]["duration_seconds"] = 31
    broken["segments"][0]["shots"][0]["start"] = 31
    broken["segments"][0]["shots"][1]["end"] = 40
    before = copy.deepcopy(broken)
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    message = str(exc.value)
    assert "未通过结构校验" in message
    assert exc.value.error_code == "planning_invalid_response"
    assert exc.value.retryable is True
    assert exc.value.retry_action == "retry_planning"
    assert "string_too_long" not in message and "segments.0" not in message
    assert "input" not in message and "ctx" not in message
    assert_redacted(exc.value)
    assert broken == before and len(calls) == 4


def test_live_wan_video_inside_images_wrapper(settings, install_transport):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/tasks/task_known_video"
        return httpx.Response(200, json={"data": {"id": "task_known_video", "status": "completed", "progress": 100,
            "result": {"images": [{"url": ["https://cdn.example/generated.mp4"], "expires_at": 2000000000}]}}})
    install_transport(handle)
    result = asyncio.run(Gateway(settings).poll_video("task_known_video"))
    assert result == {"status": "succeeded", "download_url": "https://cdn.example/generated.mp4", "progress": 100}
    assert len(calls) == 1


def test_wan_images_wrapper_retains_https_requirement(settings, install_transport):
    install_transport(lambda request: httpx.Response(200, json={"data": {"status": "completed",
        "result": {"images": [{"url": ["http://cdn.example/" + SECRET + ".mp4"]}]}}}))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).poll_video("task_known_video"))
    assert_redacted(exc.value)


@pytest.mark.parametrize("progress,accepted", [(0, True), (30.5, True), (100, True), (-1, False), (101, False),
                                               (True, False), ("30", False), (None, False), ({}, False)])
def test_video_poll_only_returns_sanitized_numeric_progress(settings, install_transport, progress, accepted):
    install_transport(lambda request: httpx.Response(200, json={"data": {"status": "processing", "progress": progress,
        "error": {"message": SECRET}, "result": {"internal": SECRET}}}))
    result = asyncio.run(Gateway(settings).poll_video("task_known_video"))
    assert result == ({"status": "running", "progress": progress} if accepted else {"status": "running"})
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("result", [{}, {"images": []}, {"images": [{"url": []}]},
    {"video_url": "http://cdn.example/" + SECRET}, {"video_url": "https://"},
    {"video_url": "https://cdn.example:bad/" + SECRET}])
def test_completed_without_usable_https_url_is_nonretryable(settings, install_transport, result):
    install_transport(lambda request: httpx.Response(200, json={"data": {"status": "completed", "result": result}}))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).poll_video("task_completed"))
    assert getattr(exc.value, "retryable", True) is False
    assert_redacted(exc.value)


def test_temporary_task_query_error_remains_retryable(settings, install_transport):
    install_transport(lambda request: httpx.Response(503, text=SECRET))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings).poll_video("task_running"))
    assert getattr(exc.value, "retryable", True) is True
    assert_redacted(exc.value)


@pytest.mark.parametrize("invalid", ["missing_shots", "shot_over_limit", "segment_mismatch"])
def test_native_shot_mismatch_is_rejected_before_upload_or_paid_submit(settings, source, plan, monkeypatch, invalid):
    import copy
    segment = {**copy.deepcopy(plan["segments"][0]), "audio_strategy": "native"}
    if invalid == "missing_shots":
        for shot in segment["shots"]:
            shot.pop("narration")
    elif invalid == "shot_over_limit":
        segment["shots"][0]["narration"] = "看清蓝瓶的轮廓和外观细节。"
        segment["narration"] = segment["shots"][0]["narration"]
    else:
        segment["narration"] = "不同的说法。"
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid shot narration cannot reach upload or provider submit")
    monkeypatch.setattr(Gateway, "_original_reference_url", forbidden)
    monkeypatch.setattr(Gateway, "_submit", forbidden)
    with pytest.raises(SubmissionError, match="镜头时长或内容分配不一致"):
        asyncio.run(Gateway(settings).submit_video(segment, [source], "not-submitted"))


def test_repair_is_triggered_by_single_shot_overload_even_when_segment_and_total_fit(settings, install_transport, plan):
    import copy
    overloaded = copy.deepcopy(plan)
    overloaded["segments"][0]["shots"][0]["narration"] = "看清蓝瓶的轮廓和外观细节。"
    overloaded["segments"][0]["narration"] = overloaded["segments"][0]["shots"][0]["narration"]
    overloaded["script"] = "".join(s["narration"] for s in overloaded["segments"])
    assert len(overloaded["script"]) < 58 and len(overloaded["segments"][0]["narration"]) < 29
    calls = []
    def handle(request):
        calls.append(request)
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][1]["content"])
        assert context["review_scope"] == "timing"
        assert context["shot_slots"][0]["max_characters"] == 11
        assert context["shot_slots"][0]["target_characters"] == 7
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"narrations": ["蓝瓶的轮廓。", "看清细节。", "了解商品详情。", ""]})}}]})
    install_transport(handle)
    result = asyncio.run(Gateway(settings)._fit_script(overloaded, 20, {}))
    assert len(calls) == 1 and result["script"] == "蓝瓶的轮廓。看清细节。了解商品详情。"
    assert result["segments"][0]["narration"] == "蓝瓶的轮廓。看清细节。"
    for old_segment, new_segment in zip(overloaded["segments"], result["segments"]):
        assert old_segment["prompt"] == new_segment["prompt"]
        for old_shot, new_shot in zip(old_segment["shots"], new_segment["shots"]):
            assert {k: v for k, v in old_shot.items() if k != "narration"} == {k: v for k, v in new_shot.items() if k != "narration"}


def test_shot_repair_length_error_reports_only_numbers_and_never_response_text(settings, install_transport, plan):
    import copy
    overloaded = copy.deepcopy(plan)
    overloaded["segments"][0]["narration"] = "看清蓝瓶的轮廓和外观细节。"
    overloaded["script"] = "".join(s["narration"] for s in overloaded["segments"])
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"narrations": [SECRET, "", "了解商品详情。", ""]})}}]})
    install_transport(handle)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(Gateway(settings)._fit_script(overloaded, 20, {}))
    assert_redacted(exc.value)
    assert f"镜头1为{len(SECRET)}字，上限11字" in str(exc.value)
    assert len(calls) == 1


def test_scene_submission_saves_actual_setting_and_resume_preserves_it(settings, install_transport, public_dns, source, plan, tmp_path, prepared_original):
    plan['strategy'] = {'brief': {'scenario': '办公室下午茶'}, 'selected_direction_id': 'office',
                        'directions': [{'id': 'office', 'scenario': '办公室茶水区，背景看见工位', 'production_notes': ['保留办公椅与隔断']} ]}
    plan['segments'][1]['shots'][0]['description'] = '拉远时看见办公桌'
    pixels = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(pixels, 'PNG')
    calls, submitted, recorded_skills = [], [], []
    def handle(request):
        calls.append(request.method)
        if request.method == 'POST':
            assert recorded_skills and recorded_skills[0]['stage'] == 'asset_prompts'
            submitted.append(json.loads(request.content))
            return httpx.Response(200, json={'data': {'id': 'scene_office'}})
        if request.url.host == 'api.ai-tudou.net':
            return httpx.Response(200, json={'data': {'status': 'completed', 'result': {'images': [{'url': 'https://cdn.example/scene.png'}]}}})
        return httpx.Response(200, content=pixels.getvalue())
    install_transport(handle)
    gateway = Gateway(settings)
    result = asyncio.run(gateway.create_designs({}, [source], plan, tmp_path, on_skill=recorded_skills.append))
    saved = json.loads((tmp_path / 'scene_task.json').read_text())['design_request']
    assert saved['prompt'] == submitted[0]['prompt']
    assert saved['scenario'] == '办公室下午茶'
    assert '办公室茶水区，背景看见工位' in saved['prompt']
    assert '拉远时看见办公桌' not in saved['prompt']
    assert result[0]['metadata']['design_request'] == saved
    assert recorded_skills == saved['skill_trace']
    recorded_skills.clear()
    before = len(calls)
    plan['strategy']['brief']['scenario'] = '改成海边'
    resumed = asyncio.run(gateway.create_designs({}, [source], plan, tmp_path, on_skill=recorded_skills.append))
    assert len(calls) == before, 'Recovery must reuse the saved receipt, never charge for a replacement'
    assert resumed[0]['metadata']['design_request'] == saved
    assert recorded_skills == saved['skill_trace']


@pytest.mark.parametrize("ending", ["，", ",", "、", "：", ":", "；", ";"])
def test_copy_review_rejects_unfinished_sentence_without_retry(settings, install_transport, plan, ending):
    before = copy.deepcopy(plan)
    words = [f"蓝瓶{ending} \n", "", "一只蓝瓶，自成风景。", ""]
    calls = []
    def handle(request):
        calls.append(request)
        _, context = request_context(request)
        assert context["review_scope"] == "advertising_expression_and_timing" and len(calls) == 1
        return chat_output({"narrations": words})
    install_transport(handle)
    with pytest.raises(ProviderError, match="未完成的句子") as exc:
        asyncio.run(Gateway(settings)._fit_script(plan, 20, {}, review_expression=True))
    assert_redacted(exc.value)
    assert words[0].strip() not in str(exc.value)
    assert len(calls) == 1 and plan == before


@pytest.mark.parametrize("words", [
    ["给桌面一点蓝。", "", "一只蓝瓶，自成风景。", ""],
    ["拧开瓶盖。", "", "把蓝瓶放上桌面。", ""],
])
def test_copy_review_accepts_complete_sentences_and_silent_slots(settings, install_transport, plan, words):
    """Syntax checks must not ban legitimate operation copy or force every slot to speak."""
    before = copy.deepcopy(plan)
    calls = []
    def handle(request):
        calls.append(request)
        _, context = request_context(request)
        assert context["review_scope"] == "advertising_expression_and_timing" and len(calls) == 1
        return chat_output({"narrations": words})
    install_transport(handle)
    result = asyncio.run(Gateway(settings)._fit_script(plan, 20, {}, review_expression=True))
    assert result["script"] == "".join(words) and len(calls) == 1
    assert [shot["narration"] for segment in result["segments"] for shot in segment["shots"]] == words
    assert plan == before


@pytest.mark.parametrize("echo", [False, True])
def test_storyboard_derived_narration_is_owned_by_server(settings, install_transport, source, plan, strategy_stage, echo):
    outputs = stage_outputs(strategy_stage, plan)
    compiled = outputs["storyboard"]
    compiled.pop("script")
    for segment in compiled["segments"]:
        segment.pop("narration")
    if echo:
        compiled["script"] = SECRET
        for segment in compiled["segments"]:
            segment["narration"] = SECRET
    calls = []
    def handle(request):
        _, context = request_context(request)
        calls.append(context["stage"])
        if context["stage"] == "storyboard":
            assert "script" not in context["schema"]["properties"]
            assert "narration" not in context["schema"]["$defs"]["Segment"]["properties"]
            assert "shot_schedule" not in context
            words = [group["text"] for segment in context["expression_schedule"] for group in segment["groups"] if group["text"]]
            assert words == [beat["narration"] for beat in outputs["ad_script"]["beats"] if beat["narration"]]
        return chat_output(outputs[context["stage"]])
    install_transport(handle)
    result = asyncio.run(Gateway(settings).plan({"brief": "蓝瓶商品"}, [source], []))
    assert result["script"] == plan["script"]
    assert [segment["narration"] for segment in result["segments"]] == [segment["narration"] for segment in plan["segments"]]
    assert SECRET not in json.dumps(result) and len(calls) == 5


@pytest.mark.parametrize('stage_skills', [False, True])
def test_storyboard_uses_scoped_reasoning_with_full_new_skill_capacity(settings, install_transport, stage_skills):
    calls = []
    def handle(request):
        data, context = request_context(request)
        calls.append(data)
        if stage_skills:
            assert data['thinking'] == {'type': 'enabled'}
            assert data['reasoning_effort'] == 'high'
            assert data['max_tokens'] == 32768
        else:
            assert data['thinking'] == {'type': 'disabled'}
            assert 'reasoning_effort' not in data
        return chat_output({'checked': True})
    install_transport(handle)
    context = {'stage': 'storyboard'}
    if stage_skills:
        context['stage_skills'] = {'skills': [{'id': 'ads-video-storyboard-direction'}]}
    assert asyncio.run(Gateway(settings)._planner_json('JSON', context, [])) == {'checked': True}
    assert len(calls) == 1
