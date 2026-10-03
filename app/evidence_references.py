"""Short, request-local merchant citations; saved facts keep canonical IDs.

Only the already-filtered evidence registry is eligible. Quotes are never
rewritten or matched approximately, and unknown references remain invalid.
"""
from copy import deepcopy


EVIDENCE_REFERENCE_VERSION = 1


def _translate_facts(value, mapping):
    if isinstance(value, dict):
        result = {key: _translate_facts(item, mapping) for key, item in value.items()}
        if value.get("source_kind") == "merchant" and isinstance(value.get("source_id"), str):
            result["source_id"] = mapping.get(value["source_id"], value["source_id"])
        return result
    if isinstance(value, list):
        return [_translate_facts(item, mapping) for item in value]
    return value


def strategy_evidence_request(context):
    """Return a detached wire context and its exact alias-to-ID mapping."""
    registry = context.get("evidence_sources")
    if context.get("stage") != "strategy" or not context.get("stage_skills") or not isinstance(registry, dict):
        return deepcopy(context), {}
    if not registry or not all(isinstance(key, str) and isinstance(text, str) for key, text in registry.items()):
        return deepcopy(context), {}
    aliases, number = {}, 1
    for canonical in sorted(registry):
        while f"merchant_{number}" in registry:
            number += 1
        aliases[f"merchant_{number}"] = canonical
        number += 1
    inverse = {canonical: alias for alias, canonical in aliases.items()}
    wire = _translate_facts(context, inverse)
    wire["evidence_sources"] = {alias: registry[canonical] for alias, canonical in aliases.items()}
    # Feedback contains reference lists rather than fact objects. Keep those
    # lists in the same request-local namespace as the source registry.
    for issue in wire.get("validation_feedback", []):
        for field in ("allowed_source_ids", "matching_source_ids"):
            if isinstance(issue.get(field), list):
                issue[field] = [inverse.get(item, item) for item in issue[field]]
    wire["evidence_reference_contract"] = {
        "version": EVIDENCE_REFERENCE_VERSION,
        "instruction": "merchant事实的source_id从本请求evidence_sources的短编号中准确选择。程序会恢复真实来源编号，无需复制历史消息的长编号。text逐字摘录该编号对应的连续原文，保留原文错别字，不增字、润色或拼接；创意与台词可以另行组织，事实引文不能改写。"}
    return wire, aliases


def restore_merchant_references(result, aliases):
    return _translate_facts(result, aliases) if aliases else deepcopy(result)
