"""Persist technical feedback received from Feishu and reconcile it with Qiyu."""

from __future__ import annotations

from itertools import combinations
import json
from pathlib import Path
import re
from secrets import token_hex
import time
from urllib.parse import urlsplit

from .config import Settings
from .qiyu.ticket import MAX_REPLY_ATTACHMENTS, TicketClient, TicketError, TicketReplyUnconfirmed, TicketReplyVisible
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
    investigation TEXT NOT NULL DEFAULT '',
    feishu_url TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    writeback_status TEXT NOT NULL,
    written_at INTEGER,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ticket_feedback_retry
ON ticket_feedback(writeback_status) WHERE writeback_status IN ('pending', 'failed', 'uncertain');
CREATE TABLE IF NOT EXISTS ticket_feedback_delivery (
    ticket_id TEXT PRIMARY KEY,
    planned_files TEXT,
    last_text_revision TEXT,
    last_written_at INTEGER
);
CREATE TABLE IF NOT EXISTS ticket_feedback_sent_file (
    ticket_id TEXT NOT NULL,
    file_token_hash TEXT NOT NULL,
    PRIMARY KEY (ticket_id, file_token_hash)
);
CREATE TABLE IF NOT EXISTS ticket_feedback_reply_author (
    ticket_id TEXT NOT NULL,
    reply_token TEXT NOT NULL,
    author TEXT NOT NULL,
    PRIMARY KEY (ticket_id, reply_token)
);
"""
_PUBLIC_FIELDS = ("status", "reply", "resolved_version", "investigation", "feishu_url", "received_at",
                  "writeback_status", "written_at", "error")
_STATES = {
    "pending": "待回传",
    "succeeded": "已回传",
    "failed": "回传失败",
    "uncertain": "待核对七鱼回复",
}
ATTACHMENT_ONLY_REPLY = "请查看随回复提交的附件。"


class AttachmentDownloadError(Exception):
    pass


class FeedbackChanged(Exception):
    """The source was edited while its attachments were downloading."""


def _path(cfg: Settings) -> Path:
    source = Path(cfg.runtime.db_path).resolve()
    path = source.with_name(source.stem + ".ticket-feedback.db")
    if path.is_symlink() or (path.exists() and source.exists() and path.samefile(source)):
        raise ValueError("工单反馈数据库路径无效。")
    return path


def _normalized(feedback: dict) -> dict:
    values = {}
    for key in ("status", "reply", "version", "investigation"):
        value = feedback.get(key, "")
        if not isinstance(value, str):
            raise ValueError("飞书技术反馈字段格式无效。")
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if normalized or key != "investigation":
            values[key] = normalized
    files = feedback.get("attachments") or []
    if not isinstance(files, list) or any(
        not isinstance(item, dict) or set(item) != {"file_token", "name", "size"}
        or not isinstance(item["file_token"], str) or not item["file_token"]
        or not isinstance(item["name"], str) or not item["name"]
        or type(item["size"]) is not int or item["size"] < 0
        for item in files
    ):
        raise ValueError("飞书技术回复附件格式无效。")
    if files:
        values["attachments"] = sorted(files, key=lambda item: item["file_token"])
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


def _reply_text(values: dict, token: str) -> str:
    parts = ["语海 Voc · 飞书技术反馈"]
    for key, label in (("status", "处理状态"), ("version", "解决版本"), ("reply", "技术回复"),
                       ("investigation", "技术支持排查结果")):
        if values.get(key):
            parts.append(f"{label}：{values[key]}")
    if len(parts) == 1 and values.get("attachments"):
        parts.append(ATTACHMENT_ONLY_REPLY)
    parts.append(f"〔语海同步 {token}〕")
    return "\n".join(parts)


def _writeback(cfg: Settings, ticket_id: str, text: str, *, reconcile: bool, attachments=(),
               attachment_manifest=()) -> tuple[str, int | None, str]:
    try:
        client = TicketClient(cfg.qiyu)
        if reconcile:
            if not client.has_feedback(int(ticket_id), text, attachment_manifest=attachment_manifest):
                return "uncertain", None, "等待七鱼回复记录确认。"
        else:
            options = {"attachments": attachments} if attachments else {}
            client.post_feedback(int(ticket_id), text, **options)
    except TicketReplyVisible as exc:
        return "uncertain", None, str(exc)
    except TicketReplyUnconfirmed:
        return "uncertain", None, "等待七鱼回复记录确认。"
    except TicketError as exc:
        return ("uncertain" if reconcile else "failed"), None, str(exc)
    return "succeeded", int(time.time() * 1000), ""


def _stored_text(row):
    text = {"status": row["status"], "reply": row["reply"], "version": row["resolved_version"]}
    if "investigation" in row.keys() and row["investigation"]:
        text["investigation"] = row["investigation"]
    return text


def _legacy_files(row, files):
    """Recover attachment identities only if the pre-upgrade revision matches."""
    text = _stored_text(row)
    if row["revision"] == digest(text):
        return []
    if row["revision"] == digest({**text, "attachments": files}):
        return files
    # Before this upgrade the source cell was limited to five files.
    if len(files) <= MAX_REPLY_ATTACHMENTS * 2:
        for count in range(1, min(MAX_REPLY_ATTACHMENTS, len(files) - 1) + 1):
            for subset in combinations(files, count):
                if row["revision"] == digest({**text, "attachments": subset}):
                    return list(subset)
    return None


def _mark_delivered(conn, ticket_id, text, files, written_at):
    conn.execute("""
        UPDATE ticket_feedback_delivery SET last_text_revision = ?, last_written_at = ? WHERE ticket_id = ?
    """, (digest(text), written_at, ticket_id))
    conn.executemany("INSERT OR IGNORE INTO ticket_feedback_sent_file VALUES (?, ?)",
                     ((ticket_id, digest(item["file_token"])) for item in files))


def _delivery(conn, ticket_id, previous, files):
    query = "SELECT * FROM ticket_feedback_delivery WHERE ticket_id = ?"
    delivery = conn.execute(query, (ticket_id,)).fetchone()
    if delivery is None:
        legacy = _legacy_files(previous, files) if previous is not None else []
        conn.execute("INSERT INTO ticket_feedback_delivery (ticket_id, planned_files) VALUES (?, ?)",
                     (ticket_id, json.dumps(legacy) if legacy is not None else None))
        if previous is not None and previous["writeback_status"] == "succeeded":
            _mark_delivered(conn, ticket_id, _stored_text(previous), legacy or [], previous["written_at"])
        delivery = conn.execute(query, (ticket_id,)).fetchone()
    return delivery


def _finish(conn, ticket_id, token, text, files, state, written_at, error):
    with transaction(conn):
        saved = conn.execute("""UPDATE ticket_feedback SET writeback_status = ?, written_at = ?, error = ?
                                WHERE ticket_id = ? AND reply_token = ?""", (state, written_at, error, ticket_id, token))
        if saved.rowcount == 1 and state == "succeeded":
            _mark_delivered(conn, ticket_id, text, files, written_at)


def _result(state, written_at, error, *, changed):
    return {"status": _STATES[state], "returned_at": written_at, "changed": changed, "error": error}


def _prepare(conn):
    conn.executescript(_SCHEMA)
    if "investigation" not in {row[1] for row in conn.execute("PRAGMA table_info(ticket_feedback)")}:
        with transaction(conn):
            if "investigation" not in {row[1] for row in conn.execute("PRAGMA table_info(ticket_feedback)")}:
                conn.execute("ALTER TABLE ticket_feedback ADD COLUMN investigation TEXT NOT NULL DEFAULT ''")


def ingest(cfg: Settings, *, ticket_id: str, record_id: str, feedback: dict, feishu_url: str = "", download=None,
           resolve_author=None) -> dict:
    """Run under the Feishu table's write lock and keep inbox data before external writes."""
    if (not re.fullmatch(r"[1-9][0-9]{0,18}", ticket_id) or int(ticket_id) > 2 ** 63 - 1
            or not re.fullmatch(r"rec[A-Za-z0-9]{4,60}", record_id)):
        raise ValueError("飞书反馈的工单编号或记录编号无效。")
    values = _normalized(feedback)
    if not any(values.values()):
        return {"status": "", "returned_at": None, "changed": False, "error": ""}
    revision = digest(values)
    files = values.get("attachments", [])
    text = {key: value for key, value in values.items() if key != "attachments"}
    path = _path(cfg)
    with connection(path, write=True) as conn:
        _prepare(conn)
        with transaction(conn):
            same_ticket = conn.execute("SELECT * FROM ticket_feedback WHERE ticket_id = ?", (ticket_id,)).fetchone()
            bound_ticket = conn.execute("SELECT ticket_id FROM ticket_feedback WHERE record_id = ?", (record_id,)).fetchone()
            if ((same_ticket and same_ticket["record_id"] != record_id)
                    or (bound_ticket and bound_ticket["ticket_id"] != ticket_id)):
                raise ValueError("飞书记录和七鱼工单的关联已变化，请核对工单 ID。")
            delivery = _delivery(conn, ticket_id, same_ticket, files)

        changed = same_ticket is None or same_ticket["revision"] != revision
        if not changed and same_ticket["writeback_status"] == "succeeded":
            return _result("succeeded", same_ticket["written_at"], "", changed=False)
        planned = json.loads(delivery["planned_files"]) if delivery["planned_files"] is not None else None
        if planned is None and same_ticket is not None:
            planned = _legacy_files(same_ticket, files)
        reconcile = same_ticket is not None and same_ticket["writeback_status"] == "uncertain"
        if changed and reconcile and files:
            present = {item["file_token"] for item in files}
            if planned is None or any(item["file_token"] in present for item in planned):
                # An edit can arrive before the previous reply's attachments become visible.
                # Resolve those files against the previous marker before selecting the next reply.
                if planned is None:
                    return _result("uncertain", None, "上一轮回复的附件仍待七鱼确认。", changed=False)
                previous_text = _stored_text(same_ticket)
                previous_token = same_ticket["reply_token"]
                state, written_at, error = _writeback(
                    cfg, ticket_id, _reply_text({**previous_text, "attachments": planned}, previous_token),
                    reconcile=True, attachment_manifest=tuple((item["name"], item["size"]) for item in planned),
                )
                _finish(conn, ticket_id, previous_token, previous_text, planned, state, written_at, error)
                if state != "succeeded":
                    return _result(state, written_at, error, changed=False)
                delivery = _delivery(conn, ticket_id, same_ticket, files)

        with transaction(conn):
            if changed:
                sent = {row[0] for row in conn.execute(
                    "SELECT file_token_hash FROM ticket_feedback_sent_file WHERE ticket_id = ?", (ticket_id,))}
                planned = [item for item in files if digest(item["file_token"]) not in sent]
                unchanged_text = delivery["last_text_revision"] == digest(text)
                if not planned and (unchanged_text or not any(text.values())):
                    written_at = delivery["last_written_at"]
                    conn.execute("""UPDATE ticket_feedback SET revision=?, status=?, reply=?, resolved_version=?, investigation=?,
                                    writeback_status='succeeded', written_at=?, error='' WHERE ticket_id=?""",
                                 (revision, text["status"], text["reply"], text["version"], text.get("investigation", ""),
                                  written_at, ticket_id))
                    return _result("succeeded", written_at, "", changed=False)
                token = token_hex(8)
                conn.execute("""
                    INSERT INTO ticket_feedback
                        (ticket_id, record_id, revision, reply_token, status, reply, resolved_version, investigation, feishu_url,
                         received_at, writeback_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    ON CONFLICT(ticket_id) DO UPDATE SET revision = excluded.revision, reply_token = excluded.reply_token,
                        status = excluded.status, investigation = excluded.investigation,
                        reply = excluded.reply, resolved_version = excluded.resolved_version, feishu_url = excluded.feishu_url,
                        received_at = excluded.received_at, writeback_status = 'pending', written_at = NULL, error = ''
                """, (ticket_id, record_id, revision, token, text["status"], text["reply"], text["version"],
                      text.get("investigation", ""),
                      _origin_link(feishu_url), int(time.time() * 1000)))
                conn.execute("UPDATE ticket_feedback_delivery SET planned_files = ? WHERE ticket_id = ?",
                             (json.dumps(planned), ticket_id))
            else:
                token = same_ticket["reply_token"]
            reconcile = reconcile and not changed
        if planned is None:
            raise ValueError("七鱼技术回复的待回传附件无法核对。")
        if resolve_author is not None and changed:
            previous = _stored_text(same_ticket) if same_ticket is not None else {}
            changed_text = {role: item for role, item in text.items() if item and item != previous.get(role)}
            author = resolve_author(changed_text, planned)
            if author:
                conn.execute("INSERT OR IGNORE INTO ticket_feedback_reply_author VALUES (?, ?, ?)",
                             (ticket_id, token, author))
        manifest = tuple((item["name"], item["size"]) for item in planned)
        attachments = ()
        try:
            if planned and not reconcile:
                if download is None:
                    raise AttachmentDownloadError("飞书技术回复附件暂时无法下载。")
                attachments = download(planned)
                if tuple((name, len(data)) for name, data in attachments) != manifest:
                    raise AttachmentDownloadError("飞书下载的附件与本次技术回复不一致。")
        except FeedbackChanged:
            return _result("pending", None, "", changed=False)
        except AttachmentDownloadError as exc:
            state, written_at, error = "failed", None, str(exc)
        else:
            # Persist the intent before sending. After an interruption only read back the same token.
            conn.execute("UPDATE ticket_feedback SET writeback_status = 'uncertain' WHERE ticket_id = ? AND reply_token = ?",
                         (ticket_id, token))
            state, written_at, error = _writeback(cfg, ticket_id, _reply_text({**text, "attachments": planned}, token),
                                                  reconcile=reconcile, attachments=attachments,
                                                  attachment_manifest=manifest)
        _finish(conn, ticket_id, token, text, planned, state, written_at, error)
    return _result(state, written_at, error, changed=changed)


def get(cfg: Settings, ticket_id: str) -> dict | None:
    path = _path(cfg)
    if not path.is_file():
        return None
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ticket_feedback'").fetchone():
            return None
        row = conn.execute("SELECT * FROM ticket_feedback WHERE ticket_id = ?", (ticket_id,)).fetchone()
    if row is None:
        return None
    values = dict(row)
    return {field: values.get(field, "") for field in _PUBLIC_FIELDS}


def reply_authors(cfg: Settings, ticket_id: str) -> dict[str, str]:
    path = _path(cfg)
    if not path.is_file():
        return {}
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ticket_feedback_reply_author'").fetchone():
            return {}
        return dict(conn.execute("SELECT reply_token,author FROM ticket_feedback_reply_author WHERE ticket_id=?", (ticket_id,)))


def confirmed(cfg: Settings, ticket_id: str, feedback: dict) -> bool:
    path = _path(cfg)
    if not path.is_file():
        return False
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ticket_feedback'").fetchone():
            return False
        return conn.execute("""
            SELECT 1 FROM ticket_feedback WHERE ticket_id = ? AND revision = ? AND writeback_status = 'succeeded'
        """, (ticket_id, digest(_normalized(feedback)))).fetchone() is not None


def retry_records(cfg: Settings, *, limit: int = 100, after: str = "") -> dict[str, str]:
    path = _path(cfg)
    if not path.is_file():
        return {}
    with connection(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ticket_feedback'").fetchone():
            return {}
        return dict(conn.execute("""
            SELECT ticket_id, record_id FROM ticket_feedback
            WHERE writeback_status IN ('pending', 'failed', 'uncertain') AND ticket_id > ? ORDER BY ticket_id LIMIT ?
        """, (after, limit)))
