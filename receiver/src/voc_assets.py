"""Isolated, versioned derived storage for VOC v2. Never opens the business DB.

The old VOC tables and manually curated knowledge are deliberately not migrated
or updated. All writers use short transactions; inference is owned by the engine.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS v2_source (
 qa_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, pipeline TEXT NOT NULL,
 snapshot TEXT NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS v2_topic (
 id TEXT PRIMARY KEY, question TEXT NOT NULL, theme TEXT NOT NULL, boundary TEXT NOT NULL,
 domain TEXT NOT NULL DEFAULT '', primary_intent TEXT NOT NULL DEFAULT '',
 scope_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', manual INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS v2_intent (
 id TEXT PRIMARY KEY, qa_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, topic_id TEXT NOT NULL,
 quote TEXT NOT NULL, start_pos INTEGER NOT NULL, end_pos INTEGER NOT NULL, payload TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS v2_intent_topic ON v2_intent(topic_id,active);
CREATE TABLE IF NOT EXISTS v2_intent_version (
 intent_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
 fingerprint TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(intent_id,revision));
CREATE TABLE IF NOT EXISTS v2_asset (
 id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT NOT NULL, version INTEGER NOT NULL,
 variant_key TEXT NOT NULL DEFAULT '',
 fingerprint TEXT NOT NULL, status TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
 scope TEXT NOT NULL, payload TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 UNIQUE(topic_id,fingerprint));
CREATE TABLE IF NOT EXISTS v2_exception (
 id TEXT PRIMARY KEY, topic_id TEXT, qa_id INTEGER, kind TEXT NOT NULL, state TEXT NOT NULL,
 fingerprint TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS v2_vector (
 model TEXT NOT NULL, text_hash TEXT NOT NULL, vector TEXT NOT NULL,
 PRIMARY KEY(model,text_hash));
CREATE TABLE IF NOT EXISTS v2_job (
 id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL, total INTEGER NOT NULL,
 processed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
 skipped INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,
 usage_tokens INTEGER NOT NULL DEFAULT 0, reserved_tokens INTEGER NOT NULL DEFAULT 0,
 model TEXT NOT NULL, pipeline TEXT NOT NULL, owner TEXT NOT NULL,
 filters TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS v2_one_active_job ON v2_job((1)) WHERE status='running';
CREATE TABLE IF NOT EXISTS v2_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, asset_id TEXT NOT NULL,
 detail TEXT NOT NULL, created_at REAL NOT NULL);
"""


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


def store_path(cfg: Any) -> Path:
    source = Path(cfg.runtime.db_path).resolve()
    target = source.with_name(source.stem + ".voc-v2.db")
    if target.is_symlink() or target.exists() and source.exists() and target.samefile(source):
        raise ValueError("派生数据库必须是独立文件，不能指向原业务数据库。")
    return target


@contextmanager
def connection(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    path = path.resolve()
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    else:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15,
                               isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
    finally:
        conn.close()


def initialize(path: Path) -> None:
    with connection(path, write=True) as conn:
        conn.executescript(SCHEMA)
        if "variant_key" not in {row[1] for row in conn.execute("PRAGMA table_info(v2_asset)")}:
            conn.execute("ALTER TABLE v2_asset ADD COLUMN variant_key TEXT NOT NULL DEFAULT ''")
        topic_columns = {row[1] for row in conn.execute("PRAGMA table_info(v2_topic)")}
        for name in ("domain", "primary_intent"):
            if name not in topic_columns:
                conn.execute(f"ALTER TABLE v2_topic ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS v2_asset_variant ON v2_asset(topic_id,variant_key,version)")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def topic_id(question_key: str, scope: list[dict]) -> str:
    return "t_" + digest([question_key, scope])[:24]


def intent_id(qa_id: int, start: int, end: int, quote: str) -> str:
    # Independent of model wording, answer edits, run, model and pipeline version.
    return "i_" + digest([qa_id, start, end, quote])[:28]


def save_intent(conn: sqlite3.Connection, row: dict) -> None:
    old = conn.execute("SELECT * FROM v2_intent WHERE id=?", (row["id"],)).fetchone()
    payload = dump(row["data"])
    changed = old is None or old["payload"] != payload or old["fingerprint"] != row["fingerprint"] or old["topic_id"] != row["topic_id"]
    revision = (old["revision"] + int(changed)) if old else 1
    now = time.time()
    conn.execute("""INSERT INTO v2_intent(id,qa_id,fingerprint,topic_id,quote,start_pos,end_pos,payload,active,revision,updated_at)
      VALUES (?,?,?,?,?,?,?,?,1,?,?) ON CONFLICT(id) DO UPDATE SET
      qa_id=excluded.qa_id,fingerprint=excluded.fingerprint,topic_id=excluded.topic_id,quote=excluded.quote,
      start_pos=excluded.start_pos,end_pos=excluded.end_pos,payload=excluded.payload,
      active=1,revision=excluded.revision,updated_at=excluded.updated_at""",
                 (row["id"], row["qa_id"], row["fingerprint"], row["topic_id"], row["quote"],
                  row["start"], row["end"], payload, revision, now))
    if changed:
        conn.execute("INSERT INTO v2_intent_version VALUES (?,?,?,?,?)",
                     (row["id"], revision, dump({**row, "data": row["data"]}), row["fingerprint"], now))


def put_exception(conn: sqlite3.Connection, *, kind: str, fingerprint: str,
                  payload: dict, topic_id: str | None = None, qa_id: int | None = None) -> None:
    key = "e_" + digest([kind, topic_id, qa_id, fingerprint])[:28]
    # Keep an explicit human disposition on the identical evidence version.
    conn.execute("""INSERT INTO v2_exception VALUES (?,?,?,?,?,?,?,?)
      ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
                 (key, topic_id, qa_id, kind, "open", fingerprint, dump(payload), time.time()))


def put_asset(conn: sqlite3.Connection, *, topic: dict, fingerprint: str,
              status: str, answer: str, scope: str, payload: dict, variant_key: str = "") -> int:
    old = conn.execute("SELECT id FROM v2_asset WHERE topic_id=? AND fingerprint=?",
                       (topic["id"], fingerprint)).fetchone()
    if old:
        return old["id"]
    version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM v2_asset WHERE topic_id=? AND variant_key=?",
                           (topic["id"], variant_key)).fetchone()[0]
    now = time.time()
    return conn.execute("""INSERT INTO v2_asset(topic_id,version,variant_key,fingerprint,status,question,answer,scope,payload,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (topic["id"], version, variant_key, fingerprint, status,
      topic["question"], answer, scope, dump(payload), now, now)).lastrowid
