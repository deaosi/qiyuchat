"""Persist technical feedback received from Feishu and reconcile it with Qiyu."""

from __future__ import annotations

from pathlib import Path
import re
from secrets import token_hex
import time
from urllib.parse import urlsplit

from .config import Settings
from .qiyu.ticket import TicketClient, TicketError, TicketReplyUnconfirmed, TicketReplyVisible
from .voc_assets import connection, digest, transaction


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_feedback (
    ticket_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL UNIQUE,
    revision TEXT NOT NULL,
    reply_token TEXT NOT NULL,
    status TEXT NOT NULL,
    reply TEXT NOT NULL,
    resolved_version TEXT NOT NULL,
    feishu_url TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    writeback_status TEXT NOT NULL,
    written_at INTEGER,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ticket_feedback_retry
ON ticket_feedback(writeback_status) WHERE writeback_status IN ('pending', 'failed', 'uncertain');
"""
_PUBLIC = """SELECT status, reply, resolved_version, feishu_url, received_at,
                   writeback_status, written_at, error
            FROM ticket_feedback WHERE ticket_id = ?"""
_STATES = {
    "pending": "待回传",
    "succeeded": "已回传",
    "failed": "回传失败",
    "uncertain": "待核对七鱼回复",
}


def _path(cfg: Settings) -> Path:
    source = Path(cfg.runtime.db_path).resolve()
    path = source.with_name(source.stem + ".ticket-feedback.db")
    if path.is_symlink() or (path.exists() and source.exists() and path.samefile(source)):
        raise ValueError("工单反馈数据库路径无效。")
    return path


def _normalized(feedback: dict) -> dict[str, str]:
    values = {}
    for key in ("status", "reply", "version"):
        value = feedback.get(key, "")
        if not isinstance(value, str):
            raise ValueError("飞书技术反馈字段格式无效。")
        values[key] = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return values


def _origin_link(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = parts.hostname or ""
    if parts.scheme == "https" and not parts.username and not parts.password and (
        host.endswith(".feishu.cn") or host.endswith(".larksuite.com")
    ):
        return value
    return ""


def _reply_text(values: dict[str, str], token: str) -> str:
    parts = ["语海 Voc · 飞书技术反馈"]
    for key, label in (("status", "处理状态"), ("version", "解决版本"), ("reply", "技术回复")):
        if values[key]:
            parts.append(f"{label}：{values[key]}")
    parts.append(f"〔语海同步 {token}〕")
    return "\n".join(parts)


def _writeback(cfg: Settings, ticket_id: str, text: str, *, reconcile: bool) -> tuple[str, int | None, str]:
    try:
        client = TicketClient(cfg.qiyu)
        if reconcile:
            if not client.has_feedback(int(ticket_id), text):
                return "uncertain", None, "等待七鱼回复记录确认。"
        else:
            client.post_feedback(int(ticket_id), text)
    except TicketReplyVisible as exc:
        return "uncertain", None, str(exc)
    except TicketReplyUnconfirmed:
        return "uncertain", None, "等待七鱼回复记录确认。"
    except TicketError as exc:
        return ("uncertain" if reconcile else "failed"), None, str(exc)
    return "succeeded", int(time.time() * 1000), ""


def ingest(cfg: Settings, *, ticket_id: str, record_id: str, feedback: dict, feishu_url: str = "") -> dict:
    """Run under the Feishu table's write lock and keep inbox data before external writes."""
    if (not re.fullmatch(r"[1-9][0-9]{0,18}", ticket_id) or int(ticket_id) > 2 ** 63 - 1
            or not re.fullmatch(r"rec[A-Za-z0-9]{4,60}", record_id)):
        raise ValueError("飞书反馈的工单编号或记录编号无效。")
    values = _normalized(feedback)
    if not any(values.values()):
        return {"status": "", "returned_at": None, "changed": False, "error": ""}
    revision = digest(values)
    path = _path(cfg)
    with connection(path, write=True) as conn:
        conn.executescript(_SCHEMA)
        with transaction(conn):
            same_ticket = conn.execute("SELECT * FROM ticket_feedback WHERE ticket_id = ?", (ticket_id,)).fetchone()
            bound_ticket = conn.execute("SELECT ticket_id FROM ticket_feedback WHERE record_id = ?", (record_id,)).fetchone()
            if ((same_ticket and same_ticket["record_id"] != record_id)
                    or (bound_ticket and bound_ticket["ticket_id"] != ticket_id)):
                raise ValueError("飞书记录和七鱼工单的关联已变化，请核对工单 ID。")
            changed = same_ticket is None or same_ticket["revision"] != revision
            if not changed and same_ticket["writeback_status"] == "succeeded":
                return {"status": _STATES["succeeded"], "returned_at": same_ticket["written_at"],
                        "changed": False, "error": ""}
            if changed:
                token = token_hex(8)
                conn.execute("""
                    INSERT INTO ticket_feedback
                        (ticket_id, record_id, revision, reply_token, status, reply, resolved_version, feishu_url,
                         received_at, writeback_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    ON CONFLICT(ticket_id) DO UPDATE SET revision = excluded.revision, reply_token = excluded.reply_token,
                        status = excluded.status,
                        reply = excluded.reply, resolved_version = excluded.resolved_version, feishu_url = excluded.feishu_url,
                        received_at = excluded.received_at, writeback_status = 'pending', written_at = NULL, error = ''
                """, (ticket_id, record_id, revision, token, values["status"], values["reply"], values["version"],
                      _origin_link(feishu_url), int(time.time() * 1000)))
            else:
                token = same_ticket["reply_token"]
            reconcile = not changed and same_ticket["writeback_status"] == "uncertain"
            # Persist the intent before sending. After an interruption only read back the same token.
            conn.execute("UPDATE ticket_feedback SET writeback_status = 'uncertain' WHERE ticket_id = ?", (ticket_id,))
        state, written_at, error = _writeback(cfg, ticket_id, _reply_text(values, token), reconcile=reconcile)
        conn.execute("""UPDATE ticket_feedback SET writeback_status = ?, written_at = ?, error = ?
                        WHERE ticket_id = ? AND reply_token = ?""", (state, written_at, error, ticket_id, token))
    return {"status": _STATES[state], "returned_at": written_at, "changed": changed, "error": error}


def get(cfg: Settings, ticket_id: str) -> dict | None:
    path = _path(cfg)
    if not path.is_file():
        return None
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ticket_feedback'").fetchone():
            return None
        row = conn.execute(_PUBLIC, (ticket_id,)).fetchone()
    return dict(row) if row is not None else None


def retry_records(cfg: Settings) -> dict[str, str]:
    path = _path(cfg)
    if not path.is_file():
        return {}
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ticket_feedback'").fetchone():
            return {}
        return dict(conn.execute("""
            SELECT ticket_id, record_id FROM ticket_feedback
            WHERE writeback_status IN ('pending', 'failed', 'uncertain')
        """))
