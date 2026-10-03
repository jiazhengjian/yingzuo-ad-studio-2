"""Load application-owned, versioned stage instructions and public provenance.

These files are product logic, not instructions uploaded by a merchant. No model
output or project field can select a file. Read on each request so the trace
identifies the bytes actually supplied, including edits made between turns.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


SKILLS_ROOT = Path(__file__).with_name("skills")


def _hash(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _json_hash(value) -> str:
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _read_resource(root: Path, resource: str) -> str:
    relative = Path(resource)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Skill 资源必须位于自身目录中")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Skill 资源不能指向目录外")
    return path.read_text(encoding="utf-8")


def _shared_resource(path: str) -> str:
    """Shared references are application files, never selectable skill roots."""
    relative = Path(path)
    if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2
            or relative.parts[0] != "shared"):
        raise ValueError("共用规则必须位于 shared/ 目录中")
    return str(Path(*relative.parts[1:]))


def _shared_reference(item: dict) -> dict:
    root = SKILLS_ROOT / "shared"
    if root.is_symlink():
        raise ValueError("共用规则目录不能使用符号链接")
    content = _read_resource(root, _shared_resource(item["path"]))
    return {**{key: item[key] for key in ("id", "path", "version")},
            "content": content, "content_sha256": _hash(content)}


def _registry() -> dict:
    data = json.loads((SKILLS_ROOT / "registry.json").read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("skills"), list):
        raise ValueError("Skill 注册表版本不支持")
    seen = set()
    for item in data["skills"]:
        identifier = item.get("id", "")
        if not re.fullmatch(r"ads-video-[a-z0-9]+(?:-[a-z0-9]+)*", identifier) or identifier in seen:
            raise ValueError("Skill ID 无效或重复")
        seen.add(identifier)
        if item.get("runtime_status") not in {"pending", "active"}:
            raise ValueError("Skill 运行状态无效")
        if item.get("mode") not in {"model", "review", "compiler"}:
            raise ValueError("Skill 执行模式无效")
        if not item.get("stages") or not item.get("version"):
            raise ValueError("Skill 缺少阶段或版本")
        if not item.get("display_name", "").startswith("Ads Video "):
            raise ValueError("Skill 显示名称须以 Ads Video 开头")
        resources = item.get("resources", [])
        if not resources or resources[0] != "SKILL.md" or len(resources) != len(set(resources)):
            raise ValueError("Skill 资源必须显式列出 SKILL.md 且不能重复")
        stage_resources = item.get("stage_resources", {})
        if not isinstance(stage_resources, dict):
            raise ValueError("Skill 分阶段资源映射无效")
        for stage, selected in stage_resources.items():
            if (stage not in item["stages"] or not isinstance(selected, list)
                    or not selected or selected[0] != "SKILL.md"
                    or any(not isinstance(path, str) or path not in resources for path in selected)
                    or len(selected) != len(set(selected))):
                raise ValueError("Skill 分阶段资源必须属于注册资源且包含 SKILL.md")
    references = data.get("shared_references", [])
    if not isinstance(references, list):
        raise ValueError("共用规则注册表无效")
    seen_references, seen_paths = set(), set()
    for item in references:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", item["id"])
                or item["id"] in seen_references or item["id"] in seen):
            raise ValueError("共用规则 ID 无效或重复")
        path, stages = item.get("path"), item.get("stages")
        if (not isinstance(path, str) or not isinstance(item.get("version"), str) or not item["version"].strip()
                or not isinstance(stages, list) or not stages
                or any(not isinstance(stage, str) or not stage for stage in stages)
                or len(stages) != len(set(stages))):
            raise ValueError("共用规则路径、阶段或版本无效")
        normalized_path = _shared_resource(path)
        if normalized_path in seen_paths:
            raise ValueError("共用规则路径不能重复")
        forms = item.get("ad_forms")
        if forms is not None and (not isinstance(forms, list) or not forms
                or any(form not in {"auto", "brand_tvc", "scene_story", "presenter_pitch"} for form in forms)
                or len(forms) != len(set(forms))):
            raise ValueError("共用规则的广告类型范围无效")
        seen_references.add(item["id"])
        seen_paths.add(normalized_path)
    return data


def _bundle(item: dict, *, include_documents: bool = False, stage: str | None = None,
            compiled_resource: str | None = None):
    root = SKILLS_ROOT / item["id"]
    if root.is_symlink():
        raise ValueError("Skill 目录不能使用符号链接")
    documents = [(name, _read_resource(root, name)) for name in item["resources"]]
    main = documents[0][1]
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", main, re.S)
    if not frontmatter:
        raise ValueError("Skill 缺少 YAML frontmatter")
    name = re.search(r"^name:\s*[\"']?([a-z0-9-]+)[\"']?\s*$", frontmatter[1], re.M)
    if not name or name[1] != item["id"] or not re.search(r"^description:\s*\S", frontmatter[1], re.M):
        raise ValueError("Skill 名称或说明与注册表不一致")
    # Hash every registered source from one read snapshot, while sending only
    # this stage's declared resources. An unrelated review mode stays readable
    # on disk without silently being included in the current model request.
    source_content = "\n\n".join(f"<!-- {name} -->\n{body}" for name, body in documents)
    selected = ([compiled_resource] if compiled_resource is not None else
                item.get("stage_resources", {}).get(stage, item["resources"]))
    document_map = dict(documents)
    content = (document_map[compiled_resource] if compiled_resource is not None else
               "\n\n".join(f"<!-- {name} -->\n{document_map[name]}" for name in selected))
    bundle = {
        "id": item["id"], "display_name": item["display_name"], "version": item["version"],
        "mode": item["mode"], "runtime_status": item["runtime_status"],
        "resources": [{"path": name, "sha256": _hash(body)} for name, body in documents],
        "content": content, "content_sha256": _hash(content),
        "source_content_sha256": _hash(source_content), "loaded_sections": list(selected),
        "consumed_resources": [{"path": name, "sha256": _hash(document_map[name])} for name in selected],
        "execution_kind": "template_compilation" if compiled_resource is not None else "model_context",
        "content_hash_scope": "template" if compiled_resource is not None else "model_context",
    }
    return (bundle, dict(documents)) if include_documents else bundle


def stage_skill_context(stage: str, mode: str | None = None, *, playbook_form: str | None = None) -> dict:
    """Return model instructions; media compilers must use compiler_template.

    A playbook is injected only for the requested form. Omitting the form keeps
    the historical shared set, so checkpoints of untyped briefs stay stable.
    """
    if mode is not None and mode not in {"model", "review"}:
        raise ValueError("模型阶段只支持 model/review；素材模板须通过 compiler_template 编译")
    registry = _registry()
    skills = [_bundle(item, stage=stage) for item in registry["skills"]
              if stage in item["stages"] and item["runtime_status"] == "active"
              and item["mode"] in {"model", "review"}
              and (mode is None or item["mode"] == mode)]
    if not skills:
        raise ValueError(f"阶段 {stage} 尚未绑定运行 Skill")
    shared = [_shared_reference(item) for item in registry.get("shared_references", [])
              if stage in item["stages"]
              and (not item.get("ad_forms") or playbook_form in item["ad_forms"])]
    return {"schema_version": 1, "stage": stage, "skills": skills, "shared_references": shared,
            "content_sha256": _json_hash({
                "skills": [(s["id"], s["version"], s["content_sha256"]) for s in skills],
                "shared_references": [(r["id"], r["path"], r["version"], r["content_sha256"]) for r in shared],
            })}


def skill_trace(context: dict) -> list[dict]:
    """Safe public provenance. Never include prompt contents or model reasoning."""
    stage_context = context.get("stage_skills", context)
    return [{"stage": stage_context["stage"], **{key: item[key] for key in
             ("id", "display_name", "version", "mode", "resources", "content_sha256",
              "source_content_sha256", "loaded_sections", "consumed_resources",
              "execution_kind", "content_hash_scope") if key in item}}
            for item in stage_context["skills"]]


def shared_reference_trace(context: dict) -> list[dict]:
    """Trace shared model context separately from skill loads or invocations."""
    stage_context = context.get("stage_skills", context)
    return [{"stage": stage_context["stage"],
             **{key: item[key] for key in ("id", "path", "version", "content_sha256")},
             "execution_kind": "shared_reference", "content_hash_scope": "shared_reference"}
            for item in stage_context.get("shared_references", [])]


def compiler_template(skill_id: str, resource: str) -> tuple[str, dict]:
    """Load a media template; do not forward SKILL frontmatter to a media model."""
    item = next((item for item in _registry()["skills"] if item["id"] == skill_id), None)
    if item is None or item["mode"] != "compiler" or item["runtime_status"] != "active":
        raise ValueError("素材编译 Skill 尚未接入")
    if resource == "SKILL.md" or resource not in item["resources"]:
        raise ValueError("素材模板未列入 Skill 资源清单")
    bundle, documents = _bundle(item, include_documents=True, compiled_resource=resource)
    trace = skill_trace({"stage": item["stages"][0], "skills": [bundle]})[0]
    trace["compiled_resource"] = resource
    return documents[resource], trace


def registry_metadata() -> dict:
    """Describe registered sources and availability, never evidence of execution."""
    registry = _registry()
    skills = []
    for item in registry["skills"]:
        source_hash = _bundle(item)["source_content_sha256"]
        skills.append({
            **{key: item[key] for key in ("id", "display_name", "version", "runtime_status", "stages", "mode")},
            "availability": "available" if item["runtime_status"] == "active" else "pending",
            "execution_kind": "template_compilation" if item["mode"] == "compiler" else "model_context",
            # Keep the former metadata hash as a source-hash alias. A registry
            # read has no model request or compiled template to attest to.
            "content_sha256": source_hash, "source_content_sha256": source_hash,
            "content_hash_scope": "registered_sources",
        })
    shared = []
    for item in registry.get("shared_references", []):
        reference = _shared_reference(item)
        shared.append({**{key: reference[key] for key in ("id", "path", "version", "content_sha256")},
                       "stages": item["stages"], "availability": "available",
                       "content_hash_scope": "registered_source"})
    return {"registry_version": registry["schema_version"],
            "evidence_kind": "registration",
            "registry_sha256": _json_hash({"registry": registry, "skills": skills, "shared_references": shared}),
            "skills": skills, "shared_references": shared}
