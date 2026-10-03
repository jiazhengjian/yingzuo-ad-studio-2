"""Verify the real lean-draft route loads only its declared authoring method."""

import hashlib
import shutil

import pytest

from app import runtime_skills


SCRIPT = "ads-video-script-planning"
DIALOGUE = "ads-video-dialogue-writing"


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (
            "content_draft",
            {
                SCRIPT: ["SKILL.md", "references/content-draft.md"],
                DIALOGUE: ["SKILL.md", "references/ad-dialogue-examples.md"],
            },
        ),
        (
            "ad_script",
            {
                SCRIPT: ["SKILL.md", "references/timed-script.md"],
                DIALOGUE: [
                    "SKILL.md",
                    "references/speech-timing.md",
                    "references/speech-picture-alignment.md",
                    "references/ad-dialogue-examples.md",
                ],
            },
        ),
    ],
)
def test_actual_registry_routes_draft_format_to_its_method(stage, expected):
    context = runtime_skills.stage_skill_context(stage)
    assert [item["id"] for item in context["skills"]] == list(expected)
    assert [item["id"] for item in context["shared_references"]] == ["script-expression"]
    traces = runtime_skills.skill_trace(context)
    for skill, trace in zip(context["skills"], traces, strict=True):
        assert skill["loaded_sections"] == expected[skill["id"]]
        assert trace["stage"] == stage
        assert trace["execution_kind"] == "model_context"
        assert "content" not in trace
        assert trace["content_sha256"] == hashlib.sha256(skill["content"].encode()).hexdigest()
        assert [row["path"] for row in trace["consumed_resources"]] == skill["loaded_sections"]
        for row in trace["consumed_resources"]:
            source = runtime_skills.SKILLS_ROOT / skill["id"] / row["path"]
            assert row["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            assert source.read_text() in skill["content"]
        for row in trace["resources"]:
            if row["path"] not in skill["loaded_sections"]:
                source = runtime_skills.SKILLS_ROOT / skill["id"] / row["path"]
                assert source.read_text() not in skill["content"]


def test_timed_method_edits_do_not_change_lean_model_payload(tmp_path, monkeypatch):
    copied = tmp_path / "skills"
    shutil.copytree(runtime_skills.SKILLS_ROOT, copied)
    monkeypatch.setattr(runtime_skills, "SKILLS_ROOT", copied)
    lean_before = runtime_skills.stage_skill_context("content_draft")
    full_before = runtime_skills.stage_skill_context("ad_script")
    timed = copied / SCRIPT / "references/timed-script.md"
    timed.write_text(timed.read_text() + "\nTiming-only method revision.\n")
    lean_after = runtime_skills.stage_skill_context("content_draft")
    full_after = runtime_skills.stage_skill_context("ad_script")
    assert lean_before["content_sha256"] == lean_after["content_sha256"]
    assert lean_before["skills"][0]["content"] == lean_after["skills"][0]["content"]
    assert lean_before["skills"][0]["source_content_sha256"] != lean_after["skills"][0]["source_content_sha256"]
    assert full_before["content_sha256"] != full_after["content_sha256"]
    assert "Timing-only method revision." in full_after["skills"][0]["content"]


def test_lean_draft_does_not_replace_director_dialogue_method():
    context = runtime_skills.stage_skill_context("storyboard")
    dialogue = next(item for item in context["skills"] if item["id"] == DIALOGUE)
    assert dialogue["loaded_sections"] == ["SKILL.md", "references/ad-dialogue-examples.md"]
    assert SCRIPT not in {item["id"] for item in context["skills"]}
    assert any(item["id"] == "ads-video-storyboard-direction" for item in context["skills"])
