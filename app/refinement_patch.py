"""Sparse scoped edits merged into an exact draft, before all existing guards.

This is an output adapter, not a content validator or a repair generator. It
never invents values, changes identities, clears omitted fields or applies
defaults. A composed draft still needs canonical, scope, timing, lock, compile
and independent review checks at the caller.
"""
from __future__ import annotations

from copy import deepcopy


REFINEMENT_OUTPUT_VERSION = 2
_SUMMARY = ("core_message", "audience_takeaway", "product_role")
_BEAT = ("purpose", "message", "visual_role", "narration", "evidence_ids",
         "boundary_before", "continuity_group", "timing")
_TIMING = ("visual_seconds", "speech_overlap", "action_plan", "start_state", "end_state", "time_transition")
_SHOT = ("coverage", "description", "purpose", "evidence_ids", "character_ids", "continuity")
_CONTINUITY = ("start_state", "end_state", "allowed_changes", "camera_move", "entry_transition", "entry_reason")
_STATES = ("start_state", "end_state")


SPARSE_REFINEMENT_INSTRUCTION = """本次返修优先返回schema的patch分支，这一输出合同替代通用Skill中的完整稿输出要求。
patch.beats与patch.shots只列实际修改的既有ID，每项为{id,set}；set仅列要修改的字段。timing、continuity也只列修改的子字段，数组若修改须给该字段完整新数组。未列字段由程序从本次draft_storyboard原样保留，不代表省略审核；不可用null删除字段，不得写未知ID、改ID或越过repair_scope。相邻未受影响内容只可改允许的start_state/end_state。
只有scope允许时才写patch.summary。要新增、删除或重排镜头，或在allow_beat_restructure=true时重组内容拍，使用同一schema的完整ad_script/shots分支；不得混合两种分支。允许全局重组不代表必须重组：仅调整现有内容或时长时仍用patch，patch本身不能增删或重排任何内容拍、镜头。两种分支都须遵守原范围、固定台词、容量与估时守卫，合成后完整编译并独立审核，不输出通过声明，不只降低估时。"""


def _identities(rows):
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in rows):
        raise ValueError("稀疏返修须基于完整的既有内容身份")
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("稀疏返修的原稿身份不能重复")
    return result


def _scope_ids(scope, key, known):
    ids = scope.get(key, [])
    if (not isinstance(ids, list) or any(not isinstance(item, str) for item in ids)
            or len(ids) != len(set(ids)) or not set(ids).issubset(known)):
        raise ValueError("稀疏返修范围必须唯一对应既有内容")
    return ids


def _permissions(before, scope):
    beats = _identities(before["ad_script"]["beats"])
    shots = _identities(before["shots"])
    allowed = {}
    for collection, known, main_key, adjacent_key in (
            ("beats", beats, "beat_ids", "adjacent_state_beat_ids"),
            ("shots", shots, "shot_ids", "adjacent_state_shot_ids")):
        main = _scope_ids(scope, main_key, known)
        adjacent = [item for item in _scope_ids(scope, adjacent_key, known) if item not in main]
        allowed[collection] = (main, adjacent)
    return allowed


def sparse_refinement_compatible(current, scope):
    """Keep full output after a prior full candidate changed scoped identities.

The original scope is never widened or rewritten. A complete candidate can
legitimately replace affected shots, then hit the existing estimate guard. Its
next attempt still validates against the original baseline; it just cannot use
an old-ID patch contract to describe the new current shot identities.
"""
    for rows, keys in ((current["ad_script"]["beats"], ("beat_ids", "adjacent_state_beat_ids")),
                       (current["shots"], ("shot_ids", "adjacent_state_shot_ids"))):
        known = _identities(rows)
        for key in keys:
            ids = scope.get(key, [])
            if (not isinstance(ids, list) or any(not isinstance(item, str) for item in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError("稀疏返修范围必须唯一对应既有内容")
            if not set(ids).issubset(known):
                return False
    return True


def _partial(definitions, definition, fields):
    # Reuse the existing typed leaves instead of sending their long business
    # descriptions again for every editable/adjacent group. JSON Schema
    # defaults are annotations; the adapter never applies them to omissions.
    properties = definitions[definition]["properties"]
    if not set(fields).issubset(properties):
        raise ValueError("稀疏返修字段与完整合同不一致")
    selected = {key: {"$ref": f"#/$defs/{definition}/properties/{key}"} for key in fields}
    return {"type": "object", "properties": selected, "additionalProperties": False, "minProperties": 1}


def sparse_refinement_schema(full_schema, before, scope):
    """Same request may use sparse updates or the original complete contract."""
    allowed = _permissions(before, scope)
    full = deepcopy(full_schema)
    definitions = full.pop("$defs")
    fields = {}
    for collection, definition, editable, nested, nested_definition, nested_fields in (
            ("beats", "AdBeat", _BEAT, "timing", "BeatTiming", _TIMING),
            ("shots", "DirectedShot", _SHOT, "continuity", "ShotContinuity", _CONTINUITY)):
        variants = []
        main, adjacent = allowed[collection]
        for ids, adjacent_only in ((main, False), (adjacent, True)):
            if not ids:
                continue
            changes = _partial(definitions, definition, (nested,) if adjacent_only else editable)
            changes["properties"][nested] = _partial(definitions, nested_definition,
                                                     _STATES if adjacent_only else nested_fields)
            variants.append({"type": "object", "properties": {
                "id": {"type": "string", "enum": ids}, "set": changes},
                "required": ["id", "set"], "additionalProperties": False})
        if variants:
            fields[collection] = {"type": "array", "minItems": 1, "maxItems": len(main) + len(adjacent),
                "items": variants[0] if len(variants) == 1 else {"oneOf": variants}}
    if scope.get("allow_summary_update") is True:
        fields["summary"] = _partial(definitions, "AdScript", _SUMMARY)
    patch = {"type": "object", "properties": fields, "additionalProperties": False, "minProperties": 1}
    return {"oneOf": [
        {"type": "object", "properties": {"patch": patch}, "required": ["patch"], "additionalProperties": False},
        full], "$defs": definitions}


def _object(value, keys):
    if not isinstance(value, dict) or not value or not set(value).issubset(keys):
        raise ValueError("稀疏返修包含空修改、未知字段或超出范围的字段")
    return value


def _has_null(value):
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_null(item) for item in value)
    return False


def compose_sparse_refinement(raw, current, scope):
    """Apply only explicit, allowed edits to the exact draft sent this request.

Types, bounds and semantic invariants are checked by the original canonical
and full-plan validators after this adapter. A missing field is inherited;
explicit null/unknown fields are rejected, never discarded or defaulted.
"""
    if not isinstance(raw, dict) or set(raw) != {"patch"} or _has_null(raw):
        raise ValueError("稀疏返修须只包含patch，不能混入完整稿或使用null")
    permissions = _permissions(current, scope)
    allowed_keys = {name for name, (main, adjacent) in permissions.items() if main or adjacent}
    if scope.get("allow_summary_update") is True:
        allowed_keys.add("summary")
    patch = _object(raw["patch"], allowed_keys)
    result = deepcopy(current)
    if "summary" in patch:
        result["ad_script"].update(deepcopy(_object(patch["summary"], _SUMMARY)))
    for collection, editable, nested, nested_fields in (
            ("beats", _BEAT, "timing", _TIMING), ("shots", _SHOT, "continuity", _CONTINUITY)):
        if collection not in patch:
            continue
        rows = patch[collection]
        main, adjacent = permissions[collection]
        if not isinstance(rows, list) or not rows or len(rows) > len(main) + len(adjacent):
            raise ValueError("稀疏返修列表须包含范围内的既有修改")
        targets = _identities(result["ad_script"]["beats"] if collection == "beats" else result["shots"])
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "set"} or not isinstance(row["id"], str):
                raise ValueError("稀疏返修每项须明确给出既有id与set")
            identity = row["id"]
            if identity in seen or identity not in main + adjacent:
                raise ValueError("稀疏返修不能重复、虚构或修改范围外的身份")
            seen.add(identity)
            adjacent_only = identity not in main
            updates = _object(row["set"], (nested,) if adjacent_only else editable)
            target = targets[identity]
            for key, value in updates.items():
                if key == nested:
                    updates_nested = _object(value, _STATES if adjacent_only else nested_fields)
                    if not isinstance(target.get(key), dict):
                        raise ValueError("稀疏返修不能补造原稿缺失的执行结构")
                    target[key].update(deepcopy(updates_nested))
                else:
                    target[key] = deepcopy(value)
    if result == current:
        raise ValueError("稀疏返修没有产生实际修改")
    return result
