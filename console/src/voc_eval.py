"""Explicit, bounded multi-model quality comparison on isolated source fixtures.

Default is plan-only. --execute is required for real inference. Models are input
parameters, never provider-name heuristics. The saved gateway profile is not
changed; each model gets a disposable source DB and derived shadow DB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path

from . import voc_assets as assets, voc_engine as engine
from .voc_quality import hard_scope_key
from .config import Settings, load_settings
from .voc_model import VocModelError

CASES = [
    {"question": "底座该怎么安装？", "answer": "打开安装页面，点击固定底座并保存设置。", "expected_topic": "base_install", "answer_kind": "resolved_answer", "must_not_be_ready": False},
    {"question": "如何把底座装好并完成设置？", "answer": "打开安装页面，点击固定底座并保存设置。", "expected_topic": "base_install", "answer_kind": "resolved_answer", "must_not_be_ready": False},
    {"question": "在哪里申请订单发票？", "answer": "进入订单详情，点击申请发票并填写抬头后提交。", "expected_topic": "invoice", "answer_kind": "resolved_answer", "must_not_be_ready": False},
    {"question": "商品一般多久发出？", "answer": "商品将在7天内发货，请注意签收。", "expected_topic": "shipping_time", "answer_kind": "resolved_answer", "must_not_be_ready": True},
    {"question": "下单后需要等多久才能发货？", "answer": "商品将在30天内发货，请注意签收。", "expected_topic": "shipping_time", "answer_kind": "resolved_answer", "must_not_be_ready": True},
    {"question": "知识同步报错怎么办？", "answer": "麻烦您提供报错截图和店铺名称，我帮您继续核对。", "expected_topic": "sync_error", "answer_kind": "collect_info", "must_not_be_ready": True},
]


def load_cases(input_path: str = "") -> list[dict]:
    cases = json.loads(Path(input_path).read_text(encoding="utf-8"))["cases"] if input_path else CASES
    if not isinstance(cases, list) or not 1 <= len(cases) <= 50:
        raise engine.EngineError("评估样本须为 1–50 条。")
    for case in cases:
        if (not isinstance(case, dict) or not all(isinstance(case.get(key), str) and case[key] for key in ("question", "expected_topic", "answer_kind"))
                or not isinstance(case.get("answer"), str) or len(case["question"]) + len(case["answer"]) > 8000
                or case["answer_kind"] not in ("resolved_answer", "collect_info", "contextual", "none")):
            raise engine.EngineError("评估样本字段不符合约定。")
    return cases


def _fixture(target: Path, cases: list[dict]) -> None:
    conn = sqlite3.connect(target)
    try:
        conn.executescript("""
        CREATE TABLE qa_pair(qa_id INTEGER PRIMARY KEY,session_id TEXT,customer_id TEXT,customer_name TEXT,group_name TEXT,staff_name TEXT,asked_at INTEGER,question TEXT,answer TEXT,cluster_id INTEGER);
        CREATE TABLE raw_message(session_id TEXT,seq INTEGER,role TEXT,content TEXT,ts INTEGER,source_message_id TEXT);
        CREATE TABLE qa_origin(qa_id INTEGER PRIMARY KEY,evidence_key TEXT UNIQUE,session_id TEXT,question_message_ids TEXT,answer_message_ids TEXT,fingerprint TEXT,revision INTEGER,active INTEGER);
        """)
        for qa_id, case in enumerate(cases, 1):
            sid = f"evaluation-{qa_id}"
            qid, aid = f"source:{sid}:q", f"source:{sid}:a"
            conn.execute("INSERT INTO qa_pair VALUES (?,?,?,?,?,?,?,?,?,0)", (qa_id, sid, f"synthetic-customer-{qa_id}", "评估合成客户", "评估合成群", "评估合成客服", 0, case["question"], case["answer"]))
            conn.execute("INSERT INTO raw_message VALUES (?,?,?,?,?,?)", (sid, 1, "user", case["question"], 0, qid))
            if case["answer"]:
                conn.execute("INSERT INTO raw_message VALUES (?,?,?,?,?,?)", (sid, 2, "staff", case["answer"], 0, aid))
            conn.execute("INSERT INTO qa_origin VALUES (?,?,?,?,?,?,1,1)", (qa_id, "eval-" + assets.digest([sid, qid]), sid, json.dumps([qid]), json.dumps([aid] if case["answer"] else []), "eval-source"))
        conn.commit()
    finally:
        conn.close()


def _debug_events(cfg: Settings, events: list[dict]) -> list[dict]:
    """Private evaluation diagnostics, never a change to the production API.

    Keep actual extracted decisions before the disposable DB is removed. Public
    evidence alone omits missing_info, proposed topic ID and scope decisions that
    explain why a correctly grounded event was split or withheld from knowledge.
    """
    identities = {item.get("event_id") or item.get("id") for item in events} - {None, ""}
    if not identities:
        return events
    rows = {row["id"]: row for row in engine._events(cfg, {}) if row["id"] in identities}
    result = []
    for item in events:
        row = rows.get(item.get("event_id") or item.get("id"))
        if row:
            data = row["data"]
            item = {**item, "topic_id": row["topic_id"], "intent_revision": row["revision"],
                    "extraction": {**{key: data.get(key) for key in (
                        "question", "theme", "boundary", "answer_kind", "scope", "claims",
                        "missing_info", "improvements", "gap_audit", "conflicts", "claim_context", "source_complete", "source_intent_count", "candidate_context", "domain", "extraction_attempts")},
                        "proposed_topic_id": data.get("topic_id"),
                        "hard_scope": [{"kind": kind, "quote": quote} for kind, quote in hard_scope_key(data)],
                        "possible_duplicate": data.get("possible_duplicate", [])}}
        result.append(item)
    return result


def _successful_retries(job: dict) -> int | None:
    # Every completed source has one or two attempts. Failed/skipped sources
    # make subtraction ambiguous, so incomplete jobs report unknown, not zero.
    if (job.get("status") == "completed" and job.get("failed") == 0 and job.get("skipped") == 0
            and type(job.get("calls")) is int and type(job.get("processed")) is int):
        return max(0, job["calls"] - job["processed"])
    return None


def measure(cfg: Settings, cases: list[dict], result: dict) -> dict:
    topics = engine.topics(cfg, {})["items"]
    events = [{**item, "topic_id": topic["id"]} for topic in topics
              for item in engine.topic_detail(cfg, {}, topic["id"])["events"]]
    # Keep the predicted grouping independent of generated Chinese titles.
    by_qa: dict[int, set[str]] = {}
    for topic in topics:
        for item in engine.topic_detail(cfg, {}, topic["id"])["events"]:
            by_qa.setdefault(item["qa_id"], set()).add(topic["id"])
    true_positive = false_positive = false_negative = 0
    for first in range(1, len(cases) + 1):
        for second in range(first + 1, len(cases) + 1):
            expected = cases[first - 1]["expected_topic"] == cases[second - 1]["expected_topic"]
            predicted = bool(by_qa.get(first, set()) & by_qa.get(second, set()))
            true_positive += expected and predicted
            false_positive += not expected and predicted
            false_negative += expected and not predicted
    kinds = {item["qa_id"]: item["answer_kind"] for item in events}
    accuracy = sum(kinds.get(number) == case["answer_kind"] for number, case in enumerate(cases, 1)) / len(cases)
    ready = engine.knowledge(cfg, {})["items"]
    forbidden = {number for number, case in enumerate(cases, 1) if case.get("must_not_be_ready", False)}
    false_ready = sum(bool({item["qa_id"] for item in asset["evidence"]} & forbidden) for asset in ready)
    expected_ready = {number for number, case in enumerate(cases, 1)
                      if case["answer_kind"] == "resolved_answer" and not case.get("must_not_be_ready", False)}
    ready_covered = {item["qa_id"] for asset in ready for item in asset["evidence"]}
    positive_ready_recall = len(expected_ready & ready_covered) / len(expected_ready) if expected_ready else 1.0
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    debug_events = _debug_events(cfg, events)
    report = {"source_cases": len(cases), "covered_cases": len(by_qa), "event_count": len(events),
            "topic_count": len(topics), "pairwise_precision": precision, "pairwise_recall": recall,
            "answer_kind_accuracy": accuracy, "critical_false_ready": false_ready,
            "expected_ready_cases": len(expected_ready), "expected_ready_covered": len(expected_ready & ready_covered),
            "positive_ready_recall": positive_ready_recall, "missing_ready_case_ids": sorted(expected_ready - ready_covered),
            "ready_knowledge": len(ready), "job": result["job"],
            "actual_attempts": result["job"].get("calls",0),
            "successful_retried_sources": _successful_retries(result["job"]),
            "first_pass_complete": result["job"].get("processed",0) == len(cases) and result["job"].get("calls",0) == len(cases),
            "passed": len(by_qa) == len(cases) and not false_positive and not false_negative and accuracy == 1 and false_ready == 0 and positive_ready_recall == 1 and result["job"]["status"] == "completed",
            "topics": topics, "events": debug_events, "knowledge": ready,
            "exceptions": engine.exceptions(cfg, {})["items"]}
    if getattr(cfg.runtime, "voc_domain", "") == "zhihuige":
        report = _measure_support(cfg, cases, result, report)
    return report


def _measure_support(cfg: Settings, cases: list[dict], result: dict, report: dict) -> dict:
    """Keep the generic six-case gate intact; add merchant-support semantics."""
    events = report["events"]
    domains = {item["qa_id"]: item.get("domain", {}) for item in events}
    contexts = {item["qa_id"]: item.get("domain", {}) for item in report["exceptions"]
                if item["kind"] == "context_needed" and item.get("domain")}
    actual = {**domains, **contexts}
    expected_context = {i for i, c in enumerate(cases, 1) if c.get("expected_fragment_role") == "reproduction_context"}
    expected_requests = set(range(1, len(cases) + 1)) - expected_context
    by_qa = {item["qa_id"] for item in events}
    def accuracy(field, expected):
        checked = [(i, c[expected]) for i, c in enumerate(cases, 1) if expected in c]
        return sum(actual.get(i, {}).get(field) == value for i, value in checked) / len(checked) if checked else 1.0
    primary = accuracy("primary_intent", "expected_primary_intent")
    diagnosis = sum(actual.get(i, {}).get("diagnosis", {}).get("status") == c["expected_diagnosis"]
                    for i, c in enumerate(cases, 1) if "expected_diagnosis" in c) / max(1, sum("expected_diagnosis" in c for c in cases))
    fragment = accuracy("fragment_role", "expected_fragment_role")
    expected_kind = {"configuration_howto":"configuration_guide", "configuration_resolution":"troubleshooting_advice"}
    kind_by_qa = {}
    for item in report["knowledge"]:
        for evidence in item["evidence"]:
            kind_by_qa.setdefault(evidence["qa_id"], set()).add(item["kind"])
    positive = [(i, expected_kind.get(c.get("expected_knowledge_kind"), c.get("expected_knowledge_kind")))
                for i, c in enumerate(cases, 1) if not c.get("must_not_be_ready") and c.get("expected_knowledge_kind")]
    knowledge_accuracy = sum(kind in kind_by_qa.get(i, set()) for i, kind in positive) / len(positive) if positive else 1.0
    kinds = {item["qa_id"]: item["answer_kind"] for item in events}
    kinds.update({i: "none" for i in contexts})
    answer_accuracy = sum(kinds.get(i) == c["answer_kind"] for i, c in enumerate(cases, 1)) / len(cases)
    report.update(covered_cases=len(actual), request_cases=len(expected_requests), covered_request_cases=len(by_qa & expected_requests),
                  context_needed_cases=len(contexts), consultation_count=engine.summary(cfg,{})["consultation_count"],
                  primary_intent_accuracy=primary, diagnosis_accuracy=diagnosis, fragment_role_accuracy=fragment,
                  knowledge_kind_accuracy=knowledge_accuracy, answer_kind_accuracy=answer_accuracy,
                  context_fragments=[{"qa_id":i,"domain":value} for i,value in sorted(contexts.items())])
    report["passed"] = (len(actual) == len(cases) and by_qa == expected_requests and set(contexts) == expected_context
                        and primary == diagnosis == fragment == knowledge_accuracy == answer_accuracy == 1
                        and report["pairwise_precision"] == report["pairwise_recall"] == report["positive_ready_recall"] == 1
                        and report["critical_false_ready"] == 0 and result["job"]["status"] == "completed")
    return report


def evaluate(cfg: Settings, models: list[str], cases: list[dict], *, max_calls: int = 6,
             max_tokens: int = 300000, domain: str = "") -> dict:
    if not models or len(models) > 5 or any(not isinstance(model, str) or not model.strip() or len(model) > 200 for model in models):
        raise engine.EngineError("评估须明确给出 1–5 个模型ID。")
    reports = []
    for model in models:
        with tempfile.TemporaryDirectory(prefix="qiyu-voc-eval-") as directory:
            target = Path(directory) / "synthetic.db"
            _fixture(target, cases)
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            isolated = replace(cfg, llm=replace(cfg.llm, model_main=model),
                               runtime=replace(cfg.runtime, db_path=str(target), voc_domain=domain))
            # Reuse only the local embedding cache; no production data is read.
            try:
                result = engine.run(isolated, {}, limit=len(cases), max_calls=max_calls,
                                    max_tokens=max_tokens, shadow=True)
                measured = measure(isolated, cases, result)
                measured.update(model=model, source_unchanged=hashlib.sha256(target.read_bytes()).hexdigest() == before)
                reports.append(measured)
            except Exception as error:
                safe = str(error)[:240] if isinstance(error, (engine.EngineError, VocModelError)) else "评估服务异常，未完成该模型对比。"
                reports.append({"model": model, "passed": False, "error": safe})
    return {"mode": "executed", "fixture": "synthetic" if cases is CASES else "provided",
            "models": reports, "passed": all(item.get("passed") and item.get("source_unchanged") for item in reports)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模型无关VOC质量对照，默认只显示计划")
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--input", default="", help="可选JSON {cases:[question,answer,expected_topic,answer_kind,must_not_be_ready]}")
    parser.add_argument("--domain", choices=("", "zhihuige"), default="", help="知汇鸽领域使用独立九样本主验收；默认保留六条通用回归")
    parser.add_argument("--execute", action="store_true", help="明确执行有预算的真实模型请求；仅写临时影子库")
    parser.add_argument("--max-calls", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=300000, help="每个模型的保守token预算，按完整三份任务说明+TopK+输出预留")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        if args.domain == "zhihuige" and not args.input:
            from .voc_domain import DOMAIN_CASES
            cases = DOMAIN_CASES
        else:
            cases = load_cases(args.input)
        result = evaluate(load_settings(), args.models, cases, max_calls=args.max_calls, max_tokens=args.max_tokens, domain=args.domain) if args.execute else {
            "mode": "plan", "models": args.models, "source_cases": len(cases), "max_calls_per_model": args.max_calls,
            "max_tokens_per_model": args.max_tokens, "model_calls": 0, "production_writes": False, "domain": args.domain}
        if args.output:
            Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if not args.execute or result["passed"] else 1
    except Exception:
        print(json.dumps({"mode": "failed", "error": "评估输入或本机配置无效。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
