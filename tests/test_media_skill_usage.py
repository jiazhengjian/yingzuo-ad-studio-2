"""Show recorded media compilers without replaying jobs or exposing requests."""
from copy import deepcopy
import json

from app.db import encoded
from test_api import client, create, image_bytes


def trace(stage, version="0.8.1", digest="a"):
    asset = stage == "asset_prompts"
    return {"id": "ads-video-asset-prompts" if asset else "ads-video-video-prompts",
            "display_name": "Ads Video Asset Prompts" if asset else "Ads Video Video Prompts",
            "version": version, "mode": "compiler", "stage": stage,
            "content_sha256": digest * 64,
            "resources": [{"path": "private-resource-path", "sha256": digest * 64}],
            "compiled_resource": "references/private-template.txt"}


def historical_job(store, pid, payload=None):
    """Seed persisted history; this test never schedules provider generation."""
    job = store.create_job(pid, "plan", 0, payload)
    with store._connection(True) as connection:
        connection.execute("UPDATE jobs SET kind='generate',status='ready' WHERE id=?", (job["id"],))
    return store.get_job(job["id"])


def frozen_step(store, job, key, traces, *, snapshot=None):
    request = {"skill_trace": traces, "prompt": "private-frozen-prompt",
               "headers": {"authorization": "private-header"}, "provider_url": "https://private.invalid/signed"}
    with store._connection(True) as connection:
        connection.execute("INSERT INTO steps VALUES(?,?,?,?,?,?,?,?,?)", (
            job["id"], key, "downloaded", encoded(snapshot if snapshot is not None else {"video_request": request}),
            "private-provider-task", encoded({"skill_trace": [trace("video_prompts", "99.0.0")]}), None, 10, 20))


def scene_asset(app, pid, job_id, traces, *, generated=True, name="scene.png"):
    path = app.state.settings.media_root / name
    path.write_bytes(image_bytes())
    return app.state.store.add_asset(pid, "scene", name, path, "image/png", metadata={
        "job_id": job_id, "generated": generated, "design_request": {"skill_trace": traces}})


def test_historical_media_usage_comes_from_frozen_sources_without_writes(tmp_path):
    c, app = client(tmp_path)
    store, pid = app.state.store, create(c)
    job = historical_job(store, pid, {"skill_trace": [trace("video_prompts", "98.0.0")]})
    image_trace, video_trace = trace("asset_prompts"), trace("video_prompts", "0.9.2", "b")
    scene_asset(app, pid, job["id"], [image_trace])
    frozen_step(store, job, "video:first", [video_trace])
    frozen_step(store, job, "video:second", [video_trace])
    store.update_job(job["id"], status="ready", phase="complete")
    events_before, job_before = store.events_after(pid), store.get_job(job["id"])
    response = c.get(f"/api/projects/{pid}")
    assert response.status_code == 200
    public_job = response.json()["jobs"][0]
    usage = public_job["media_skill_usage"]
    safe_keys = {"id", "display_name", "version", "mode", "stage", "content_sha256"}
    assert usage == [{key: value for key, value in item.items() if key in safe_keys}
                     for item in (image_trace, video_trace)]
    assert all(set(item) == safe_keys for item in usage)
    assert not any(secret in json.dumps(public_job) for secret in (
        "private-frozen-prompt", "private-header", "private.invalid", "private-resource-path", "private-template", "98.0.0", "99.0.0"))
    assert store.events_after(pid) == events_before and store.get_job(job["id"]) == job_before


def test_media_usage_keeps_recorded_versions_and_project_ownership(tmp_path):
    c, app = client(tmp_path)
    store, first, second = app.state.store, create(c), create(c)
    own = historical_job(store, first)
    other = historical_job(store, second)
    frozen_step(store, own, "video:a", [trace("video_prompts", "0.8.0", "a")])
    frozen_step(store, own, "video:b", [trace("video_prompts", "0.9.0", "b")])
    frozen_step(store, other, "video:other", [trace("video_prompts", "9.9.9", "c")])
    # A foreign asset cannot claim provenance for this project's job.
    scene_asset(app, second, own["id"], [trace("asset_prompts", "9.9.9")])
    usage = c.get(f"/api/projects/{first}").json()["jobs"][0]["media_skill_usage"]
    assert [(item["stage"], item["version"]) for item in usage] == [
        ("video_prompts", "0.8.0"), ("video_prompts", "0.9.0")]


def test_missing_or_invalid_evidence_does_not_invent_usage(tmp_path):
    c, app = client(tmp_path)
    store, pid = app.state.store, create(c)
    job = historical_job(store, pid)
    invalid = [None, "not-a-trace", {}, trace("asset_prompts")]
    for changes in ({"mode": "llm"}, {"content_sha256": "missing"}, {"version": "https://secret.invalid"},
                    {"display_name": "Ads Video https://secret.invalid"}):
        item = deepcopy(trace("video_prompts")); item.update(changes); invalid.append(item)
    frozen_step(store, job, "video:bad", invalid)
    frozen_step(store, job, "video:legacy", [], snapshot={"prompt": "legacy-no-trace"})
    scene_asset(app, pid, job["id"], [trace("asset_prompts")], generated=False)
    assert "media_skill_usage" not in c.get(f"/api/projects/{pid}").json()["jobs"][0]


def test_failed_image_compilation_trace_is_visible_before_any_asset(tmp_path):
    c, app = client(tmp_path)
    store, pid = app.state.store, create(c)
    image_trace = trace("asset_prompts")
    job = historical_job(store, pid, {"media_skill_trace": [image_trace, image_trace]})
    store.update_job(job["id"], status="failed", phase="creating_designs")
    detail = c.get(f"/api/projects/{pid}").json()
    assert not detail["assets"]
    assert len(detail["jobs"][0]["media_skill_usage"]) == 1
    assert detail["jobs"][0]["media_skill_usage"][0]["version"] == image_trace["version"]
