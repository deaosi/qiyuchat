"""看板查询。所有视图共用筛选口径，返回内容默认脱敏。"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import extract as E
from ..app import get_db
from ..clean.rules import mask_pii
from ..config import Settings, settings
from ..extract.concepts import concept_signature
from ..extract.identity import HIDDEN_ROLES, SOURCE_IDENTITY_ROLE, sender_context
from ..extract.text import _attachment, _structured, clean_visible_text
from ..local import csv_bytes

try:
    _DETAIL_ZONE = ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError:
    _DETAIL_ZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')


def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000)) if ms else ""


def _time(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "时间未知"


def _bounds(days: int = 0, start: str = "", end: str = "") -> tuple[int, int]:
    if days < 0 or days > 3660:
        raise ValueError("日期范围须在 0–3660 天之间")
    try:
        first = date.fromisoformat(start) if start else None
        last = date.fromisoformat(end) if end else None
    except ValueError:
        raise ValueError("日期格式应为 YYYY-MM-DD") from None
    if first and last and first > last:
        raise ValueError("开始日期不能晚于结束日期")
    if not first and not last and days:
        first = date.today() - timedelta(days=days - 1)
        last = date.today()
    low = int(datetime.combine(first, datetime.min.time()).timestamp() * 1000) if first else 0
    high = int(datetime.combine(last + timedelta(days=1), datetime.min.time()).timestamp() * 1000) if last else 0
    return low, high


def _public(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    sid = str(item.get("session_id") or "")
    cid = str(item.get("customer_id") or "") or "anonymous-" + hashlib.sha256(sid.encode()).hexdigest()[:16]
    if mask_pii(cid) != cid:
        cid = "customer-" + hashlib.sha256(cid.encode()).hexdigest()[:16]
    item["customer_id"] = cid
    question = clean_visible_text(str(item.get("question") or ""))
    item["question"] = question
    item["answer"] = clean_visible_text(str(item.get("answer") or ""))
    item["q_norm"] = E.normalize(question)
    for key in ("question", "answer", "q_norm", "customer_name", "group_name", "staff_name", "session_id"):
        item[key] = mask_pii(str(item.get(key) or ""))
    item["group_name"] = item["group_name"] or "未分组"
    item["staff_name"] = item["staff_name"] or "未记录客服"
    item["customer_name"] = item["customer_name"] or "匿名客户"
    item["asked_at_text"] = _time(int(item.get("asked_at") or 0))
    item["replied"] = bool(item.get("answer", "").strip())
    item["concepts"] = sorted(concept_signature(item.get("question") or ""))
    return item


def _rows(cfg: Settings, days: int = 0, group: str = "", staff: str = "",
          start: str = "", end: str = "", q: str = "", replied: str = "",
          cluster: int = 0, customer: str = "") -> list[dict[str, Any]]:
    low, high = _bounds(days, start, end)
    rows = []
    for raw in get_db(cfg).iter_qa_pairs():
        stamp = int(raw["asked_at"] or 0)
        if (low or high) and (not stamp or (low and stamp < low) or (high and stamp >= high)):
            continue
        r = _public(dict(raw))
        if not r["question"].strip():
            continue
        if group and r["group_name"] != group:
            continue
        if staff and r["staff_name"] != staff:
            continue
        if customer and r["customer_id"] != customer:
            continue
        if cluster and int(r.get("cluster_id") or 0) != cluster:
            continue
        if replied == "unanswered" and r["replied"]:
            continue
        if replied == "answered" and not r["replied"]:
            continue
        if q and q.casefold() not in " ".join(str(r.get(k) or "") for k in
                                               ("question", "answer", "customer_name", "session_id")).casefold():
            continue
        rows.append(r)
    return rows


def _cluster_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        cid = int(r.get("cluster_id") or 0)
        if cid:
            buckets[cid].append(r)
    out = []
    for cid, members in buckets.items():
        counter = Counter(r["question"] for r in members)
        variants = sorted(counter, key=lambda text: (-counter[text], len(text), text))
        stamps = [int(r.get("asked_at") or 0) for r in members if r.get("asked_at")]
        out.append({
            "cluster_id": cid, "question": variants[0], "rep": variants[0],
            "size": len(members), "count": len(members), "variants": len(counter),
            "customers": len({r["customer_id"] for r in members}),
            "groups": len({r["group_name"] for r in members}),
            "unanswered": sum(not r["replied"] for r in members),
            "samples": variants[:6],
            "share": round(len(members) / len(rows) * 100, 1) if rows else 0,
            "first_seen": _day(min(stamps)) if stamps else "",
            "last_seen": _day(max(stamps)) if stamps else "",
        })
    return sorted(out, key=lambda r: (-r["count"], r["question"]))


def build_summary(cfg: Settings | None = None, **filters: Any) -> dict[str, Any]:
    cfg = cfg or settings()
    rows = _rows(cfg, **filters)
    ov = E.overall(rows)
    return {
        "qa_pair": len(rows), "customer_count": ov["customer_count"],
        "group_count": ov["group_count"], "cluster_count": ov["cluster_count"],
        "unanswered": sum(not r["replied"] for r in rows),
        "unknown_time": sum(not r.get("asked_at") for r in rows),
        "first_seen": _day(ov["first_seen"]), "last_seen": _day(ov["last_seen"]),
        "top": _cluster_items(rows)[:6],
        "recent": sorted(rows, key=lambda r: (-int(r.get("asked_at") or 0), -r["qa_id"]))[:5],
        "local": get_db(cfg).local_status(),
        **{k: v for k, v in get_db(cfg).stats().items() if k in ("raw_session", "raw_message", "clean_result")},
    }


def trend(cfg: Settings | None = None, days: int = 30, **filters: Any) -> dict[str, Any]:
    cfg = cfg or settings()
    rows = _rows(cfg, days=days, **filters)
    low, high = _bounds(days, filters.get("start", ""), filters.get("end", ""))
    valid_days = [_day(int(r["asked_at"])) for r in rows if r.get("asked_at")]
    first = datetime.fromtimestamp(low / 1000).date() if low else (date.fromisoformat(min(valid_days)) if valid_days else date.today() - timedelta(days=29))
    last = datetime.fromtimestamp(high / 1000).date() - timedelta(days=1) if high else max(date.today(), first, date.fromisoformat(max(valid_days)) if valid_days else date.today())
    truncated = (last - first).days >= 366
    if truncated:
        first = last - timedelta(days=365)
    daily = Counter(_day(int(r["asked_at"])) for r in rows if r.get("asked_at"))
    series = []
    cursor = first
    while cursor <= last:
        key = cursor.isoformat()
        series.append({"date": key, "questions": daily.get(key, 0)})
        cursor += timedelta(days=1)
    return {"days": len(series), "series": series, "truncated": truncated,
            "unknown_time": sum(not r.get("asked_at") for r in rows)}


def questions(cfg: Settings | None = None, top: int = 20, **filters: Any) -> dict[str, Any]:
    rows = _rows(cfg or settings(), **filters)
    return {"total": len(rows), "items": _cluster_items(rows)[:max(1, min(top, 100))]}


def clusters(cfg: Settings | None = None, **filters: Any) -> dict[str, Any]:
    rows = _rows(cfg or settings(), **filters)
    return {"items": _cluster_items(rows)}


def groups(cfg: Settings | None = None, top: int = 10, **filters: Any) -> dict[str, Any]:
    rows = _rows(cfg or settings(), **filters)
    items = E.by_group(rows, top_n=top)
    unanswered = Counter(r["group_name"] for r in rows if not r["replied"])
    for item in items:
        item["unanswered"] = unanswered[item["group_name"]]
    return {"items": items}


def customers(cfg: Settings | None = None, top: int = 50, **filters: Any) -> dict[str, Any]:
    rows = _rows(cfg or settings(), **filters)
    items = E.by_customer(rows, top_n=3)[:max(1, min(top, 500))]
    unanswered = Counter(r["customer_id"] for r in rows if not r["replied"])
    for item in items:
        item["first_seen"] = _day(item["first_seen"])
        item["last_seen"] = _day(item["last_seen"])
        item["unanswered"] = unanswered[item["customer_id"]]
    return {"items": items}


def pairs(cfg: Settings | None = None, limit: int = 25, offset: int = 0,
          **filters: Any) -> dict[str, Any]:
    rows = sorted(_rows(cfg or settings(), **filters),
                  key=lambda r: (-int(r.get("asked_at") or 0), -r["qa_id"]))
    offset, limit = max(0, offset), max(1, min(500, limit))
    return {"total": len(rows), "offset": offset, "limit": limit,
            "items": rows[offset:offset + limit]}


def _detail_time(value: Any) -> tuple[int, str]:
    try:
        stamp = int(value or 0)
        if stamp <= 0:
            return 0, '时间未知'
        return stamp, datetime.fromtimestamp(stamp / 1000, _DETAIL_ZONE).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError, OverflowError, OSError):
        return 0, '时间未知'


def _media_kinds(value: Any, depth: int = 0) -> list[str]:
    if depth > 8:
        return []
    if isinstance(value, str):
        try:
            return _media_kinds(json.loads(value), depth + 1)
        except (ValueError, RecursionError):
            return []
    if isinstance(value, list):
        return [kind for item in value for kind in _media_kinds(item, depth + 1)]
    if not isinstance(value, dict):
        return []
    keys = {str(key).lower(): item for key, item in value.items()}
    if _attachment(value):
        kind = str(keys.get('type') or keys.get('msgtype') or '').lower()
        mime = str(keys.get('mimetype') or keys.get('mime') or keys.get('contenttype') or '').lower()
        image = kind in ('image', 'picture', 'photo') or mime.startswith('image/')
        if not kind and not mime:
            suffixes = [str(keys.get(name) or '').lower().split('?', 1)[0].rsplit('.', 1)[-1]
                        for name in ('ext', 'url', 'downloadurl', 'name', 'filename')]
            image = any(ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'heic', 'avif') for ext in suffixes)
        return ['image' if image else 'attachment']
    return [kind for key in ('attach', 'attachment', 'attachments')
            for kind in _media_kinds(keys.get(key), depth + 1)]


def _detail_content(value: Any) -> str:
    raw = str(value or '')
    content = clean_visible_text(raw)
    if content:
        return mask_pii(content)
    # Only a recognized, text-empty media object gets a placeholder. Ordinary
    # customer JSON remains ordinary text; transport/identity rows stay hidden.
    try:
        payload = json.loads(raw)
        if isinstance(payload, str):
            payload = json.loads(payload)
        recognized, _text = _structured(payload)
        kinds = _media_kinds(payload) if recognized else []
        if kinds:
            return '[图片]' if all(kind == 'image' for kind in kinds) else '[附件]'
    except (ValueError, TypeError, RecursionError):
        pass
    return ''


def _detail_origin(db: Any, raw: dict[str, Any]) -> tuple[set[str], set[str], bool]:
    tables = {row[0] for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    question_ids, answer_ids = set(), set()
    if 'qa_origin' in tables:
        origin = db._conn.execute('SELECT * FROM qa_origin WHERE qa_id=? AND active=1', (raw['qa_id'],)).fetchone()
        if origin is not None and origin['session_id'] == raw['session_id']:
            try:
                question, answer = json.loads(origin['question_message_ids']), json.loads(origin['answer_message_ids'])
                if all(isinstance(items, list) and all(isinstance(item, str) and item for item in items) for items in (question, answer)):
                    question_ids, answer_ids = set(question), set(answer)
            except (ValueError, TypeError):
                pass
    partial = False
    if 'push_sync_state' in tables:
        state = db._conn.execute('SELECT status,archive_reason FROM push_sync_state WHERE session_id=?', (raw['session_id'],)).fetchone()
        partial = bool(state and (state['archive_reason'] or state['status'] != 'completed'))
    return question_ids, answer_ids, partial


def detail(cfg: Settings, qa_id: int) -> dict[str, Any] | None:
    db = get_db(cfg)
    raw = db.get_pair(qa_id)
    if not raw:
        return None
    pair = _public(raw)
    if not pair["question"].strip():
        return None
    # Use the original session identity. The public pair's ID may be masked.
    messages = db.get_messages(raw["session_id"])
    question_ids, answer_ids, partial = _detail_origin(db, raw)
    visible_messages = []
    for message in sender_context(messages, raw["session_id"], str(raw.get("customer_id") or "")):
        if message['role'] in HIDDEN_ROLES:
            continue
        content = _detail_content(message['content'])
        if content:
            stamp, display_time = _detail_time(message.get('ts'))
            source_id = str(message.get('source_message_id') or '')
            visible_messages.append({'role': message['role'], 'content': content, 'time': display_time,
                                     'ts': stamp, 'source_message_id': source_id,
                                     'is_question': message['role'] == 'user' and source_id in question_ids,
                                     'is_answer': message['role'] == 'staff' and source_id in answer_ids})
    archived = not bool(messages)
    availability = 'archived' if archived else 'empty' if not visible_messages else 'partial' if partial else 'available'
    return {'pair': pair, 'messages': visible_messages, 'archived': archived,
            'message_count': len(visible_messages), 'time_zone': 'Asia/Shanghai', 'availability': availability,
            'context_scope': 'session_customer' if any(m.get('role') == SOURCE_IDENTITY_ROLE for m in messages) else 'legacy_session'}


def meta(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or settings()
    rows = _rows(cfg)
    return {"groups": sorted({r["group_name"] for r in rows}),
            "staff": sorted({r["staff_name"] for r in rows}),
            "mock": cfg.runtime.mock, "db_name": get_db(cfg).path.replace("\\", "/").rsplit("/", 1)[-1],
            "total": len(rows), "unknown_time": sum(not r.get("asked_at") for r in rows),
            "qiyu": "reserved"}


def status(cfg: Settings) -> dict[str, Any]:
    tasks = get_db(cfg).recent_local_tasks()
    for task in tasks:
        task["created_at_text"] = _time(task["created_at"])
        if "filename" in task["result"]:
            task["result"]["filename"] = mask_pii(task["result"]["filename"])
    return {**get_db(cfg).local_status(), "tasks": tasks,
            "total_pairs": len(_rows(cfg)), "stored_pairs": get_db(cfg).stats()["qa_pair"]}


def export_pairs(cfg: Settings, **filters: Any) -> bytes:
    rows = sorted(_rows(cfg, **filters), key=lambda r: (-int(r.get("asked_at") or 0), -r["qa_id"]))
    return csv_bytes(
        ["问答ID", "会话ID", "客户ID", "客户名称", "客户群", "客服", "提问时间", "问题原话", "归一化问法", "客服回复", "回复状态", "问题簇"],
        [[r["qa_id"], r["session_id"], r["customer_id"], r["customer_name"], r["group_name"],
          r["staff_name"], r["asked_at_text"], r["question"], r["q_norm"], r["answer"],
          "有回复" if r["replied"] else "未回复", r["cluster_id"]] for r in rows],
    )
