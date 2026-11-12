"""A private registry for opaque, read-only Feishu evidence links."""

from __future__ import annotations

from contextlib import closing
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from ..clean.rules import mask_pii
from ..config import Settings
from ..extract.identity import SOURCE_IDENTITY_ROLE, decode_identity
from ..voc_assets import connection
from . import case_media, guest_evidence_view

TOKEN_PATTERN = r"[A-Za-z0-9_-]{43}"
PUBLIC_PATH = "/guest/evidence/"
PUBLIC_ORIGIN = "https://qiyu.fanwulink.cn"
_WEEK = 7 * 86400
_TTL_WEEKS = 12
_MAX_SOURCE_MESSAGES = 4000
_MAX_CONTEXT_TEXT = 1200
_FULL_MESSAGE_LIMIT = 6000
_FULL_CONTEXT_MESSAGES = 400
_FULL_CONTEXT_CHARS = 90_000
_MAX_CASES = 20
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TOKEN = re.compile(TOKEN_PATTERN)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_URL = re.compile(r"""(?:[a-z][a-z0-9+.-]*://|www\.|(?<![\w@])(?:[a-z0-9-]+\.)+(?:cn|com|net|org|io|ai|top|cc)\b)[^\s，。；！？、<>'"]*""", re.I)
_SOURCE_ID = re.compile(r"\bqiyu-(?:external(?:-sha256)?|user|session):[^\s，。；！？、]+", re.I)
_PRIVATE_VALUE = re.compile(
    r"((?:会话|客户|客服|店铺|商家|商品|账号|订单|消息|工单|运单)\s*(?:ID|编号|号)|"
    r"密码|口令|密钥|验证码|微信号|token|api[_ -]?key)\s*[:：=]?\s*([a-z0-9_.-]{4,})", re.I
)
_KIND = {"topics": "高频问题", "sops": "知识 SOP"}
_ROLE = {"user": "客户", "staff": "客服"}
_ZONE = timezone(timedelta(hours=8))
_ENTRY_LIMITS = {"title": 800, "summary": 1600, "detail": 16000}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guest_evidence (
    token TEXT PRIMARY KEY,
    dedupe TEXT NOT NULL UNIQUE,
    expires INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS guest_evidence_expires ON guest_evidence(expires);
"""

def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clean(value: object, names: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str):
        return ""
    text = _CONTROL.sub("", value)
    text = _URL.sub("[链接已隐藏]", text)
    text = _SOURCE_ID.sub("[标识已隐藏]", text)
    text = _PRIVATE_VALUE.sub(r"\1 [已脱敏]", text)
    if names is not None:
        text = names.sub("[名称已隐藏]", text)
    return mask_pii(text).strip()


def _date(value: object) -> bool:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _entry(value: dict, names: re.Pattern[str] | None) -> dict | None:
    if not isinstance(value, dict) or value.get("kind") not in ("topics", "sops"):
        return None
    start, end = value.get("start"), value.get("end")
    if not _date(start) or not _date(end) or start > end:
        return None
    result = {"kind": value["kind"], "start": start, "end": end}
    for field, limit in _ENTRY_LIMITS.items():
        text = _clean(value.get(field), names)
        if len(text) > limit or (field != "summary" and not text):
            return None
        result[field] = text
    return result


def _private_names(pair: dict, session: dict, messages: list[dict]) -> re.Pattern[str] | None:
    values = [
        pair.get("customer_name"), pair.get("customer_id"), pair.get("group_name"), pair.get("staff_name"),
        pair.get("session_id"), session.get("user_name"), session.get("user_id"), session.get("from_group"),
        session.get("staff_name"), session.get("staff_id"),
    ]
    for message in messages:
        if message["role"] != SOURCE_IDENTITY_ROLE:
            continue
        identity = decode_identity(message["content"], pair["session_id"])
        if identity:
            values.extend((identity["customer_name"], identity["customer_id"]))
    common = {"客户", "客服", "匿名客户", "未分组", "未记录客服"}
    names = {name.strip() for name in values if isinstance(name, str) and 2 <= len(name.strip()) <= 1000} - common
    if not names:
        return None
    alternatives = []
    for name in sorted(names, key=lambda item: (-len(item), item)):
        safe = re.escape(name)
        if name.isascii():
            safe = r"(?<![A-Za-z0-9])" + safe + r"(?![A-Za-z0-9])"
        alternatives.append(safe)
    return re.compile("|".join(alternatives), re.I)


def _message(role: str, stamp: object, value: object, names: re.Pattern[str] | None,
             limit: int = _MAX_CONTEXT_TEXT) -> dict | None:
    text = _clean(case_media.project(value)[0], names)
    if not text:
        return None
    try:
        time_text = datetime.fromtimestamp(int(stamp or 0) / 1000, _ZONE).strftime("%Y-%m-%d %H:%M:%S") if int(stamp or 0) > 0 else ""
    except (ValueError, TypeError, OverflowError, OSError):
        time_text = ""
    return {"role": role, "time": time_text, "text": text[:limit] + ("…" if len(text) > limit else "")}


def _related_ids(value: object) -> set[str]:
    try:
        result = json.loads(value) if isinstance(value, str) else []
    except (ValueError, RecursionError):
        return set()
    if not isinstance(result, list):
        return set()
    return {item for item in result if isinstance(item, str) and item}


def _controlled_context(pair: dict, origin: dict, rows: list[dict], anchor: str,
                        names: re.Pattern[str] | None, *, full: bool) -> tuple[list[dict], bool, int | None]:
    customer = pair.get("customer_id")
    questions = _related_ids(origin.get("question_message_ids"))
    answers = _related_ids(origin.get("answer_message_ids"))
    if not questions or not isinstance(customer, str) or not customer or customer.startswith("qiyu-session:"):
        return [], False, None
    segments: list[list[dict]] = []
    current: list[dict] | None = None
    for row in rows:
        role = row["role"]
        if role == SOURCE_IDENTITY_ROLE:
            owner = decode_identity(row["content"], pair["session_id"])
            current = [] if owner and owner["customer_id"] == customer else None
            if current is not None:
                segments.append(current)
        elif role == "boundary":
            current = None
        elif current is not None and (role in _ROLE or full and role == "bot"):
            current.append(row)
    matching = [
        segment for segment in segments
        if any(row["role"] == "user" and row["source_message_id"] in questions for row in segment)
    ]
    if len(matching) != 1:
        return [], False, None
    segment = matching[0]
    q_index = next(index for index, row in enumerate(segment) if row["role"] == "user" and row["source_message_id"] in questions)
    a_index = q_index
    for index, row in enumerate(segment):
        message_id = row["source_message_id"]
        if message_id in questions | answers and anchor and _digest(message_id) == anchor and abs(index - q_index) <= 8:
            a_index = index
            break
    if full:
        # Keep the nearest messages if a verified identity segment is unusually large.
        begin = max(0, min(q_index - _FULL_CONTEXT_MESSAGES // 2, len(segment) - _FULL_CONTEXT_MESSAGES))
        stop, text_limit = begin + _FULL_CONTEXT_MESSAGES, _FULL_MESSAGE_LIMIT
    else:
        # Always show the question, even when the SOP is anchored to a following answer.
        begin, stop = max(0, min(q_index, a_index) - 3), max(q_index, a_index) + 5
        text_limit = _MAX_CONTEXT_TEXT
    projected = [(index, message) for index, row in enumerate(segment[begin:stop], begin)
                 if (message := _message(row["role"], row["ts"], row["content"], names, text_limit)) is not None]
    limited = full and (len(segment) > _FULL_CONTEXT_MESSAGES or any(
        len(message["text"]) > _FULL_MESSAGE_LIMIT for _, message in projected
    ))
    if full and sum(len(message["text"]) for _, message in projected) > _FULL_CONTEXT_CHARS:
        limited = True
        size = 0
        kept = []
        for index, message in sorted(projected, key=lambda item: (abs(item[0] - q_index), item[0])):
            size += len(message["text"])
            if size > _FULL_CONTEXT_CHARS:
                break
            kept.append((index, message))
        projected = sorted(kept, key=lambda item: item[0])
    anchored_row = segment[a_index]
    anchor_index = a_index if anchored_row["role"] == "user" and anchored_row["source_message_id"] in questions else q_index
    position = next((position for position, (index, _) in enumerate(projected) if index == anchor_index), None)
    return [message for _, message in projected], limited, position


def _case(cfg: Settings, qa_id: int, anchor: str, *, full: bool = False) -> tuple[str, list[dict], str, re.Pattern[str] | None] | None:
    source = Path(cfg.runtime.db_path)
    if not source.is_file() or type(qa_id) is not int or qa_id <= 0:
        return None
    try:
        with connection(source) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM qa_pair WHERE qa_id=?", (qa_id,)).fetchone()
            if row is None:
                return None
            pair = dict(row)
            sid = pair["session_id"]
            session_row = db.execute(
                "SELECT user_name,user_id,staff_name,staff_id,from_group FROM raw_session WHERE session_id=?", (sid,)
            ).fetchone()
            if session_row is None:
                return None
            tables = {item[0] for item in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "push_sync_state" in tables:
                state = db.execute("SELECT status,archive_reason FROM push_sync_state WHERE session_id=?", (sid,)).fetchone()
                if state and (state["status"] == "archived" or state["archive_reason"]):
                    return None
            origin_row = db.execute("SELECT * FROM qa_origin WHERE qa_id=?", (qa_id,)).fetchone() if "qa_origin" in tables else None
            origin = dict(origin_row) if origin_row else {}
            if origin and (origin.get("active") != 1 or origin.get("session_id") != sid):
                return None
            rows = [dict(item) for item in db.execute(
                "SELECT role,content,ts,source_message_id FROM raw_message WHERE session_id=? ORDER BY seq LIMIT ?",
                (sid, _MAX_SOURCE_MESSAGES + 1),
            )]
    except (sqlite3.Error, OSError):
        return None
    if not rows:
        return None
    names = _private_names(pair, dict(session_row), rows)
    context, limited, position = (_controlled_context(pair, origin, rows, anchor, names, full=full)
                                  if len(rows) <= _MAX_SOURCE_MESSAGES else ([], False, None))
    scope = ("limited" if limited else "context") if context else "pair"
    if not context:
        limit = _FULL_MESSAGE_LIMIT if full else _MAX_CONTEXT_TEXT
        question = _message("user", pair.get("asked_at"), pair.get("question"), names, limit)
        answer = _message("staff", 0, pair.get("answer"), names, limit)
        if question is None:
            return None
        context = [question] + ([answer] if answer else [])
        position = 0
    # Do not substitute a different customer, source revision, or transcript.
    fingerprint = _digest([
        sid, pair.get("customer_id"), pair.get("asked_at"), pair.get("question"), pair.get("answer"),
        pair.get("created_at"), origin.get("evidence_key"), origin.get("fingerprint"),
        origin.get("revision"), scope, context,
    ])
    # A presentation marker should never change the fingerprint of an existing link.
    if position is not None:
        context[position]["anchor"] = True
    return fingerprint, context, scope, names


def _registry(cfg: Settings) -> Path:
    return Path(cfg.runtime.state_dir).resolve() / "voc-guest-evidence.db"


def _sections(value, names) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:24]:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"), names)[:180]
        lines = item.get("lines")
        if not title or not isinstance(lines, list):
            continue
        clean_lines = [line for value in lines[:80] if (line := _clean(value, names)[:3000])]
        if clean_lines:
            result.append({"title": title, "lines": clean_lines})
    return result


def _view(value, names) -> dict:
    if not isinstance(value, dict):
        return {}
    taxonomy, metrics = [], []
    for item in (value["taxonomy"] if isinstance(value.get("taxonomy"), list) else [])[:100]:
        if not isinstance(item, dict):
            continue
        primary, secondary = (_clean(item.get(key), names)[:250] for key in ("primary", "secondary"))
        if primary or secondary:
            row = {"primary": primary, "secondary": secondary}
            if type(item.get("count")) is int and item["count"] >= 0:
                row["count"] = item["count"]
            taxonomy.append(row)
    for item in (value["metrics"] if isinstance(value.get("metrics"), list) else [])[:16]:
        if not isinstance(item, dict):
            continue
        label = _clean(item.get("label"), names)[:200]
        metric_value = item.get("value")
        display = _clean(str(metric_value) if type(metric_value) is int else metric_value, names)[:200]
        if label and display:
            metrics.append({"label": label, "value": display})
    return {"taxonomy": taxonomy, "metrics": metrics, "sections": _sections(value.get("sections"), names)}


def _make_payload(cfg: Settings, qa_id, anchor_id, entry, view) -> dict | None:
    anchor = _digest(anchor_id) if isinstance(anchor_id, str) and 0 < len(anchor_id) <= 1024 else ""
    primary = _case(cfg, qa_id, anchor) if qa_id is not None else None
    if qa_id is not None and primary is None:
        return None
    names = primary[3] if primary else None
    safe = _entry(entry, names)
    if safe is None:
        return None
    safe.update(_view(view, names))
    raw_cases = view.get("cases", []) if isinstance(view, dict) else []
    raw_cases = raw_cases if isinstance(raw_cases, list) else []
    candidates = raw_cases[:_MAX_CASES]
    if qa_id is not None and not any(isinstance(item, dict) and item.get("qa_id") == qa_id for item in candidates):
        if len(candidates) < _MAX_CASES:
            candidates = [*candidates, {"qa_id": qa_id, "anchor_id": anchor_id}]

    cases = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        source = candidate.get("qa_id")
        if type(source) is not int or source <= 0 or source in seen:
            continue
        raw_anchor = candidate.get("anchor_id")
        pinned = _digest(raw_anchor) if isinstance(raw_anchor, str) and 0 < len(raw_anchor) <= 1024 else ""
        evidence = _case(cfg, source, pinned, full=True)
        if evidence is None:
            continue
        seen.add(source)
        case_names = evidence[3]
        cases.append({"qa": source, "anchor": pinned, "fingerprint": evidence[0],
                      "title": _clean(candidate.get("title"), case_names)[:450],
                      "category": _clean(candidate.get("category"), case_names)[:200],
                      "time": _clean(candidate.get("time"), case_names)[:45],
                      "sections": _sections(candidate.get("sections"), case_names)})
    total = view.get("total_cases", len(cases)) if isinstance(view, dict) else len(cases)
    total = max(total, len(cases)) if type(total) is int and total >= 0 else len(cases)
    return {"v": 2, "entry": safe, "cases": cases, "total_cases": total}


def _save(cfg: Settings, value: dict, replace_token: str = "") -> str:
    payload = _canonical(value)
    now = int(time.time())
    expires = (now // _WEEK + _TTL_WEEKS + 1) * _WEEK
    dedupe = _digest([expires, payload])
    path = _registry(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.executescript(_SCHEMA)
            db.execute("DELETE FROM guest_evidence WHERE expires<=?", (now,))
            if replace_token:
                previous = db.execute("SELECT payload FROM guest_evidence WHERE token=?", (replace_token,)).fetchone()
                if not previous:
                    return ""
                old_entry = _entry(json.loads(previous[0]).get("entry"), None)
                new_entry = _entry(value["entry"], None)
                if old_entry is None or old_entry != new_entry:
                    return ""
                # Keep URLs already shared with readers. A previously minted twin may
                # also remain accessible until its own expiry.
                collision = db.execute("SELECT token FROM guest_evidence WHERE dedupe=? AND token<>?",
                                       (dedupe, replace_token)).fetchone()
                if collision:
                    dedupe = _digest([dedupe, replace_token])
                db.execute("UPDATE guest_evidence SET dedupe=?,expires=?,payload=? WHERE token=?",
                           (dedupe, expires, payload, replace_token))
                return PUBLIC_ORIGIN + PUBLIC_PATH + replace_token
            db.execute("INSERT INTO guest_evidence VALUES (?,?,?,?) ON CONFLICT(dedupe) DO NOTHING",
                       (secrets.token_urlsafe(32), dedupe, expires, payload))
            found = db.execute("SELECT token FROM guest_evidence WHERE dedupe=?", (dedupe,)).fetchone()
            if found and _TOKEN.fullmatch(found[0]):
                return PUBLIC_ORIGIN + PUBLIC_PATH + found[0]
    except (sqlite3.Error, OSError, ValueError, RecursionError):
        return ""
    return ""


def issue_url(cfg: Settings, qa_id: int | None, anchor_id: str = "", *, entry: dict, view: dict | None = None) -> str:
    """Persist redacted result text and exact evidence bindings for an opaque guest link."""
    payload = _make_payload(cfg, qa_id, anchor_id, entry, view)
    return _save(cfg, payload) if payload else ""


def upgrade_url(cfg: Settings, token: str, *, entry: dict, view: dict) -> str:
    """Upgrade a previously published result only if its original content matches."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return ""
    payload = _make_payload(cfg, None, "", entry, view)
    return _save(cfg, payload, replace_token=token) if payload else ""


def _render_case(cfg: Settings, binding: dict, *, full: bool) -> dict | None:
    if not isinstance(binding, dict) or not isinstance(binding.get("anchor"), str):
        return None
    case = _case(cfg, binding.get("qa"), binding["anchor"], full=full)
    if case is None or not secrets.compare_digest(case[0], str(binding.get("fingerprint", ""))):
        return None
    return {"title": _clean(binding.get("title"), case[3]),
            "category": _clean(binding.get("category"), case[3]),
            "time": _clean(binding.get("time"), case[3]),
            "sections": _sections(binding.get("sections"), case[3]),
            "messages": case[1], "scope": case[2]}


def resolve_payload(cfg: Settings, token: str) -> dict | None:
    """Read the public projection for an exact bearer token."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    try:
        with connection(_registry(cfg)) as db:
            row = db.execute("SELECT payload FROM guest_evidence WHERE token=? AND expires>?",
                             (token, int(time.time()))).fetchone()
        if not row:
            return None
        value = json.loads(row["payload"])
        if not isinstance(value, dict) or value.get("v") not in (1, 2):
            return None
        raw_entry = value.get("entry")
        entry = _entry(raw_entry, None)
        if entry is None:
            return None
        if value["v"] == 1:
            bindings = [value["case"]] if isinstance(value.get("case"), dict) else []
            total = len(bindings)
        else:
            entry.update(_view(raw_entry, None))
            bindings = value.get("cases")
            if not isinstance(bindings, list):
                return None
            total = value.get("total_cases", len(bindings))
            if type(total) is not int or total < len(bindings):
                return None
        cases = [case for binding in bindings[:_MAX_CASES]
                 if (case := _render_case(cfg, binding, full=value["v"] == 2)) is not None]
        return {"entry": entry, "cases": cases, "total_cases": total}
    except (sqlite3.Error, OSError, ValueError, RecursionError):
        return None


def render_payload(payload: dict) -> bytes:
    """Render the scoped projection without a second registry read."""
    return guest_evidence_view.render_page(**payload)


def resolve(cfg: Settings, token: str) -> bytes | None:
    """Resolve an exact bearer token to the standalone HTML page."""
    payload = resolve_payload(cfg, token)
    return render_payload(payload) if payload is not None else None
