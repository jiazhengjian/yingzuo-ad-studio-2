#!/usr/bin/env python3
"""Historical DeepSeek-only evaluation, offline unless --real-planner is explicit.

This comparison tool does not follow the active product planner configuration.

The default path uses only the standard library, never imports the application,
reads no credentials, and makes no network requests. This is an evidence pack,
not an automated score for advertising effectiveness or conversion.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "ad_expression_cases.json"
CRITERIA = (
    "product_relevance", "audience_relevance", "factual_support",
    "audiovisual_complement", "natural_speech", "narrative_progression",
)


def scrub(value, secret=""):
    """Remove credential-shaped strings and local identity from output only."""
    if isinstance(value, dict):
        return {key: "[redacted]" if key == "reasoning_content" or re.search(r"(?i)api.?key|authorization|secret|access.?token", key)
                else scrub(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, secret) for item in value]
    if not isinstance(value, str):
        return value
    if secret:
        value = value.replace(secret, "[redacted]")
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "[redacted]", value)
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.-]+", "Bearer [redacted]", value)
    value = re.sub(r"/(?:Users|home)/[^\s\"'<>]+", "[local-path]", value)
    return value


def write_json(path, value, secret=""):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(scrub(value, secret), handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def diagnostics(narrations, case):
    """Narrow lexical/length hints. No hint is a semantic quality verdict."""
    issues = []
    for index, text in enumerate(narrations):
        if re.search(r"[，,、：:；;]$", text.strip()):
            issues.append({"code": "incomplete_sentence", "index": index})
    joined = "".join(narrations)
    budget = math.floor(case["duration_seconds"] * 2.9)
    if len(joined) > budget:
        issues.append({"code": "total_character_budget_exceeded", "characters": len(joined), "maximum": budget})
    for term in case.get("unsupported_terms", []):
        if term in joined:
            issues.append({"code": "unsupported_term_requires_review", "term": term})
    return {
        "narration_characters": len(joined), "maximum_characters": budget,
        "issues": issues,
        "limitation": "关键词仅提示复核；没有命中不证明事实正确。字数不是实测音长，无法验证原生语音、音画同步或广告效果。",
    }


def validate_fixtures(pack):
    def require(condition, code):
        if not condition:
            raise ValueError(code)
    cases = pack["cases"]
    baseline_ids = {item["id"] for item in pack["baselines"]}
    require(len(baseline_ids) == len(pack["baselines"]), "duplicate_baseline_id")
    require(len({case["id"] for case in cases}) == len(cases), "duplicate_case_id")
    require(tuple(item["criterion"] for item in pack["rubric"]) == CRITERIA, "rubric_contract")
    controls_checked = 0
    for case in cases:
        require(re.fullmatch(r"[a-z0-9_]+", case["id"]), "unsafe_case_id")
        require(case["duration_seconds"] == 20, "fixture_duration")
        facts = {item["id"]: item for item in case["brief"]["facts"]}
        require(len(facts) == len(case["brief"]["facts"]), "duplicate_fact_id")
        require(set(case["direction"]["evidence_ids"]) <= facts.keys(), "direction_fact_membership")
        require(set(case["baseline_ids"]) <= baseline_ids, "baseline_membership")
        for fact in facts.values():
            if fact["source_kind"] == "merchant":
                require(fact["text"] in case["evidence_sources"][fact["source_id"]], "merchant_exact_excerpt")
            else:
                require(fact["source_kind"] == "image", "unsupported_source_kind")
                require(fact["source_id"] in case["original_asset_ids"], "image_source_membership")
        for baseline in pack["baselines"]:
            require(baseline["kind"] in ("historical_actual", "constructed_negative_control"), "baseline_provenance")
            if baseline["id"] in case["baseline_ids"] and baseline.get("expected_diagnostics"):
                actual = {item["code"] for item in diagnostics(baseline["narrations"], case)["issues"]}
                require(set(baseline["expected_diagnostics"]) <= actual, "negative_control_not_detected")
                controls_checked += 1
    return {"cases": len(cases), "negative_control_assertions": controls_checked,
            "status": "fixture_integrity_passed", "semantic_quality_verified": False}


def human_review(pack):
    return [{**item, "status": "not_reviewed", "evidence": ""} for item in pack["rubric"]]


def case_record(pack, case):
    baselines = [{**item, "diagnostics": diagnostics(item["narrations"], case)}
                 for item in pack["baselines"] if item["id"] in case["baseline_ids"]]
    return {
        "case_id": case["id"], "label": case["label"], "input": case,
        "input_provenance": pack["provenance"], "baselines": baselines,
        "status": "not_generated", "planner_requests": 0,
        "human_review": human_review(pack), "semantic_quality_verified": False,
        "actual_audio_timing_verified": False, "conversion_effectiveness_verified": False,
        "database_writes": False, "media_requests": 0,
    }


def application_contract(case):
    # Pure contract modules only; never Settings.from_env(), store, media or API.
    sys.path.insert(0, str(ROOT))
    from app.ad_script import AdScript, ScriptReview, script_context, validate_script
    from app.strategy import ProductBrief, CreativeDirection, validate_brief, build_strategy, select_rules
    brief = validate_brief(ProductBrief.model_validate(case["brief"]),
                           case["evidence_sources"], set(case["original_asset_ids"]))
    direction = CreativeDirection.model_validate(case["direction"])
    strategy = build_strategy(brief, [direction], direction.id, select_rules(brief))
    return AdScript, ScriptReview, script_context, validate_script, strategy


def safe_exception(error, secret=""):
    # Pydantic's default str(error) includes input values. Keep only field/type.
    if callable(getattr(error, "errors", None)):
        return {"type": type(error).__name__, "fields": [
            {"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()
        ]}
    if isinstance(error, ValueError):
        return {"type": type(error).__name__, "message": scrub(str(error), secret)[:500]}
    return {"type": type(error).__name__}


async def planner_request(system, context, key, model):
    """Fixed planner endpoint, no tools/media, one attempt, no raw HTTP logs."""
    import httpx
    from app.ad_script import script_request_options
    payload = {
        "model": model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"}, "stream": False,
        **script_request_options(),
    }
    async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
        response = await client.post("https://api.deepseek.com/chat/completions",
                                     headers={"Authorization": "Bearer " + key}, json=payload)
    if response.status_code != 200:
        raise ValueError(f"planner_http_{response.status_code}")
    if len(response.content) > 1_000_000:
        raise ValueError("planner_response_too_large")
    choice = response.json()["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("planner_response_incomplete")
    result = json.loads(choice["message"]["content"])
    if not isinstance(result, dict):
        raise ValueError("planner_response_not_object")
    if result.get("type") == "json_object":
        result.pop("type")
    return result


async def evaluate_real(pack, case, key, model):
    record = case_record(pack, case)
    try:
        AdScript, ScriptReview, context_for, validate, strategy = application_contract(case)
        from app.ad_script import AD_SCRIPT_SYSTEM, SCRIPT_REVIEW_SYSTEM, script_request_options
        context = context_for(strategy, case["duration_seconds"])
        request_options = script_request_options()
        record["request_configuration"] = {
            "planner_model": model,
            "thinking": request_options.get("thinking", {}).get("type"),
            "reasoning_effort": request_options.get("reasoning_effort"),
            "max_tokens": request_options.get("max_tokens"),
        }
        record["runtime_skill"] = {
            "version": context["runtime_skill"]["version"],
            "content_sha256": context["runtime_skill"]["content_sha256"],
        }
        context["schema"] = AdScript.model_json_schema()
        record["planner_requests"] += 1
        draft_raw = await planner_request(AD_SCRIPT_SYSTEM, context, key, model)
        record["draft"] = scrub(draft_raw, key)
        draft = validate(draft_raw, strategy, case["duration_seconds"], final=False)
        review_context = context_for(strategy, case["duration_seconds"], script=draft)
        review_context["schema"] = ScriptReview.model_json_schema()
        record["planner_requests"] += 1
        review_raw = await planner_request(SCRIPT_REVIEW_SYSTEM, review_context, key, model)
        record["model_review"] = scrub(review_raw, key)
        review = ScriptReview.model_validate(review_raw)
        final = validate(review.script, strategy, case["duration_seconds"], final=True)
        record["candidate"] = final.model_dump()
        record["candidate_diagnostics"] = diagnostics([beat.narration for beat in final.beats], case)
        record["status"] = "candidate_awaiting_human_review" if review.ready else "model_review_rejected"
        record["structural_validation"] = "passed"
    except Exception as error:
        record["status"] = "request_or_contract_failed"
        record["error"] = safe_exception(error, key)
    return record


def evaluate_candidate(pack, case, directory):
    record = case_record(pack, case)
    try:
        _, ScriptReview, _, validate, strategy = application_contract(case)
        raw = json.loads((directory / (case["id"] + ".json")).read_text(encoding="utf-8"))
        review_ready = True
        # Accept either an evaluator record, a review envelope, or an AdScript.
        candidate = raw.get("candidate") if isinstance(raw, dict) else None
        if isinstance(raw, dict) and raw.get("model_review"):
            saved_review = ScriptReview.model_validate(raw["model_review"])
            record["model_review"] = saved_review.model_dump()
            review_ready = saved_review.ready
        if candidate is None and isinstance(raw, dict) and "ready" in raw:
            review = ScriptReview.model_validate(raw)
            record["model_review"] = review.model_dump()
            review_ready = review.ready
            candidate = review.script
        final = validate(candidate if candidate is not None else raw, strategy, case["duration_seconds"], final=True)
        record["candidate"] = final.model_dump()
        record["candidate_diagnostics"] = diagnostics([beat.narration for beat in final.beats], case)
        record["structural_validation"] = "passed"
        record["status"] = "candidate_awaiting_human_review" if review_ready else "model_review_rejected"
    except FileNotFoundError:
        record["status"] = "candidate_file_missing"
    except Exception as error:
        record["status"] = "candidate_contract_failed"
        record["error"] = safe_exception(error)
    return record


def render_report(summary, records):
    lines = ["# 广告脚本验收记录", "", f"模式：{summary['mode']}", "",
             "这些输入是作者编写的文字测试资料，图片事实已作为文字给定。本次不检查识图，不写用户项目，不生成图片、视频或音频。",
             "历史真实台词与构造反例分别标注。模型自审通过、结构通过和人工判断是不同的证据；本报告不给出转化分数。", ""]
    for record in records:
        lines += ["## " + record["label"], "", "状态：" + record["status"], "",
                  "商家输入：" + record["input"]["merchant_input"], ""]
        if record.get("runtime_skill"):
            configuration = record.get("request_configuration", {})
            lines += [
                "运行规则版本：" + str(record["runtime_skill"]["version"]) +
                "；规则内容摘要：`" + record["runtime_skill"]["content_sha256"] + "`。", "",
                "请求设置：" + str(configuration.get("planner_model")) +
                "，thinking=" + str(configuration.get("thinking")) +
                "，reasoning_effort=" + str(configuration.get("reasoning_effort")) +
                "，max_tokens=" + str(configuration.get("max_tokens")) + "。", "",
            ]
        lines += ["### 基线与反例", ""]
        for item in record["baselines"]:
            kind = "历史真实台词" if item["kind"] == "historical_actual" else "构造反例，非模型输出"
            lines += [f"- **{kind}**：{' '.join(item['narrations'])}  ", "  已知问题：" + item["known_issue"]]
        if record.get("draft"):
            lines += ["", "### 新流程初稿", "", "```json", json.dumps(record["draft"], ensure_ascii=False, indent=2), "```"]
        if record.get("candidate"):
            candidate = record["candidate"]
            lines += ["", "### 审核后候选", "", "核心表达：" + candidate["core_message"], "",
                      "希望观众记住：" + candidate["audience_takeaway"], ""]
            for index, beat in enumerate(candidate["beats"], 1):
                lines += [f"{index}. 内容目的：{beat['purpose']}  ", f"   要传达：{beat['message']}  ",
                          f"   画面承担：{beat['visual_role']}  ", f"   台词：{beat['narration'] or '（无台词）'}", ""]
        if record.get("error"):
            lines += ["", "未通过：`" + json.dumps(record["error"], ensure_ascii=False) + "`", ""]
        lines += ["", "### 人工验收（尚未填写）", "", "| 维度 | 检查问题 | 结论与具体句子依据 |", "| --- | --- | --- |"]
        for criterion in record["human_review"]:
            lines.append(f"| {criterion['criterion']} | {criterion['question']} | 待验收 |")
        lines += ["", "同用例相对判断：待填写。请具体指出哪里改善、哪里仍不成立，不只写‘更自然’。",
                  "新增商家事实用例与旧蛋糕资料不同，只能做表达方式对照，不能当作受控模型提升实验。",
                  "原生音长、音画对应、声音自然度和实际投放效果均未验证。", ""]
    return "\n".join(lines)


def credentials(args):
    # Called exclusively after --real-planner. No automatic .env discovery.
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    model = args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
    if args.credentials_file:
        from dotenv import dotenv_values
        values = dotenv_values(args.credentials_file, interpolate=False)
        key = (values.get("DEEPSEEK_API_KEY") or "").strip()
        if not args.model:
            model = values.get("DEEPSEEK_MODEL") or model
    if not key:
        raise ValueError("real_planner_requires_explicit_credentials")
    return key, model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--output", type=Path, required=True, help="New scratch directory; existing paths are never overwritten")
    parser.add_argument("--case", action="append", dest="case_ids", help="Repeat for a subset of case IDs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real-planner", action="store_true", help="Explicitly allow up to two paid text requests per selected fixture")
    mode.add_argument("--candidate-dir", type=Path, help="Offline validation of existing case_id.json script outputs")
    parser.add_argument("--credentials-file", type=Path, help="Only with --real-planner; read this exact file, never autodiscover")
    parser.add_argument("--model", help="Only with --real-planner")
    args = parser.parse_args(argv)
    if (args.credentials_file or args.model) and not args.real_planner:
        parser.error("credential/model flags require --real-planner")
    if args.output.exists():
        parser.error("output must be a new scratch directory")
    try:
        pack = json.loads(args.fixtures.read_text(encoding="utf-8"))
        integrity = validate_fixtures(pack)
        selected = [case for case in pack["cases"] if not args.case_ids or case["id"] in args.case_ids]
        if not selected or (args.case_ids and set(args.case_ids) - {case["id"] for case in selected}):
            raise ValueError("unknown_or_empty_case_selection")
        key, model = credentials(args) if args.real_planner else ("", None)
    except Exception as error:
        print(json.dumps({"status": "setup_failed", "error": safe_exception(error)}, ensure_ascii=False))
        return 2
    # Keep evaluation outputs private; .local or an explicit external scratch.
    target = args.output.resolve()
    if target.is_relative_to(ROOT) and not target.is_relative_to(ROOT / ".local"):
        parser.error("output must be private scratch, outside source/runtime/data trees")
    target.mkdir(parents=True, exist_ok=False)
    run_mode = "real_script_and_review" if args.real_planner else "candidate_contract_only" if args.candidate_dir else "offline_fixture_and_negative_controls"
    records = []
    for case in selected:
        if args.real_planner:
            record = asyncio.run(evaluate_real(pack, case, key, model))
        elif args.candidate_dir:
            record = evaluate_candidate(pack, case, args.candidate_dir)
        else:
            record = case_record(pack, case)
        records.append(record)
        write_json(target / (case["id"] + ".json"), record, key)
        print(json.dumps({"case_id": case["id"], "status": record["status"], "planner_requests": record["planner_requests"]}, ensure_ascii=False), flush=True)
    summary = {"mode": run_mode, "created_at": datetime.now(timezone.utc).isoformat(),
               "fixture_integrity": integrity, "planner_model": model,
               "requests": sum(record["planner_requests"] for record in records),
               "semantic_quality_verified": False, "media_requests": 0, "database_writes": False}
    write_json(target / "summary.json", summary, key)
    with (target / "review.md").open("x", encoding="utf-8") as handle:
        handle.write(scrub(render_report(summary, records), key))
    failed = any(record["status"] in ("request_or_contract_failed", "candidate_contract_failed", "candidate_file_missing", "model_review_rejected") for record in records)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
