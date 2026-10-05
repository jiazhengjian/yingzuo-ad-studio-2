"""System-side planning failures stay inside the job.

A merchant is asked only for a missing fact, a contradiction with something they
locked, or a missing required asset. Formatting, timing, citations, continuity
and transport slips are salvaged, repaired once, retried once, then accepted.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re

from pydantic import ValidationError


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
     "handling": "丢掉多余字段再校验"},
    {"path": "broken_json", "klass": "b",
     "where": "策划回复不是完整 JSON",
     "handling": "补上括号、再请求这一步一次"},
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
     "handling": "修一次审核记录，仍不行就收下已编译的方案"},
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


def drop_extra_forbidden(model, data):
    """Remove fields the model forbids. Other validation errors stay for the caller."""
    if not isinstance(data, dict):
        return data
    current = deepcopy(data)
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
