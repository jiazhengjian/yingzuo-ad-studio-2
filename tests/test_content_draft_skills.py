"""Script planning owns dialogue files; retired draft stages stay unbound."""

import hashlib
import shutil
from pathlib import Path

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


LIPSYNC_CLAIM = "没看没听就不能声称口型已经验证"
BRAND_SENTENCE_COUNT = "大约 15 到 20 秒、2 到 3 句"


def test_unseen_lipsync_claim_is_sent_only_inside_plan_review():
    context = runtime_skills.stage_skill_context("plan_review")
    skill = next(item for item in context["skills"] if item["id"] == "ads-video-plan-review")
    assert skill["loaded_sections"] == ["SKILL.md", "references/plan-review.md"]
    assert skill["content"].count(LIPSYNC_CLAIM) == 1
    marker = "<!-- references/plan-review.md -->"
    assert marker in skill["content"]
    assert LIPSYNC_CLAIM in skill["content"].split(marker, 1)[1]
    assert LIPSYNC_CLAIM not in skill["content"].split(marker, 1)[0]
    direction = runtime_skills.stage_skill_context("direction_review")
    direction_skill = next(item for item in direction["skills"] if item["id"] == "ads-video-plan-review")
    assert "references/plan-review.md" not in direction_skill["loaded_sections"]
    assert LIPSYNC_CLAIM not in direction_skill["content"]
    for reference in context["shared_references"]:
        assert LIPSYNC_CLAIM not in reference["content"]
    hits = []
    for folder in (Path("app"), Path("docs")):
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".py", ".json", ".txt"} and LIPSYNC_CLAIM in path.read_text():
                hits.append(path.as_posix())
    assert hits == ["app/skills/ads-video-plan-review/references/plan-review.md"]


def test_model_prompts_do_not_keep_a_shorter_brand_duration():
    from app.ad_script import AD_SCRIPT_STAGE_SYSTEM
    from app.line_quality import LINE_REWRITE_SYSTEM, line_style
    from app.planning import FILM_CONCEPT_SYSTEM, STRATEGY_SELECTION_SYSTEM, STRATEGY_SYSTEM
    from app.progressive_planning import DIRECTOR_SYSTEM, PLAN_REVIEW_SYSTEM
    from app.speech_draft import SPEECH_DRAFT_SYSTEM

    prompts = {
        "director": DIRECTOR_SYSTEM,
        "line_style": line_style("brand_tvc"),
        "line_rewrite": LINE_REWRITE_SYSTEM,
        "speech_draft": SPEECH_DRAFT_SYSTEM,
        "ad_script": AD_SCRIPT_STAGE_SYSTEM,
        "plan_review": PLAN_REVIEW_SYSTEM,
        "strategy": STRATEGY_SYSTEM,
        "strategy_selection": STRATEGY_SELECTION_SYSTEM,
        "film_concept": FILM_CONCEPT_SYSTEM,
    }
    assert "产品片时长和句数按类型 playbook" in prompts["director"]
    assert "产品片时长和句数按类型 playbook" in prompts["line_style"]
    assert "时长和句数按类型 playbook" in prompts["line_rewrite"]
    assert "产品片时长和句数按类型 playbook" in prompts["speech_draft"]
    for name, text in prompts.items():
        assert "大约15秒" not in text and "大约 15 秒" not in text, name
        assert BRAND_SENTENCE_COUNT not in text, name
    hits = []
    for path in Path("app").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".md", ".txt"}:
            text = path.read_text()
            if "大约15秒" in text or "大约 15 秒" in text:
                hits.append(path.as_posix())
    assert hits == []


def test_brand_voiceover_stays_fifteen_to_twenty_seconds_in_one_place():
    context = runtime_skills.stage_skill_context("plan_review", playbook_form="brand_tvc")
    brand = next(item for item in context["shared_references"] if item["id"] == "ad-form-playbook-brand-tvc")
    assert BRAND_SENTENCE_COUNT in brand["content"]
    assert "大约 15 秒说" not in brand["content"]
    hits = []
    for path in Path("app/skills").rglob("*"):
        if path.is_file() and path.suffix in {".md", ".txt"} and BRAND_SENTENCE_COUNT in path.read_text():
            hits.append(path.as_posix())
    assert hits == ["app/skills/shared/ad-form-playbooks/brand-tvc.md"]
    script = runtime_skills.stage_skill_context("ad_script", playbook_form="brand_tvc")
    examples = next(item for item in script["skills"] if item["id"] == "ads-video-script-planning")
    assert BRAND_SENTENCE_COUNT not in examples["content"]
    assert "大约 15 秒说" not in examples["content"]


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
