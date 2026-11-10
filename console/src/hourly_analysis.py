"""Hourly incremental VOC and evidence-grounded knowledge drafts.

The CLI owns its process and waits for each persistent worker synchronously.
It does not call the public admin API, publish knowledge, or use daemon threads.
--check is strictly read-only, including when schemas have not been installed.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import time
from pathlib import Path

from . import knowledge, voc
from .config import Settings, load_settings

TERMINAL = frozenset(("completed", "partial", "failed"))


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _active(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if table not in _tables(conn):
        return []
    # table is always a fixed module constant, never a CLI/user parameter.
    return conn.execute(f"SELECT id,owner FROM {table} WHERE status IN ('queued','running')").fetchall()


def _live(rows: list[sqlite3.Row]) -> bool:
    return any(voc._owner_alive(row["owner"]) for row in rows)


def _pending(conn: sqlite3.Connection, cfg: Settings, scope: dict) -> int:
    if "voc_processed" not in _tables(conn):
        return len(scope)
    count = 0
    for pair in scope.values():
        old = conn.execute("SELECT fingerprint FROM voc_processed WHERE qa_id=? AND prompt_version=? AND model=?",
                           (pair["qa_id"], voc.PROMPT_VERSION, cfg.llm.model_main)).fetchone()
        count += not old or old["fingerprint"] != pair["_fingerprint"]
    return count


def _knowledge_candidates(conn: sqlite3.Connection, scope: dict) -> tuple[list[int], int, int]:
    needed, cached, unavailable = [], 0, 0
    if not {"voc_topic", "voc_assignment", "voc_topic_alias"} <= _tables(conn):
        return needed, cached, unavailable
    for topic in voc._topics(conn, scope):
        if topic["status"] == "ignored":
            continue
        try:
            selected_topic, pairs = knowledge._selected(conn, {}, topic["id"], 100)
            knowledge._require_staff_answers(pairs)
        except voc.VocError:
            unavailable += 1
            continue
        fingerprint = knowledge._fingerprint(selected_topic, pairs)
        # Automatic runs respect every existing disposition, including ignored.
        # Changing only the gateway/model is not a request to recreate assets.
        exists = "knowledge_record" in _tables(conn) and conn.execute(
            "SELECT 1 FROM knowledge_record WHERE topic_id=? AND fingerprint=? LIMIT 1",
            (topic["id"], fingerprint)).fetchone()
        if exists:
            cached += 1
        else:
            needed.append(topic["id"])
    return needed, cached, unavailable


def check(cfg: Settings, voc_limit: int = 100, knowledge_limit: int = 10) -> dict:
    """Inspect existing data without startup, DDL, job creation, or model calls."""
    _limits(voc_limit, knowledge_limit)
    path = Path(cfg.runtime.db_path).resolve()
    result = {"mode": "check", "configured": voc.configured(cfg), "model": cfg.llm.model_main,
              "database_exists": path.is_file(), "voc_limit": voc_limit, "knowledge_limit": knowledge_limit,
              "active_voc_jobs": 0, "active_knowledge_jobs": 0,
              "pending_pairs": 0, "knowledge_needed": 0, "knowledge_cached": 0,
              "knowledge_unavailable": 0, "would_run": False, "status": "disabled"}
    busy = False
    if path.is_file():
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            for table, field in (("voc_job", "active_voc_jobs"), ("knowledge_job", "active_knowledge_jobs")):
                rows = _active(conn, table)
                result[field] = len(rows)
                busy = busy or _live(rows)
            scope = voc._scope(conn, {})
            result["pending_pairs"] = _pending(conn, cfg, scope)
            topics, cached, unavailable = _knowledge_candidates(conn, scope)
            result.update(knowledge_needed=len(topics), knowledge_cached=cached, knowledge_unavailable=unavailable)
        finally:
            conn.close()
    if result["configured"]:
        result["would_run"] = not busy and (result["pending_pairs"] > 0 or knowledge_limit > 0 and result["knowledge_needed"] > 0)
        result["status"] = "busy" if busy else "ready" if result["would_run"] else "idle"
    return result


def _limits(voc_limit: int, knowledge_limit: int) -> None:
    if type(voc_limit) is not int or not 1 <= voc_limit <= 500:
        raise voc.VocError("每轮 VOC 条数须为 1–500")
    if type(knowledge_limit) is not int or not 0 <= knowledge_limit <= 100:
        raise voc.VocError("每轮知识主题数须为 0–100")


def _result(voc_limit: int, knowledge_limit: int) -> dict:
    return {"mode": "run", "status": "running", "reason": "",
            "voc_limit": voc_limit, "knowledge_limit": knowledge_limit, "voc_job_id": None,
            "voc_total": 0, "voc_processed": 0, "voc_failed": 0,
            "knowledge_job_ids": [], "knowledge_created": 0, "knowledge_failed": 0, "knowledge_skipped": 0,
            "usage_tokens": 0, "error": ""}


def _queue_knowledge(cfg: Settings, topic_id: int) -> dict | None:
    """Use the core job contract with atomic all-disposition automatic dedupe."""
    with knowledge._connection(cfg) as conn:
        with voc._tx(conn):
            knowledge._assert_idle(conn)
            try:
                topic, pairs = knowledge._selected(conn, {}, topic_id, 100)
                knowledge._require_staff_answers(pairs)
            except voc.VocError:
                return None  # Ignored/merged/removed or no longer usable evidence.
            fingerprint = knowledge._fingerprint(topic, pairs)
            if conn.execute("SELECT 1 FROM knowledge_record WHERE topic_id=? AND fingerprint=? LIMIT 1", (topic_id, fingerprint)).fetchone():
                return None
            now = time.time()
            job_id = conn.execute("INSERT INTO knowledge_job(topic_id,status,total,model,fingerprint,input_json,filters,owner,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (topic_id, "queued", len(pairs), cfg.llm.model_main, fingerprint,
                                   json.dumps({"topic": topic, "pairs": pairs}, ensure_ascii=False), "{}", voc._OWNER, now, now)).lastrowid
            return knowledge._public_job(conn.execute("SELECT * FROM knowledge_job WHERE id=?", (job_id,)).fetchone())


def _job(cfg: Settings, table: str, job_id: int) -> dict:
    conn = voc._open(cfg)
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] not in TERMINAL:
            raise voc.VocError("自动整理任务尚未进入终态，请检查任务状态")
        return dict(row)
    finally:
        conn.close()


def _fail_owned_jobs(cfg: Settings, message: str, result: dict) -> None:
    conn = voc._open(cfg)
    try:
        with voc._tx(conn):
            ids = [row[0] for row in conn.execute("SELECT id FROM voc_job WHERE id=? AND owner=? AND status IN ('queued','running')", (result["voc_job_id"], voc._OWNER))]
            for job_id in ids:
                conn.execute("UPDATE voc_job_item SET status='failed',error=? WHERE job_id=? AND status IN ('queued','running')", (message, job_id))
                conn.execute("UPDATE voc_job SET status='failed',failed=(SELECT COUNT(*) FROM voc_job_item WHERE job_id=? AND status='failed'),error=?,updated_at=? WHERE id=?", (job_id, message, time.time(), job_id))
            for job_id in result["knowledge_job_ids"]:
                conn.execute("UPDATE knowledge_job SET status='failed',failed=total-processed,error=?,updated_at=? WHERE id=? AND owner=? AND status IN ('queued','running')", (message, time.time(), job_id, voc._OWNER))
    finally:
        conn.close()


def run(cfg: Settings, voc_limit: int = 100, knowledge_limit: int = 10) -> dict:
    _limits(voc_limit, knowledge_limit)
    result = _result(voc_limit, knowledge_limit)
    from . import voc_service
    if voc_service.busy(cfg):
        result.update(status="skipped",reason="new_engine_busy")
        return result
    preflight = check(cfg, voc_limit, knowledge_limit)
    if not preflight["configured"]:
        result.update(status="skipped", reason="unconfigured")
        return result
    if preflight["status"] == "busy":
        result.update(status="skipped", reason="model_busy")
        return result
    # Only after the read-only live-owner check may startup recover dead owners.
    knowledge.startup(cfg)
    try:
        with voc._connection(cfg) as conn:
            pending = _pending(conn, cfg, voc._scope(conn, {}))
        if pending:
            job = voc.start_job(cfg, {}, {"limit": voc_limit}, launch=False)["job"]
            result.update(voc_job_id=job["id"], voc_total=job["total"])
            if job["total"]:
                # Synchronous execution: the oneshot process cannot exit early
                # leaving a daemon worker in queued/running state.
                voc._worker(cfg, job["id"])
            finished = _job(cfg, "voc_job", job["id"])
            result.update(voc_processed=finished["processed"], voc_failed=finished["failed"])
            result["usage_tokens"] += finished["usage_tokens"]
        with knowledge._connection(cfg) as conn:
            topics, cached, unavailable = _knowledge_candidates(conn, voc._scope(conn, {}))
        result["knowledge_skipped"] = cached + unavailable
        for topic_id in topics[:knowledge_limit]:
            try:
                job = _queue_knowledge(cfg, topic_id)
            except voc.VocError as exc:
                if exc.status == 409:
                    raise
                result["knowledge_skipped"] += 1
                continue
            if job is None:
                result["knowledge_skipped"] += 1
                continue
            result["knowledge_job_ids"].append(job["id"])
            knowledge._worker(cfg, job["id"])
            finished = _job(cfg, "knowledge_job", job["id"])
            result["knowledge_created"] += finished["status"] == "completed"
            result["knowledge_failed"] += finished["status"] != "completed"
            result["usage_tokens"] += finished["usage_tokens"]
        result["status"] = "partial" if result["voc_failed"] or result["knowledge_failed"] else "completed"
        if result["voc_job_id"] is None and not result["knowledge_created"] and not result["knowledge_failed"]:
            result["reason"] = "no_changes"
    except KeyboardInterrupt:
        result.update(status="failed", reason="interrupted", error="自动整理进程已中断，可下轮重试")
        _fail_owned_jobs(cfg, result["error"], result)
    except Exception as exc:
        if isinstance(exc, voc.VocError) and exc.status == 409:
            result.update(status="partial" if result["voc_processed"] or result["knowledge_created"] else "skipped", reason="model_busy")
        else:
            result.update(status="failed", error=voc._job_error(exc, "自动整理失败，请查看任务状态后重试", allow_local=True))
            _fail_owned_jobs(cfg, result["error"], result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每小时增量整理 VOC 与待审知识，不自动确认或发布")
    parser.add_argument("--voc-limit", type=int, default=100)
    parser.add_argument("--knowledge-limit", type=int, default=10)
    parser.add_argument("--check", action="store_true", help="只读检查，不建任务或调用模型")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        cfg = load_settings()
        if args.check:
            result = check(cfg, args.voc_limit, args.knowledge_limit)
        else:
            # systemctl stop/SIGTERM follows the same owned-job interruption path.
            def interrupted(signum, frame):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, interrupted)
            result = run(cfg, args.voc_limit, args.knowledge_limit)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if result["status"] in ("failed", "partial") else 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": voc._job_error(exc, "自动整理配置或服务异常", allow_local=True)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
