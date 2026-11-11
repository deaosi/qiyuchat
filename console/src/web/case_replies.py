"""Read exact official session replies without rewriting retained cases."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
import threading
import time

from ..config import Settings
from ..extract import extract_pairs
from ..extract.identity import sender_context
from ..extract.text import _attachment, clean_visible_text
from ..push_sync import _expired, _numeric_identity, _project, _sender
from ..qiyu.message import source_integer
from ..qiyu.session import SessionExporter


_GATE = threading.Lock()
_NEXT_LOOKUP = 0.0
_INTERVAL = 0.25  # Two endpoints, at most four requests per endpoint per second.
_COUNTS = ('staffMessageCount', 'userMessageCount')
_IDENTITY_PREFIXES = ('qiyu-user:', 'qiyu-external:', 'qiyu-external-sha256:', 'qiyu-session:')


def _result(status: str, messages: list[dict] | None = None) -> dict:
    return {'status': status, 'messages': messages or []}


def _count(value: object) -> bool:
    if isinstance(value, str) and 1 <= len(value) <= 5 and value.isascii() and value.isdecimal():
        value = int(value)
    return type(value) is int and 0 <= value <= 50_000


def _local_session(cfg: Settings, session: dict) -> tuple[int, str] | None:
    if cfg.runtime.mock or not cfg.qiyu.app_key or not cfg.qiyu.app_secret:
        return None
    raw_id = session.get('session_id')
    sid = _numeric_identity(raw_id)
    if sid is None or str(raw_id) != str(sid):
        return None
    try:
        payload = json.loads(session.get('payload') or '')
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or _numeric_identity(payload.get('id')) != sid:
        return None
    channel = payload.get('fromType')
    if (not isinstance(channel, str) or not channel.strip() or channel.strip() == '本地导入'
            or payload.get('_source') == 'push' or not all(_count(payload.get(k)) for k in _COUNTS)):
        return None
    stamp = session.get('end_ts') or session.get('start_ts')
    if not source_integer(stamp, positive=True) or _expired({'endTime': stamp}, cfg, time.time()):
        return None
    return sid, channel


def eligible(cfg: Settings, session: dict) -> bool:
    """Only native, retained sessions with an official channel and counts."""
    return _local_session(cfg, session) is not None


def _automated(row: dict) -> bool:
    flag = row.get('autoReply')
    # A missing flag is supported by the verified API schema. Unknown values
    # cannot prove that a response was manually sent.
    return not (flag is None or flag is False or type(flag) is int and flag == 0
                or isinstance(flag, str) and flag.strip().lower() in ('', '0', 'false'))


def _staff_content(row: dict) -> str:
    content = clean_visible_text(row['msg'])
    if content:
        return content
    if row['mType'] != 2:
        return ''
    try:
        data = json.loads(row['msg'])
        if isinstance(data, str):
            data = json.loads(data)
        return '[图片]' if isinstance(data, dict) and _attachment(data) else ''
    except (ValueError, TypeError, RecursionError):
        return ''


def _retained_key(message: dict) -> tuple[int, str]:
    return message.get('ts') or 0, clean_visible_text(message.get('content') or '')


def _aliases(row: dict) -> set[tuple[str, str]]:
    result = set()
    for key in ('userId', 'externalUserId'):
        value = str(row.get(key) or '')
        if value.strip().lower() not in ('', '0', 'null', 'none'):
            result.add((key, value))
    return result


def _customer(pair: dict, retained: list[dict], projected: list[dict], rows: dict[str, dict],
              metadata: dict, sid: int) -> str:
    cid = str(pair.get('customer_id') or '')
    if not cid or cid.startswith(('anonymous-', 'qiyu-session:')):
        return ''
    visitors = [m for m in retained if m.get('role') == 'user']
    if cid.startswith(_IDENTITY_PREFIXES):
        for message in visitors:
            row = rows.get(message.get('source_message_id'))
            if not row or row['from'] != 1 or row['status'] != 1 or row['mType'] not in (1, 2):
                continue
            owner, _, unknown = _sender(row, sid)
            if (not unknown and _aliases(row) and owner == cid
                    and (message.get('ts'), message.get('content')) == (row['time'], row['msg'])):
                return cid
        return ''

    # Legacy pairs have only local customer IDs. Allow migration between ID
    # namespaces only when all active visitor rows identify the same customer
    # and both the raw customer ID and original question can be confirmed.
    source_customer = ''
    for row in rows.values():
        if row['from'] != 1 or row['status'] != 1:
            continue
        owner, _, unknown = _sender(row, sid)
        if unknown or cid not in {value for _, value in _aliases(row)} or source_customer and source_customer != owner:
            return ''
        source_customer = owner
    if not source_customer:
        return ''
    question = clean_visible_text(pair.get('question') or '')
    asked_at = pair.get('asked_at')
    if not question or not source_integer(asked_at, positive=True):
        return ''
    retained_questions = Counter(_retained_key(message) for message in visitors)
    matches = []
    for candidate in extract_pairs(projected, metadata):
        if (candidate['customer_id'] != source_customer or candidate['asked_at'] != asked_at
                or clean_visible_text(candidate['question']) != question):
            continue
        question_ids = candidate.get('question_message_ids') or []
        needed = Counter((rows[mid]['time'], clean_visible_text(rows[mid]['msg'])) for mid in question_ids)
        if needed and not (needed - retained_questions):
            matches.append(candidate)
    return source_customer if len(matches) == 1 else ''


def _lookup(cfg: Settings, pair: dict, retained: list[dict], sid: int, channel: str, exporter) -> dict:
    metadata = exporter.export_one(sid)
    if (not isinstance(metadata, dict) or type(metadata.get('id')) is not int or metadata['id'] != sid
            or metadata.get('fromType') != channel or not source_integer(metadata.get('endTime'), positive=True)
            or _expired(metadata, cfg, time.time())):
        return _result('unavailable')
    official = exporter.export_messages(sid)
    if not isinstance(official, list) or len(official) > 50_000 or sum(len(row['msg']) for row in official) > 5_000_000:
        return _result('unavailable')
    # Reuse the audited direction/count/identity projection for this exact
    # session. Here the official metadata supplies the expected message counts.
    projected = _project(metadata, official, metadata, sid)
    rows = {f"source:{sid}:{row['id']}": row for row in official}
    for message in projected:
        if message['role'] == 'staff' and _automated(rows[message['source_message_id']]):
            message['role'] = 'bot'
    staff = {message['source_message_id']: _staff_content(rows[message['source_message_id']])
             for message in projected if message['role'] == 'staff'}
    staff = {mid: content for mid, content in staff.items() if content}
    if not staff:
        return _result('no_reply')
    customer = _customer(pair, retained, projected, rows, metadata, sid)
    if not customer:
        return _result('unmatched')
    recipients: dict[tuple[str, str], set[str]] = {}
    for row in official:
        if row['from'] == 1 and row['status'] == 1:
            owner, _, unknown = _sender(row, sid)
            if not unknown:
                for alias in _aliases(row):
                    recipients.setdefault(alias, set()).add(owner)
    existing = {message['source_message_id'] for message in retained if message.get('source_message_id')}
    legacy_staff = Counter(_retained_key(message) for message in retained
                           if message.get('role') == 'staff' and not message.get('source_message_id'))
    supplemental = []
    for message in sender_context(projected, str(sid), customer):
        mid = message.get('source_message_id')
        if mid not in staff or mid in existing:
            continue
        # Turn order may establish context when no recipient is encoded. A
        # source identity pointing to another known visitor overrides order.
        if any(alias in recipients and customer not in recipients[alias] for alias in _aliases(rows[mid])):
            continue
        key = _retained_key(message)
        if legacy_staff[key]:
            legacy_staff[key] -= 1
            continue
        supplemental.append({**message, 'content': staff[mid]})
    return _result('found', supplemental) if supplemental else _result('unmatched')


def lookup(cfg: Settings, session: dict, pair: dict, retained: list[dict], *, exporter=None) -> dict:
    """Look up supplemental staff messages for a server-selected case only."""
    global _NEXT_LOOKUP
    source = _local_session(cfg, session)
    if source is None:
        return _result('unavailable')
    sid, channel = source
    if str(pair.get('session_id')) != str(sid) or not retained:
        return _result('unmatched')
    if not _GATE.acquire(blocking=False):
        return _result('busy')
    try:
        now = time.monotonic()
        if now < _NEXT_LOOKUP:
            return _result('busy')
        _NEXT_LOOKUP = now + _INTERVAL
        api = exporter if exporter is not None else SessionExporter(
            replace(cfg.qiyu, timeout=max(1, min(cfg.qiyu.timeout, 5))), retries=1)
        return _lookup(cfg, pair, retained, sid, channel, api)
    except Exception:
        # Transport exceptions can contain signed URLs or source message text.
        return _result('unavailable')
    finally:
        _GATE.release()
