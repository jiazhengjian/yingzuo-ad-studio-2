"""Official material renames retain origin, exact bytes, and paid-submit boundaries."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path

import httpx
from PIL import Image
import pytest

from app.db import Store
from app.pipeline import Pipeline
from app.providers.gateway import Gateway, ProviderError
from test_conversation_pipeline import OfflineGateway, execute, setup as pipeline_setup, turn
from test_providers import SECRET, install_transport, public_dns, settings, source


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Reference rename regression attempted a real network request")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


def upload(gateway, asset, kind):
    if kind == "product":
        return asyncio.run(gateway._original_reference_url(asset))
    return asyncio.run(gateway._uploaded_reference_url(asset, reference_kind=kind))


def receipt_path(asset, kind, digest):
    key = digest if kind == "product" else f"{kind}-{digest}"
    return Path(asset["path"]).parent / ".provider_refs" / f"{key}.json"


@pytest.mark.parametrize("kind", ["product", "scene", "character"])
@pytest.mark.parametrize("mime,suffix,format", [
    ("image/png", "png", "PNG"), ("image/jpeg", "jpg", "JPEG"), ("image/webp", "webp", "WEBP"),
])
@pytest.mark.parametrize("rename", [False, True], ids=["old-exact-name", "provider-six-hex-suffix"])
def test_upload_accepts_preserved_or_provider_renamed_file_and_reuses_verified_cache(
        settings, install_transport, public_dns, source, kind, mime, suffix, format, rename):
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(data, format)
    raw = data.getvalue()
    path = Path(source["path"]).with_suffix("." + suffix)
    path.write_bytes(raw)
    asset = {**source, "path": str(path), "mime": mime, "kind": kind,
             "metadata": {"generated": kind != "product"}}
    before = deepcopy(asset)
    digest = hashlib.sha256(raw).hexdigest()
    filename = f"{kind}-{digest}.{suffix}"
    returned_name = f"{kind}-{digest}{'-fd8c02' if rename else ''}.{suffix}"
    relative = f"/library/users/test-namespace/materials/%E6%98%A0%E4%BD%9C/{returned_name}"
    url = "https://canva.ai-tudou.net" + relative
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            assert request.url.host == "canva.ai-tudou.net"
            assert request.url.path == "/api/materials/upload"
            payload = json.loads(request.content)
            assert payload["filename"] == filename
            assert base64.b64decode(payload["data"].split(",", 1)[1]) == raw
            return httpx.Response(200, json={"success": True, "url": relative})
        assert "authorization" not in request.headers
        assert request.headers["host"] == "canva.ai-tudou.net"
        assert request.url.path.endswith("/" + returned_name)
        return httpx.Response(200, content=raw)

    install_transport(handler)
    assert upload(Gateway(settings), asset, kind) == url
    # A new adapter instance must verify and reuse the durable renamed receipt.
    assert upload(Gateway(settings), asset, kind) == url
    assert calls == ["POST", "GET", "GET"]
    receipt = json.loads(receipt_path(asset, kind, digest).read_text())
    assert receipt["state"] == "verified" and receipt["sha256"] == digest
    assert receipt["filename"] == filename and receipt["url"] == url
    assert SECRET not in json.dumps(receipt)
    assert asset == before and path.read_bytes() == raw


@pytest.mark.parametrize("bad", [
    "short_suffix", "long_suffix", "uppercase_suffix", "nonhex_suffix", "double_suffix",
    "underscore_suffix", "wrong_hash", "wrong_kind", "wrong_extension", "extra_extension",
    "wrong_host", "host_suffix", "http", "port", "username", "password", "query", "fragment",
    "outside_library",
])
def test_similar_renames_and_unapproved_urls_stop_before_download(
        settings, install_transport, source, bad):
    raw = Path(source["path"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    stem = f"product-{digest}"
    name = f"{stem}-fd8c02.png"
    names = {
        "short_suffix": f"{stem}-fd8c0.png", "long_suffix": f"{stem}-fd8c020.png",
        "uppercase_suffix": f"{stem}-FD8C02.png", "nonhex_suffix": f"{stem}-fd8c0z.png",
        "double_suffix": f"{stem}-fd8c02-ab1234.png", "underscore_suffix": f"{stem}_fd8c02.png",
        "wrong_hash": f"product-{'0' * 64}-fd8c02.png", "wrong_kind": f"scene-{digest}-fd8c02.png",
        "wrong_extension": f"{stem}-fd8c02.jpg", "extra_extension": name + ".html",
    }
    path = "/library/users/test/materials/" + names.get(bad, name)
    url = "https://canva.ai-tudou.net" + path
    alternatives = {
        "wrong_host": "https://untrusted.example" + path,
        "host_suffix": "https://canva.ai-tudou.net.untrusted.example" + path,
        "http": "http://canva.ai-tudou.net" + path,
        "port": "https://canva.ai-tudou.net:8443" + path,
        "username": "https://private@canva.ai-tudou.net" + path,
        "password": "https://private:password@canva.ai-tudou.net" + path,
        "query": url + "?private=credential", "fragment": url + "#private",
        "outside_library": "https://canva.ai-tudou.net/api/private/" + name,
    }
    url = alternatives.get(bad, url)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/api/materials/upload"
        return httpx.Response(200, json={"success": True, "url": url})

    install_transport(handler)
    with pytest.raises(ProviderError, match="未返回可校验") as error:
        upload(Gateway(settings), source, "product")
    assert len(calls) == 1
    assert url not in str(error.value) and SECRET not in str(error.value)
    receipt = json.loads(receipt_path(source, "product", digest).read_text())
    assert receipt["state"] == "uploading" and "url" not in receipt


@pytest.mark.parametrize("kind", ["product", "scene", "character"])
@pytest.mark.parametrize("content", [b"<!doctype html>material page", b"different image bytes"])
def test_valid_renamed_url_still_requires_exact_original_bytes(
        settings, install_transport, public_dns, source, kind, content):
    asset = {**source, "kind": kind}
    digest = hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest()
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"success": True,
                "url": f"/library/materials/{kind}-{digest}-fd8c02.png"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=content)

    install_transport(handler)
    with pytest.raises(ProviderError, match="字节不一致"):
        upload(Gateway(settings), asset, kind)
    assert calls == ["POST", "GET"]
    receipt = json.loads(receipt_path(asset, kind, digest).read_text())
    assert receipt["state"] == "uploading" and "url" not in receipt
    assert not list(receipt_path(asset, kind, digest).parent.glob("*.verify"))


def test_pipeline_renamed_original_reaches_image_gate_once_without_replacing_product(
        pipeline_setup, install_transport, public_dns):
    store, project, asset, decision, settings = pipeline_setup
    execute(store, settings, OfflineGateway([decision]), turn(store, project["id"], "offline-plan"))
    current = store.get_project(project["id"])
    job = store.create_job(current["id"], "generate", current["revision"], {"pause_for_creative": True})
    original = Path(asset["path"]).read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    returned_url = f"/library/users/test/materials/product-{digest}-fd8c02.png"
    scene = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(scene, "PNG")
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.url.path)
            if request.url.path == "/api/materials/upload":
                assert base64.b64decode(json.loads(request.content)["data"].split(",", 1)[1]) == original
                return httpx.Response(200, json={"success": True, "url": returned_url})
            assert request.url.path == "/v1/images/generations/async"
            payload = json.loads(request.content)
            assert base64.b64decode(payload["images"][0].split(",", 1)[1]) == original
            return httpx.Response(200, json={"data": {"id": "single_scene"}})
        if request.url.host == "api.ai-tudou.net":
            assert request.url.path == "/v1/tasks/single_scene"
            return httpx.Response(200, json={"data": {"status": "completed", "result": {
                "images": [{"url": ["https://cdn.example/scene.png"]}]}}})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=original if request.headers["host"] == "canva.ai-tudou.net" else scene.getvalue())

    install_transport(handler)
    result = execute(store, settings, Gateway(settings), job)
    assert result["status"] == "awaiting_input" and result["submission_count"] == 0
    assert posts == ["/api/materials/upload", "/v1/images/generations/async"]
    gate = store.pending_gate(project["id"], "creative")
    assert gate and gate["status"] == "pending"
    assets = store.project_detail(project["id"])["assets"]
    assert len(assets) == 2 and sum(a["kind"] == "scene" for a in assets) == 1
    assert store.get_asset(asset["id"]) == asset and Path(asset["path"]).read_bytes() == original
    # A restored worker reuses both the completed design and the waiting gate.
    restored = Store(store.db_path, store.media_root)
    repeated = asyncio.run(Pipeline(restored, Gateway(settings), settings).execute(job["id"]))
    assert repeated["status"] == "awaiting_input"
    assert posts == ["/api/materials/upload", "/v1/images/generations/async"]
    assert restored.pending_gate(project["id"], "creative")["id"] == gate["id"]
