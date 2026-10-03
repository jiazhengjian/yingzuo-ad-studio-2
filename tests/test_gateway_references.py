"""Expired reference links must fail or refresh before a paid video POST."""
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

from app.providers.gateway import Gateway, ProviderError, SubmissionError, atomic_json
from test_providers import settings, install_transport, public_dns, source, plan, SECRET


def cached_reference(source):
    raw = Path(source["path"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    url = f"https://canva.ai-tudou.net/library/materials/product-{digest}.png"
    receipt = Path(source["path"]).parent / ".provider_refs" / f"{digest}.json"
    receipt.parent.mkdir()
    atomic_json(receipt, {"state": "verified", "sha256": digest, "url": url})
    return raw, digest, url, receipt


@pytest.mark.parametrize("stale", ["html", "404", "changed_image"])
def test_expired_verified_original_refreshes_once_and_checks_bytes(settings, install_transport, public_dns, source, stale):
    raw, digest, url, receipt = cached_reference(source)
    posts, gets = [], []
    def handler(request):
        if request.method == "POST":
            assert request.url.path == "/api/materials/upload"
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "url": url})
        gets.append(request)
        assert "authorization" not in request.headers
        if not posts:
            if stale == "404":
                return httpx.Response(404)
            content = b"<!DOCTYPE html><html>app</html>" if stale == "html" else b"another image"
            return httpx.Response(200, content=content)
        return httpx.Response(200, content=raw)
    install_transport(handler)
    assert asyncio.run(Gateway(settings)._original_reference_url(source)) == url
    assert len(posts) == 1 and len(gets) == 2
    assert posts[0]["filename"] == f"product-{digest}.png"
    assert json.loads(receipt.read_text())["state"] == "verified"
    assert receipt.with_name(f"{digest}.previous.json").is_file()
    assert Path(source["path"]).read_bytes() == raw


def test_valid_cached_original_is_rechecked_without_upload(settings, install_transport, public_dns, source):
    raw, _, url, _ = cached_reference(source)
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, content=raw)
    install_transport(handler)
    assert asyncio.run(Gateway(settings)._original_reference_url(source)) == url
    assert calls == ["GET"]


def test_reupload_still_serves_html_stops_before_video(settings, install_transport, public_dns, source, plan):
    _, _, url, receipt = cached_reference(source)
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(200, json={"success": True, "url": url})
        return httpx.Response(200, content=b"<!doctype html>still invalid")
    install_transport(handler)
    scene = {"id": "scene", "kind": "scene", "metadata": {"provider_url": "https://cdn.example/scene.png"}}
    segment = {**plan["segments"][0], "audio_strategy": "native", "reference_asset_ids": [source["id"], "scene"]}
    with pytest.raises(SubmissionError) as err:
        asyncio.run(Gateway(settings).submit_video(segment, [source, scene], "no-paid-call"))
    assert err.value.uncertain is False
    assert err.value.error_code == "reference_image_unreadable"
    assert posts == ["/api/materials/upload"]
    assert json.loads(receipt.read_text())["state"] != "verified"


def test_scene_png_filename_serving_approved_webp_pixels_is_valid(settings, install_transport, public_dns, tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "pink").save(buffer, "WEBP")
    path = tmp_path / "scene.png"
    with Image.open(io.BytesIO(buffer.getvalue())) as im:
        im.convert("RGB").save(path)
    scene = {"path": str(path), "metadata": {"provider_url": "https://cdn.example/scene.png"}}
    install_transport(lambda req: httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "image/webp"}))
    assert asyncio.run(Gateway(settings)._scene_reference_url(scene)) == scene["metadata"]["provider_url"]


@pytest.mark.parametrize("bad_content", [b"<!doctype html>not an image", None])
def test_scene_html_or_different_pixels_still_fail_when_reupload_rejected(settings, install_transport, public_dns, source, bad_content):
    if bad_content is None:
        buffer = io.BytesIO(); Image.new("RGB", (8, 8), "red").save(buffer, "PNG")
        bad_content = buffer.getvalue()
    scene = {**source, "kind": "scene", "metadata": {"provider_url": "https://cdn.example/scene.png"}}
    posts = []
    def handler(request):
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(503)
        return httpx.Response(200, content=bad_content)
    install_transport(handler)
    with pytest.raises(ProviderError, match="拒绝已确认场景图上传"):
        asyncio.run(Gateway(settings)._scene_reference_url(scene))
    assert posts == ["/api/materials/upload"]
    assert not list((Path(source["path"]).parent / ".provider_refs").glob("*.scene-verify"))


@pytest.mark.parametrize("kind", ["scene", "character"])
@pytest.mark.parametrize("stale", ["html", "404", "changed_pixels"])
def test_approved_design_recovers_exact_local_bytes_without_regeneration(
        settings, install_transport, public_dns, source, kind, stale):
    raw = Path(source["path"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    asset = {**source, "kind": kind, "metadata": {"generated": True,
        "provider_url": "https://cdn.example/old.png", "character_id": "char_approved"}}
    before = deepcopy(asset)
    uploaded_url = f"https://canva.ai-tudou.net/library/materials/{kind}-{digest}.png"
    other = io.BytesIO(); Image.new("RGB", (8, 8), "red").save(other, "PNG")
    posts, gets = [], []
    def handler(request):
        if request.method == "POST":
            assert request.url.host == "canva.ai-tudou.net"
            assert request.url.path == "/api/materials/upload"
            payload = json.loads(request.content)
            posts.append(payload)
            assert payload["filename"] == f"{kind}-{digest}.png"
            assert base64.b64decode(payload["data"].split(",", 1)[1]) == raw
            return httpx.Response(200, json={"success": True, "url": uploaded_url})
        assert "authorization" not in request.headers
        gets.append(request.headers["host"])
        if request.headers["host"] == "cdn.example":
            if stale == "404": return httpx.Response(404)
            return httpx.Response(200, content=other.getvalue() if stale == "changed_pixels" else b"<html>expired</html>")
        assert request.headers["host"] == "canva.ai-tudou.net"
        return httpx.Response(200, content=raw)
    install_transport(handler)
    gateway = Gateway(settings)
    assert asyncio.run(gateway._scene_reference_url(asset)) == uploaded_url
    assert gets == ["cdn.example", "canva.ai-tudou.net"] and len(posts) == 1
    # A later retry verifies and reuses the upload, even if the generated CDN
    # URL remains stale. It never changes the approved asset or source receipt.
    assert asyncio.run(gateway._scene_reference_url(asset)) == uploaded_url
    assert len(posts) == 1 and len(gets) == 4
    receipt_path = Path(source["path"]).parent / ".provider_refs" / f"{kind}-{digest}.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["state"] == "verified" and receipt["sha256"] == digest
    assert asset == before and Path(source["path"]).read_bytes() == raw
    assert SECRET not in receipt_path.read_text()
    assert not list(receipt_path.parent.glob("*.verify"))
    assert not list(receipt_path.parent.glob("*.scene-verify"))


@pytest.mark.parametrize("kind", ["scene", "character"])
@pytest.mark.parametrize("new_content", [b"<html>still invalid</html>", b"changed bytes"])
def test_recovered_design_url_must_match_local_bytes(settings, install_transport, public_dns, source, kind, new_content):
    digest = hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest()
    asset = {**source, "kind": kind, "metadata": {"provider_url": "https://cdn.example/expired.png"}}
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert request.url.path == "/api/materials/upload"
            return httpx.Response(200, json={"success": True,
                "url": f"/library/materials/{kind}-{digest}.png"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=new_content)
    install_transport(handler)
    with pytest.raises(ProviderError, match="字节不一致"):
        asyncio.run(Gateway(settings)._scene_reference_url(asset))
    assert [path for method, path in calls if method == "POST"] == ["/api/materials/upload"]
    receipt = json.loads((Path(source["path"]).parent / ".provider_refs" / f"{kind}-{digest}.json").read_text())
    assert receipt["state"] == "uploading" and "url" not in receipt


@pytest.mark.parametrize("kind", ["scene", "character"])
def test_design_recovery_rejects_unapproved_upload_url(settings, install_transport, public_dns, source, kind):
    asset = {**source, "kind": kind, "metadata": {"provider_url": "http://127.0.0.1/private"}}
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/api/materials/upload"
        return httpx.Response(200, json={"success": True, "url": "https://127.0.0.1/" + SECRET})
    install_transport(handler)
    with pytest.raises(ProviderError) as error:
        asyncio.run(Gateway(settings)._scene_reference_url(asset))
    assert len(calls) == 1  # Neither unsafe reference URL can be downloaded.
    assert SECRET not in str(error.value) and "127.0.0.1" not in str(error.value)


@pytest.mark.parametrize("kind", ["scene", "character"])
def test_design_recovery_requires_a_valid_approved_local_image(settings, install_transport, source, kind):
    Path(source["path"]).write_bytes(b"not an approved image")
    asset = {**source, "kind": kind, "metadata": {"provider_url": "https://cdn.example/stale.png"}}
    def forbidden(request): pytest.fail("Cannot upload or download without the approved local image")
    install_transport(forbidden)
    with pytest.raises(ProviderError, match="本地文件无法读取"):
        asyncio.run(Gateway(settings)._scene_reference_url(asset))


@pytest.mark.parametrize("detail,code", [
    ({"message": "Cannot determine file type for https://private/" + SECRET}, "reference_image_unreadable"),
    ("task failed, code: InvalidParameter , message: Cannot determine file type for " + SECRET, "reference_image_unreadable"),
    ({"message": SECRET}, "video_generation_failed"),
])
def test_vendor_failure_classification_never_exposes_raw_error(settings, install_transport, detail, code):
    install_transport(lambda req: httpx.Response(200, json={"data": {"status": "failed", "error": detail}}))
    result = asyncio.run(Gateway(settings).poll_video("known-task"))
    assert result["status"] == "failed" and result["error_code"] == code
    assert SECRET not in json.dumps(result) and "https://" not in json.dumps(result)
