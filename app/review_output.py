"""Smaller review output contracts without synthesizing review evidence.

Only explanations on explicitly successful checks may be omitted. Verdicts,
identities, numeric estimates, aggregate conclusions and source quotations stay
in the canonical shape. The response needs no decoder or invented positive
note: existing conservative lowering and canonical validation still run.
"""
from __future__ import annotations

from copy import deepcopy


REVIEW_OUTPUT_VERSION = "2"

# Explicitly registered review definitions only: an arbitrary field named note
# in a plan, an evidence object or another schema is not an optimization target.
_SUCCESS_FIELDS = {
    "ScriptCheck": ("passed",),
    "ScriptExecutionCheck": ("action_capacity", "state_continuity"),
    "AVSpanCheck": ("passed",),
    "AVShotCheck": ("passed",),
    "AVShotPacing": ("action_timing_passed", "cut_necessity_passed"),
}


def compact_review_schema(schema: dict) -> dict:
    """Allow missing success notes, require authored notes for every rejection.

The canonical models also validate this condition after failure lowering. A
nominally passing item rejected on semantic grounds cannot acquire a fabricated
note; bounded format repair must supply its authored reason. The deterministic
action-window guard may attach its explicitly labeled numeric calculation when
it alone establishes a new rejection; it never invents a semantic explanation.
Provided notes remain nonempty strings on the wire. Existing full responses
remain valid; missing/null verdicts and missing rows are never repaired here.
"""
    result = deepcopy(schema)
    for name, success_fields in _SUCCESS_FIELDS.items():
        definition = result.get("$defs", {}).get(name)
        if definition is None:
            continue
        properties = definition.get("properties", {})
        if (not isinstance(properties.get("note"), dict)
                or any(properties.get(field, {}).get("type") != "boolean"
                       for field in success_fields)):
            raise ValueError(f"Unsupported review output definition: {name}")
        note = properties["note"]
        if "anyOf" in note:
            text_options = [option for option in note["anyOf"] if option.get("type") == "string"]
            if len(text_options) != 1 or any(option.get("type") not in ("string", "null")
                                              for option in note["anyOf"]):
                raise ValueError(f"Unsupported review note contract: {name}")
            text_schema = text_options[0]
            note.pop("anyOf")
            note.update(text_schema)
        if note.get("type") != "string" or note.get("minLength", 0) < 1:
            raise ValueError(f"Review notes must retain nonempty authored text: {name}")
        note.pop("default", None)
        definition["required"] = [field for field in definition.get("required", []) if field != "note"]
        # Unless all explicit success fields are true, note is required. The
        # original required fields/types independently reject missing verdicts.
        conditional = {
            "if": {"properties": {field: {"const": True} for field in success_fields},
                   "required": list(success_fields)},
            "else": {"required": ["note"]},
        }
        conditions = definition.setdefault("allOf", [])
        if conditional not in conditions:
            conditions.append(conditional)
    return result
