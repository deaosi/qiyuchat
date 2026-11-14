"""Store pending ticket sync work durably across runs."""

from collections.abc import Iterable
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_work (
    lane TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    ready_at INTEGER NOT NULL,
    PRIMARY KEY (lane, ticket_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ticket_work_ready ON ticket_work(lane, ready_at, ticket_id);
"""


@contextmanager
def _database(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("工单同步队列路径无效。")
        if os.name != "nt" and stat.S_IMODE(mode) != 0o600:
            path.chmod(0o600)
    else:
        os.close(descriptor)

    try:
        with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
            conn.executescript(_SCHEMA)
            yield conn
    except sqlite3.Error:
        raise ValueError("工单同步队列暂不可用。") from None


def enqueue(path: Path, lane: str, entries: dict[str, dict], *, now_ms: int, expedite_changes: bool = True) -> None:
    """Coalesce by ticket ID. An identical replay retains its existing retry time."""
    if not entries:
        return
    with _database(path) as conn:
        conn.executemany("""
            INSERT INTO ticket_work (lane, ticket_id, payload, ready_at) VALUES (?, ?, ?, ?)
            ON CONFLICT (lane, ticket_id) DO UPDATE SET
                payload = excluded.payload,
                ready_at = CASE WHEN ? THEN min(ticket_work.ready_at, excluded.ready_at)
                                ELSE ticket_work.ready_at END
            WHERE ticket_work.payload <> excluded.payload
        """, ((lane, key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False), now_ms, expedite_changes) for key, value in entries.items()))


def due(path: Path, lane: str, *, now_ms: int, limit: int) -> dict[str, dict]:
    """Read a bounded batch in due-time order. The ticket worker holds its lock."""
    if type(limit) is not int or limit < 1:
        raise ValueError("工单同步队列批次大小无效。")
    with _database(path) as conn:
        rows = conn.execute("""
            SELECT ticket_id, payload FROM ticket_work WHERE lane = ? AND ready_at <= ?
            ORDER BY ready_at, ticket_id LIMIT ?
        """, (lane, now_ms, limit))
        return {key: json.loads(payload) for key, payload in rows}


def finish(path: Path, lane: str, keys: Iterable[str]) -> None:
    """Acknowledge a batch after the remote writes are confirmed."""
    with _database(path) as conn:
        conn.executemany("DELETE FROM ticket_work WHERE lane = ? AND ticket_id = ?",
                         ((lane, key) for key in keys))


def defer(path: Path, lane: str, keys: Iterable[str], *, until_ms: int) -> None:
    with _database(path) as conn:
        conn.executemany("UPDATE ticket_work SET ready_at = ? WHERE lane = ? AND ticket_id = ?",
                         ((until_ms, lane, key) for key in keys))


def count(path: Path, lane: str) -> int:
    with _database(path) as conn:
        return conn.execute("SELECT count(*) FROM ticket_work WHERE lane = ?", (lane,)).fetchone()[0]


def next_due(path: Path, lane: str) -> int | None:
    with _database(path) as conn:
        row = conn.execute("SELECT ready_at FROM ticket_work WHERE lane = ? ORDER BY ready_at LIMIT 1",
                           (lane,)).fetchone()
        return row[0] if row else None
