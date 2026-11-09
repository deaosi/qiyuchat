"""Evidence-grounded, versioned knowledge drafts. No automatic publication."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import voc
from .config import Settings
from .knowledge_model import PROMPT_VERSION, _staff_answer, analyze_knowledge
from .local import csv_bytes

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_record (
 id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL,
 question TEXT NOT NULL, answer TEXT NOT NULL, scope TEXT NOT NULL,
 cautions TEXT NOT NULL, reply_patterns TEXT NOT NULL, conflicts TEXT NOT NULL,
 missing_info TEXT NOT NULL, source_quotes TEXT NOT NULL, evidence_ids TEXT NOT NULL,
 evidence_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','confirmed','ignored')),
 revision INTEGER NOT NULL DEFAULT 1, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 fingerprint TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS knowledge_input ON knowledge_record(topic_id,fingerprint,model,prompt_version);
CREATE TABLE IF NOT EXISTS knowledge_revision (
 knowledge_id INTEGER NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(knowledge_id,revision));
CREATE TABLE IF NOT EXISTS knowledge_job (
 id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL, knowledge_id INTEGER,
 status TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
 failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
 model TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', usage_tokens INTEGER NOT NULL DEFAULT 0,
 fingerprint TEXT NOT NULL, input_json TEXT NOT NULL, filters TEXT NOT NULL, owner TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_one_active ON knowledge_job((1)) WHERE status IN ('queued','running');
"""

_LOCK = threading.RLock()
_INITIALIZED: set[str] = set()
_WORKERS: dict[str, threading.Thread] = {}

def startup(cfg: Settings) -> None:
    voc.startup(cfg)
    path = voc._path(cfg)
    with _LOCK:
        if path in _INITIALIZED:
            return
        conn = voc._open(cfg)
        try:
            conn.executescript(SCHEMA)
            with voc._tx(conn):
                for job in conn.execute("SELECT id,owner,total,processed FROM knowledge_job WHERE status IN ('queued','running')").fetchall():
                    if not voc._owner_alive(job["owner"]):
                        conn.execute("UPDATE knowledge_job SET status='failed',failed=total-processed,error='服务重启中断，请重试',updated_at=? WHERE id=?", (time.time(), job["id"]))
            _INITIALIZED.add(path)
        finally:
            conn.close()

@contextmanager
def _connection(cfg: Settings) -> Iterator[sqlite3.Connection]:
    startup(cfg)
    conn = voc._open(cfg)
    try:
        yield conn
    finally:
        conn.close()

def _public(row: sqlite3.Row) -> dict:
    result = {key: row[key] for key in ("id", "topic_id", "question", "answer", "scope", "status", "revision", "model", "prompt_version")}
    for key in ("cautions", "reply_patterns", "conflicts", "missing_info", "source_quotes", "evidence_ids"):
        result[key] = json.loads(row[key])
    result.update(created_at=voc._when(row["created_at"]), updated_at=voc._when(row["updated_at"]))
    return result

def _record(conn: sqlite3.Connection, knowledge_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM knowledge_record WHERE id=?", (knowledge_id,)).fetchone()
    if row is None:
        raise voc.VocError("知识记录不存在", 404)
    return row

def _public_job(row: sqlite3.Row) -> dict:
    return {**voc._public_job(row), "topic_id": row["topic_id"], "knowledge_id": row["knowledge_id"]}

def _status(status: str) -> None:
    if status not in ("all", "draft", "confirmed", "ignored"):
        raise voc.VocError("知识状态无效")

def list_knowledge(cfg: Settings, status: str = "all", topic_id: int = 0) -> dict:
    _status(status)
    with _connection(cfg) as conn:
        conditions, values = [], []
        if status != "all":
            conditions.append("status=?")
            values.append(status)
        if topic_id:
            conditions.append("topic_id=?")
            values.append(topic_id)
        sql = "SELECT * FROM knowledge_record" + (" WHERE " + " AND ".join(conditions) if conditions else "") + " ORDER BY id DESC"
        items = [_public(row) for row in conn.execute(sql, values)]
        return {"items": items, "total": len(items)}

def detail(cfg: Settings, knowledge_id: int) -> dict:
    with _connection(cfg) as conn:
        row = _record(conn, knowledge_id)
        return {"knowledge": _public(row), "evidence": json.loads(row["evidence_json"])}

def jobs(cfg: Settings) -> dict:
    with _connection(cfg) as conn:
        return {"items": [_public_job(row) for row in conn.execute("SELECT * FROM knowledge_job ORDER BY id DESC LIMIT 30")]}

def _selected(conn: sqlite3.Connection, filters: dict, topic_id: int, limit: int) -> tuple[dict, list[dict]]:
    topic = dict(voc._topic(conn, topic_id))
    if topic["status"] == "ignored" or voc._resolve(conn, topic_id) != topic_id:
        raise voc.VocError("主题已被忽略或合并，请刷新后选择有效主题", 409)
    scope = voc._scope(conn, filters)
    members = voc._members(conn, topic_id, scope)
    # Actual staff replies get priority; include whole sources, never truncate quotes.
    members.sort(key=lambda row: (not bool(_staff_answer(row["answer"])), -row["asked_at"], -row["qa_id"]))
    selected: list[dict] = []
    budget = 20_000
    for member in members[:limit]:
        pair = {**member, "_fingerprint": scope[member["qa_id"]]["_fingerprint"]}
        cost = len(json.dumps({"qa_id": pair["qa_id"], "question": pair["question"], "answer": pair["answer"]}, ensure_ascii=False))
        if cost > budget:
            continue
        selected.append(pair)
        budget -= cost
    if not selected:
        raise voc.VocError("当前范围没有可整理的有效证据，或单条回复过长；请缩小范围")
    return topic, selected

def _require_staff_answers(pairs: list[dict]) -> None:
    if not any(_staff_answer(pair.get("answer")) for pair in pairs):
        raise voc.VocError("当前范围没有有效客服回复，请补充客服侧会话或导入含回答的记录后再沉淀知识")

def _fingerprint(topic: dict, pairs: list[dict]) -> str:
    import hashlib
    source = [{"qa_id": p["qa_id"], "fingerprint": p["_fingerprint"]} for p in pairs]
    return hashlib.sha256(json.dumps([{k: topic[k] for k in ("id", "question", "theme", "boundary")}, source], sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def start_job(cfg: Settings, filters: dict, body: dict, launch: bool = True) -> dict:
    if cfg.runtime.mock:
        raise voc.VocError("演示库禁止调用真实模型，请切换业务数据")
    if not voc.configured(cfg):
        raise voc.VocError("模型尚未配置或处于演练模式，请联系管理员")
    topic_id = voc._integer(body.get("topic_id"), "主题 ID")
    limit = voc._integer(body.get("limit", 100), "来源条数", 1, 100)
    with _connection(cfg) as conn:
        topic, pairs = _selected(conn, filters, topic_id, limit)
        _require_staff_answers(pairs)
        fingerprint = _fingerprint(topic, pairs)
        with voc._tx(conn):
            _assert_idle(conn)
            previous = conn.execute("SELECT id FROM knowledge_record WHERE topic_id=? AND fingerprint=? AND prompt_version=? AND model=? AND status IN ('draft','confirmed') ORDER BY id DESC LIMIT 1", (topic_id, fingerprint, PROMPT_VERSION, cfg.llm.model_main)).fetchone()
            if previous:
                job = conn.execute("SELECT * FROM knowledge_job WHERE knowledge_id=? AND status='completed' ORDER BY id DESC LIMIT 1", (previous["id"],)).fetchone()
                if job:
                    return {"job": _public_job(job)}
            now = time.time()
            job_id = conn.execute("INSERT INTO knowledge_job(topic_id,status,total,model,fingerprint,input_json,filters,owner,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (topic_id, "queued", len(pairs), cfg.llm.model_main, fingerprint, json.dumps({"topic": topic, "pairs": pairs}, ensure_ascii=False), json.dumps(filters, ensure_ascii=False), voc._OWNER, now, now)).lastrowid
            job = _public_job(conn.execute("SELECT * FROM knowledge_job WHERE id=?", (job_id,)).fetchone())
    if launch:
        thread = threading.Thread(target=_worker, args=(cfg, job_id), name=f"knowledge-{job_id}", daemon=True)
        with _LOCK:
            _WORKERS[voc._path(cfg)] = thread
        thread.start()
    return {"job": job}

def _assert_idle(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM voc_job WHERE status IN ('queued','running') LIMIT 1").fetchone() or conn.execute("SELECT 1 FROM knowledge_job WHERE status IN ('queued','running') LIMIT 1").fetchone():
        raise voc.VocError("当前数据源已有模型整理任务运行，请等待完成", 409)

def _worker(cfg: Settings, job_id: int) -> None:
    used = 0
    try:
        with _connection(cfg) as conn:
            conn.execute("UPDATE knowledge_job SET status='running',updated_at=? WHERE id=?", (time.time(), job_id))
            job = dict(conn.execute("SELECT * FROM knowledge_job WHERE id=?", (job_id,)).fetchone())
            snapshot = json.loads(job["input_json"])
        # No sqlite connection/transaction is alive while the gateway runs.
        _require_staff_answers(snapshot["pairs"])
        output = analyze_knowledge(cfg.llm, snapshot["topic"], snapshot["pairs"])
        used = output["usage_tokens"]
        proposal = output["knowledge"]
        with _connection(cfg) as conn, voc._tx(conn):
            current_topic = dict(voc._topic(conn, job["topic_id"]))
            scope = voc._scope(conn, json.loads(job["filters"]))
            current_members = {pair["qa_id"] for pair in voc._members(conn, job["topic_id"], scope)}
            if current_topic["status"] == "ignored" or voc._resolve(conn, job["topic_id"]) != job["topic_id"] or any(k for k in ("question", "theme", "boundary") if current_topic[k] != snapshot["topic"][k]) or any(p["qa_id"] not in current_members or p["_fingerprint"] != scope[p["qa_id"]]["_fingerprint"] for p in snapshot["pairs"]):
                raise voc.VocError("分析期间来源或主题已变更，请重新整理")
            now = time.time()
            evidence = [{k: v for k, v in pair.items() if not k.startswith("_")} for pair in snapshot["pairs"]]
            knowledge_id = conn.execute("INSERT INTO knowledge_record(topic_id,question,answer,scope,cautions,reply_patterns,conflicts,missing_info,source_quotes,evidence_ids,evidence_json,status,model,prompt_version,fingerprint,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job["topic_id"], proposal["question"], proposal["answer"], proposal["scope"], json.dumps(proposal["cautions"], ensure_ascii=False), json.dumps(proposal["reply_patterns"], ensure_ascii=False), json.dumps(proposal["conflicts"], ensure_ascii=False), json.dumps(proposal["missing_info"], ensure_ascii=False), json.dumps(proposal["source_quotes"], ensure_ascii=False), json.dumps([p["qa_id"] for p in evidence]), json.dumps(evidence, ensure_ascii=False), "draft", cfg.llm.model_main, PROMPT_VERSION, job["fingerprint"], now, now)).lastrowid
            voc._audit(conn, "knowledge_generate", job["topic_id"], None, {"knowledge_id": knowledge_id, "evidence_ids": [p["qa_id"] for p in evidence], "model": cfg.llm.model_main, "prompt_version": PROMPT_VERSION})
            conn.execute("UPDATE knowledge_job SET status='completed',knowledge_id=?,processed=total,usage_tokens=?,updated_at=? WHERE id=?", (knowledge_id, used, now, job_id))
    except Exception as exc:
        used = max(used, max(0, int(getattr(exc, "usage_tokens", 0) or 0)))
        message = voc._job_error(exc, "知识整理失败或来源已变更，请缩小范围后重试", allow_local=True)
        try:
            with _connection(cfg) as conn:
                conn.execute("UPDATE knowledge_job SET status='failed',failed=total,error=?,usage_tokens=?,updated_at=? WHERE id=?", (message, used, time.time(), job_id))
        except Exception:
            pass
    finally:
        with _LOCK:
            _WORKERS.pop(voc._path(cfg), None)

def save(cfg: Settings, body: dict) -> dict:
    knowledge_id = voc._integer(body.get("id"), "知识 ID")
    question = voc._text(body.get("question"), "标准问题", 120, 3)
    answer = voc._text(body.get("answer"), "知识答案", 4000)
    scope = voc._text(body.get("scope", ""), "适用条件", 1000)
    cautions = body.get("cautions", [])
    if not isinstance(cautions, list) or len(cautions) > 20:
        raise voc.VocError("注意事项须为最多 20 条文字")
    cautions = [voc._text(value, "注意事项", 500, 1) for value in cautions]
    status = body.get("status")
    if status not in ("draft", "confirmed", "ignored"):
        raise voc.VocError("知识状态无效")
    with _connection(cfg) as conn, voc._tx(conn):
        row = _record(conn, knowledge_id)
        voc._revision(row, body.get("revision"))
        evidence = json.loads(row["evidence_json"])
        if status == "confirmed" and (not answer or not any(pair["answer"].strip() for pair in evidence)):
            raise voc.VocError("缺少有效客服答复或知识答案为空，暂不能确认收录")
        prior = _public(row)
        conn.execute("INSERT INTO knowledge_revision VALUES (?,?,?,?)", (knowledge_id, row["revision"], json.dumps(prior, ensure_ascii=False), time.time()))
        conn.execute("UPDATE knowledge_record SET question=?,answer=?,scope=?,cautions=?,status=?,revision=revision+1,updated_at=? WHERE id=?", (question, answer, scope, json.dumps(cautions, ensure_ascii=False), status, time.time(), knowledge_id))
        voc._audit(conn, "knowledge_save", row["topic_id"], None, {"knowledge_id": knowledge_id, "previous_revision": row["revision"], "status": status})
        return {"knowledge": _public(_record(conn, knowledge_id))}

def export_csv(cfg: Settings, status: str = "confirmed") -> bytes:
    items = list_knowledge(cfg, status)["items"]
    return csv_bytes(["知识ID", "主题ID", "标准问题", "知识答案", "适用条件", "注意事项", "缺失信息", "来源问答ID", "状态", "人工版本", "生成模型", "生成时间"], [[k["id"], k["topic_id"], k["question"], k["answer"], k["scope"], "\n".join(k["cautions"]), "\n".join(k["missing_info"]), ",".join(str(i) for i in k["evidence_ids"]), k["status"], k["revision"], k["model"], k["created_at"]] for k in items])

def handle_get(cfg: Settings, name: str, *, status: str = "all", knowledge_id: int = 0, topic_id: int = 0) -> dict:
    if name == "voc/knowledge":
        return list_knowledge(cfg, status, topic_id)
    if name == "voc/knowledge/detail":
        return detail(cfg, knowledge_id)
    if name == "voc/knowledge/jobs":
        return jobs(cfg)
    raise voc.VocError("知识接口不存在", 404)

def handle_post(cfg: Settings, name: str, filters: dict, body: Any) -> dict:
    if not isinstance(body, dict):
        raise voc.VocError("请求内容须为 JSON 对象")
    if name == "voc/knowledge/run":
        return start_job(cfg, filters, body)
    if name == "voc/knowledge/save":
        return save(cfg, body)
    raise voc.VocError("知识接口不存在", 404)
