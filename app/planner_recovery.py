"""System-side planning failures stay inside the job.

A merchant is asked only for a missing fact, a contradiction with something they
locked, or a missing required asset. Formatting, timing, citations, continuity
and transport slips are salvaged, repaired once, retried once, then accepted.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
import types
from typing import Union, get_args, get_origin

from pydantic import BaseModel, ValidationError


# Markers that still stop the job and show a plain-Chinese card.
MERCHANT_MARKERS = (
    "用户明确固定的台词",
    "用户固定台词",
    "已选无人声方向不能增加台词",
)

# Every terminating path and who owns it. Class (b) must not end the job.
RECOVERY_PATHS = (
    {"path": "fixed_speech", "klass": "a",
     "where": "progressive_planning.compile 未保留用户固定台词",
     "handling": "停住，卡片说明固定台词没有保留"},
    {"path": "silent_direction", "klass": "a",
     "where": "validate_script 无人声方向被加上台词",
     "handling": "停住，卡片说明已选无人声"},
    {"path": "missing_product_image", "klass": "a",
     "where": "strategy 无商品原图",
     "handling": "留在澄清，不进入方案"},
    {"path": "planner_credentials", "klass": "a",
     "where": "PlannerConfigurationError",
     "handling": "卡片说明检查连接凭证或余额"},
    {"path": "schema_shape", "klass": "b",
     "where": "分镜结构读不出来",
     "handling": "修一次、重写一次，然后用本地可编译分镜"},
    {"path": "extra_fields", "klass": "b",
     "where": "模型多写了 schema 没有的字段",
     "handling": "校验前把重复字段并回原位，未知字段丢掉，再校验"},
    {"path": "broken_json", "klass": "b",
     "where": "策划回复不是完整 JSON",
     "handling": "先取第一个完整对象，再补括号；仍不行就重试这一步一次，然后换备用模型，不把商家停住"},
    {"path": "wrapped_envelope", "klass": "b",
     "where": "审核被包在 content 里",
     "handling": "取出 content 再校验"},
    {"path": "citation_mismatch", "klass": "b",
     "where": "建议或视图引文对不上镜头",
     "handling": "引文挪到对得上的镜头，对不上就丢掉，不作废已通过的审核"},
    {"path": "evidence_mapping", "klass": "b",
     "where": "节拍依据没有落到镜头",
     "handling": "修一次，仍缺就把依据写到覆盖该拍的镜头上并继续"},
    {"path": "selected_cta_evidence", "klass": "b",
     "where": "选中方向的去向依据没有进台词",
     "handling": "商家指定了去向就补上那一句并记上依据；没指定就不要求这句"},
    {"path": "ending_hold", "klass": "b",
     "where": "最后一句和结尾停留叠在一起",
     "handling": "把定格起点对齐台词结束，不够就缩短最后一句，然后收下"},
    {"path": "capacity", "klass": "b",
     "where": "动作或台词放不进时长",
     "handling": "返修一次，没有变好就收下目前的方案"},
    {"path": "continuity", "klass": "b",
     "where": "前后状态衔接",
     "handling": "结束状态改回这一镜实际做了的事，连续镜头跟着改，然后收下"},
    {"path": "review_incomplete", "klass": "b",
     "where": "综合审核记录不完整",
     "handling": "修一次审核记录，仍不行就去掉这份记录，收下已编译的方案"},
    {"path": "review_not_converged", "klass": "b",
     "where": "审核问题反复出现",
     "handling": "返修一轮后收下目前的方案"},
    {"path": "refinement_noop_or_scope", "klass": "b",
     "where": "返修没改内容或改出了范围",
     "handling": "留下返修前已经编译的方案"},
    {"path": "direction_review_extra", "klass": "b",
     "where": "方向审核多了字段或没通过",
     "handling": "丢掉多余字段；仍不通过就沿用策略阶段已经收下的方向"},
    {"path": "transport", "klass": "b",
     "where": "策划请求 5xx、超时或连接中断",
     "handling": "写下请求摘要和响应摘录，同一模型再试一次，有备用模型再用一次"},
    {"path": "deterministic_rejection", "klass": "b",
     "where": "策划请求 HTTP 400，同一份请求再发也不会被接受",
     "handling": "不重发同一份请求；提示里缺 JSON 就补上一句，有备用模型再用一次，然后继续"},
    {"path": "optional_polish", "klass": "b",
     "where": "台词润色或镜头润色失败",
     "handling": "留下原稿，继续到参考图确认，不因此停住"},
    {"path": "fallback_skeleton", "klass": "b",
     "where": "分镜阶段没有留下任何可用稿",
     "handling": "先再请求一次完整分镜；仍没有就记下占位稿，不把它当成完成的方案，也不用它替换已有稿"},
    {"path": "skill_trace_cap", "klass": "b",
     "where": "服务端累积的 skill_trace 超过方案上限",
     "handling": "去重后只保留最近的记录，校验前截断，记账不能停任务"},
    {"path": "contentless_reply", "klass": "b",
     "where": "策划停在空正文，或推理用光输出上限后没有正文",
     "handling": "同一模型用低推理再试一次，有备用模型再用一次；审核阶段收下目前的方案"},
)


def _issue_text(issues):
    if isinstance(issues, dict):
        return "".join(str(value) for value in issues.values())
    if isinstance(issues, (list, tuple)):
        return "".join(str(value) for value in issues)
    return str(issues or "")


def needs_merchant(message, issues=()):
    text = str(message or "") + _issue_text(issues)
    return any(marker in text for marker in MERCHANT_MARKERS)


# Stored retryable flags from older runs are not consulted. Resume is refused
# only for these four merchant stops.
_CREDENTIAL_CODES = frozenset({"planning_configuration_failed"})
_IMAGE_MARKERS = ("没有商品原图", "无商品原图", "请先上传商品原图", "请先上传真实商品图")


def merchant_stop(error_code, message="", issues=(), retry_action=None):
    """Return the merchant-stop id when resume must wait, else None."""
    if error_code in _CREDENTIAL_CODES or retry_action == "check_connection":
        return "planner_credentials"
    text = str(message or "") + _issue_text(issues)
    if "用户明确固定的台词" in text or "用户固定台词" in text:
        return "fixed_speech"
    if "已选无人声方向不能增加台词" in text or "无人声方向" in text:
        return "silent_direction"
    if error_code == "missing_product_image" or any(marker in text for marker in _IMAGE_MARKERS):
        return "missing_product_image"
    return None


JSON_OUTPUT_LINE = "请只输出 JSON 对象。"


def _flatten_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        parts = []
        for key in ("text", "content"):
            if key in value:
                parts.append(_flatten_text(value[key]))
        return "\n".join(parts)
    return ""


def text_mentions_json(*parts):
    blob = "\n".join(_flatten_text(part) for part in parts)
    return re.search(r"json", blob, re.IGNORECASE) is not None


SKILL_TRACE_LIMIT = 30
EVIDENCE_LIST_LIMIT = 12
REFERENCE_LIST_LIMIT = 10
NOTE_TEXT_LIMIT = 300


def bound_server_list(items, limit):
    """Drop duplicate bookkeeping and keep the most recent entries.

    Server-owned lists such as skill traces grow across stages. A schema
    max_length is not a reason to fail the job.
    """
    if not isinstance(items, list) or type(limit) is not int or limit < 1:
        return items
    seen = {}
    order = []
    for item in items:
        if isinstance(item, dict):
            try:
                key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            except (TypeError, ValueError):
                key = repr(item)
        elif isinstance(item, (str, int)):
            key = item
        else:
            key = repr(item)
        if key in seen:
            order.remove(key)
        seen[key] = item
        order.append(key)
    kept = [seen[key] for key in order]
    if len(kept) > limit:
        kept = kept[-limit:]
    return kept


def append_reference_ids(existing, additions, limit=REFERENCE_LIST_LIMIT):
    """Dedupe image ids. Keep ids already on the segment, then the newest additions."""
    base = []
    for item in existing or []:
        if isinstance(item, str) and item and item not in base:
            base.append(item)
    extra = []
    for item in additions or []:
        if isinstance(item, str) and item and item not in base and item not in extra:
            extra.append(item)
    if len(base) >= limit:
        return base[:limit]
    room = limit - len(base)
    if len(extra) <= room:
        return base + extra
    return base + extra[-room:]


def clip_server_text(value, limit=NOTE_TEXT_LIMIT):
    if isinstance(value, str) and type(limit) is int and limit >= 0 and len(value) > limit:
        return value[:limit]
    return value


def ensure_json_prompt(system, *rest):
    """DeepSeek rejects json_object mode unless the prompt itself says json."""
    if text_mentions_json(system, *rest):
        return system if isinstance(system, str) else ""
    base = system if isinstance(system, str) else ""
    return base.rstrip() + "\n" + JSON_OUTPUT_LINE


def _delete_path(data, loc):
    if not loc:
        return
    cursor = data
    for part in loc[:-1]:
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        elif isinstance(cursor, list) and isinstance(part, int) and 0 <= part < len(cursor):
            cursor = cursor[part]
        else:
            return
    last = loc[-1]
    if isinstance(cursor, dict) and last in cursor:
        cursor.pop(last, None)


def _blank_payload(value):
    return value is None or value == "" or value == [] or value == {}


def _unwrap_annotation(annotation):
    origin = get_origin(annotation)
    if origin is not None and getattr(origin, "__name__", "") == "Annotated" or str(origin) == "typing.Annotated":
        args = get_args(annotation)
        if args:
            return _unwrap_annotation(args[0])
    return annotation


def _field_model(field):
    """Return (model, is_list) for one schema field, else (None, False)."""
    annotation = _unwrap_annotation(field.annotation)
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        found = []
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            nested, is_list = _field_model(type("_F", (), {"annotation": arg})())
            if nested is not None:
                found.append((nested, is_list))
        if len(found) == 1:
            return found[0]
        return None, False
    if origin is list:
        args = get_args(annotation)
        if not args:
            return None, False
        nested, _ = _field_model(type("_F", (), {"annotation": args[0]})())
        if nested is not None:
            return nested, True
        return None, False
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, False
    return None, False


def normalize_model_payload(model, data):
    """Move a duplicated field into its one canonical object, and drop the rest.

    Models sometimes repeat ``key_visual`` on the direction and inside
    ``film_concept``. The copy is merged when the canonical value is empty,
    and discarded when it is already filled. An unknown field that cannot be
    placed is dropped. A missing parent object is not invented, and a list is
    not sprayed with a parent extra.
    """
    if not isinstance(data, dict) or not isinstance(model, type) or not issubclass(model, BaseModel):
        return data
    current = deepcopy(data)
    singular = {}
    for name, field in model.model_fields.items():
        nested, is_list = _field_model(field)
        if nested is None:
            continue
        value = current.get(name)
        if is_list:
            if isinstance(value, list):
                current[name] = [
                    normalize_model_payload(nested, item) if isinstance(item, dict) else item
                    for item in value]
            continue
        singular[name] = nested
        if isinstance(value, dict):
            current[name] = normalize_model_payload(nested, value)
    if model.model_config.get("extra") != "forbid":
        return current
    for key in [item for item in current if item not in model.model_fields]:
        homes = [name for name, nested in singular.items() if key in nested.model_fields]
        if len(homes) == 1:
            home = homes[0]
            nested = current.get(home)
            if isinstance(nested, dict):
                extra_value = current.get(key)
                if key not in nested or _blank_payload(nested.get(key)):
                    if not _blank_payload(extra_value):
                        nested[key] = extra_value
                        current[home] = normalize_model_payload(singular[home], nested)
        current.pop(key, None)
    return current


def drop_extra_forbidden(model, data):
    """Remove fields the model forbids. Other validation errors stay for the caller."""
    if not isinstance(data, dict):
        return data
    current = normalize_model_payload(model, data)
    for _ in range(12):
        try:
            model.model_validate(current)
            return current
        except ValidationError as exc:
            extras = [item for item in exc.errors() if item.get("type") == "extra_forbidden"]
            if not extras:
                return current
            for item in extras:
                _delete_path(current, item.get("loc") or ())
    return current


def unwrap_planner_object(value):
    """Some replies wrap the real object in {"content": {...}}."""
    if not isinstance(value, dict):
        return value
    content = value.get("content")
    if not isinstance(content, dict):
        return value
    outer_markers = {"kind", "ready", "checks", "beats", "shots", "ad_script", "directions"}
    if outer_markers.intersection(value) and "content" not in outer_markers:
        if "ready" in value or "kind" in value or "checks" in value:
            return value
    if set(value) == {"content"} or ({"ready", "checks", "alignment", "kind"} & set(content)
                                      and not ({"ready", "kind", "checks"} & set(value))):
        return content
    return value


def _strip_trailing_commas(text):
    out = []
    in_string = False
    escape = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            look = index + 1
            while look < len(text) and text[look] in " \t\r\n":
                look += 1
            if look < len(text) and text[look] in "}]":
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _top_level_object_start(text, start):
    """Index of a `{` that is not inside a string or an array."""
    in_string = False
    escape = False
    brackets = 0
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "[":
            brackets += 1
        elif char == "]" and brackets:
            brackets -= 1
        elif char == "{" and brackets == 0:
            return index
    return None


def _object_end(text, start):
    """Index just after the `}` that closes text[start], or None."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _json_correction(used_fence, used_comma, prefix, remainder):
    lead = prefix.strip()
    tail = remainder.strip()
    only_closers = bool(tail) and set(tail) <= set("}]")
    if used_fence:
        return "code_fence"
    if used_comma and not lead and not tail:
        return "trailing_comma"
    if not lead and tail == "}":
        return "single_redundant_closing_brace"
    if not lead and only_closers:
        return "trailing_extra_closers"
    if used_comma:
        return "trailing_comma"
    return "first_balanced_object"


def tolerant_planner_object(text):
    """First balanced object, after fences, trailing commas, extra closers and prose.

    A value that is already valid JSON is not handled here. An array, null or
    other non-object stays a failure, including an object nested only inside
    that array. Unterminated strings are not invented.
    """
    if not isinstance(text, str):
        raise json.JSONDecodeError("策划回复不是文本", "", 0)
    original = text.replace("\ufeff", "").replace("\u00a0", " ").replace("\v", " ").replace("\f", " ")
    cleaned = re.sub(r"```(?:json)?", "", original, flags=re.IGNORECASE)
    used_fence = cleaned != original
    cursor = 0
    while True:
        start = _top_level_object_start(cleaned, cursor)
        if start is None:
            break
        end = _object_end(cleaned, start)
        if end is None:
            # A truncated outer object must be closed, not replaced by an inner one.
            raise json.JSONDecodeError("策划回复不是完整 JSON", cleaned, start)
        slice_text = cleaned[start:end]
        parsed = None
        used_comma = False
        for candidate in (slice_text, _strip_trailing_commas(slice_text)):
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                parsed = value
                used_comma = candidate != slice_text
                break
        if parsed is None:
            cursor = start + 1
            continue
        correction = _json_correction(used_fence, used_comma, cleaned[:start], cleaned[end:])
        return parsed, correction
    raise json.JSONDecodeError("策划回复不是完整 JSON", cleaned, 0)


def repair_planner_json(text):
    """Close a truncated object. Does not invent missing strings or drop a second value."""
    if not isinstance(text, str):
        raise ValueError("策划回复不是文本")
    stripped = text.strip(" \t\r\n")
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?[ \t\r\n]*", "", stripped)
        stripped = re.sub(r"[ \t\r\n]*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        opens = stripped.count("{") - stripped.count("}")
        brackets = stripped.count("[") - stripped.count("]")
        if opens <= 0 and brackets <= 0:
            raise
        stripped = stripped + ("]" * max(brackets, 0)) + ("}" * max(opens, 0))
        parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("策划回复须是对象", stripped, 0)
    return unwrap_planner_object(parsed)


_COMPLETION = re.compile(r"已关掉|已经关掉|关掉了|已关闭|关上了|停风|已经停|已停风")


def reconcile_shot_continuity(shots):
    """An end state that the shot never shows is put back to the start state.

    A continuous next shot inherits that state, so the cut does not pretend
    the missing action already happened.
    """
    if not isinstance(shots, list):
        return False
    changed = False
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        continuity = shot.get("continuity")
        if not isinstance(continuity, dict):
            continue
        end = continuity.get("end_state") if isinstance(continuity.get("end_state"), str) else ""
        if not _COMPLETION.search(end):
            continue
        description = shot.get("description") if isinstance(shot.get("description"), str) else ""
        allowed = continuity.get("allowed_changes") if isinstance(continuity.get("allowed_changes"), str) else ""
        if _COMPLETION.search(description) or _COMPLETION.search(allowed):
            continue
        start = continuity.get("start_state") if isinstance(continuity.get("start_state"), str) else ""
        if not start.strip():
            start = "商品保持入镜时的状态"
        continuity["end_state"] = start
        changed = True
        if index + 1 >= len(shots) or not isinstance(shots[index + 1], dict):
            continue
        nxt = shots[index + 1].get("continuity")
        if isinstance(nxt, dict) and nxt.get("entry_transition") == "continuous":
            nxt["start_state"] = start
    return changed
