"""Consume verified DATA_ONLINE_SESSION_END records outside the webhook process.

Run: python -m src.push_sync --limit 20
No LLM, Feishu, global extraction or role inference is used. Each CLI owns a DB
connection; claims and writes are short transactions with no network inside.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import Settings, load_settings
from .db import DB, SOURCE_ARCHIVE_TRIGGERS
from .extract import extract_pairs
from .extract.identity import SOURCE_IDENTITY_ROLE, encode_identity
from .qiyu.common import QiyuError
from .qiyu.message import source_integer, validate_messages
from .qiyu.session import SessionExporter

EVENT_TYPE = "DATA_ONLINE_SESSION_END"
VERSION = "push-session-v2"
LEGACY_VERSION = "push-session-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS push_sync_state (
 session_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, version TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL, retry_after REAL NOT NULL DEFAULT 0,
 safe_error TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '',
 claim_until REAL NOT NULL DEFAULT 0, source_version TEXT NOT NULL DEFAULT '',
 source_json TEXT NOT NULL DEFAULT '',archive_reason TEXT NOT NULL DEFAULT '',archived_at REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS push_sync_retry ON push_sync_state(status,retry_after);
""" + SOURCE_ARCHIVE_TRIGGERS


class PushSyncError(RuntimeError):
    """Only fixed safe messages, never source content or request URLs."""


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_version(tag: str, key: str, payload: dict) -> str:
    return hashlib.sha256(_json([tag, key, payload]).encode()).hexdigest()


def _latest(conn: sqlite3.Connection) -> tuple[list[dict], int]:
    result: dict[str, dict] = {}
    invalid = 0
    for row in conn.execute("SELECT event_key,payload,received_at FROM push_event WHERE event_type=? ORDER BY received_at DESC,event_key DESC", (EVENT_TYPE,)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, RecursionError):
            invalid += 1
            continue
        if not isinstance(payload, dict) or not source_integer(payload.get("id"), positive=True):
            invalid += 1
            continue
        sid = str(payload["id"])
        if sid in result:
            continue
        version = _event_version(VERSION, row["event_key"], payload)
        result[sid] = {"sid": sid, "payload": payload, "event_key": row["event_key"], "version": version}
    return list(result.values()), invalid


def _counts(value: dict) -> tuple[int, int]:
    fields = (value.get("staffMessageCount"), value.get("userMessageCount"))
    if any(type(v) is not int or not 0 <= v <= 50_000 for v in fields):
        raise PushSyncError("来源会话缺少有效的客服及访客消息总数")
    return fields


def _numeric_identity(value: Any) -> int | None:
    if source_integer(value, positive=True):
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        number = int(value)
        return number if source_integer(number, positive=True) else None
    return None


def _expired(payload: dict, cfg: Settings, now: float) -> bool:
    if cfg.runtime.retention_days <= 0:
        return False
    stamp = payload.get("endTime") or payload.get("startTime")
    if type(stamp) is not int or stamp <= 0:
        return False
    cutoff = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=cfg.runtime.retention_days)
    return stamp < int(cutoff.timestamp() * 1000)


def _sender(row: dict, sid: int) -> tuple[str, str, bool]:
    external = row.get("externalUserId")
    if isinstance(external, str) and external.strip():
        if len(external) > 900 or any(ord(char) < 32 for char in external):
            cid = "qiyu-external-sha256:" + hashlib.sha256(external.encode()).hexdigest()
        else:
            cid = "qiyu-external:" + external
        unknown = False
    elif type(row.get("userId")) is int and row["userId"] != 0:
        cid, unknown = "qiyu-user:" + str(row["userId"]), False
    else:
        # Direction still proves a visitor. Separate each unidentified turn
        # rather than inventing equality between anonymous senders.
        cid, unknown = "qiyu-session:" + str(sid), True
    name = row.get("userName")
    return cid, name.strip()[:300] if isinstance(name, str) and name.strip() else "来源访客", unknown


def _project(metadata: dict, rows: list[dict], event: dict, sid: int, *, legacy: bool = False) -> list[dict]:
    if type(metadata.get("id")) is not int or metadata["id"] != sid:
        raise PushSyncError("来源会话详情与结束事件的会话 ID 不一致")
    if not source_integer(metadata.get("endTime"), positive=True):
        raise PushSyncError("来源会话详情缺少有效结束时间，暂停同步")
    expected = _counts(event)
    actual = _counts(metadata)
    if any(a < e for a, e in zip(actual, expected)):
        raise PushSyncError("来源会话详情尚未覆盖结束事件的消息总数，请稍后重试")
    try:
        validate_messages(rows, sid)
    except ValueError as exc:
        raise PushSyncError(str(exc)) from None
    returned = Counter(row["from"] for row in rows)
    if returned[0] < actual[0] or returned[1] < actual[1]:
        raise PushSyncError("来源消息响应不完整，未覆盖会话客服及访客消息总数")
    result = []
    current_sender = None
    for row in sorted(rows, key=lambda r: (r["time"], r["id"])):
        role = "boundary"
        # 1=text, 2=image are verified source types; 110 is the documented bot
        # type. Unknown/system types stay as boundaries, never human answers.
        if row["status"] == 1 and row["from"] == 0 and row["mType"] == 110:
            role = "bot"
        elif row["status"] == 1 and row["mType"] in (1, 2):
            if row["from"] == 0:
                role = "staff" if row["staffId"] > 0 else "bot"
            elif row["from"] == 1:
                if not legacy:
                    role = "user"
                else:
                    visitor = _numeric_identity(row.get("userId"))
                    if visitor is not None and visitor == _numeric_identity(metadata.get("userId")):
                        role = "user"
        if role == "user" and not legacy:
            cid, name, unknown = _sender(row, sid)
            if cid != current_sender or unknown:
                result.append({"role": SOURCE_IDENTITY_ROLE,
                               "content": encode_identity(str(sid), cid, name), "ts": row["time"]})
                current_sender = cid
        elif role == "boundary" and not legacy:
            current_sender = None
        result.append({"role": role, "content": row["msg"], "ts": row["time"],
                       **({"source_message_id": f"source:{sid}:{row['id']}"} if not legacy else {})})
    return result


def _legacy_origin(conn: sqlite3.Connection, old: sqlite3.Row | None) -> dict | None:
    """Prove that a retained snapshot was completed by the known v1 projector."""
    if not old or not old["source_json"] or not old["source_version"]:
        return None
    try:
        snapshot = json.loads(old["source_json"])
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(snapshot, dict) or snapshot.get("projection_version", LEGACY_VERSION) != LEGACY_VERSION:
        return None
    for row in conn.execute("SELECT event_key,payload FROM push_event WHERE event_type=?", (EVENT_TYPE,)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(payload, dict) or str(payload.get("id")) != old["session_id"]:
            continue
        if _event_version(LEGACY_VERSION, row["event_key"], payload) == old["source_version"]:
            return {"snapshot": snapshot, "event_key": row["event_key"], "payload": payload}
    return None


def _protect_existing(db: DB, sid: str, source_json: str, source: list[dict], projected: list[dict], *, migrate_legacy: bool = False, repair_roles: bool = False) -> None:
    old = db.get_messages(sid)
    if repair_roles:
        if db._conn.execute("SELECT 1 FROM qa_pair WHERE session_id=? LIMIT 1", (sid,)).fetchone():
            raise PushSyncError("修复目标已有问答资产，须先核对稳定映射，未覆盖")
        cursor = 0
        for message in old:
            while cursor < len(source) and (str(source[cursor]["msg"]).strip() != str(message["content"]).strip() or source[cursor]["time"] != message["ts"]):
                cursor += 1
            if cursor == len(source):
                raise PushSyncError("已存原文无法逐条匹配官方完整来源，未修复")
            cursor += 1
        return
    if source_json:
        try:
            previous = json.loads(source_json)["messages"]
            validate_messages(previous, int(sid))
        except (TypeError, KeyError, ValueError, RecursionError):
            raise PushSyncError("已存来源快照格式不一致，暂停覆盖") from None
        current = {row["id"]: row for row in source}
        fields = ("id", "sessionId", "from", "staffId", "userId", "externalUserId", "mType", "status", "time", "msg")
        for row in previous:
            match = current.get(row["id"])
            if match is None or any(match.get(k) != row.get(k) for k in fields):
                raise PushSyncError("来源响应缺失或改变了已存消息，暂停覆盖")
        if migrate_legacy:
            snapshot = json.loads(source_json)
            known_old = _project(snapshot["session"], previous, snapshot["session"], int(sid), legacy=True)
            has_qa = db._conn.execute("SELECT 1 FROM qa_pair WHERE session_id=? LIMIT 1", (sid,)).fetchone()
            if not has_qa and old == known_old:
                return
            raise PushSyncError("旧版身份投影迁移条件不满足，暂停覆盖")
        # Identity controls and sequence numbers are derived, not source
        # messages: inserting an earlier real message can move a control.
        # Accept that only after proving both immutable source-ID coverage
        # above and exact reproduction of the previous stored projection.
        snapshot = json.loads(source_json)
        known_old = _project(snapshot["session"], previous, snapshot["session"], int(sid))
        if len(old) == len(known_old) and all(all(expected.get(k) == v for k,v in stored.items()) for stored,expected in zip(old,known_old)):
            return
    # Without source IDs, every old role/content/timestamp must match in order.
    # A mismatch is not permission to infer a role or discard original content.
    cursor = 0
    for message in old:
        while cursor < len(projected) and any(projected[cursor].get(key) != value for key,value in message.items()):
            cursor += 1
        if cursor == len(projected):
            raise PushSyncError("新来源无法完整匹配已存会话原文，暂停覆盖")
        cursor += 1


def _extract_one(db: DB, sid: str) -> int:
    session = dict(db._conn.execute("SELECT * FROM raw_session WHERE session_id=?", (sid,)).fetchone())
    messages = db.get_messages(sid)
    pairs = extract_pairs(messages, session)
    input_data = {k: session.get(k) for k in ("session_id", "user_id", "user_name", "from_group", "staff_name", "start_ts")}
    fingerprint = hashlib.sha256(json.dumps([input_data, messages], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    # Reuse exact existing normalized-question clusters, or allocate IDs only
    # for this session. Never recluster or modify another QA/VOC asset.
    clusters = {r["q_norm"]: r["cluster_id"] for r in db._conn.execute("SELECT q_norm,MIN(cluster_id) cluster_id FROM qa_pair WHERE cluster_id>0 GROUP BY q_norm")}
    next_id = int(db._conn.execute("SELECT COALESCE(MAX(cluster_id),0)+1 FROM qa_pair").fetchone()[0])
    db.replace_session_pairs(sid, pairs, fingerprint, "extracted" if pairs else "no_question")
    for pair in db.iter_qa_pairs([sid]):
        norm = pair["q_norm"] or ""
        if norm not in clusters:
            clusters[norm] = next_id
            next_id += 1
        db._conn.execute("UPDATE qa_pair SET cluster_id=? WHERE qa_id=?", (clusters[norm], pair["qa_id"]))
    return len(pairs)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, PushSyncError):
        return str(exc)[:240]
    if isinstance(exc, QiyuError) and type(exc.code) is int:
        return f"七鱼接口返回错误（code={exc.code}），请检查接口权限或稍后重试"
    return "源会话同步失败，请检查服务连接或来源格式后重试"


def _repair_eligibility(conn: sqlite3.Connection, cfg: Settings, event: dict, now: float) -> str:
    row = conn.execute("SELECT * FROM push_sync_state WHERE session_id=?", (event["sid"],)).fetchone()
    if not row:
        return "no_previous_sync"
    state = dict(row)
    reason = state.get("archive_reason", "")
    retry_candidate = reason == "legacy_overwrite_candidate"
    if retry_candidate and state["status"] == "running" and state["claim_until"] > now:
        return "repair_already_running"
    statuses = ("archived","failed","running") if retry_candidate else ("archived",)
    if state["status"] not in statuses or reason not in ("", "legacy_overwrite_candidate") or state["source_json"] or state["source_version"] or (state["safe_error"] and not reason):
        return "not_legacy_overwrite"
    if state["event_key"] != event["event_key"] or state["version"] not in (_event_version(VERSION,event["event_key"],event["payload"]),_event_version(LEGACY_VERSION,event["event_key"],event["payload"])):
        return "event_version_unproven"
    if not source_integer(event["payload"].get("endTime"),positive=True) or _expired(event["payload"],cfg,now):
        return "outside_retention"
    session = conn.execute("SELECT * FROM raw_session WHERE session_id=?", (event["sid"],)).fetchone()
    extracted = conn.execute("SELECT processed_at FROM extract_state WHERE session_id=?", (event["sid"],)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM raw_message WHERE session_id=?", (event["sid"],)).fetchone()[0]
    origin_time = state.get("archived_at", 0) if reason else state["updated_at"]
    if not origin_time or not session or not count or not extracted or type(session["fetched_at"]) is not int or type(extracted[0]) is not int or session["fetched_at"] < origin_time*1000 or extracted[0] < origin_time*1000:
        return "replacement_timeline_unproven"
    if not source_integer(session["end_ts"],positive=True) or _expired({"endTime":session["end_ts"]},cfg,now):
        return "outside_retention"
    if conn.execute("SELECT 1 FROM raw_message WHERE session_id=? AND role='source_identity' LIMIT 1", (event["sid"],)).fetchone():
        return "identity_not_lost"
    if conn.execute("SELECT 1 FROM qa_pair WHERE session_id=? LIMIT 1", (event["sid"],)).fetchone():
        return "existing_question_assets"
    return "eligible_with_operator_confirmation"


def check_repairs(cfg: Settings, *, session_id: int | None = None, now: float | None = None) -> dict:
    if session_id is not None and not source_integer(session_id, positive=True):
        raise PushSyncError("会话 ID 须为正整数")
    path = Path(cfg.runtime.db_path).resolve()
    if not path.is_file():
        return {"eligible":0,"reasons":{},"read_only":True}
    conn = sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='push_sync_state'").fetchone():
            return {"eligible":0,"reasons":{},"read_only":True}
        reasons: dict[str,int] = {}
        for event in _latest(conn)[0]:
            if session_id is not None and event["sid"] != str(session_id):
                continue
            reason = _repair_eligibility(conn,cfg,event,time.time() if now is None else now)
            reasons[reason] = reasons.get(reason,0)+1
        return {"eligible":reasons.get("eligible_with_operator_confirmation",0),"reasons":reasons,"read_only":True}
    finally:
        conn.close()


def sync_pending(cfg: Settings, limit: int = 20, *, session_id: int | None = None,
                 exporter: SessionExporter | None = None, now: float | None = None,
                 repair_overwritten: bool = False, confirm_overwrite: bool = False) -> dict:
    if repair_overwritten and not confirm_overwrite:
        raise PushSyncError("旧误归档修复须先检查日志并明确 --confirm-overwrite，未执行")
    if cfg.runtime.mock:
        raise PushSyncError("演示库禁止调用真实七鱼接口")
    if not cfg.qiyu.app_key or not cfg.qiyu.app_secret:
        raise PushSyncError("七鱼接口凭据尚未配置")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise PushSyncError("每次同步条数须为 1–100")
    if session_id is not None and not source_integer(session_id, positive=True):
        raise PushSyncError("会话 ID 须为正整数")
    clock = lambda: time.time() if now is None else now
    db = DB(cfg.runtime.db_path)
    owner = uuid.uuid4().hex
    result = {"scanned": 0, "invalid_events": 0, "claimed": 0, "completed": 0, "failed": 0,
              "skipped": 0, "archived": 0, "qa_pairs": 0, "test_marked_sessions": 0,"repaired":0}
    api = exporter or SessionExporter(cfg.qiyu)
    last_call = 0.0
    def call(method, sid):
        nonlocal last_call
        delay = .25 - (time.monotonic() - last_call)
        if delay > 0:
            time.sleep(delay)
        last_call = time.monotonic()
        return method(sid)
    try:
        db._conn.executescript(SCHEMA)
        candidates, result["invalid_events"] = _latest(db._conn)
        if session_id is not None:
            candidates = [row for row in candidates if row["sid"] == str(session_id)]
        result["scanned"] = len(candidates)
        for event in candidates:
            if result["claimed"] >= limit:
                break
            sid = event["sid"]
            repairing = False
            with _transaction(db._conn):
                old = db._conn.execute("SELECT * FROM push_sync_state WHERE session_id=?", (sid,)).fetchone()
                if old and old["archive_reason"] == "legacy_overwrite_candidate" and not repair_overwritten:
                    result["skipped"] += 1
                    continue
                if repair_overwritten:
                    if _repair_eligibility(db._conn,cfg,event,clock()) != "eligible_with_operator_confirmation":
                        result["skipped"] += 1
                        continue
                    repairing = True
                if old and old["status"] == "running" and old["claim_until"] > clock():
                    result["skipped"] += 1
                    continue
                if old and old["status"] == "archived" and not repairing:
                    result["skipped"] += 1
                    continue
                if not repairing and old and old["version"] == event["version"] and (old["status"] in ("completed", "archived") or old["retry_after"] > clock()):
                    result["skipped"] += 1
                    continue
                if _expired(event["payload"], cfg, clock()):
                    db._conn.execute("INSERT INTO push_sync_state(session_id,event_key,version,status,updated_at,safe_error,archive_reason,archived_at) VALUES (?,?,?,'archived',?,?,'retention',?) ON CONFLICT(session_id) DO UPDATE SET event_key=excluded.event_key,version=excluded.version,status='archived',source_json='',source_version='',updated_at=excluded.updated_at,safe_error=excluded.safe_error,archive_reason='retention',archived_at=excluded.archived_at", (sid, event["event_key"], event["version"], clock(), "来源已超过原文保留期，未补采",clock()))
                    result["archived"] += 1
                    continue
                legacy_origin = _legacy_origin(db._conn, old)
                if legacy_origin and db._conn.execute("SELECT 1 FROM qa_pair WHERE session_id=? LIMIT 1", (sid,)).fetchone():
                    # Automatic migration is bounded to v1 snapshots that
                    # produced no QA. Existing question assets need review.
                    result["skipped"] += 1
                    continue
                replay = (legacy_origin is not None and legacy_origin["event_key"] == event["event_key"]
                          and legacy_origin["payload"] == event["payload"])
                attempts = old["attempts"] + 1 if old and old["version"] == event["version"] else 1
                previous_source = old["source_json"] if old else ""
                lease = clock() + max(600, cfg.qiyu.timeout * 8 + 30)
                db._conn.execute("INSERT INTO push_sync_state(session_id,event_key,version,status,attempts,updated_at,owner,claim_until) VALUES (?,?,?,'running',?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET event_key=excluded.event_key,version=excluded.version,status='running',attempts=excluded.attempts,updated_at=excluded.updated_at,owner=excluded.owner,claim_until=excluded.claim_until,retry_after=0,safe_error=''", (sid, event["event_key"], event["version"], attempts, clock(), owner, lease))
                if repairing and not old["archive_reason"]:
                    db._conn.execute("UPDATE push_sync_state SET archive_reason='legacy_overwrite_candidate',archived_at=? WHERE session_id=?", (old["updated_at"],sid))
                result["claimed"] += 1
            try:
                _counts(event["payload"])
                if not source_integer(event["payload"].get("endTime"), positive=True):
                    raise PushSyncError("结束事件缺少有效会话结束时间，暂停同步")
                if replay:
                    metadata = legacy_origin["snapshot"]["session"]
                    rows = legacy_origin["snapshot"]["messages"]
                else:
                    metadata = call(api.export_one, int(sid))
                    rows = call(api.export_messages, int(sid))
                projected = _project(metadata, rows, event["payload"], int(sid))
                test_marker = any(re.search(r"测试|\btest\b|联调|回调验证|接口验证", row["msg"], re.I) for row in rows)
                source = _json({"projection_version": VERSION, "session": metadata,
                                "messages": sorted(rows, key=lambda r: (r["time"], r["id"])), "explicit_test_marker": test_marker})
                if len(source.encode()) > 20_000_000:
                    raise PushSyncError("单会话来源快照过大，暂停自动同步")
                with _transaction(db._conn):
                    claim = db._conn.execute("SELECT * FROM push_sync_state WHERE session_id=?", (sid,)).fetchone()
                    if claim["owner"] != owner or claim["status"] != "running" or claim["version"] != event["version"]:
                        raise PushSyncError("同步任务已由其它进程接管，请稍后重试")
                    if claim["source_json"] != previous_source:
                        raise PushSyncError("同步期间原始记录已归档，请重新核对来源")
                    latest = next((row for row in _latest(db._conn)[0] if row["sid"] == sid), None)
                    if not latest or latest["version"] != event["version"]:
                        raise PushSyncError("同步期间收到更新的结束事件，请稍后重试")
                    if _expired(metadata, cfg, clock()) or _expired(event["payload"], cfg, clock()):
                        raise PushSyncError("来源会话已超过原文保留期，暂停同步")
                    _protect_existing(db, sid, previous_source, sorted(rows,key=lambda r:(r["time"],r["id"])), projected, migrate_legacy=legacy_origin is not None,repair_roles=repairing)
                    db.upsert_sessions([metadata], verified_source=True)
                    db.upsert_messages(sid, projected, verified_source=True)
                    count = _extract_one(db, sid)
                    db._conn.execute("UPDATE push_sync_state SET status='completed',updated_at=?,retry_after=0,safe_error='',owner='',claim_until=0,source_version=version,source_json=?,archive_reason='',archived_at=0 WHERE session_id=?", (clock(), source, sid))
                    if repairing:
                        db._conn.execute("INSERT INTO source_write_audit(session_id,operation,reason,incoming_count,retained_count,created_at) VALUES (?,'repair','operator_confirmed_legacy_overwrite',?,?,?)", (sid,len(rows),len(projected),int(clock()*1000)))
                result["completed"] += 1
                result["repaired"] += repairing
                result["qa_pairs"] += count
                result["test_marked_sessions"] += bool(test_marker)
            except Exception as exc:
                with _transaction(db._conn):
                    db._conn.execute("UPDATE push_sync_state SET status=?,updated_at=?,retry_after=?,safe_error=?,owner='',claim_until=0 WHERE session_id=? AND owner=? AND version=?", ("archived" if repairing else "failed",clock(), clock() + min(3600, 60 * 2 ** min(attempts - 1, 6)), _safe_error(exc), sid, owner, event["version"]))
                result["failed"] += 1
        return result
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补采已验签的七鱼结束会话，不调用大模型或飞书")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--session-id", type=int, default=None)
    parser.add_argument("--check-repair", action="store_true", help="只读检查旧误归档候选，不联网或写库")
    parser.add_argument("--repair-overwritten", action="store_true", help="仅修复已核对的旧快照覆盖目标")
    parser.add_argument("--confirm-overwrite", action="store_true", help="操作员已核对来源日志并确认普通覆盖")
    args = parser.parse_args(argv)
    # The existing HTTP helper may include signed URLs in diagnostics. This
    # dedicated process prints counts/safe errors only, never transport logs.
    logging.disable(logging.CRITICAL)
    try:
        cfg=load_settings()
        if args.check_repair:
            print(_json(check_repairs(cfg,session_id=args.session_id)))
            return 0
        result = sync_pending(cfg, args.limit, session_id=args.session_id,
                              repair_overwritten=args.repair_overwritten,confirm_overwrite=args.confirm_overwrite)
        print(_json(result))
        return 1 if result["failed"] else 0
    except Exception as exc:
        print(_json({"error": _safe_error(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
