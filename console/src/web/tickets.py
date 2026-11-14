"""Safe ticket projections for the authenticated VOC dashboard and publication jobs."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

from ..clean.rules import mask_pii
from ..config import Settings
from ..qiyu.ticket import PAGE_SIZE, TicketClient, TicketError, _id
from . import ticket_media


_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_LINKED_IDS = re.compile(r"[0-9]+(?:\s*[,，]\s*[0-9]+)*")
_PRIVATE_LABELS = frozenset(("用户姓名", "访客姓名", "用户手机", "手机号码", "手机号", "邮箱",
                            "用户邮箱", "访客邮箱", "访客手机", "联系电话", "用户uid", "访客uid"))
_FILTER_SCAN_LIMIT = 2000
_CREATE_ACTIONS = frozenset((1, 2, 127, 128, 129, 130))
_SUBMITTER_CACHE = "/var/lib/qiyu-feishu-sync/ticket-submitters.json"
_SUBMITTER_CACHE_LIMIT = 8_000_000
TICKET_STATUSES = {1: "已提交", 5: "待申领", 10: "处理中", 20: "已完结"}
TICKET_PRIORITIES = {5: "一般", 8: "紧急", 10: "非常紧急"}


def _bounds(start: str, end: str) -> tuple[int, int]:
    today = datetime.now(_ZONE).date()
    try:
        if (start and not _DAY.fullmatch(start)) or (end and not _DAY.fullmatch(end)):
            raise ValueError
        last = date.fromisoformat(end) if end else today
        first = date.fromisoformat(start) if start else last - timedelta(days=29)
        next_day = last + timedelta(days=1)
    except (ValueError, OverflowError):
        raise ValueError("日期格式应为 YYYY-MM-DD") from None
    days = (next_day - first).days
    if not 1 <= days <= 90:
        raise ValueError("工单查询日期范围须在 1 至 90 天之间")
    return (int(datetime.combine(first, time.min, _ZONE).timestamp() * 1000),
            int(datetime.combine(next_day, time.min, _ZONE).timestamp() * 1000) - 1)


def _millis(value: Any) -> int | None:
    stamp = _id(value)
    return stamp if stamp is not None and stamp < 253402300800000 else None


class _RichText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.media: list[dict] = []
        self.hidden: str = ""

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden:
            return
        if tag in ("script", "style"):
            self.hidden = tag
        if tag == "br":
            self.parts.append("\n")
        if tag == "img":
            values = dict(attrs)
            self.media.append({"url": values.get("src"), "name": values.get("alt") or "工单截图", "inline_image": True})

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden:
                self.hidden = ""
            return
        if tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return mask_pii(value.strip())[:maximum]


def _attachments(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _field_value(value: Any, maximum: int, *, rich: bool = False) -> tuple[str, list[dict]]:
    if not isinstance(value, str):
        return "", []
    stripped = value.strip()
    if not rich and stripped.startswith(("[", "{")):
        try:
            structured = json.loads(stripped)
        except (ValueError, RecursionError):
            structured = None
        if isinstance(structured, dict) and isinstance(structured.get("path"), str):
            return _text(structured["path"], maximum), []
        candidates = _attachments(structured)
        if candidates and all("url" in item or "response" in item for item in candidates):
            return "", candidates
    if rich or re.search(r"<img\s", value, re.IGNORECASE):
        parser = _RichText()
        parser.feed(value)
        parser.close()
        return _text("".join(parser.parts), maximum), parser.media
    return _text(value, maximum), []


def _project_fields(value: Any, secret: str, *, history: bool = False) -> list[dict]:
    if not isinstance(value, list):
        return []
    fields = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get("title") or entry.get("titleLang") if history else entry.get("name"), 100)
        if label.casefold() in _PRIVATE_LABELS or (not history and not label):
            continue
        content, candidates = _field_value(entry.get("content" if history else "value"), 4000)
        images, attachments = ticket_media.links(candidates, secret)
        if history and not (content or images or attachments):
            continue
        field = {"label": label, "value": content}
        if images:
            field["images"] = images
        if attachments:
            field["attachments"] = attachments
        fields.append(field)
    return fields


def _linked_sessions(cfg: Settings, ticket_ids: set[int]) -> dict[int, list[dict]]:
    """Match documented comma-separated ticket IDs on the retained session rows."""
    links: dict[int, list[dict]] = defaultdict(list)
    path = Path(cfg.runtime.db_path).resolve()
    if not ticket_ids or not path.is_file():
        return links
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "raw_session" not in tables:
            return links
        rows = db.execute("""
            SELECT session_id, start_ts, worksheet_ids
            FROM raw_session
            WHERE worksheet_ids IS NOT NULL AND trim(worksheet_ids) <> ''
            ORDER BY start_ts DESC, session_id
        """)
        for session_id, started_at, value in rows:
            raw = str(value).strip()
            if not _LINKED_IDS.fullmatch(raw):
                continue
            matches = ticket_ids.intersection(_id(part.strip()) for part in re.split(r"[,，]", raw))
            if not matches:
                continue
            pair = None
            if "qa_pair" in tables:
                pair = db.execute("""
                    SELECT qa_id FROM qa_pair WHERE session_id = ? AND trim(question) <> ''
                    ORDER BY qa_id LIMIT 1
                """, (session_id,)).fetchone()
            item = {"session_id": mask_pii(str(session_id)), "started_at": _millis(started_at),
                    "qa_id": pair[0] if pair else None}
            for ticket_id in matches:
                links[ticket_id].append(item)
    return links


def submitter_names(history: list[dict], staff_id: int | None) -> set[str]:
    from ..voc_feishu_content import _public_text

    if staff_id is None:
        return set()
    names = {_public_text(event.get("operator"), None)[:120] for event in history
             if _id(event.get("actionType")) in _CREATE_ACTIONS and _id(event.get("operatorId")) == staff_id}
    names.discard("")
    return names


def _submitter_cache() -> dict:
    path = Path(os.environ.get("YUHAI_TICKET_SUBMITTERS_PATH", "").strip() or _SUBMITTER_CACHE)
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        with path.open("rb") as handle:
            data = handle.read(_SUBMITTER_CACHE_LIMIT + 1)
        cache = json.loads(data) if len(data) <= _SUBMITTER_CACHE_LIMIT else None
    except (OSError, ValueError, RecursionError):
        return {}
    return cache if isinstance(cache, dict) else {}


def _cached_submitter(cache: dict, ticket_id: int, staff_id: int | None) -> str:
    from ..voc_feishu_content import _public_text

    entry = cache.get(str(ticket_id))
    if staff_id is None or not isinstance(entry, dict) or _id(entry.get("staff_id")) != staff_id:
        return ""
    return _public_text(entry.get("name"), None)[:120]


def _public(row: dict, linked_session_count: int, submitter: str) -> dict:
    status = _id(row.get("status"))
    priority = _id(row.get("priority"))
    return {
        "id": str(_id(row.get("id"))),
        "title": _text(row.get("title"), 300),
        "status": TICKET_STATUSES.get(status, f"状态 {status}" if status else ""),
        "status_code": status,
        "priority": TICKET_PRIORITIES.get(priority, f"优先级 {priority}" if priority else ""),
        "priority_code": priority,
        "owner": _text(row.get("holderName") or row.get("staffName"), 120),
        "submitter": submitter,
        "created_at": _millis(row.get("createTime")),
        "updated_at": _millis(row.get("modifyTime")),
        "linked_session_count": linked_session_count,
    }


def list_tickets(cfg: Settings, *, start: str = "", end: str = "", q: str = "",
                 offset: int = 0, limit: int = 20, status: str = "",
                 updated_start: str = "", updated_end: str = "", include_submitter_id: bool = False) -> dict:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
        raise ValueError("工单分页参数无效")
    if len(q) > 120:
        raise ValueError("工单搜索词须在 120 字以内")
    status_code = _id(status)
    if status and status_code not in TICKET_STATUSES:
        raise ValueError("工单状态无效")
    q = q.strip().casefold()
    ticket_id = _id(q)
    bounds = {}
    if start or end or not (ticket_id or updated_start or updated_end):
        bounds["start"], bounds["end"] = _bounds(start, end)
    if updated_start or updated_end:
        bounds["opStart"], bounds["opEnd"] = _bounds(updated_start, updated_end)
    if ticket_id:
        bounds["ticketId"] = ticket_id

    client = TicketClient(cfg.qiyu)
    if status_code or (q and not ticket_id):
        # The upstream contract only supports ID and visitor filters. Title/status
        # matching therefore goes over the dated result set before pagination.
        consumed = 0
        matching = []
        seen = set()
        while True:
            upstream_total, upstream = client.search(offset=consumed, limit=PAGE_SIZE, bounds=bounds)
            if upstream_total > _FILTER_SCAN_LIMIT:
                raise ValueError("工单过多，请缩短日期范围或按工单编号查询")
            if not upstream:
                break
            identifiers = {_id(row["id"]) for row in upstream}
            if len(identifiers) != len(upstream) or seen & identifiers:
                raise TicketError("七鱼工单列表在查询期间有变动，请刷新重试")
            seen.update(identifiers)
            matching.extend(row for row in upstream if (not status_code or _id(row.get("status")) == status_code)
                            and (not q or ticket_id or q in _text(row.get("title"), 300).casefold()))
            consumed += len(upstream)
            if consumed >= upstream_total:
                break
        total, rows = len(matching), matching[offset:offset + limit]
    else:
        total, rows = client.search(offset=offset, limit=limit, bounds=bounds)
    links = _linked_sessions(cfg, {_id(row["id"]) for row in rows})
    cache = _submitter_cache() if rows and not include_submitter_id else {}
    items = []
    for row in rows:
        identifier, staff_id = _id(row["id"]), _id(row.get("staffId"))
        item = _public(row, len(links[identifier]), _cached_submitter(cache, identifier, staff_id))
        if include_submitter_id:
            item["submitter_id"] = staff_id
        items.append(item)
    return {"items": items,
            "total": total, "has_more": offset + len(rows) < total, "offset": offset, "limit": limit,
            "source": "qiyu"}


def ticket_detail(cfg: Settings, ticket_id: str) -> dict:
    identifier = _id(ticket_id)
    if identifier is None:
        raise ValueError("工单编号无效")
    client = TicketClient(cfg.qiyu)
    row = client.detail(identifier)
    try:
        logs = client.history(identifier)
        history_available = True
    except TicketError:
        logs = []
        history_available = False
    sessions = _linked_sessions(cfg, {identifier})[identifier]
    category = row.get("questionType")
    secret = cfg.qiyu.app_secret
    description, embedded = _field_value(row.get("content"), 12000, rich=row.get("isRichText") in (True, 1, "1"))
    images, attachments = ticket_media.links([*embedded, *_attachments(row.get("attachments"))], secret)
    staff_id = _id(row.get("staffId"))
    names = submitter_names(logs, staff_id)
    submitter = next(iter(names)) if len(names) == 1 else ""
    if not names:
        submitter = _cached_submitter(_submitter_cache(), identifier, staff_id)
    result = _public(row, len(sessions), submitter)
    result.update(
        description=description,
        category=_text(category.get("path") or category.get("name"), 300) if isinstance(category, dict) else "",
        template=_text(row.get("templateName"), 200),
        fields=_project_fields(row.get("custom"), secret),
        images=images, attachments=attachments,
    )
    history = []
    for index, entry in enumerate(logs):
        event_images, event_attachments = ticket_media.links(_attachments(entry.get("attachments")), secret)
        history.append({
            "id": str(_id(entry.get("id")) or f"log-{index + 1}"),
            "action": _text(entry.get("action") or entry.get("actionLang"), 200) or "工单变更",
            "operator": _text(entry.get("operator"), 120),
            "time": _millis(entry.get("time")),
            "details": _project_fields(entry.get("info"), secret, history=True),
            "images": event_images,
            "attachments": event_attachments,
        })
    history.sort(key=lambda entry: entry["time"] or 0, reverse=True)
    timestamps = [result["updated_at"], _millis(row.get("lastFinishTime")), *(entry["time"] for entry in history)]
    result["updated_at"] = max((stamp for stamp in timestamps if stamp is not None), default=None)
    return {"ticket": result, "history": history, "history_available": history_available, "sessions": sessions}
