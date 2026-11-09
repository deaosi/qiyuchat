"""存储层：SQLite 单一文件，幂等 upsert，水位线。

表设计原则：
- raw_*   原始数据，只增不改，session_id 做主键保证重跑幂等
- clean_* 清洗结果，主键 (session_id, prompt_version)，换 prompt 可并存对比
- watermark 增量同步水位，一张表放所有游标
- task_log 异步导出任务台账，防止重复建任务
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .log import get

log = get("db")

EXTRACT_VERSION = "local-rules-v2"

# 滑动窗口裁剪时「这条消息该删了」的判定。
# 两条路：自己有可信时间戳且过期；或者所属会话已经过期（连带清理，覆盖 ts 缺失的旧数据）。
# 绝不能只写 ts < cutoff —— ts 为 NULL 的行会被当成「不是过期」永久留着，
# 而 ts=0 的行又会被当成最古老的数据误删。
# 消息只跟着会话走：会话没了，消息就是孤儿，按窗口边界（cutoff）删。
# 宽限期只作用于会话，不作用于消息 —— 否则已抽取的会话删了，消息还多留一周。
_MSG_EXPIRED = (
    "session_id NOT IN (SELECT session_id FROM raw_session) "
    "AND (ts IS NULL OR ts <= 0 OR ts < ?)"
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS raw_session (
    session_id    TEXT PRIMARY KEY,
    start_ts      INTEGER,
    end_ts        INTEGER,
    s_type        INTEGER,
    staff_id      TEXT,
    staff_name    TEXT,
    user_id       TEXT,
    user_name     TEXT,
    from_group    TEXT,
    from_type     TEXT,
    category      TEXT,
    evaluation    INTEGER,
    round_number  INTEGER,
    is_valid      INTEGER,
    first_reply_cost INTEGER,
    avg_resp_duration INTEGER,
    worksheet_ids TEXT,
    payload       TEXT,
    fetched_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_raw_session_start ON raw_session(start_ts);

CREATE TABLE IF NOT EXISTS raw_message (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT,
    content    TEXT,
    ts         INTEGER,
    PRIMARY KEY (session_id, seq));

-- 给 prune 用：按时间删历史消息，没索引会全表扫
CREATE INDEX IF NOT EXISTS idx_raw_message_ts ON raw_message(ts);

CREATE TABLE IF NOT EXISTS clean_result (
    session_id     TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT,
    category       TEXT,
    sub_category   TEXT,
    emotion        TEXT,
    resolved       INTEGER,
    risk           TEXT,
    demand         TEXT,
    result         TEXT,
    product        TEXT,
    followup       INTEGER,
    one_line       TEXT,
    quality_score  INTEGER,
    confidence     REAL,
    payload        TEXT,
    created_at     INTEGER,
    PRIMARY KEY (session_id, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_clean_cat ON clean_result(category);

CREATE TABLE IF NOT EXISTS watermark (
    name       TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS task_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT,
    params     TEXT,
    task_key   TEXT,
    file_url   TEXT,
    status     TEXT,
    created_at INTEGER,
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_task_key ON task_log(task_key);

-- ============ 问答记录（不调模型，纯抽取） ============
CREATE TABLE IF NOT EXISTS qa_pair (
    qa_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    idx          INTEGER NOT NULL,
    customer_id  TEXT,
    customer_name TEXT,
    group_name   TEXT,
    staff_name   TEXT,
    asked_at     INTEGER,
    question     TEXT,
    answer       TEXT,
    q_norm       TEXT,
    cluster_id   INTEGER DEFAULT 0,
    created_at   INTEGER,
    UNIQUE(session_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_qa_customer ON qa_pair(customer_id);
CREATE INDEX IF NOT EXISTS idx_qa_cluster  ON qa_pair(cluster_id);
CREATE INDEX IF NOT EXISTS idx_qa_time     ON qa_pair(asked_at);

CREATE TABLE IF NOT EXISTS extract_state (
    session_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    pair_count INTEGER NOT NULL DEFAULT 0,
    processed_at INTEGER NOT NULL
);

-- ============ 七鱼推送（消息与事件接收url） ============
-- 推送是流式的：MSG 事件只带 uid，不带 sessionId，所以必须靠 uid_session 把
-- 「会话开始通知」里拿到的 sessionId 记住，后续同 uid 的 MSG 才能归到同一个会话。
CREATE TABLE IF NOT EXISTS uid_session (
    uid        TEXT PRIMARY KEY,
    session_id TEXT,
    staff_id   TEXT,
    staff_name TEXT,
    started_at INTEGER,
    ended_at   INTEGER,
    updated_at INTEGER
);

-- 幂等台账：七鱼推失败会重发，MSG 用 msgId 去重，其余事件用 eventType+uid+time。
CREATE TABLE IF NOT EXISTS push_event (
    event_key   TEXT PRIMARY KEY,
    event_type  TEXT,
    uid         TEXT,
    session_id  TEXT,
    ts          INTEGER,
    payload     TEXT,
    received_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_push_event_uid ON push_event(uid);
CREATE INDEX IF NOT EXISTS idx_push_event_type ON push_event(event_type);

CREATE TABLE IF NOT EXISTS q_cluster (
    run_id         TEXT NOT NULL,
    cluster_id     INTEGER NOT NULL,
    rep_question   TEXT,
    rep_norm       TEXT,
    size           INTEGER,
    customer_count INTEGER,
    group_count    INTEGER,
    first_seen     INTEGER,
    last_seen      INTEGER,
    PRIMARY KEY (run_id, cluster_id)
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # timeout=30：receiver 和看板是两个进程共享同一个库，
        # 默认 5 秒在批量写时容易撞 "database is locked"。
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------- 基础 ----------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        # 导入整批数据时复用同一事务；内部 upsert 用 SAVEPOINT，不能提前提交。
        nested = self._conn.in_transaction
        name = f"sp_{time.monotonic_ns()}"
        self._conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN")
        try:
            yield self._conn
            if nested:
                self._conn.execute(f"RELEASE SAVEPOINT {name}")
            else:
                self._conn.commit()
        except Exception:
            if nested:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self._conn.execute(f"RELEASE SAVEPOINT {name}")
            else:
                self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ---------- 原始会话 ----------

    def upsert_sessions(self, rows: Iterable[dict[str, Any]]) -> int:
        """按 session_id 幂等写入。返回实际新增/更新的条数。"""
        now = int(time.time() * 1000)
        sql = """
        INSERT INTO raw_session (
            session_id, start_ts, end_ts, s_type, staff_id, staff_name, user_id, user_name,
            from_group, from_type, category, evaluation, round_number, is_valid,
            first_reply_cost, avg_resp_duration, worksheet_ids, payload, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            start_ts=excluded.start_ts, end_ts=excluded.end_ts,
            s_type=excluded.s_type, staff_id=excluded.staff_id,
            staff_name=excluded.staff_name, user_id=excluded.user_id,
            user_name=excluded.user_name, from_group=excluded.from_group,
            from_type=excluded.from_type, category=excluded.category,
            evaluation=excluded.evaluation, round_number=excluded.round_number,
            is_valid=excluded.is_valid, first_reply_cost=excluded.first_reply_cost,
            avg_resp_duration=excluded.avg_resp_duration,
            worksheet_ids=excluded.worksheet_ids,
            payload=excluded.payload, fetched_at=excluded.fetched_at
        """
        n = 0
        with self.tx() as c:
            for r in rows:
                payload = json.dumps(r, ensure_ascii=False)
                old = c.execute("SELECT payload FROM raw_session WHERE session_id=?",
                                (str(r.get("id")),)).fetchone()
                if old is None or old[0] != payload:
                    c.execute("DELETE FROM extract_state WHERE session_id=?", (str(r.get("id")),))
                c.execute(sql, (
                    str(r.get("id")),
                    _int(r.get("startTime")),
                    _int(r.get("endTime")),
                    _int(r.get("sType")),
                    str(r.get("staffId") or ""),
                    r.get("staffName") or "",
                    r.get("userId") or "",
                    r.get("userName") or "",
                    r.get("fromGroup") or "",
                    r.get("fromType") or "",
                    r.get("category") or "",
                    _int(r.get("evaluation")),
                    _int(r.get("roundNumber")),
                    _int(r.get("isValid")),
                    _int(r.get("firstReplyCost")),
                    _int(r.get("avgRespDuration")),
                    r.get("worksheetIds") or "",
                    json.dumps(r, ensure_ascii=False),
                    now,
                ))
                n += 1
        return n

    def iter_sessions(self, limit: int = 0, only_uncleaned: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM raw_session"
        args: list[Any] = []
        if only_uncleaned:
            sql += " WHERE session_id NOT IN (SELECT session_id FROM clean_result WHERE prompt_version=?)"
            args.append(only_uncleaned)
        sql += " ORDER BY start_ts DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._conn.execute(sql, args).fetchall()

    # ---------- 消息正文 ----------

    def upsert_messages(self, session_id: str, msgs: list[dict[str, Any]]) -> int:
        """正文是会话快照，更新时删除旧尾部，且让规则清洗重新处理。"""
        normalized = [{"role": m.get("role", ""), "content": m.get("content", ""),
                       "ts": _int(m.get("ts"))} for m in msgs]
        if self.get_messages(session_id) == normalized:
            return len(msgs)
        with self.tx() as c:
            c.execute("DELETE FROM raw_message WHERE session_id=?", (session_id,))
            c.executemany(
                "INSERT INTO raw_message (session_id, seq, role, content, ts) VALUES (?,?,?,?,?)",
                [(session_id, i, m["role"], m["content"], m["ts"])
                 for i, m in enumerate(normalized)],
            )
            c.execute("DELETE FROM extract_state WHERE session_id=?", (session_id,))
        return len(msgs)

    # ---------- 七鱼推送事件 ----------

    def record_push(self, event_key: str, event_type: str = "", uid: str = "",
                    session_id: str = "", ts: int = 0, payload: str = "") -> bool:
        """写入幂等台账。返回 True = 首次处理，False = 重复推送（直接 ack 丢掉）。"""
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO push_event "
                "(event_key,event_type,uid,session_id,ts,payload,received_at) VALUES (?,?,?,?,?,?,?)",
                (event_key, event_type, uid, session_id, ts, payload, int(time.time() * 1000)),
            )
            return cur.rowcount > 0

    def bind_uid_session(self, uid: str, session_id: str, staff_id: str = "",
                         staff_name: str = "", ts: int = 0) -> None:
        """会话开始：记住 uid → sessionId，并把会话落进 raw_session。"""
        now = int(time.time() * 1000)
        with self.tx() as c:
            c.execute(
                "INSERT INTO uid_session (uid,session_id,staff_id,staff_name,started_at,ended_at,updated_at) "
                "VALUES (?,?,?,?,?,NULL,?) "
                "ON CONFLICT(uid) DO UPDATE SET "
                "session_id=excluded.session_id, staff_id=excluded.staff_id, "
                "staff_name=excluded.staff_name, started_at=excluded.started_at, "
                "ended_at=NULL, updated_at=excluded.updated_at",
                (uid, session_id, staff_id, staff_name, ts or now, now),
            )

    def close_uid_session(self, uid: str, ts: int = 0) -> None:
        """会话结束：同时更新 uid_session 和 raw_session.end_ts。

        只更新 uid_session 是不够的 —— rotate 按 raw_session.end_ts 算窗口，
        漏写会让「已经结束的会话」一直停在 end_ts=0。
        """
        ended = ts or int(time.time() * 1000)
        now = int(time.time() * 1000)
        with self.tx() as c:
            c.execute("UPDATE uid_session SET ended_at=?, updated_at=? WHERE uid=?",
                      (ended, now, uid))
            c.execute(
                "UPDATE raw_session SET end_ts=? WHERE session_id="
                "(SELECT session_id FROM uid_session WHERE uid=?)",
                (ended, uid),
            )

    def current_session_for_uid(self, uid: str) -> str:
        row = self._conn.execute(
            "SELECT session_id FROM uid_session WHERE uid=?", (uid,)
        ).fetchone()
        return (row["session_id"] if row else "") or ""

    def append_message(self, session_id: str, role: str, content: str, ts: int = 0) -> int:
        """流式追加一条消息。不能用 upsert_messages —— 那是整段快照替换。"""
        with self.tx() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(seq),-1) AS m FROM raw_message WHERE session_id=?",
                (session_id,),
            ).fetchone()
            nxt = int(row["m"]) + 1
            c.execute(
                "INSERT INTO raw_message (session_id, seq, role, content, ts) VALUES (?,?,?,?,?)",
                (session_id, nxt, role, content, ts),
            )
            c.execute("DELETE FROM extract_state WHERE session_id=?", (session_id,))
        return nxt

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT role, content, ts FROM raw_message WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in rows]

    def get_transcript(self, session_id: str) -> str:
        rows = self._conn.execute(
            "SELECT role, content FROM raw_message WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        if not rows:
            return ""
        lines = []
        for r in rows:
            who = {"user": "客户", "staff": "客服", "bot": "机器人", "system": "系统"}.get(r["role"], r["role"])
            lines.append(f"{who}: {r['content']}")
        return "\n".join(lines)

    # ---------- 清洗结果 ----------

    def upsert_clean(self, row: dict[str, Any]) -> None:
        sql = """
        INSERT INTO clean_result (
            session_id, prompt_version, model, category, sub_category, emotion, resolved, risk,
            demand, result, product, followup, one_line, quality_score, confidence, payload, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id, prompt_version) DO UPDATE SET
            model=excluded.model, category=excluded.category, sub_category=excluded.sub_category,
            emotion=excluded.emotion, resolved=excluded.resolved, risk=excluded.risk,
            demand=excluded.demand, result=excluded.result, product=excluded.product,
            followup=excluded.followup, one_line=excluded.one_line,
            quality_score=excluded.quality_score, confidence=excluded.confidence,
            payload=excluded.payload, created_at=excluded.created_at
        """
        with self.tx() as c:
            c.execute(sql, (
                str(row["session_id"]), row["prompt_version"], row.get("model", ""),
                row.get("category", ""), row.get("sub_category", ""), row.get("emotion", ""),
                _int(row.get("resolved")), row.get("risk", ""),
                row.get("demand", ""), row.get("result", ""), row.get("product", ""),
                _int(row.get("followup")), row.get("one_line", ""),
                _int(row.get("quality_score")), float(row.get("confidence") or 0.0),
                json.dumps(row.get("payload", {}), ensure_ascii=False),
                int(time.time() * 1000),
            ))

    def iter_clean(self, prompt_version: str, limit: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM clean_result WHERE prompt_version=?"
        args: list[Any] = [prompt_version]
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._conn.execute(sql, args).fetchall()

    # ---------- 水位 ----------

    def get_watermark(self, name: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM watermark WHERE name=?", (name,)).fetchone()
        return row["value"] if row else default

    def set_watermark(self, name: str, value: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO watermark (name, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (name, value, int(time.time() * 1000)),
            )

    # ---------- 任务台账 ----------

    def task_exists(self, task_key: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM task_log WHERE task_key=? LIMIT 1", (task_key,)).fetchone()
        return row is not None

    def log_task(self, kind: str, params: dict, task_key: str, status: str, file_url: str = "") -> None:
        now = int(time.time() * 1000)
        with self.tx() as c:
            c.execute(
                "INSERT INTO task_log (kind, params, task_key, file_url, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (kind, json.dumps(params, ensure_ascii=False), task_key, file_url, status, now, now),
            )

    # ---------- 问答记录 ----------

    def upsert_qa_pairs(self, pairs: Iterable[dict[str, Any]]) -> int:
        """按 (session_id, idx) 幂等写入。返回条数。"""
        now = int(time.time() * 1000)
        sql = """
        INSERT INTO qa_pair (
            session_id, idx, customer_id, customer_name, group_name, staff_name,
            asked_at, question, answer, q_norm, cluster_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,0,?)
        ON CONFLICT(session_id, idx) DO UPDATE SET
            question=excluded.question, answer=excluded.answer, q_norm=excluded.q_norm,
            customer_id=excluded.customer_id, customer_name=excluded.customer_name,
            group_name=excluded.group_name, staff_name=excluded.staff_name,
            asked_at=excluded.asked_at, cluster_id=0
        """
        n = 0
        with self.tx() as c:
            for p in pairs:
                c.execute(sql, (
                    str(p.get("session_id")), int(p.get("idx") or 0),
                    str(p.get("customer_id") or ""), p.get("customer_name") or "",
                    p.get("group_name") or "", p.get("staff_name") or "",
                    int(p.get("asked_at") or 0), p.get("question") or "",
                    p.get("answer") or "", p.get("q_norm") or "", now,
                ))
                n += 1
        return n

    def iter_qa_pairs(self, session_ids: list[str] | None = None,
                      limit: int = 0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM qa_pair"
        args: list[Any] = []
        if session_ids:
            marks = ",".join("?" * len(session_ids))
            sql += f" WHERE session_id IN ({marks})"
            args.extend(session_ids)
        sql += " ORDER BY asked_at"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._conn.execute(sql, args).fetchall()

    def sessions_without_pairs(self, limit: int = 0) -> list[sqlite3.Row]:
        # 零问答/缺正文也有处理状态，只有输入改变或规则升级才需要重抽。
        sql = ("SELECT s.* FROM raw_session s LEFT JOIN extract_state e "
               "ON s.session_id=e.session_id WHERE e.session_id IS NULL "
               "OR e.version != ? ORDER BY s.start_ts DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._conn.execute(sql, (EXTRACT_VERSION,)).fetchall()

    def replace_session_pairs(self, session_id: str, pairs: list[dict[str, Any]],
                              fingerprint: str, status: str) -> int:
        with self.tx() as c:
            self.upsert_qa_pairs(pairs)
            keep = [int(p["idx"]) for p in pairs]
            if keep:
                marks = ",".join("?" for _ in keep)
                c.execute(f"DELETE FROM qa_pair WHERE session_id=? AND idx NOT IN ({marks})",
                          [session_id, *keep])
            else:
                c.execute("DELETE FROM qa_pair WHERE session_id=?", (session_id,))
            c.execute(
                "INSERT INTO extract_state VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET version=excluded.version, "
                "fingerprint=excluded.fingerprint, status=excluded.status, "
                "pair_count=excluded.pair_count, processed_at=excluded.processed_at",
                (session_id, EXTRACT_VERSION, fingerprint, status, len(pairs),
                 int(time.time() * 1000)),
            )
        return len(pairs)

    def local_status(self) -> dict[str, Any]:
        q = lambda sql: self._conn.execute(sql).fetchone()[0]
        counts = dict(self._conn.execute(
            "SELECT e.status, COUNT(*) FROM extract_state e JOIN raw_session s "
            "ON s.session_id=e.session_id WHERE e.version=? GROUP BY e.status",
            (EXTRACT_VERSION,),
        ).fetchall())
        last = q("SELECT MAX(processed_at) FROM extract_state") or 0
        return {
            "sessions": q("SELECT COUNT(*) FROM raw_session"),
            "with_content": q("SELECT COUNT(DISTINCT m.session_id) FROM raw_message m "
                              "JOIN raw_session s ON s.session_id=m.session_id"),
            "messages": q("SELECT COUNT(*) FROM raw_message"),
            "processed": sum(counts.values()),
            "pending": q("SELECT COUNT(*) FROM raw_session s LEFT JOIN extract_state e "
                         "ON s.session_id=e.session_id WHERE e.session_id IS NULL "
                         f"OR e.version != '{EXTRACT_VERSION}'"),
            "with_pairs": counts.get("extracted", 0),
            "no_question": counts.get("no_question", 0),
            "no_content": counts.get("no_content", 0),
            "archived": counts.get("archived", 0),
            "last_processed": last,
            "version": EXTRACT_VERSION,
        }

    def get_pair(self, qa_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM qa_pair WHERE qa_id=?", (qa_id,)).fetchone()
        return dict(row) if row else None

    def recent_local_tasks(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT kind, params, status, created_at FROM task_log "
            "WHERE kind IN ('local_import', 'local_clean') ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"kind": r["kind"], "result": json.loads(r["params"] or "{}"),
                 "status": r["status"], "created_at": r["created_at"]} for r in rows]

    def set_cluster_ids(self, mapping: dict[int, int]) -> None:
        """qa_id -> cluster_id"""
        with self.tx() as c:
            for qa_id, cid in mapping.items():
                c.execute("UPDATE qa_pair SET cluster_id=? WHERE qa_id=?", (cid, qa_id))

    def save_clusters(self, run_id: str, clusters: list[dict[str, Any]]) -> None:
        sql = """
        INSERT INTO q_cluster (run_id, cluster_id, rep_question, rep_norm, size,
                               customer_count, group_count, first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_id, cluster_id) DO UPDATE SET
            rep_question=excluded.rep_question, rep_norm=excluded.rep_norm,
            size=excluded.size, customer_count=excluded.customer_count,
            group_count=excluded.group_count, first_seen=excluded.first_seen,
            last_seen=excluded.last_seen
        """
        with self.tx() as c:
            for cl in clusters:
                c.execute(sql, (
                    run_id, int(cl["cluster_id"]), cl.get("rep_question", ""),
                    cl.get("rep_norm", ""), int(cl.get("size") or 0),
                    int(cl.get("customer_count") or 0), int(cl.get("group_count") or 0),
                    int(cl.get("first_seen") or 0), int(cl.get("last_seen") or 0),
                ))

    def top_clusters(self, run_id: str, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM q_cluster WHERE run_id=? ORDER BY size DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()

    def stats(self) -> dict[str, int]:
        q = lambda s: self._conn.execute(s).fetchone()[0]  # noqa: E731
        return {
            "raw_session": q("SELECT COUNT(*) FROM raw_session"),
            "raw_message": q("SELECT COUNT(*) FROM raw_message"),
            "clean_result": q("SELECT COUNT(*) FROM clean_result"),
            "qa_pair": q("SELECT COUNT(*) FROM qa_pair"),
            "q_cluster": q("SELECT COUNT(DISTINCT cluster_id) FROM qa_pair WHERE cluster_id>0"),
            "task_log": q("SELECT COUNT(*) FROM task_log"),
        }

    def prune(self, message_days: int = 90, session_days: int = 0,
              vacuum: bool = False) -> dict[str, int]:
        """清理历史原始数据。qa_pair 永远不动 —— 它是分析资产，且比消息小得多。

        message_days: 删掉多少天前的 raw_message（0=不删）
        session_days: 删掉多少天前的 raw_session（0=不删）
        vacuum:       清理后是否 VACUUM 回收磁盘（会锁库，离线跑）
        """
        import time as _t

        now_ms = int(_t.time() * 1000)
        out = {"raw_message": 0, "raw_session": 0}
        if message_days > 0:
            cutoff = now_ms - message_days * 86400_000
            cur = self._conn.execute("DELETE FROM raw_message WHERE ts < ?", (cutoff,))
            out["raw_message"] = cur.rowcount or 0
        if session_days > 0:
            cutoff = now_ms - session_days * 86400_000
            cur = self._conn.execute("DELETE FROM raw_session WHERE end_ts < ?", (cutoff,))
            out["raw_session"] = cur.rowcount or 0
        self._conn.commit()
        if vacuum:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.execute("VACUUM")
        return out

    def rotate(self, window_days: int = 60, now_ms: int | None = None,
               grace_days: int = 7, dry_run: bool = False) -> dict[str, int]:
        """滑动窗口裁剪：保留最近 window_days 个自然日，删掉更早的原始明细。

        和 prune 的区别：prune 按「当前时刻往前推 N×24h」，窗口在小时级滑动，
        同一天的数据可能半留半删。rotate 对齐到**本地自然日 00:00**，
        所以是「进一天、出一天」，每天的窗口边界是固定的。

        以会话为单位裁剪，消息跟着会话走 —— 因为一条消息脱离会话就没有意义。
        两个保护：
        - grace_days：过期但还没抽过问答的会话多留几天，等 extract 补上；
        - 已抽过问答的会话立即过期即删，不占宽限。

        qa_pair / q_cluster 不动 —— 结构化问答是分析资产，原文才是要清的那部分。
        """
        import time as _t

        if window_days <= 0:
            return {"raw_message": 0, "raw_session": 0, "orphan_session": 0,
                    "cutoff_ms": 0, "window_days": window_days, "grace_days": grace_days}

        now = now_ms if now_ms is not None else int(_t.time() * 1000)
        lt = _t.localtime(now / 1000)
        # 当天 00:00:00（本地时区）的毫秒时间戳
        midnight = int(_t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                  0, 0, 0, 0, 0, -1)) * 1000)
        cutoff = midnight - window_days * 86400_000
        grace = max(0, grace_days) * 86400_000

        # end_ts=0 表示「还没结束」（receiver 收到 SESSION_START 时就是 0）。
        # 直接拿 end_ts 比 cutoff 会把所有进行中的会话当成最老的数据删掉，
        # 所以先用 start_ts 兜底，两个都是 0 的（来路不明）一律不删。
        eff = "COALESCE(NULLIF(s.end_ts,0), NULLIF(s.start_ts,0), 0)"
        ses_sql = (
            f"FROM raw_session s WHERE {eff} > 0 AND {eff} < ? "
            f"AND ({eff} < ? OR EXISTS "
            "(SELECT 1 FROM qa_pair q WHERE q.session_id = s.session_id))"
        )
        ses_args = (cutoff, cutoff - grace)
        # 即将被删且没抽过问答的会话 = 抽取漏了，删掉就永久丢数据
        orphan = self._conn.execute(
            "SELECT COUNT(*) " + ses_sql + " AND NOT EXISTS "
            "(SELECT 1 FROM qa_pair q WHERE q.session_id = s.session_id)", ses_args
        ).fetchone()[0]

        if dry_run:
            msg = self._conn.execute(
                "SELECT COUNT(*) FROM raw_message WHERE " + _MSG_EXPIRED,
                (cutoff,)).fetchone()[0]
            ses = self._conn.execute("SELECT COUNT(*) " + ses_sql, ses_args).fetchone()[0]
            return {"raw_message": msg, "raw_session": ses, "orphan_session": orphan,
                    "cutoff_ms": cutoff, "window_days": window_days, "grace_days": grace_days}

        # 1) 删过期会话
        cur = self._conn.execute(
            "DELETE FROM raw_session WHERE session_id IN "
            "(SELECT s.session_id " + ses_sql + ")", ses_args)
        removed_ses = cur.rowcount or 0
        # 2) 删孤儿消息（会话已不存在）+ 自身时间戳已过宽限期的消息
        cur = self._conn.execute("DELETE FROM raw_message WHERE " + _MSG_EXPIRED,
                                 (cutoff,))
        removed_msg = cur.rowcount or 0
        self._conn.commit()

        return {"raw_message": removed_msg, "raw_session": removed_ses,
                "orphan_session": orphan, "cutoff_ms": cutoff,
                "window_days": window_days, "grace_days": grace_days}

    def db_size_mb(self) -> float:
        """数据库文件总大小（含 WAL）。"""
        import os as _os

        path = self.path
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += _os.path.getsize(path + suffix)
            except OSError:
                pass
        return round(total / 1024 / 1024, 1)

    def backup(self, dest: str) -> str:
        """在线备份到 dest。用 sqlite3 的 backup API，不需要 sqlite3 命令行工具。

        比 cp 安全：cp 可能拷到写了一半的 WAL；backup 保证一致性快照。
        """
        import os as _os
        from pathlib import Path as _P

        _P(dest).parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(dest)
        try:
            with target:
                self._conn.backup(target)
        finally:
            target.close()
        size = _os.path.getsize(dest) / 1024 / 1024
        return f"{dest} ({size:.1f} MB)"


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0
