"""Versioned VOC topics and asynchronous analysis, isolated from the rule pipeline.

Every request/worker owns its sqlite connection. Model calls never run in a DB
transaction or through app.get_db(). Existing raw/QA/rule-cluster rows are read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .clean.rules import mask_pii
from .config import Settings
from .extract.text import clean_visible_text
from .extract.identity import HIDDEN_ROLES, SOURCE_IDENTITY_ROLE, sender_context
from .voc_model import PROMPT_VERSION, VocModelError, analyze_batch

SCHEMA = """
CREATE TABLE IF NOT EXISTS voc_topic (
 id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, question_key TEXT NOT NULL UNIQUE,
 theme TEXT NOT NULL, boundary TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','confirmed','ignored')),
 revision INTEGER NOT NULL DEFAULT 1, manual INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS voc_assignment (
 qa_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','confirmed','excluded')),
 confidence REAL NOT NULL, reason TEXT NOT NULL, fingerprint TEXT NOT NULL,
 prompt_version TEXT NOT NULL, model TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(qa_id,topic_id));
CREATE INDEX IF NOT EXISTS voc_assignment_topic ON voc_assignment(topic_id);
CREATE TABLE IF NOT EXISTS voc_processed (
 qa_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, prompt_version TEXT NOT NULL, model TEXT NOT NULL,
 disposition TEXT NOT NULL, reason TEXT NOT NULL, job_id INTEGER NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(qa_id,prompt_version,model));
CREATE TABLE IF NOT EXISTS voc_job (
 id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,
 total INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
 failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
 model TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
 usage_tokens INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL, filters TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS voc_one_active_job ON voc_job((1)) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS voc_job_item (
 job_id INTEGER NOT NULL, qa_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 input_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(job_id,qa_id));
CREATE TABLE IF NOT EXISTS voc_topic_alias (source_id INTEGER PRIMARY KEY,target_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS voc_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, topic_id INTEGER,
 qa_id INTEGER, detail TEXT NOT NULL, created_at REAL NOT NULL);
"""

class VocError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _job_error(exc: BaseException, fallback: str, *, allow_local: bool = False) -> str:
    """Only designated safe errors may cross the worker/API boundary."""
    safe_types = (VocModelError, VocError) if allow_local else (VocModelError,)
    if isinstance(exc, safe_types):
        return str(exc).strip()[:240] or fallback
    return fallback

_INIT_LOCK = threading.RLock()
_INITIALIZED: set[str] = set()
_WORKERS: dict[str, threading.Thread] = {}
_OWNER = f"{os.getpid()}:{uuid.uuid4().hex}"

def _path(cfg: Settings) -> str:
    return str(Path(cfg.runtime.db_path).resolve())

def _open(cfg: Settings) -> sqlite3.Connection:
    path = _path(cfg)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn

@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

def _owner_alive(owner: str) -> bool:
    if owner == _OWNER:
        return True
    try:
        pid = int(owner.split(":", 1)[0])
        if pid == os.getpid():
            return False
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
            return bool(handle)
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ValueError, ProcessLookupError, OSError):
        return False

def startup(cfg: Settings) -> None:
    """Initialize/recover once per DB in this process; never cancel a live job."""
    path = _path(cfg)
    with _INIT_LOCK:
        if path in _INITIALIZED:
            return
        conn = _open(cfg)
        try:
            conn.executescript(SCHEMA)
            with _tx(conn):
                for row in conn.execute("SELECT id,owner FROM voc_job WHERE status IN ('queued','running')").fetchall():
                    if not _owner_alive(row["owner"]):
                        job_id = row["id"]
                        conn.execute("UPDATE voc_job_item SET status='failed',error='服务重启中断，请重试' WHERE job_id=? AND status IN ('queued','running')", (job_id,))
                        failures = conn.execute("SELECT COUNT(*) FROM voc_job_item WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
                        conn.execute("UPDATE voc_job SET status='failed',failed=?,error='服务重启中断，请重试',updated_at=? WHERE id=?", (failures, time.time(), job_id))
            _INITIALIZED.add(path)
        finally:
            conn.close()

@contextmanager
def _connection(cfg: Settings) -> Iterator[sqlite3.Connection]:
    startup(cfg)
    conn = _open(cfg)
    try:
        yield conn
    finally:
        conn.close()

def configured(cfg: Settings) -> bool:
    return bool(not cfg.runtime.mock and not cfg.llm.dry_run and cfg.llm.base_url.strip()
                and cfg.llm.model_main.strip() and cfg.llm.api_key)

def _when(stamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else ""

def _asked(stamp: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp / 1000)) if stamp else "时间未知"

def _key(text: str) -> str:
    return " ".join(text.split()).casefold()

def _text(value: Any, name: str, limit: int, minimum: int = 0) -> str:
    if not isinstance(value, str):
        raise VocError(f"{name}须为 {minimum}–{limit} 字的文字")
    result = mask_pii(clean_visible_text(value.strip()))
    if not minimum <= len(result) <= limit:
        raise VocError(f"{name}须为 {minimum}–{limit} 字的文字")
    return result

def _integer(value: Any, name: str, minimum: int = 1, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VocError(f"{name}须为 {minimum}–{maximum} 的整数")
    return value

def _bounds(filters: dict[str, Any]) -> tuple[int, int]:
    days = int(filters.get("days") or 0)
    if not 0 <= days <= 3660:
        raise VocError("日期范围须在 0–3660 天之间")
    try:
        first = date.fromisoformat(filters["start"]) if filters.get("start") else None
        last = date.fromisoformat(filters["end"]) if filters.get("end") else None
    except (TypeError, ValueError):
        raise VocError("日期格式应为 YYYY-MM-DD") from None
    if first and last and first > last:
        raise VocError("开始日期不能晚于结束日期")
    if not first and not last and days:
        first, last = date.today() - timedelta(days=days - 1), date.today()
    low = int(datetime.combine(first, datetime.min.time()).timestamp() * 1000) if first else 0
    high = int(datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp() * 1000) if last else 0
    return low, high

def _scope(conn: sqlite3.Connection, filters: dict[str, Any], *, qa_id: int | None = None,
           include_messages: bool = True, _bounds_override: tuple[int, int] | None = None) -> dict[int, dict[str, Any]]:
    low, high = _bounds(filters) if _bounds_override is None else _bounds_override
    try:
        if qa_id is None:
            query, args = "SELECT * FROM qa_pair", []
            if _bounds_override and (low or high):
                query += " WHERE asked_at>0"
                if low:
                    query += " AND asked_at>=?"
                    args.append(low)
                if high:
                    query += " AND asked_at<?"
                    args.append(high)
            raw_rows = conn.execute(query + " ORDER BY asked_at DESC,qa_id DESC", args).fetchall()
        else:
            raw_rows = conn.execute("SELECT * FROM qa_pair WHERE qa_id=?", (qa_id,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return {}
        raise
    out: dict[int, dict[str, Any]] = {}
    context_cache: dict[str, tuple[list[dict], list[dict]]] = {}
    for raw in raw_rows:
        row = dict(raw)
        stamp = int(row.get("asked_at") or 0)
        if (low or high) and (not stamp or (low and stamp < low) or (high and stamp >= high)):
            continue
        question = mask_pii(clean_visible_text(row.get("question") or ""))
        if not question:
            continue
        answer = mask_pii(clean_visible_text(row.get("answer") or ""))
        sid = str(row.get("session_id") or "")
        cid = str(row.get("customer_id") or "") or "anonymous-" + hashlib.sha256(sid.encode()).hexdigest()[:16]
        if mask_pii(cid) != cid:
            cid = "customer-" + hashlib.sha256(cid.encode()).hexdigest()[:16]
        public = {"qa_id": int(row["qa_id"]), "question": question, "answer": answer,
                  "customer_id": cid, "customer_name": mask_pii(str(row.get("customer_name") or "匿名客户")),
                  "group_name": mask_pii(str(row.get("group_name") or "未分组")),
                  "staff_name": mask_pii(str(row.get("staff_name") or "未记录客服")),
                  "session_id": mask_pii(sid), "cluster_id": int(row.get("cluster_id") or 0),
                  "asked_at": stamp, "asked_at_text": _asked(stamp)}
        if any(filters.get(k) and public[v] != filters[k] for k, v in (("group", "group_name"), ("staff", "staff_name"), ("customer", "customer_id"))):
            continue
        if filters.get("cluster") and int(filters["cluster"]) != public["cluster_id"]:
            continue
        if filters.get("replied") == "answered" and not answer or filters.get("replied") == "unanswered" and answer:
            continue
        query = str(filters.get("q") or "").casefold()
        if query and query not in " ".join(str(public[k]) for k in ("question", "answer", "customer_name", "session_id")).casefold():
            continue
        if not include_messages:
            out[public["qa_id"]] = public
            continue
        if sid not in context_cache:
            try:
                messages = [dict(m) for m in conn.execute("SELECT role,content,ts FROM raw_message WHERE session_id=? ORDER BY seq", (sid,))]
            except sqlite3.OperationalError:
                messages = []
            visible = []
            for message in messages:
                content = mask_pii(clean_visible_text(message.get("content") or ""))
                if message.get("role") not in HIDDEN_ROLES and content:
                    visible.append({"role": str(message.get("role") or ""), "content": content})
            context_cache[sid] = messages, visible
        messages, visible = context_cache[sid]
        if any(message.get("role") == SOURCE_IDENTITY_ROLE for message in messages):
            visible = []
            for message in sender_context(messages, sid, str(row.get("customer_id") or "")):
                content = mask_pii(clean_visible_text(message.get("content") or ""))
                if content:
                    visible.append({"role": str(message.get("role") or ""), "content": content})
        input_row = {key: row.get(key) for key in ("qa_id", "session_id", "question", "answer", "customer_id", "customer_name", "group_name", "staff_name", "asked_at")}
        fingerprint = hashlib.sha256(json.dumps([input_row, messages], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
        public["_fingerprint"] = fingerprint
        public["messages"] = visible
        out[public["qa_id"]] = public
    return out

def _public_job(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    fields = ("id", "status", "total", "processed", "failed", "skipped", "model", "error", "usage_tokens")
    return {**{k: row[k] for k in fields}, "created_at": _when(row["created_at"]), "updated_at": _when(row["updated_at"])}

def _topic(conn: sqlite3.Connection, topic_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM voc_topic WHERE id=?", (topic_id,)).fetchone()
    if row is None:
        raise VocError("VOC 主题不存在", 404)
    return row

def _members(conn: sqlite3.Connection, topic_id: int, scope: dict[int, dict]) -> list[dict]:
    result = []
    for item in conn.execute("SELECT * FROM voc_assignment WHERE topic_id=? AND status IN ('pending','confirmed')", (topic_id,)):
        pair = scope.get(item["qa_id"])
        if pair and pair["_fingerprint"] == item["fingerprint"]:
            result.append({**{k: v for k, v in pair.items() if not k.startswith("_") and k != "messages"},
                           "confidence": item["confidence"], "reason": item["reason"], "status": item["status"]})
    return sorted(result, key=lambda r: (-r["asked_at"], -r["qa_id"]))

def _public_topic(conn: sqlite3.Connection, row: sqlite3.Row, scope: dict[int, dict]) -> dict:
    members = _members(conn, row["id"], scope)
    stamps = [m["asked_at"] for m in members if m["asked_at"]]
    return {"id": row["id"], "question": row["question"], "theme": row["theme"], "boundary": row["boundary"],
            "status": row["status"], "revision": row["revision"], "count": len(members),
            "answered_count": sum(bool(m["answer"].strip()) for m in members),
            "customers": len({m["customer_id"] for m in members}),
            "pending_count": sum(m["status"] == "pending" for m in members),
            "first_seen": _asked(min(stamps)) if stamps else "", "last_seen": _asked(max(stamps)) if stamps else "",
            "samples": list(dict.fromkeys(m["question"] for m in members))[:5]}

def _topics(conn: sqlite3.Connection, scope: dict[int, dict], status: str = "all", topic_q: str = "") -> list[dict]:
    if status not in ("all", "pending", "confirmed", "ignored"):
        raise VocError("VOC 主题状态无效")
    result = []
    for row in conn.execute("SELECT * FROM voc_topic ORDER BY id"):
        if status != "all" and row["status"] != status:
            continue
        if topic_q and topic_q.casefold() not in " ".join((row["question"], row["theme"], row["boundary"])).casefold():
            continue
        public = _public_topic(conn, row, scope)
        # Include empty manually-created topics, but not unrelated filtered topics.
        has_members = conn.execute("SELECT 1 FROM voc_assignment WHERE topic_id=? LIMIT 1", (row["id"],)).fetchone()
        if public["count"] or not has_members:
            result.append(public)
    return sorted(result, key=lambda t: (-t["count"], t["id"]))

def summary(cfg: Settings, filters: dict[str, Any]) -> dict:
    with _connection(cfg) as conn:
        scope = _scope(conn, filters)
        processed = 0
        for pair in scope.values():
            hit = conn.execute("SELECT fingerprint FROM voc_processed WHERE qa_id=? AND prompt_version=? AND model=?", (pair["qa_id"], PROMPT_VERSION, cfg.llm.model_main)).fetchone()
            processed += bool(hit and hit["fingerprint"] == pair["_fingerprint"])
        topics = _topics(conn, scope)
        return {"configured": configured(cfg), "model": cfg.llm.model_main, "prompt_version": PROMPT_VERSION,
                "total_pairs": len(scope), "pending_pairs": len(scope) - processed, "processed_pairs": processed,
                "answered_pairs": sum(bool(pair["answer"].strip()) for pair in scope.values()),
                "topic_count": len(topics), "pending_topics": sum(t["status"] == "pending" for t in topics),
                "confirmed_topics": sum(t["status"] == "confirmed" for t in topics), "ignored_topics": sum(t["status"] == "ignored" for t in topics),
                "latest_job": _public_job(conn.execute("SELECT * FROM voc_job ORDER BY id DESC LIMIT 1").fetchone())}

def topics(cfg: Settings, filters: dict, status: str = "all", topic_q: str = "") -> dict:
    with _connection(cfg) as conn:
        items = _topics(conn, _scope(conn, filters), status, topic_q)
        return {"items": items, "total": len(items)}

def detail(cfg: Settings, filters: dict, topic_id: int, limit: int = 100, offset: int = 0, evidence_status: str = "all") -> dict:
    with _connection(cfg) as conn:
        scope = _scope(conn, filters)
        topic = _topic(conn, topic_id)
        evidence = _members(conn, topic_id, scope)
        if evidence_status not in ("all", "pending", "confirmed"):
            raise VocError("证据状态无效")
        if evidence_status != "all":
            evidence = [member for member in evidence if member["status"] == evidence_status]
        return {"topic": _public_topic(conn, topic, scope), "evidence": evidence[max(0, offset):max(0, offset) + max(1, min(500, limit))], "total": len(evidence)}

def jobs(cfg: Settings) -> dict:
    with _connection(cfg) as conn:
        return {"items": [_public_job(r) for r in conn.execute("SELECT * FROM voc_job ORDER BY id DESC LIMIT 30")]}

def _audit(conn: sqlite3.Connection, action: str, topic_id: int | None, qa_id: int | None, detail: dict) -> None:
    conn.execute("INSERT INTO voc_audit(action,topic_id,qa_id,detail,created_at) VALUES (?,?,?,?,?)", (action, topic_id, qa_id, json.dumps(detail, ensure_ascii=False), time.time()))

def _revision(row: sqlite3.Row, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != row["revision"]:
        raise VocError("主题已更新，请刷新后重试", 409)

def _resolve(conn: sqlite3.Connection, topic_id: int) -> int:
    seen: set[int] = set()
    while topic_id not in seen:
        seen.add(topic_id)
        alias = conn.execute("SELECT target_id FROM voc_topic_alias WHERE source_id=?", (topic_id,)).fetchone()
        if not alias:
            return topic_id
        topic_id = alias["target_id"]
    raise VocError("主题合并关系异常", 409)

def _new_topic(conn: sqlite3.Connection, question: str, theme: str, boundary: str, manual: bool = False, status: str = "pending") -> int:
    now = time.time()
    return conn.execute("INSERT INTO voc_topic(question,question_key,theme,boundary,status,manual,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (question, _key(question), theme, boundary, status, int(manual), now, now)).lastrowid

def save_topic(cfg: Settings, filters: dict, body: dict) -> dict:
    question = _text(body.get("question"), "标准问题", 120, 3)
    theme = _text(body.get("theme"), "主题", 60, 1)
    boundary = _text(body.get("boundary", ""), "归并边界", 500)
    status = body.get("status", "pending")
    if status not in ("pending", "confirmed", "ignored"):
        raise VocError("VOC 主题状态无效")
    with _connection(cfg) as conn:
        scope = _scope(conn, filters)
        try:
            with _tx(conn):
                if body.get("id") is None:
                    topic_id = _new_topic(conn, question, theme, boundary, True, status)
                else:
                    topic_id = _integer(body["id"], "主题 ID")
                    row = _topic(conn, topic_id)
                    _revision(row, body.get("revision"))
                    if _resolve(conn, topic_id) != topic_id:
                        raise VocError("此主题已合并，请编辑目标主题", 409)
                    conn.execute("UPDATE voc_topic SET question=?,question_key=?,theme=?,boundary=?,status=?,manual=1,revision=revision+1,updated_at=? WHERE id=?", (question, _key(question), theme, boundary, status, time.time(), topic_id))
                if status == "confirmed":
                    for member in _members(conn, topic_id, scope):
                        if member["status"] == "pending":
                            conn.execute("UPDATE voc_assignment SET status='confirmed',updated_at=? WHERE topic_id=? AND qa_id=?", (time.time(), topic_id, member["qa_id"]))
                _audit(conn, "save_topic", topic_id, None, {"question": question, "theme": theme, "boundary": boundary, "status": status, "scope_qa_ids": list(scope)})
        except sqlite3.IntegrityError:
            raise VocError("已有同名标准问题，请合并或使用不同名称", 409) from None
        return {"topic": _public_topic(conn, _topic(conn, topic_id), scope)}

def evidence_action(cfg: Settings, filters: dict, body: dict) -> dict:
    topic_id = _integer(body.get("topic_id"), "主题 ID")
    qa_ids = body.get("qa_ids")
    if not isinstance(qa_ids, list) or not 1 <= len(qa_ids) <= 500:
        raise VocError("请选择 1–500 条证据问答")
    qa_ids = [_integer(value, "问答 ID") for value in qa_ids]
    if len(set(qa_ids)) != len(qa_ids):
        raise VocError("问答 ID 不能重复")
    action = body.get("action")
    if action not in ("exclude", "confirm", "split"):
        raise VocError("证据操作无效")
    with _connection(cfg) as conn:
        scope = _scope(conn, filters)
        with _tx(conn):
            topic = _topic(conn, topic_id)
            _revision(topic, body.get("revision"))
            members = {r["qa_id"]: r for r in conn.execute("SELECT * FROM voc_assignment WHERE topic_id=?", (topic_id,))}
            for qa_id in qa_ids:
                if qa_id not in scope or qa_id not in members or members[qa_id]["fingerprint"] != scope[qa_id]["_fingerprint"]:
                    raise VocError("证据不在当前有效范围，请刷新", 409)
            target_id = topic_id
            if action == "split":
                question = _text(body.get("new_question"), "新标准问题", 120, 3)
                theme = _text(body.get("new_theme", topic["theme"]), "新主题", 60, 1)
                try:
                    target_id = _new_topic(conn, question, theme, "人工拆分，请核对归并边界", True)
                except sqlite3.IntegrityError:
                    raise VocError("已有同名标准问题，请改名或合并", 409) from None
            changed = False
            for qa_id in qa_ids:
                member = members[qa_id]
                desired = "confirmed" if action == "confirm" else "excluded"
                if member["status"] == "excluded" and action != "exclude":
                    raise VocError("已排除的证据不能直接确认或拆分", 409)
                if member["status"] != desired:
                    conn.execute("UPDATE voc_assignment SET status=?,updated_at=? WHERE topic_id=? AND qa_id=?", (desired, time.time(), topic_id, qa_id))
                    _audit(conn, action, topic_id, qa_id, {"target_id": target_id, "previous_status": member["status"]})
                    changed = True
                elif action == "exclude" and not _manually_excluded(conn, qa_id, topic_id):
                    # Explicitly excluding a previously model-retired member is
                    # still a new human decision, not an automatic exclusion.
                    _audit(conn, action, topic_id, qa_id, {"target_id": target_id, "previous_status": member["status"]})
                    changed = True
                if action == "split":
                    conn.execute("INSERT INTO voc_assignment VALUES (?,?,?,?,?,?,?,?,?,?)", (qa_id, target_id, "pending", member["confidence"], "人工拆分：" + member["reason"][:490], member["fingerprint"], member["prompt_version"], member["model"], time.time(), time.time()))
            if changed:
                conn.execute("UPDATE voc_topic SET revision=revision+1,updated_at=? WHERE id=?", (time.time(), topic_id))
        return {"topic": _public_topic(conn, _topic(conn, target_id), scope)}

def merge_topics(cfg: Settings, filters: dict, body: dict) -> dict:
    source_id = _integer(body.get("source_id"), "来源主题 ID")
    target_id = _integer(body.get("target_id"), "目标主题 ID")
    if source_id == target_id:
        raise VocError("不能合并到自身")
    with _connection(cfg) as conn:
        scope = _scope(conn, filters)
        with _tx(conn):
            source, target = _topic(conn, source_id), _topic(conn, target_id)
            _revision(source, body.get("source_revision"))
            _revision(target, body.get("target_revision"))
            if _resolve(conn, source_id) != source_id or _resolve(conn, target_id) != target_id or target["status"] == "ignored":
                raise VocError("主题已合并或目标被忽略，请刷新", 409)
            current = _scope(conn, {})
            active_source_ids = {member["qa_id"] for member in _members(conn, source_id, current)}
            moved_ids, restored_ids = [], []
            for member in conn.execute("SELECT * FROM voc_assignment WHERE topic_id=?", (source_id,)).fetchall():
                if member["qa_id"] not in active_source_ids:
                    continue
                existing = conn.execute("SELECT * FROM voc_assignment WHERE qa_id=? AND topic_id=?", (member["qa_id"], target_id)).fetchone()
                if existing and existing["status"] == "excluded":
                    # This explicit human merge supersedes the target's older
                    # exclusion, notably the tombstone left by a prior split.
                    _audit(conn, "merge_restore", target_id, member["qa_id"],
                           {"source_id": source_id, "previous_status": "excluded",
                            "previously_manual": _manually_excluded(conn, member["qa_id"], target_id)})
                    restored_ids.append(member["qa_id"])
                current_fingerprint = current.get(member["qa_id"], {}).get("_fingerprint")
                if not existing or existing["status"] == "excluded" or (member["fingerprint"] == current_fingerprint and existing["fingerprint"] != current_fingerprint):
                    conn.execute("INSERT INTO voc_assignment VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(qa_id,topic_id) DO UPDATE SET status=excluded.status,confidence=excluded.confidence,reason=excluded.reason,fingerprint=excluded.fingerprint,prompt_version=excluded.prompt_version,model=excluded.model,updated_at=excluded.updated_at", (member["qa_id"], target_id, member["status"], member["confidence"], member["reason"], member["fingerprint"], member["prompt_version"], member["model"], time.time(), time.time()))
                elif existing["status"] != "excluded" and existing["fingerprint"] == member["fingerprint"] and member["status"] == "confirmed":
                    conn.execute("UPDATE voc_assignment SET status='confirmed',updated_at=? WHERE qa_id=? AND topic_id=?", (time.time(), member["qa_id"], target_id))
                moved_ids.append(member["qa_id"])
            conn.execute("UPDATE voc_assignment SET status='excluded',updated_at=? WHERE topic_id=?", (time.time(), source_id))
            conn.execute("INSERT INTO voc_topic_alias VALUES (?,?)", (source_id, target_id))
            conn.execute("UPDATE voc_topic SET status='ignored',manual=1,revision=revision+1,updated_at=? WHERE id=?", (time.time(), source_id))
            conn.execute("UPDATE voc_topic SET manual=1,revision=revision+1,updated_at=? WHERE id=?", (time.time(), target_id))
            _audit(conn, "merge", source_id, None,
                   {"target_id": target_id, "moved_qa_ids": moved_ids,
                    "restored_target_exclusions": restored_ids})
        return {"topic": _public_topic(conn, _topic(conn, target_id), scope)}

def start_job(cfg: Settings, filters: dict, body: dict, launch: bool = True) -> dict:
    if cfg.runtime.mock:
        raise VocError("演示库禁止调用真实模型，请切换业务数据")
    if not configured(cfg):
        raise VocError("模型尚未配置或仍处于演练模式，请联系管理员")
    limit = _integer(body.get("limit", 500), "整理条数", 1, 500)
    with _connection(cfg) as conn:
        scope = _scope(conn, filters)
        candidates = []
        for pair in scope.values():
            done = conn.execute("SELECT fingerprint FROM voc_processed WHERE qa_id=? AND prompt_version=? AND model=?", (pair["qa_id"], PROMPT_VERSION, cfg.llm.model_main)).fetchone()
            if not done or done["fingerprint"] != pair["_fingerprint"]:
                candidates.append(pair)
        candidates = candidates[:limit]
        try:
            with _tx(conn):
                if conn.execute("SELECT 1 FROM voc_job WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    raise VocError("当前数据源已有 VOC 整理任务运行，请等待完成", 409)
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_job'").fetchone() and conn.execute("SELECT 1 FROM knowledge_job WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    raise VocError("当前数据源已有知识整理任务运行，请等待完成", 409)
                now = time.time()
                status = "queued" if candidates else "completed"
                job_id = conn.execute("INSERT INTO voc_job(status,total,model,created_at,updated_at,owner,filters) VALUES (?,?,?,?,?,?,?)", (status, len(candidates), cfg.llm.model_main, now, now, _OWNER, json.dumps(filters, ensure_ascii=False))).lastrowid
                conn.executemany("INSERT INTO voc_job_item(job_id,qa_id,fingerprint,input_json) VALUES (?,?,?,?)", [(job_id, p["qa_id"], p["_fingerprint"], json.dumps(p, ensure_ascii=False)) for p in candidates])
        except sqlite3.IntegrityError:
            raise VocError("当前数据源已有 VOC 整理任务运行，请等待完成", 409) from None
        job = _public_job(conn.execute("SELECT * FROM voc_job WHERE id=?", (job_id,)).fetchone())
    if candidates and launch:
        thread = threading.Thread(target=_worker, args=(cfg, job_id), name=f"voc-{job_id}", daemon=True)
        with _INIT_LOCK:
            _WORKERS[_path(cfg)] = thread
        thread.start()
    return {"job": job}

def _manually_excluded(conn: sqlite3.Connection, qa_id: int, topic_id: int) -> bool:
    decision = conn.execute("SELECT action FROM voc_audit WHERE topic_id=? AND ((qa_id=? AND action IN ('exclude','split','merge_restore')) OR action='merge') ORDER BY id DESC LIMIT 1", (topic_id, qa_id)).fetchone()
    return bool(decision and decision["action"] in ("exclude", "split", "merge"))


def _apply_result(conn: sqlite3.Connection, cfg: Settings, job_id: int, result: dict, pair: dict) -> None:
    qa_id, fingerprint = pair["qa_id"], pair["_fingerprint"]
    wanted: set[int] = set()
    for intent in result["intents"]:
        if intent["topic_id"] is not None:
            topic_id = _resolve(conn, intent["topic_id"])
            topic = _topic(conn, topic_id)
        else:
            topic = conn.execute("SELECT * FROM voc_topic WHERE question_key=?", (_key(intent["question"]),)).fetchone()
            if topic:
                topic_id = _resolve(conn, topic["id"])
                topic = _topic(conn, topic_id)
            else:
                topic_id = _new_topic(conn, intent["question"], intent["theme"], intent["boundary"])
                topic = _topic(conn, topic_id)
        if topic["status"] == "ignored":
            continue
        wanted.add(topic_id)
        previous = conn.execute("SELECT * FROM voc_assignment WHERE qa_id=? AND topic_id=?", (qa_id, topic_id)).fetchone()
        if previous and previous["status"] == "excluded" and _manually_excluded(conn, qa_id, topic_id):
            continue
        state = "confirmed" if previous and previous["status"] == "confirmed" and previous["fingerprint"] == fingerprint else "pending"
        now = time.time()
        conn.execute("INSERT INTO voc_assignment VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(qa_id,topic_id) DO UPDATE SET status=excluded.status,confidence=excluded.confidence,reason=excluded.reason,fingerprint=excluded.fingerprint,prompt_version=excluded.prompt_version,model=excluded.model,updated_at=excluded.updated_at", (qa_id, topic_id, state, intent["confidence"], intent["reason"], fingerprint, PROMPT_VERSION, cfg.llm.model_main, now, now))
        conn.execute("UPDATE voc_topic SET revision=revision+1,updated_at=? WHERE id=?", (now, topic_id))
    # Stale model-only memberships do not survive a changed input. Human/excluded
    # decisions remain auditable and stale fingerprints are never counted.
    for old in conn.execute("SELECT topic_id FROM voc_assignment WHERE qa_id=? AND status='pending'", (qa_id,)).fetchall():
        if old["topic_id"] not in wanted:
            conn.execute("UPDATE voc_assignment SET status='excluded',updated_at=? WHERE qa_id=? AND topic_id=?", (time.time(), qa_id, old["topic_id"]))
            conn.execute("UPDATE voc_topic SET revision=revision+1,updated_at=? WHERE id=?", (time.time(), old["topic_id"]))
    conn.execute("INSERT INTO voc_processed VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(qa_id,prompt_version,model) DO UPDATE SET fingerprint=excluded.fingerprint,disposition=excluded.disposition,reason=excluded.reason,job_id=excluded.job_id,updated_at=excluded.updated_at", (qa_id, fingerprint, PROMPT_VERSION, cfg.llm.model_main, "excluded" if result["excluded"] else "processed", result["reason"], job_id, time.time()))
    conn.execute("UPDATE voc_job_item SET status='completed',error='' WHERE job_id=? AND qa_id=?", (job_id, qa_id))

def _can_split_batch(exc: BaseException) -> bool:
    """Only safe model failures can improve with a smaller input batch."""
    if not isinstance(exc, VocModelError) or exc.retryable is not True:
        return False
    # The adapter already exposes only a safe, fixed HTTP status message. In
    # particular, retryable 429 must not fan out into more rate-limited calls.
    status = getattr(exc, "status", None)
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc))
    if match:
        status = int(match.group(1))
    return not (type(status) is int and 400 <= status < 500 and status != 408)


def _worker(cfg: Settings, job_id: int) -> None:
    try:
        with _connection(cfg) as conn:
            conn.execute("UPDATE voc_job SET status='running',updated_at=? WHERE id=?", (time.time(), job_id))
            pending = [dict(r) for r in conn.execute("SELECT * FROM voc_job_item WHERE job_id=? ORDER BY qa_id", (job_id,))]
        batch_size = max(1, min(20, cfg.llm.batch_size))
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            batches = [[json.loads(item["input_json"]) for item in chunk]]
            # Each failed parent is replaced by two disjoint, smaller children.
            # Leaves are attempted once; a chunk of N rows makes at most 2N-1
            # serial calls. A failed leaf never prevents its sibling from running.
            while batches:
                pairs = batches.pop()
                with _connection(cfg) as conn:
                    available = [dict(r) for r in conn.execute("SELECT id,question,theme,boundary,status FROM voc_topic WHERE status IN ('pending','confirmed') ORDER BY id")]
                # No live sqlite connection or transaction is held during a call.
                try:
                    output = analyze_batch(cfg.llm, pairs, available)
                except Exception as exc:
                    usage = exc.usage_tokens if isinstance(exc, VocModelError) else 0
                    consumed = usage if type(usage) is int and usage >= 0 else 0
                    split = len(pairs) > 1 and _can_split_batch(exc)
                    message = _job_error(exc, "模型调用或结果校验失败，可重试")
                    with _connection(cfg) as conn, _tx(conn):
                        conn.execute("UPDATE voc_job SET usage_tokens=usage_tokens+?,updated_at=? WHERE id=?", (consumed, time.time(), job_id))
                        if not split:
                            conn.executemany("UPDATE voc_job_item SET status='failed',error=? WHERE job_id=? AND qa_id=?", [(message, job_id, p["qa_id"]) for p in pairs])
                            conn.execute("UPDATE voc_job SET failed=failed+?,error=? WHERE id=?", (len(pairs), message, job_id))
                    if split:
                        middle = len(pairs) // 2
                        batches.extend((pairs[middle:], pairs[:middle]))
                    continue
                with _connection(cfg) as conn:
                    with _tx(conn):
                        current = _scope(conn, {})
                        conn.execute("UPDATE voc_job SET usage_tokens=usage_tokens+? WHERE id=?", (output["usage_tokens"], job_id))
                        for result in output["results"]:
                            # Bridge guarantees coverage but may reorder results.
                            pair = next(p for p in pairs if p["qa_id"] == result["qa_id"])
                            live = current.get(pair["qa_id"])
                            if not live or live["_fingerprint"] != pair["_fingerprint"]:
                                conn.execute("UPDATE voc_job_item SET status='failed',error='分析期间原问答已变更，请重试' WHERE job_id=? AND qa_id=?", (job_id, pair["qa_id"]))
                                conn.execute("UPDATE voc_job SET failed=failed+1,error='分析期间有原问答变更，请重试' WHERE id=?", (job_id,))
                                continue
                            _apply_result(conn, cfg, job_id, result, pair)
                            conn.execute("UPDATE voc_job SET processed=processed+1,updated_at=? WHERE id=?", (time.time(), job_id))
        with _connection(cfg) as conn, _tx(conn):
            job = conn.execute("SELECT * FROM voc_job WHERE id=?", (job_id,)).fetchone()
            status = "partial" if job["failed"] and job["processed"] else "failed" if job["failed"] else "completed"
            conn.execute("UPDATE voc_job SET status=?,error=CASE WHEN ?='completed' THEN '' ELSE error END,updated_at=? WHERE id=?", (status, status, time.time(), job_id))
    except Exception as exc:
        message = _job_error(exc, "任务执行中断，可重试")
        try:
            with _connection(cfg) as conn, _tx(conn):
                conn.execute("UPDATE voc_job_item SET status='failed',error=? WHERE job_id=? AND status IN ('queued','running')", (message, job_id))
                failed = conn.execute("SELECT COUNT(*) FROM voc_job_item WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
                conn.execute("UPDATE voc_job SET status='failed',failed=?,error=?,updated_at=? WHERE id=?", (failed, message, time.time(), job_id))
        except Exception:
            pass
    finally:
        with _INIT_LOCK:
            _WORKERS.pop(_path(cfg), None)

def handle_get(cfg: Settings, name: str, filters: dict, *, status: str = "all", topic_q: str = "", topic_id: int = 0, limit: int = 100, offset: int = 0, evidence_status: str = "all") -> dict:
    if name == "voc/summary":
        return summary(cfg, filters)
    if name == "voc/topics":
        return topics(cfg, filters, status, topic_q)
    if name == "voc/detail":
        return detail(cfg, filters, topic_id, limit, offset, evidence_status)
    if name == "voc/jobs":
        return jobs(cfg)
    raise VocError("VOC 接口不存在", 404)

def handle_post(cfg: Settings, name: str, filters: dict, body: Any) -> dict:
    if name.startswith("voc/knowledge/"):
        from . import knowledge
        return knowledge.handle_post(cfg, name, filters, body)
    if not isinstance(body, dict):
        raise VocError("请求内容须为 JSON 对象")
    if name == "voc/run":
        return start_job(cfg, filters, body)
    if name == "voc/topic":
        return save_topic(cfg, filters, body)
    if name == "voc/merge":
        return merge_topics(cfg, filters, body)
    if name == "voc/evidence":
        return evidence_action(cfg, filters, body)
    raise VocError("VOC 接口不存在", 404)
