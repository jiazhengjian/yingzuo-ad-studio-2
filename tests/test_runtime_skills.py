"""Stage isolation and honest provenance for the product's instruction files."""
import hashlib
import json

import pytest

from app import runtime_skills


@pytest.fixture
def skill_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_skills, "SKILLS_ROOT", tmp_path)
    entries = []
    for suffix, stage, status in (("script-planning", "ad_script", "active"),
                                  ("dialogue-writing", "ad_script", "active"),
                                  ("reference-research", "reference_research", "pending")):
        identifier = "ads-video-" + suffix
        directory = tmp_path / identifier
        directory.mkdir()
        (directory / "SKILL.md").write_text(
            f"---\nname: {identifier}\ndescription: Test stage rules\n---\nStage {suffix}.\n")
        entries.append({"id": identifier, "display_name": "Ads Video " + suffix,
                        "version": "1.0.0", "stages": [stage], "mode": "model",
                        "resources": ["SKILL.md"], "runtime_status": status})
    registry = {"schema_version": 1, "skills": entries}
    (tmp_path / "registry.json").write_text(json.dumps(registry))
    return tmp_path, registry


def test_composed_stage_only_loads_its_own_rules(skill_registry):
    context = runtime_skills.stage_skill_context("ad_script")
    assert [item["id"] for item in context["skills"]] == [
        "ads-video-script-planning", "ads-video-dialogue-writing"]
    trace = runtime_skills.skill_trace({"stage_skills": context})
    assert len(trace) == 2
    assert all(item["stage"] == "ad_script" and "content" not in item for item in trace)
    assert all(item["execution_kind"] == "model_context" for item in trace)
    assert "Test stage rules" not in json.dumps(trace)
    with pytest.raises(ValueError, match="尚未绑定"):
        runtime_skills.stage_skill_context("reference_research")


def test_edits_change_actual_content_and_trace_without_restart(skill_registry):
    root, _ = skill_registry
    before = runtime_skills.stage_skill_context("ad_script")
    path = root / "ads-video-script-planning" / "SKILL.md"
    path.write_text(path.read_text() + "Revised advertising rule.\n")
    after = runtime_skills.stage_skill_context("ad_script")
    assert before["content_sha256"] != after["content_sha256"]
    assert before["skills"][0]["resources"] != after["skills"][0]["resources"]
    assert "Revised advertising rule" in after["skills"][0]["content"]


@pytest.mark.parametrize("resource", ["../secret.txt", "/secret.txt", "link.txt"])
def test_registry_cannot_read_outside_skill(skill_registry, tmp_path, resource):
    root, registry = skill_registry
    directory = root / registry["skills"][0]["id"]
    (directory / "link.txt").symlink_to(root / "registry.json")
    registry["skills"][0]["resources"].append(resource)
    (root / "registry.json").write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="目录"):
        runtime_skills.stage_skill_context("ad_script")


def test_broken_frontmatter_fails_instead_of_silent_empty_prompt(skill_registry):
    root, _ = skill_registry
    (root / "ads-video-script-planning" / "SKILL.md").write_text("missing frontmatter")
    with pytest.raises(ValueError, match="frontmatter"):
        runtime_skills.stage_skill_context("ad_script")


def test_compiler_only_sends_registered_template_not_skill_metadata(skill_registry):
    root, registry = skill_registry
    entry = registry["skills"][0]
    entry["mode"] = "compiler"
    entry["resources"].append("template.txt")
    (root / entry["id"] / "template.txt").write_text("Exact approved media instructions.")
    (root / "registry.json").write_text(json.dumps(registry))
    text, trace = runtime_skills.compiler_template(entry["id"], "template.txt")
    assert text == "Exact approved media instructions."
    assert trace["compiled_resource"] == "template.txt"
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert trace["content_sha256"] == digest
    assert trace["loaded_sections"] == ["template.txt"]
    assert trace["consumed_resources"] == [{"path": "template.txt", "sha256": digest}]
    assert trace["execution_kind"] == "template_compilation"
    assert trace["content_hash_scope"] == "template"
    assert {row["path"] for row in trace["resources"]} == {"SKILL.md", "template.txt"}
    assert "content" not in trace
    with pytest.raises(ValueError):
        runtime_skills.compiler_template(entry["id"], "SKILL.md")


def test_health_metadata_does_not_claim_pending_skill_was_executed(skill_registry):
    metadata = runtime_skills.registry_metadata()
    research = next(s for s in metadata["skills"] if s["id"].endswith("reference-research"))
    assert research["runtime_status"] == "pending"
    assert metadata["evidence_kind"] == "registration"
    assert research["availability"] == "pending" and research["mode"] == "model"
    assert research["execution_kind"] == "model_context"
    assert research["content_hash_scope"] == "registered_sources"
    assert research["content_sha256"] == research["source_content_sha256"]
    assert all("loaded_sections" not in item and "consumed_resources" not in item for item in metadata["skills"])
    assert all("content" not in item for item in metadata["skills"])


def test_compiler_trace_and_text_share_one_read_snapshot(skill_registry, monkeypatch):
    root, registry = skill_registry
    entry = registry["skills"][0]
    entry.update(mode="compiler", resources=["SKILL.md", "template.txt"])
    path = root / entry["id"] / "template.txt"
    path.write_text("before edit")
    (root / "registry.json").write_text(json.dumps(registry))
    original = runtime_skills._bundle

    def edit_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_text("after edit")
        return result

    monkeypatch.setattr(runtime_skills, "_bundle", edit_after_read)
    text, trace = runtime_skills.compiler_template(entry["id"], "template.txt")
    recorded = next(row["sha256"] for row in trace["resources"] if row["path"] == "template.txt")
    assert text == "before edit"
    assert hashlib.sha256(text.encode()).hexdigest() == recorded
    assert trace["content_sha256"] == recorded


def test_compiler_cannot_enter_model_context_even_when_stage_matches(skill_registry):
    root, registry = skill_registry
    registry["skills"][0]["mode"] = "compiler"
    (root / "registry.json").write_text(json.dumps(registry))
    context = runtime_skills.stage_skill_context("ad_script")
    assert [item["id"] for item in context["skills"]] == ["ads-video-dialogue-writing"]
    with pytest.raises(ValueError, match="compiler_template"):
        runtime_skills.stage_skill_context("ad_script", mode="compiler")
    registry["skills"][1]["mode"] = "compiler"
    (root / "registry.json").write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="尚未绑定"):
        runtime_skills.stage_skill_context("ad_script")
    metadata = runtime_skills.registry_metadata()["skills"][0]
    assert metadata["mode"] == "compiler" and metadata["execution_kind"] == "template_compilation"
    assert metadata["availability"] == "available"


def test_compiler_source_edits_do_not_claim_template_bytes_changed(skill_registry):
    root, registry = skill_registry
    entry = registry["skills"][0]
    entry.update(mode="compiler", resources=["SKILL.md", "template.txt"])
    directory = root / entry["id"]
    (directory / "template.txt").write_text("unchanged template")
    (root / "registry.json").write_text(json.dumps(registry))
    before_text, before = runtime_skills.compiler_template(entry["id"], "template.txt")
    main = directory / "SKILL.md"
    main.write_text(main.read_text() + "\nUpdated source-only documentation.")
    after_text, after = runtime_skills.compiler_template(entry["id"], "template.txt")
    assert before_text == after_text
    assert before["content_sha256"] == after["content_sha256"]
    assert before["consumed_resources"] == after["consumed_resources"]
    assert before["source_content_sha256"] != after["source_content_sha256"]
    assert before["resources"] != after["resources"]


def test_model_trace_separates_selected_resources_from_registered_sources(skill_registry):
    root, registry = skill_registry
    entry = registry["skills"][0]
    entry.update(resources=["SKILL.md", "selected.md", "other-stage.md"],
                 stage_resources={"ad_script": ["SKILL.md", "selected.md"]})
    directory = root / entry["id"]
    (directory / "selected.md").write_text("Selected rules")
    other = directory / "other-stage.md"
    other.write_text("Unselected rules")
    (root / "registry.json").write_text(json.dumps(registry))
    before = runtime_skills.stage_skill_context("ad_script")["skills"][0]
    other.write_text("Updated unselected rules")
    after = runtime_skills.stage_skill_context("ad_script")["skills"][0]
    assert before["loaded_sections"] == ["SKILL.md", "selected.md"]
    assert [row["path"] for row in before["consumed_resources"]] == before["loaded_sections"]
    assert "Unselected" not in before["content"]
    assert before["content_sha256"] == hashlib.sha256(before["content"].encode()).hexdigest()
    assert before["content_sha256"] == after["content_sha256"]
    assert before["source_content_sha256"] != after["source_content_sha256"]


def add_shared_reference(root, registry):
    directory = root / "shared"
    directory.mkdir()
    path = directory / "product-evidence.md"
    path.write_text("Shared product evidence boundaries.")
    registry["shared_references"] = [{"id": "product-evidence", "path": "shared/product-evidence.md",
                                      "version": "1.0.0", "stages": ["ad_script"]}]
    registry["skills"][0]["stages"].append("direction_review")
    (root / "registry.json").write_text(json.dumps(registry))
    return path


def test_shared_rules_are_injected_once_per_stage_and_traced_separately(skill_registry):
    root, registry = skill_registry
    path = add_shared_reference(root, registry)
    context = runtime_skills.stage_skill_context("ad_script")
    assert len(context["skills"]) == 2
    assert len(context["shared_references"]) == 1
    assert json.dumps(context).count(path.read_text()) == 1
    assert all("Shared product evidence" not in item["content"] for item in context["skills"])
    trace = runtime_skills.shared_reference_trace({"stage_skills": context})
    assert trace == [{"stage": "ad_script", "id": "product-evidence", "path": "shared/product-evidence.md",
                      "version": "1.0.0", "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "execution_kind": "shared_reference", "content_hash_scope": "shared_reference"}]
    assert len(runtime_skills.skill_trace(context)) == 2
    other = runtime_skills.stage_skill_context("direction_review")
    assert other["shared_references"] == []
    assert runtime_skills.shared_reference_trace(other) == []
    assert runtime_skills.shared_reference_trace({"stage": "historical", "skills": []}) == []


def test_shared_edits_change_selected_stage_hash_and_registry_metadata(skill_registry):
    root, registry = skill_registry
    path = add_shared_reference(root, registry)
    before = runtime_skills.stage_skill_context("ad_script")
    other_before = runtime_skills.stage_skill_context("direction_review")
    metadata_before = runtime_skills.registry_metadata()
    path.write_text("Updated evidence boundaries.")
    after = runtime_skills.stage_skill_context("ad_script")
    other_after = runtime_skills.stage_skill_context("direction_review")
    metadata_after = runtime_skills.registry_metadata()
    assert before["content_sha256"] != after["content_sha256"]
    assert before["skills"] == after["skills"]
    assert other_before == other_after
    assert metadata_before["registry_sha256"] != metadata_after["registry_sha256"]
    reference = metadata_after["shared_references"][0]
    assert reference["content_hash_scope"] == "registered_source"
    assert reference["availability"] == "available"
    assert "content" not in reference and "loaded_sections" not in reference


@pytest.mark.parametrize("path", ["../secret.txt", "/secret.txt", "other/rules.md",
                                "shared/../../secret.txt", "shared/link.md"])
def test_shared_references_cannot_escape_shared_directory(skill_registry, path):
    root, registry = skill_registry
    add_shared_reference(root, registry)
    (root / "shared" / "link.md").symlink_to(root / "registry.json")
    registry["shared_references"][0]["path"] = path
    (root / "registry.json").write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="目录"):
        runtime_skills.stage_skill_context("ad_script")


@pytest.mark.parametrize("duplicate", ["id", "path"])
def test_shared_registry_rejects_duplicate_injection(skill_registry, duplicate):
    root, registry = skill_registry
    add_shared_reference(root, registry)
    item = dict(registry["shared_references"][0])
    if duplicate == "path":
        item.update(id="another-reference", path="shared/./product-evidence.md")
    registry["shared_references"].append(item)
    (root / "registry.json").write_text(json.dumps(registry))
    with pytest.raises(ValueError, match="重复"):
        runtime_skills.stage_skill_context("ad_script")
