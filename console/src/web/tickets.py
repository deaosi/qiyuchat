"""Safe ticket projections for the authenticated VOC dashboard and publication jobs."""

from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import sqlite3
from typing import Any

from ..clean.rules import mask_pii
from ..config import Settings
from ..qiyu.ticket import PAGE_SIZE, TicketClient, TicketError, _id


_ZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_LINKED_IDS = re.compile(r"[0-9]+(?:\s*[,，]\s*[0-9]+)*")
_PRIVATE_LABELS = frozenset(("用户姓名", "访客姓名", "用户手机", "手机号码", "手机号", "邮箱",
                            "用户邮箱", "访客邮箱", "访客手机", "联系电话", "用户uid", "访客uid"))
_FILTER_SCAN_LIMIT = 2000
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

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")


def _text(value: Any, maximum: int, *, rich: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    if rich:
        parser = _RichText()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    return mask_pii(value.strip())[:maximum]


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


def _public(row: dict, linked_session_count: int) -> dict:
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
        "created_at": _millis(row.get("createTime")),
        "updated_at": _millis(row.get("modifyTime")),
        "linked_session_count": linked_session_count,
    }


def list_tickets(cfg: Settings, *, start: str = "", end: str = "", q: str = "",
                 offset: int = 0, limit: int = 20, status: str = "",
                 updated_start: str = "", updated_end: str = "") -> dict:
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
    return {"items": [_public(row, len(links[_id(row["id"])])) for row in rows],
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
    result = _public(row, len(sessions))
    result.update(
        description=_text(row.get("content"), 12000, rich=row.get("isRichText") in (True, 1, "1")),
        category=_text(category.get("path") or category.get("name"), 300) if isinstance(category, dict) else "",
        template=_text(row.get("templateName"), 200),
    )
    history = []
    for index, entry in enumerate(logs):
        details = []
        info = entry.get("info")
        if isinstance(info, list):
            for field in info:
                if not isinstance(field, dict):
                    continue
                label = _text(field.get("title") or field.get("titleLang"), 100)
                value = _text(field.get("content"), 4000)
                if label.casefold() not in _PRIVATE_LABELS and value:
                    details.append({"label": label, "value": value})
        history.append({
            "id": str(_id(entry.get("id")) or f"log-{index + 1}"),
            "action": _text(entry.get("action") or entry.get("actionLang"), 200) or "工单变更",
            "operator": _text(entry.get("operator"), 120),
            "time": _millis(entry.get("time")),
            "details": details,
        })
    history.sort(key=lambda entry: entry["time"] or 0, reverse=True)
    timestamps = [result["updated_at"], _millis(row.get("lastFinishTime")), *(entry["time"] for entry in history)]
    result["updated_at"] = max((stamp for stamp in timestamps if stamp is not None), default=None)
    return {"ticket": result, "history": history, "history_available": history_available, "sessions": sessions}
