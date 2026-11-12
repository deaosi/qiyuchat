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
from ..extract.text import clean_visible_text
from ..local import csv_bytes
from . import case_media, case_replies, case_senders

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


def _cluster_buckets(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        cid = int(r.get("cluster_id") or 0)
        if cid:
            buckets[cid].append(r)
    return buckets


def _cluster_item(cid: int, members: list[dict[str, Any]], scope_total: int,
                  *, compact: bool) -> dict[str, Any]:
    counter = Counter(r["question"] for r in members)
    variants = sorted(counter, key=lambda text: (-counter[text], len(text), text))
    stamps = [int(r.get("asked_at") or 0) for r in members if r.get("asked_at")]
    # Keep the list payload bounded even when a question includes a long transcript.
    rep = variants[0][:300] if compact else variants[0]
    samples = [variant[:200] for variant in variants[:5]] if compact else variants[:6]
    return {
        "cluster_id": cid, "question": rep, "rep": rep,
        "size": len(members), "count": len(members), "variants": len(counter),
        "customers": len({r["customer_id"] for r in members}),
        "groups": len({r["group_name"] for r in members}),
        "unanswered": sum(not r["replied"] for r in members),
        "samples": samples,
        "share": round(len(members) / scope_total * 100, 1) if scope_total else 0,
        "first_seen": _day(min(stamps)) if stamps else "",
        "last_seen": _day(max(stamps)) if stamps else "",
    }


def _cluster_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [_cluster_item(cid, members, len(rows), compact=False)
           for cid, members in _cluster_buckets(rows).items()]
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


def _cluster_match_excerpt(question: str, query: str) -> str:
    # Locate the match in original text even when casefold expands characters.
    folded_offset = question.casefold().index(query)
    match_at = 0
    for character in question:
        width = len(character.casefold())
        if folded_offset < width:
            break
        folded_offset -= width
        match_at += 1
    start = max(0, match_at - min(32, max(0, 200 - len(query))))
    end = start + 200
    return ("…" if start else "") + question[start:end] + ("…" if end < len(question) else "")


def cluster_directory(cfg: Settings, *, limit: int = 20, offset: int = 0, entity_q: str = "",
                      entity_sort: str = "questions", cluster_members: str = "all",
                      **filters: Any) -> dict[str, Any]:
    sort_field = {"questions": "count", "unanswered": "unanswered", "recent": "last_seen"}.get(entity_sort)
    if sort_field is None:
        raise ValueError("未知列表排序")
    if cluster_members not in ("all", "repeated", "single"):
        raise ValueError("未知问题簇类型")

    query = entity_q.strip().casefold()
    cluster_id_query = None
    for prefix in ("#", "问题簇", "簇"):
        if query.startswith(prefix):
            number = query.removeprefix(prefix).strip()
            if number.isascii() and number.isdecimal():
                cluster_id_query = number.lstrip("0")
            break

    rows = _rows(cfg, **filters)
    buckets = _cluster_buckets(rows)
    matches = []
    for cid, members in buckets.items():
        count = len(members)
        if (cluster_members == "repeated" and count == 1
                or cluster_members == "single" and count != 1):
            continue
        matched_question = None
        if cluster_id_query is not None:
            if str(cid) != cluster_id_query:
                continue
        elif query:
            matched_question = next((row["question"] for row in members
                                     if query in row["question"].casefold()), None)
            if matched_question is None:
                continue
        matches.append({
            "cluster_id": cid,
            "count": count,
            "unanswered": sum(not row["replied"] for row in members),
            "last_seen": max(int(row.get("asked_at") or 0) for row in members),
            "matched_question": matched_question,
        })
    matches.sort(key=lambda item: (-item[sort_field], -item["count"], item["cluster_id"]))
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    items = []
    for match in matches[offset:offset + limit]:
        cid = match["cluster_id"]
        item = _cluster_item(cid, buckets[cid], len(rows), compact=True)
        question = match["matched_question"]
        if question and not any(query in sample.casefold() for sample in [item["question"], *item["samples"]]):
            item["matched_sample"] = _cluster_match_excerpt(question, query)
        items.append(item)
    return {"items": items, "total": len(matches), "offset": offset, "limit": limit,
            "totals": {"questions": sum(item["count"] for item in matches),
                       "unanswered": sum(item["unanswered"] for item in matches)}}


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


def _matches_customer(members: list[dict[str, Any]], customer_id: str, query: str) -> bool:
    if query in customer_id.casefold():
        return True
    if any(query in row[field].casefold()
           for row in members for field in ("customer_name", "group_name")):
        return True
    alias = query.removeprefix("匿名客户 ")
    if len(alias) == 5 and alias.isascii() and alias.isdecimal():
        # Keep the alias searchable using the same hash as the UI customer label.
        value = 2166136261
        for char in customer_id:
            value = ((value ^ ord(char)) * 16777619) & 0xffffffff
        return alias == f"{value % 100000:05d}"
    return False


def directory(cfg: Settings, entity: str, *, limit: int = 20, offset: int = 0,
              entity_q: str = "", entity_sort: str = "questions", top: int = 10,
              **filters: Any) -> dict[str, Any]:
    sort_field = {"questions": "total_questions", "unanswered": "unanswered",
                  "recent": "last_seen"}.get(entity_sort)
    if sort_field is None:
        raise ValueError("未知列表排序")

    is_group = entity == "groups"
    key = "group_name" if is_group else "customer_id"
    query = entity_q.strip().casefold()
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _rows(cfg, **filters):
        buckets[row[key]].append(row)

    matches = []
    for value, members in buckets.items():
        if query and not (query in value.casefold() if is_group
                          else _matches_customer(members, value, query)):
            continue
        matches.append({
            key: value,
            "total_questions": len(members),
            "unanswered": sum(not row["replied"] for row in members),
            "last_seen": max(int(row.get("asked_at") or 0) for row in members),
        })
    matches.sort(key=lambda item: (-item[sort_field], -item["total_questions"], item[key]))
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    items = matches[offset:offset + limit]
    page_rows = [row for item in items for row in buckets[item[key]]]
    details = E.by_group(page_rows, top_n=top) if is_group else E.by_customer(page_rows, top_n=3)
    by_key = {item[key]: item for item in details}
    for item in items:
        item.update(by_key[item[key]])
        item["last_seen"] = _day(item["last_seen"])
        if not is_group:
            item["first_seen"] = _day(item["first_seen"])
            groups = Counter(row["group_name"] for row in buckets[item[key]]
                             if row["group_name"] != "未分组")
            item["group_count"] = len(groups)
            item["group_names"] = sorted(groups, key=lambda name: (-groups[name], name))[:3]
    return {"items": items, "total": len(matches), "offset": offset, "limit": limit,
            "totals": {"questions": sum(item["total_questions"] for item in matches),
                       "unanswered": sum(item["unanswered"] for item in matches)}}


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


def _detail_content(value: Any) -> str:
    content, _ = case_media.project(value)
    return mask_pii(content)


def _detail_origin(db: Any, raw: dict[str, Any], tables: set[str]) -> tuple[set[str], set[str], bool]:
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


def _case_messages(db: Any, sid: str) -> list[dict[str, Any]]:
    return [dict(row) for row in db._conn.execute(
        "SELECT seq AS context_seq,role,content,ts,source_message_id FROM raw_message "
        "WHERE session_id=? ORDER BY seq", (sid,),
    )]


def case_context_revision(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _case_reply_session(db: Any, cfg: Settings, pair: dict) -> dict[str, Any] | None:
    session_id = pair["session_id"]
    row = db._conn.execute("SELECT * FROM raw_session WHERE session_id=?", (session_id,)).fetchone()
    session = dict(row) if row else None
    if session is None or not case_replies.eligible(cfg, session):
        return None
    if db._conn.execute("SELECT 1 FROM qa_origin WHERE qa_id=? AND active=0", (pair["qa_id"],)).fetchone():
        return None
    has_sync_state = db._conn.execute("SELECT 1 FROM sqlite_master WHERE name='push_sync_state'").fetchone()
    if has_sync_state and db._conn.execute(
        "SELECT 1 FROM push_sync_state WHERE session_id=? AND (status='archived' OR archive_reason='retention')",
        (session_id,),
    ).fetchone():
        return None
    return session


def case_reply_target(cfg: Settings, qa_id: int) -> tuple[dict, dict, list[dict]] | None:
    """Capture the locally authorized case, independently of the network call."""
    db = get_db(cfg)
    pair = db.get_pair(qa_id)
    if not pair or not _public(pair)["question"].strip():
        return None
    session = _case_reply_session(db, cfg, pair)
    if session is None:
        return None
    visible = sender_context(_case_messages(db, pair["session_id"]), pair["session_id"], str(pair.get("customer_id") or ""))
    if not any(m["role"] == "user" and _detail_content(m["content"]) for m in visible):
        return None
    if any(m["role"] == "staff" and _detail_content(m["content"]) for m in visible):
        return None
    return session, pair, visible


def case_reply_response(result: dict[str, Any]) -> dict[str, Any]:
    messages = []
    for message in result["messages"]:
        content = _detail_content(message["content"])
        if content:
            stamp, display_time = _detail_time(message["ts"])
            public = {"role": "staff", "content": content, "time": display_time, "ts": stamp,
                      "source_message_id": message["source_message_id"], "is_question": False, "is_answer": False}
            images = case_media.safe_images(message.get("images"))
            if images:
                public["images"] = images
            if name := case_senders.public_name(message.get("sender_name")):
                public["sender_name"] = name
            messages.append(public)
    status = "unmatched" if result["status"] == "found" and not messages else result["status"]
    response = {"status": status, "messages": messages}
    if result.get("senders"):
        response["senders"] = result["senders"]
    if result.get("context_revision"):
        response["context_revision"] = result["context_revision"]
    return response


def detail(cfg: Settings, qa_id: int) -> dict[str, Any] | None:
    db = get_db(cfg)
    raw = db.get_pair(qa_id)
    if not raw:
        return None
    pair = _public(raw)
    if not pair["question"].strip():
        return None
    # Use the original session identity. The public pair's ID may be masked.
    sid = raw["session_id"]
    messages = _case_messages(db, sid)
    scoped = sender_context(messages, sid, str(raw.get("customer_id") or ""))
    tables = {row[0] for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    question_ids, answer_ids, partial = _detail_origin(db, raw, tables)
    labels = case_senders.saved_labels(db._conn, cfg.runtime.state_dir, sid, scoped, tables)
    visible_messages = []
    for message in scoped:
        if message['role'] in HIDDEN_ROLES:
            continue
        content, images = case_media.project(message['content'])
        if content:
            stamp, display_time = _detail_time(message.get('ts'))
            source_id = str(message.get('source_message_id') or '')
            public = {'role': message['role'], 'content': mask_pii(content), 'time': display_time,
                      'ts': stamp, 'source_message_id': source_id, 'context_seq': message['context_seq'],
                      'is_question': message['role'] == 'user' and source_id in question_ids,
                      'is_answer': message['role'] == 'staff' and source_id in answer_ids}
            if images:
                public['images'] = images
            public.update(labels.get(message['context_seq'], {}))
            visible_messages.append(public)
    archived = not bool(messages)
    availability = 'archived' if archived else 'empty' if not visible_messages else 'partial' if partial else 'available'
    roles = {message["role"] for message in visible_messages}
    can_lookup = ("user" in roles and "staff" not in roles and
                  _case_reply_session(db, cfg, raw) is not None)
    return {'pair': pair, 'messages': visible_messages, 'archived': archived,
            'message_count': len(visible_messages), 'time_zone': 'Asia/Shanghai', 'availability': availability,
            'can_lookup_replies': can_lookup, 'context_revision': case_context_revision(scoped),
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
