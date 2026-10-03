"""A failed edit must not make an already published video unavailable."""
from copy import deepcopy

import pytest

from app.db import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "studio.sqlite3", tmp_path / "media")


def _plan(label="原方案"):
    return {"script": label, "segments": [{"duration_seconds": 10}, {"duration_seconds": 10}]}


def _render_asset(store, project, name):
    path = store.media_root / name
    path.write_bytes(b"saved video " + name.encode())
    return store.add_asset(project["id"], "render", name, path, "video/mp4")


def _published(store):
    project = store.create_project("蛋糕广告", "product")
    project = store.update_plan(project["id"], _plan())
    job = store.create_job(project["id"], "generate", project["revision"])
    assert store.claim_job(job["id"])
    asset = _render_asset(store, project, "original.mp4")
    render = store.publish_render(job["id"], asset["id"], {"duration_seconds": 20})
    return store.get_project(project["id"]), render


def _edit(store, project):
    return store.create_turn(project["id"], {
        "client_turn_id": "edit-1",
        "expected_context_version": project["context_version"],
        "content": "优化结尾，保留已有视频",
    })


def test_failed_edit_preserves_published_video_and_all_project_views(store):
    project, render = _published(store)
    before = deepcopy(store.project_detail(project["id"]))
    job = _edit(store, project)
    queued = store.get_project(project["id"])
    assert queued["work_status"] == "queued"
    assert queued["result_status"] == "ready"
    assert queued["result_revision"] == render["revision"]
    assert store.claim_job(job["id"])
    store.update_job(job["id"], status="failed", error="本次方案未完成")
    store.set_project_status(project["id"], project["revision"], "failed")

    # A reopened database must display pre-existing failed edits correctly too.
    reopened = Store(store.db_path, store.media_root)
    views = [reopened.get_project(project["id"]), reopened.list_projects()[0],
             reopened.project_detail(project["id"])["project"],
             reopened.update_project_title(project["id"], "保留视频的项目")]
    for view in views:
        assert view["status"] == view["work_status"] == "failed"
        assert view["result_status"] == "ready"
        assert view["result_revision"] == render["revision"]
        assert view["current_render_id"] == render["id"]
        assert view["revision"] == project["revision"]
        assert view["plan"] == before["project"]["plan"]
    after = reopened.project_detail(project["id"])
    assert after["renders"] == before["renders"]
    assert after["assets"] == before["assets"]
    assert all(message in after["messages"] for message in before["messages"])
    assert reopened.get_job(job["id"])["status"] == "failed"


@pytest.mark.parametrize("status", ["planning", "awaiting_input", "awaiting_direction", "awaiting_creative",
                                    "partial", "cancel_requested", "canceled", "submitted_unknown"])
def test_edit_workflow_states_do_not_change_result_availability(store, status):
    project, render = _published(store)
    store.set_project_status(project["id"], project["revision"], status)
    view = store.get_project(project["id"])
    assert view["work_status"] == status
    assert view["result_status"] == "ready"
    assert view["result_revision"] == render["revision"]
    assert view["current_render_id"] == render["id"]


def test_new_plan_keeps_old_video_until_successful_publish(store):
    project, original = _published(store)
    updated = store.update_plan(project["id"], _plan("新方案"), expected_revision=project["revision"])
    assert updated["revision"] == project["revision"] + 1
    assert updated["work_status"] == "awaiting_generate"
    assert updated["result_status"] == "ready"
    assert updated["result_revision"] == original["revision"]
    assert updated["current_render_id"] == original["id"]

    job = store.create_job(project["id"], "generate", updated["revision"])
    assert store.claim_job(job["id"])
    asset = _render_asset(store, updated, "new.mp4")
    result = store.publish_render(job["id"], asset["id"], {"duration_seconds": 20})
    final = store.get_project(project["id"])
    assert final["work_status"] == final["result_status"] == "ready"
    assert final["current_render_id"] == result["id"]
    assert final["result_revision"] == updated["revision"]
    assert store.get_render(original["id"]) == original


def test_cancelled_edit_and_late_video_do_not_replace_published_result(store):
    project, original = _published(store)
    updated = store.update_plan(project["id"], _plan("新方案"))
    job = store.create_job(project["id"], "generate", updated["revision"])
    assert store.claim_job(job["id"])
    store.request_cancel(project["id"])
    asset = _render_asset(store, updated, "late.mp4")
    late = store.publish_render(job["id"], asset["id"], {})
    final = store.get_project(project["id"])
    assert late["status"] == "needs_review"
    assert final["current_render_id"] == original["id"]
    assert final["result_status"] == "ready"
    assert final["result_revision"] == original["revision"]
    assert store.get_job(job["id"])["status"] == "canceled"


def test_without_published_render_work_status_does_not_imply_result(store):
    project = store.create_project("蛋糕广告", "product")
    for status in ("draft", "queued", "awaiting_generate", "failed", "canceled", "ready"):
        store.set_project_status(project["id"], project["revision"], status)
        current = store.get_project(project["id"])
        assert current["work_status"] == status
        assert current["result_status"] == "none"
        assert current["result_revision"] is None
        assert current["current_render_id"] is None


def test_historical_render_without_current_pointer_is_not_promoted(store):
    project = store.create_project("蛋糕广告", "product")
    project = store.update_plan(project["id"], _plan())
    job = store.create_job(project["id"], "generate", project["revision"])
    assert store.claim_job(job["id"])
    store.request_cancel(project["id"])
    asset = _render_asset(store, project, "cancelled.mp4")
    historical = store.publish_render(job["id"], asset["id"], {})
    assert historical["status"] == "needs_review"
    current = store.project_detail(project["id"])["project"]
    assert current["result_status"] == "none"
    assert current["result_revision"] is None
    assert current["current_render_id"] is None
