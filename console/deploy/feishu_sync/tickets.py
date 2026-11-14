"""Sync read-only Qiyu ticket metadata to its own Feishu Base table."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import re

from deploy.feishu_sync import support, top
from src import voc_feishu_sync as sync
from src.config import settings
from src.qiyu.ticket import PAGE_SIZE, TicketError
from src.voc_feishu_content import _private_names, _public_text
from src.voc_service import maintenance
from src.web.guest_evidence import PUBLIC_ORIGIN
from src.web.tickets import TICKET_PRIORITIES, TICKET_STATUSES, list_tickets


TABLE_NAME = "七鱼工单"
CREATED_DAYS = 30
UPDATED_DAYS = 7
MATCH_SIZE = 20
_ZONE = timezone(timedelta(hours=8))
_ID = re.compile(r"[1-9][0-9]{0,18}")
_FIELDS = {"key": "工单ID", "title": "工单标题", "status": "状态", "priority": "优先级", "created": "创建时间",
           "updated": "更新时间", "sessions": "关联会话数", "source": "语海详情"}
_TYPES = {"key": 1, "title": 1, "status": 1, "priority": 1, "created": 5, "updated": 5, "sessions": 2, "source": 15}


def _table(report: sync.Target) -> sync.Target:
    table_id = top._find_table(report, TABLE_NAME)
    if table_id is None:
        definitions = [
            {"name": _FIELDS[role], "type": "text"} for role in ("key", "title", "status", "priority")
        ]
        definitions.extend({"name": _FIELDS[role], "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}}
                           for role in ("created", "updated"))
        definitions.extend((
            {"name": _FIELDS["sessions"], "type": "number", "style": {"type": "plain", "precision": 0}},
            {"name": _FIELDS["source"], "type": "text", "style": {"type": "url"}},
        ))
        table_id = top._create_table(report, TABLE_NAME, definitions)
    target = replace(report, table_id=table_id, fields=_FIELDS, destination=top._destination(report, table_id))
    fields = {row.get("field_name"): row for row in sync._paginated(target, "GET", "/fields")
              if isinstance(row, dict)}
    if (any(fields.get(_FIELDS[role], {}).get("type") != kind for role, kind in _TYPES.items())
            or fields[_FIELDS["key"]].get("is_primary") is not True):
        raise sync.SyncError("飞书七鱼工单表字段不匹配。", 409)
    return target


def _pages(cfg, **filters):
    items, seen = [], set()
    source = None
    expected_total = None
    while True:
        page = list_tickets(cfg, **filters, offset=len(items), limit=PAGE_SIZE)
        rows = page.get("items")
        page_source = page.get("source")
        total = page.get("total")
        more = page.get("has_more")
        if (not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0
                or type(more) is not bool or page_source not in ("qiyu", "linked")
                or (source is not None and source != page_source)
                or (expected_total is not None and expected_total != total)):
            raise ValueError("七鱼工单列表响应不完整。")
        source = page_source
        expected_total = total
        for item in rows:
            key = item.get("id") if isinstance(item, dict) else None
            if not isinstance(key, str) or not _ID.fullmatch(key) or key in seen:
                raise ValueError("七鱼工单列表存在无法唯一匹配的编号。")
            seen.add(key)
        items.extend(rows)
        if not more:
            if len(items) != total:
                raise ValueError("七鱼工单分页返回的数量不一致。")
            return items, source
        if not rows or len(items) >= total:
            raise ValueError("七鱼工单分页未能继续读取。")


def _time(value):
    return value if type(value) is int and 0 < value < 253_402_300_800_000 else None


def _projection(items, names=None):
    desired = {}
    for item in items:
        key = item["id"]
        linked = item.get("linked_session_count")
        status, priority = item.get("status_code"), item.get("priority_code")
        desired[key] = {
            _FIELDS["key"]: key,
            _FIELDS["title"]: _public_text(item.get("title"), names)[:300] or "未命名工单",
            _FIELDS["status"]: TICKET_STATUSES.get(status, "未识别") if type(status) is int else "未记录",
            _FIELDS["priority"]: TICKET_PRIORITIES.get(priority, "未识别") if type(priority) is int else "未记录",
            _FIELDS["created"]: _time(item.get("created_at")),
            _FIELDS["updated"]: _time(item.get("updated_at")),
            _FIELDS["sessions"]: linked if type(linked) is int and linked >= 0 else None,
            _FIELDS["source"]: {"text": "查看工单", "link": f"{PUBLIC_ORIGIN}/#tickets?id={key}"},
        }
    return desired


def _recent(cfg, today):
    end = today.isoformat()
    created_start = (today - timedelta(days=CREATED_DAYS - 1)).isoformat()
    updated_start = (today - timedelta(days=UPDATED_DAYS - 1)).isoformat()
    created, created_source = _pages(cfg, start=created_start, end=end)
    updated, updated_source = _pages(cfg, updated_start=updated_start, updated_end=end)
    combined = {item["id"]: item for item in created}
    for item in updated:
        previous = combined.get(item["id"])
        if previous is None or (_time(item.get("updated_at")) or 0) >= (_time(previous.get("updated_at")) or 0):
            combined[item["id"]] = item
    return _projection(combined.values(), _private_names(cfg, {})), {
        "created_period": [created_start, end], "updated_period": [updated_start, end],
        "created_scope": created_source, "updated_scope": updated_source,
    }


def _upsert(target, desired):
    existing = sync._existing(target, desired)
    inserts, updates = [], []
    for key, fields in desired.items():
        row = existing.get(key)
        if row is None:
            inserts.append({"fields": fields})
            continue
        changes = {name: value for name, value in fields.items()
                   if sync._value(row["fields"].get(name)) != sync._value(value)}
        if changes:
            updates.append({"record_id": row["record_id"], "fields": changes})
    sync._batch(target, "update", updates)
    sync._batch(target, "create", inserts)
    if inserts or updates:
        confirmed = sync._existing(target, desired)
        if set(confirmed) != set(desired) or any(
            sync._value(confirmed[key]["fields"].get(name)) != sync._value(value)
            for key, fields in desired.items() for name, value in fields.items()
        ):
            raise sync.SyncError("飞书未回读到完整七鱼工单记录，稍后重试。")
    return len(inserts), len(updates)


def run(state_dir):
    """Read the Beijing-time creation and operation windows, and upsert only their tickets."""
    support._safe_state(state_dir)
    cfg = settings()
    if maintenance(cfg):
        return {"status": "maintenance"}
    if cfg.runtime.mock:
        raise ValueError("生产侧工单数据未配置。")
    report = support.target_table(state_dir)
    inserted, changed = 0, 0
    with sync._write_lock(report.lock_path):
        try:
            desired, coverage = _recent(cfg, datetime.now(_ZONE).date())
        except TicketError as exc:
            raise sync.SyncError("无法读取七鱼工单，请核对七鱼接口状态或权限。", exc.http_status) from None
        if maintenance(cfg):
            return {"status": "maintenance"}
        target = _table(report)
        keys = sorted(desired, key=int)
        for offset in range(0, len(keys), MATCH_SIZE):
            group = {key: desired[key] for key in keys[offset:offset + MATCH_SIZE]}
            added, adjusted = _upsert(target, group)
            inserted += added
            changed += adjusted
    status = "partial" if "linked" in (coverage["created_scope"], coverage["updated_scope"]) else "published"
    return {"status": status, "tickets": len(desired), "created": inserted, "updated": changed,
            "url": target.destination, **coverage}
