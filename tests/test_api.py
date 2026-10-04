import io
import re
from urllib.parse import unquote

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
import app.main as main


def client(tmp_path, **settings):
    app = main.create_app(Settings(data_root=tmp_path / "data", **settings), run_worker=False)
    return TestClient(app), app


def create(c):
    response = c.post("/api/projects", json={"brief": "透明玻璃香水，突出瓶身设计", "route": "product"})
    assert response.status_code == 201
    return response.json()["id"]


def image_bytes():
    out = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(out, "PNG")
    return out.getvalue()


def test_project_upload_and_missing_credentials(tmp_path):
    c, app = client(tmp_path)
    pid = create(c)
    upload = c.post(f"/api/projects/{pid}/assets", files={"file": ("../../product.png", image_bytes(), "image/png")})
    assert upload.status_code == 201
    asset = upload.json()
    assert "path" not in asset and asset["name"] == "product.png"
    assert c.get(asset["url"]).status_code == 200
    assert c.post(f"/api/projects/{pid}/plan", json={}).status_code == 409
    assert not app.state.store.active_jobs()


def test_invalid_image_and_unknown_path_rejected(tmp_path):
    c, _ = client(tmp_path)
    pid = create(c)
    assert c.post(f"/api/projects/{pid}/assets", files={"file": ("evil.png", b"<script>alert(1)</script>", "image/png")}).status_code == 422
    assert c.get("/media/unknown").status_code == 404


def test_media_download_and_preview_contract(tmp_path):
    c, app = client(tmp_path)
    pid = create(c)
    payload = b"video-download-regression"
    path = app.state.settings.media_root / "output.mp4"
    path.write_bytes(payload)
    asset = app.state.store.add_asset(pid, "render", "广告成片", path, "video/mp4", revision=4)
    url = f"/media/{asset['id']}"
    preview = c.get(url)
    assert preview.status_code == 200 and preview.content == payload
    assert "content-disposition" not in preview.headers
    partial = c.get(url, headers={"Range": "bytes=0-4"})
    assert partial.status_code == 206 and partial.content == payload[:5]
    download = c.get(url + "?download=true")
    assert download.status_code == 200 and download.content == payload
    assert unquote(download.headers["content-disposition"]) == "attachment; filename*=utf-8''广告成片_V4.mp4"
    head = c.head(url + "?download=true")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(payload))
    assert head.headers["content-disposition"] == download.headers["content-disposition"]
    path.unlink()
    assert c.get(url + "?download=true").status_code == 404
    assert c.head("/media/unknown?download=true").status_code == 404


def test_download_filename_preserves_actual_extension_and_removes_unsafe_characters(tmp_path):
    c, app = client(tmp_path)
    pid = create(c)
    path = app.state.settings.media_root / "image.jpg"
    path.write_bytes(b"converted-image")
    asset = app.state.store.add_asset(pid, "product", '商品/原图\\测试\r\n".png', path, "image/jpeg")
    response = c.get(f"/media/{asset['id']}?download=true")
    assert response.status_code == 200
    assert unquote(response.headers["content-disposition"]) == "attachment; filename*=utf-8''商品_原图_测试___.jpg"


def test_presenter_gated(tmp_path):
    c, _ = client(tmp_path)
    assert c.post("/api/projects", json={"brief": "测试商品", "route": "presenter"}).status_code == 409
    assert c.get("/api/projects").json() == {"projects": []}


def test_cross_site_and_rebinding_rejected(tmp_path):
    c, _ = client(tmp_path)
    assert c.post("/api/projects", json={"brief": "恶意请求"}, headers={"Origin": "https://example.com"}).status_code == 403
    assert c.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400


def test_same_origin_allows_this_servers_loopback_port_only(tmp_path):
    c, _ = client(tmp_path)
    own = {"Origin": "http://127.0.0.1:9321", "Host": "127.0.0.1:9321"}
    assert c.post("/api/projects", json={"brief": "第二实例", "route": "product"}, headers=own).status_code == 201
    assert c.post("/api/projects", json={"brief": "本机另一写法", "route": "product"}, headers={
        "Origin": "http://localhost:9321", "Host": "localhost:9321"}).status_code == 201
    mismatch = c.post("/api/projects", json={"brief": "端口不同"}, headers={
        "Origin": "http://127.0.0.1:9321", "Host": "127.0.0.1:8787"})
    assert mismatch.status_code == 403 and mismatch.json()["detail"] == "仅允许本机工作台提交请求"
    mixed_host = c.post("/api/projects", json={"brief": "主机名不同"}, headers={
        "Origin": "http://localhost:9321", "Host": "127.0.0.1:9321"})
    assert mixed_host.status_code == 403 and mixed_host.json()["detail"] == "仅允许本机工作台提交请求"
    foreign = c.post("/api/projects", json={"brief": "外站"}, headers={
        "Origin": "http://evil.example", "Host": "127.0.0.1:9321"})
    assert foreign.status_code == 403 and foreign.json()["detail"] == "仅允许本机工作台提交请求"
    cross = c.post("/api/projects", json={"brief": "跨站"}, headers={**own, "Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403 and cross.json()["detail"] == "拒绝跨站请求"
    assert c.post("/api/projects", json={"brief": "开发端口", "route": "product"}, headers={
        "Origin": "http://127.0.0.1:5173"}).status_code == 201


def test_credentials_local_save_no_echo(tmp_path, monkeypatch):
    # All secret writes stay in the isolated test directory.
    monkeypatch.setattr(main, "ROOT", tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "setup.html").write_text("token=__SETUP_TOKEN__")
    c, app = client(tmp_path)
    fake = "test-secret-placeholder-12345"
    assert c.post("/api/settings/credentials", json={"deepseek_api_key": fake}).status_code == 403
    token = c.get("/setup").text.removeprefix("token=")
    saved = c.post("/api/settings/credentials", headers={"X-Setup-Token": token}, json={"deepseek_api_key": fake, "tudou_api_key": fake})
    assert saved.status_code == 200 and fake not in saved.text
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    health = c.get("/api/health")
    assert fake not in health.text and health.json()["configured"]["planner"]
    invalid = c.post("/api/settings/credentials", json={"deepseek_api_key": "sensitive " * 100})
    assert "sensitive" not in invalid.text


def test_public_detail_strips_internal_paths_and_urls(tmp_path):
    c, app = client(tmp_path)
    pid = create(c)
    path = app.state.settings.media_root / "internal.png"
    path.write_bytes(image_bytes())
    app.state.store.add_asset(pid, "scene", "场景", path, "image/png", metadata={"provider_url": "https://example.com/private-signed-url"})
    job = app.state.store.create_job(pid, "plan", 0, payload={"secret": "internal-payload"})
    response = c.get(f"/api/projects/{pid}")
    assert "internal-payload" not in response.text
    assert "private-signed-url" not in response.text
    assert str(tmp_path) not in response.text


def test_upload_limit_and_project_asset_ownership(tmp_path):
    c, app = client(tmp_path)
    first, second = create(c), create(c)
    asset = c.post(f"/api/projects/{first}/assets", files={"file": ("p.png", image_bytes(), "image/png")}).json()
    assert c.post(f"/api/projects/{second}/messages", json={"content": "反馈", "asset_id": asset["id"]}).status_code == 422
    for _ in range(2):
        assert c.post(f"/api/projects/{first}/assets", files={"file": ("p.png", image_bytes(), "image/png")}).status_code == 201
    assert c.post(f"/api/projects/{first}/assets", files={"file": ("p.png", image_bytes(), "image/png")}).status_code == 422


def test_stale_tab_cannot_replace_newer_plan(tmp_path):
    c, app = client(tmp_path)
    pid = create(c)
    asset = c.post(f"/api/projects/{pid}/assets", files={"file": ("p.png", image_bytes(), "image/png")}).json()
    plan = {"title": "商品广告", "script": "看看商品细节。", "cta": "了解详情", "language": "zh-CN", "aspect_ratio": "9:16",
            "audio_strategy": "voiceover", "facts": {"name": "香水", "claims": []}, "segments": [
                {"id": f"s{i}", "duration_seconds": 10, "purpose": "展示", "prompt": "展示商品真实外观",
                 "shots": [{"start": 0, "end": 10, "description": "展示"}], "reference_asset_ids": [asset["id"]]} for i in range(2)]}
    app.state.store.update_plan(pid, plan)
    saved = c.patch(f"/api/projects/{pid}/plan", json={"plan": plan, "accepted_revision": 1})
    assert saved.status_code == 200 and saved.json()["revision"] == 2
    stale = c.patch(f"/api/projects/{pid}/plan", json={"plan": {**plan, "script": "旧标签的文案"}, "accepted_revision": 1})
    assert stale.status_code == 409
    assert app.state.store.get_project(pid)["plan"]["script"] == "看看商品细节。"


def test_cta_endpoint_is_retired_without_side_effects(tmp_path):
    c, app = client(tmp_path)
    pid = create(c)
    before = app.state.store.project_detail(pid)
    response = c.post(f"/api/projects/{pid}/revise-cta", json={"text": "新的引导", "accepted_revision": 1, "base_render_id": "old-render"})
    assert response.status_code == 410
    assert c.post(f"/api/projects/{pid}/revise-cta", json={}).status_code == 410
    assert app.state.store.project_detail(pid) == before
    assert not app.state.store.active_jobs()


def test_project_title_patch_persists_without_changing_plan_render_or_jobs(tmp_path):
    from app.db import Store
    c, app = client(tmp_path)
    store = app.state.store
    pid = create(c)
    project = store.update_plan(pid, {"title": "方案标题保持原样", "script": "文案保持原样",
                                      "segments": [{"duration_seconds": 10}, {"duration_seconds": 10}]})
    render_path = store.media_root / "prior_render.mp4"
    render_path.write_bytes(b"EXPLICIT_TEST_RENDER_FIXTURE")
    asset = store.add_asset(pid, "render", "已完成广告", render_path, "video/mp4")
    finished_job = store.create_job(pid, "generate", project["revision"])
    store.publish_render(finished_job["id"], asset["id"], {"fixture": True})
    # Renaming metadata must also remain safe while an unrelated plan task is queued.
    store.create_job(pid, "plan", project["revision"])
    before = store.project_detail(pid)
    for title in ("新的商品广告标题", "名" * 80):
        response = c.patch(f"/api/projects/{pid}", json={"title": " \n" + title + "\t "})
        assert response.status_code == 200
        saved = response.json()
        assert saved["title"] == title and saved["updated_at"] > before["project"]["updated_at"]
        assert saved == main.public(store.get_project(pid))
        reopened = Store(store.db_path, store.media_root).project_detail(pid)
        assert reopened["project"]["title"] == title
        assert {k: v for k, v in reopened["project"].items() if k not in ("title", "updated_at")} == {
            k: v for k, v in before["project"].items() if k not in ("title", "updated_at")}
        for collection in ("assets", "messages", "jobs", "renders"):
            assert reopened[collection] == before[collection]


def test_project_title_patch_rejects_empty_oversized_and_cross_site_requests(tmp_path):
    c, app = client(tmp_path)
    pid = create(c)
    before = app.state.store.get_project(pid)
    for title in ("", " \n\t ", "名" * 81, "  " + "名" * 81 + "  "):
        assert c.patch(f"/api/projects/{pid}", json={"title": title}).status_code == 422
    assert c.patch(f"/api/projects/{pid}", json={"title": "不应写入"}, headers={"Origin": "https://example.com"}).status_code == 403
    assert c.patch(f"/api/projects/{pid}", json={"title": "不应写入"}, headers={"Host": "attacker.example"}).status_code == 400
    assert app.state.store.get_project(pid) == before
    assert not app.state.store.active_jobs()
