"""Script planning owns dialogue files; retired draft stages stay unbound."""

import hashlib
import shutil

import pytest

from app import runtime_skills


SCRIPT = "ads-video-script-planning"
AD_SCRIPT_SECTIONS = [
    "SKILL.md",
    "references/timed-script.md",
    "references/speech-timing.md",
    "references/speech-picture-alignment.md",
    "references/ad-dialogue-examples.md",
]


def test_ad_script_loads_only_script_planning_with_dialogue_references():
    context = runtime_skills.stage_skill_context("ad_script")
    assert [item["id"] for item in context["skills"]] == [SCRIPT]
    assert [item["id"] for item in context["shared_references"]] == ["script-expression"]
    skill = context["skills"][0]
    assert skill["loaded_sections"] == AD_SCRIPT_SECTIONS
    trace = runtime_skills.skill_trace(context)[0]
    assert trace["stage"] == "ad_script"
    assert trace["execution_kind"] == "model_context"
    assert "content" not in trace
    assert trace["content_sha256"] == hashlib.sha256(skill["content"].encode()).hexdigest()
    assert [row["path"] for row in trace["consumed_resources"]] == skill["loaded_sections"]
    for row in trace["consumed_resources"]:
        source = runtime_skills.SKILLS_ROOT / SCRIPT / row["path"]
        assert row["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert source.read_text() in skill["content"]


def test_retired_content_draft_stage_is_unbound():
    with pytest.raises(ValueError, match="尚未绑定"):
        runtime_skills.stage_skill_context("content_draft")


def test_timing_edits_do_not_change_line_rewrite_payload(tmp_path, monkeypatch):
    copied = tmp_path / "skills"
    shutil.copytree(runtime_skills.SKILLS_ROOT, copied)
    monkeypatch.setattr(runtime_skills, "SKILLS_ROOT", copied)
    narrow_before = runtime_skills.stage_skill_context("line_rewrite")
    full_before = runtime_skills.stage_skill_context("ad_script")
    timed = copied / SCRIPT / "references/speech-timing.md"
    timed.write_text(timed.read_text() + "\nTiming-only method revision.\n")
    narrow_after = runtime_skills.stage_skill_context("line_rewrite")
    full_after = runtime_skills.stage_skill_context("ad_script")
    assert narrow_before["content_sha256"] == narrow_after["content_sha256"]
    assert narrow_before["skills"][0]["content"] == narrow_after["skills"][0]["content"]
    assert narrow_before["skills"][0]["source_content_sha256"] != narrow_after["skills"][0]["source_content_sha256"]
    assert full_before["content_sha256"] != full_after["content_sha256"]
    assert "Timing-only method revision." in full_after["skills"][0]["content"]


def test_storyboard_loads_dialogue_examples_without_the_timing_references():
    context = runtime_skills.stage_skill_context("storyboard")
    script = next(item for item in context["skills"] if item["id"] == SCRIPT)
    assert script["loaded_sections"] == ["SKILL.md", "references/ad-dialogue-examples.md"]
    assert any(item["id"] == "ads-video-storyboard-direction" for item in context["skills"])


def test_registered_skills_match_the_consolidated_set():
    metadata = runtime_skills.registry_metadata()
    ids = [item["id"] for item in metadata["skills"]]
    assert ids == [
        "ads-video-reference-research",
        "ads-video-sound-direction",
        "ads-video-script-planning",
        "ads-video-storyboard-direction",
        "ads-video-asset-prompts",
        "ads-video-video-prompts",
        "ads-video-plan-review",
        "ads-video-video-review",
    ]
    pending = {item["id"] for item in metadata["skills"] if item["runtime_status"] == "pending"}
    assert pending == {"ads-video-reference-research", "ads-video-video-review"}
    for stage in (
        "reference_research", "video_review", "speech_draft", "content_draft",
        "legacy_storyboard", "script_review", "alignment_review", "orchestrator",
    ):
        with pytest.raises(ValueError, match="尚未绑定"):
            runtime_skills.stage_skill_context(stage)
