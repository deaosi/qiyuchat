"""Sync Qiyu tickets and technical responses through their Feishu Base table."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
from time import monotonic

from deploy.feishu_sync import support, top
from src import ticket_feedback
from src import voc_feishu_sync as sync
from src.config import settings
from src.qiyu.ticket import PAGE_SIZE, TicketClient, TicketError, _id
from src.voc_feishu_content import _private_names, _public_text
from src.voc_service import maintenance
from src.web import ticket_guest
from src.web.guest_evidence import PUBLIC_ORIGIN
from src.web.tickets import TICKET_PRIORITIES, TICKET_STATUSES, _field_value, list_tickets, submitter_names


TABLE_NAME = "七鱼工单"
CREATED_DAYS = 30
UPDATED_DAYS = 7
MATCH_SIZE = 20
DETAIL_LIMIT = 12
DETAIL_BUDGET_SECONDS = 25
_ZONE = timezone(timedelta(hours=8))
_ID = re.compile(r"[1-9][0-9]{0,18}")
_FIELDS = {"key": "工单ID", "title": "工单标题", "status": "状态", "priority": "优先级", "submitter": "提报人",
           "created": "创建时间", "updated": "更新时间", "sessions": "关联会话数", "source": "语海详情"}
_TYPES = {"key": 1, "title": 1, "status": 1, "priority": 1, "created": 5, "updated": 5, "sessions": 2, "source": 15}
_DETAIL = {"summary": "工单描述", "platform": "平台", "software": "软件版本"}
_INPUT = {"status": "技术处理状态", "reply": "技术回复", "version": "解决版本"}
_OUTPUT = {"status": "回传状态", "returned_at": "回传时间", "error": "回传失败原因"}
_SYNC_ERRORS = (ValueError, TypeError, KeyError, OSError, sync.SyncError)


def _column_specs() -> dict:
    names = (_FIELDS["submitter"], *_DETAIL.values(), *_INPUT.values(), *_OUTPUT.values())
    specs = {name: {"name": name, "type": "text"} for name in names}
    specs[_OUTPUT["returned_at"]].update(type="datetime", style={"format": "yyyy-MM-dd HH:mm"})
    return specs


def _table(report: sync.Target) -> tuple[sync.Target, str]:
    table_id = top._find_table(report, TABLE_NAME)
    if table_id is None:
        definitions = [
            {"name": _FIELDS[role], "type": "text"} for role in ("key", "title", "status", "priority", "submitter")
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
    definitions = _column_specs()
    for name, definition in definitions.items():
        if name not in fields:
            top._cli(target, "+field-create", "--table-id", table_id, "--json", json.dumps(definition, ensure_ascii=False))
    if not set(definitions) <= fields.keys():
        fields = {row.get("field_name"): row for row in sync._paginated(target, "GET", "/fields") if isinstance(row, dict)}
    for name in definitions:
        expected = 5 if name == _OUTPUT["returned_at"] else 1
        allowed = (1, 3) if name == _INPUT["status"] else (expected,)
        if fields.get(name, {}).get("type") not in allowed:
            raise sync.SyncError(f"飞书七鱼工单表字段名称或类型不匹配：{name}。", 409)
    submitter_id = fields[_FIELDS["submitter"]].get("field_id")
    if not isinstance(submitter_id, str) or not submitter_id:
        raise sync.SyncError("无法核对飞书工单提报人字段。", 409)
    return replace(target, fields={**target.fields, **_DETAIL}), submitter_id


def _position_submitter(target):
    args = ("--table-id", target.table_id, "--view-id", top._view_id(target))
    current = top._cli(target, "+view-get-visible-fields", *args).get("visible_fields")
    priority, submitter = _FIELDS["priority"], _FIELDS["submitter"]
    if (not isinstance(current, list) or not all(isinstance(name, str) for name in current)
            or len(set(current)) != len(current) or priority not in current):
        raise sync.SyncError("无法核对飞书工单视图的优先级列。", 409)
    order = [name for name in current if name != submitter]
    order.insert(order.index(priority) + 1, submitter)
    if current != order:
        top._cli(target, "+view-set-visible-fields", *args,
                 "--json", json.dumps({"visible_fields": order}, ensure_ascii=False))
        if top._cli(target, "+view-get-visible-fields", *args).get("visible_fields") != order:
            raise sync.SyncError("飞书工单视图的提报人列位置未能回读确认。", 409)


def _pages(cfg, **filters):
    items, seen = [], set()
    source = None
    expected_total = None
    while True:
        page = list_tickets(cfg, **filters, offset=len(items), limit=PAGE_SIZE, include_submitter_id=True)
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


def _legacy_source_link(key):
    return f"{PUBLIC_ORIGIN}/#tickets?id={key}"


def _source_matches(cfg, key, cell):
    source = sync._value(cell)
    if not isinstance(source, dict) or not isinstance(source.get("link"), str):
        return False
    link = source["link"]
    return link == _legacy_source_link(key) or ticket_guest.matches(cfg, key, link)


def _projection(cfg, items, names=None):
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
            _FIELDS["source"]: {"text": "查看工单", "link": ticket_guest.url_for(cfg, key)},
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
    return _projection(cfg, combined.values(), _private_names(cfg, {})), {
        "created_period": [created_start, end], "updated_period": [updated_start, end],
        "created_scope": created_source, "updated_scope": updated_source,
    }, {key: item.get("submitter_id") for key, item in combined.items()}


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


def _migrate_old_links(cfg, target, exporting):
    lookup = replace(target, fields={"key": _FIELDS["key"], "source": _FIELDS["source"]})
    expected = {}
    for page in _record_pages(lookup, {"field_names": list(lookup.fields.values())}):
        for row in page:
            fields = row.get("fields") if isinstance(row, dict) else None
            if not isinstance(fields, dict):
                continue
            key = sync._value(fields.get(_FIELDS["key"]))
            source = sync._value(fields.get(_FIELDS["source"]))
            if (not isinstance(key, str) or not _ID.fullmatch(key) or key in exporting
                    or not isinstance(source, dict) or source.get("link") != _legacy_source_link(key)):
                continue
            if key in expected:
                raise sync.SyncError("历史飞书工单编号重复，无法更新来源链接。", 409)
            expected[key] = row.get("record_id")
    migrated = 0
    candidates = list(expected)
    for offset in range(0, len(candidates), MATCH_SIZE):
        keys = candidates[offset:offset + MATCH_SIZE]
        incoming = dict.fromkeys(keys)
        verified = sync._existing(lookup, incoming)
        if any(key not in verified or verified[key]["record_id"] != expected[key] for key in keys):
            raise sync.SyncError("历史飞书工单在迁移期间有变动，请重试。", 409)
        updates, links = [], {}
        for key in keys:
            fields = verified[key]["fields"]
            source = sync._value(fields.get(_FIELDS["source"]))
            if not isinstance(source, dict) or source.get("link") != _legacy_source_link(key):
                continue
            link = {"text": "查看工单", "link": ticket_guest.url_for(cfg, key)}
            links[key] = link
            updates.append({"record_id": expected[key], "fields": {_FIELDS["source"]: link}})
        sync._batch(lookup, "update", updates)
        if updates:
            confirmed = sync._existing(lookup, links)
            if any(sync._value(confirmed.get(key, {}).get("fields", {}).get(_FIELDS["source"])) != link
                   for key, link in links.items()):
                raise sync.SyncError("历史工单访客链接未能回读确认。", 409)
        migrated += len(updates)
    return migrated


def _summary(row, names):
    fields = {}
    custom = row.get("custom")
    if isinstance(custom, list):
        for item in custom:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                fields[item["name"].strip()] = _field_value(item.get("value"), 4000)[0]
    private = [fields.get(name) for name in ("客户群名称", "后台负责人", "店铺名")]
    patterns = [re.escape(value) for value in private if value]
    if names is not None:
        patterns.append(names.pattern)
    redaction = re.compile("|".join(sorted(patterns, key=len, reverse=True)), re.I) if patterns else None
    original, _ = _field_value(row.get("content"), 4000, rich=row.get("isRichText") in (True, 1, "1"))
    description = _public_text(fields.get("问题需求") or original, redaction)
    return {
        _DETAIL["summary"]: description[:1500],
        _DETAIL["platform"]: _public_text(fields.get("平台&版本") or fields.get("平台"), redaction)[:120],
        _DETAIL["software"]: _public_text(fields.get("软件版本"), redaction)[:120],
    }


def _submitters(cfg, desired, staff_ids, state_dir):
    cache_path = state_dir / "ticket-submitters.json"
    cache = support._read_state(cache_path) if cache_path.exists() else {}
    if not isinstance(cache, dict):
        raise ValueError("工单提报人缓存无法读取。")
    fetched = filled = unavailable = deferred = 0
    started = monotonic()
    now = int(datetime.now(timezone.utc).timestamp())
    client = TicketClient(cfg.qiyu)
    changed = False
    pending = []
    for key, row in desired.items():
        staff_id = staff_ids[key]
        if staff_id is None:
            unavailable += 1
            continue
        previous = cache.get(key)
        retry_at = 0
        if isinstance(previous, dict) and previous.get("staff_id") == staff_id:
            name = previous.get("name")
            if isinstance(name, str) and name:
                row[_FIELDS["submitter"]] = name
                filled += 1
                continue
            if type(previous.get("retry_at")) is int:
                retry_at = previous["retry_at"]
        if retry_at <= now:
            pending.append((retry_at, key, staff_id))
    for _, key, staff_id in sorted(pending):
        if fetched >= DETAIL_LIMIT or monotonic() - started >= DETAIL_BUDGET_SECONDS:
            deferred += 1
            continue
        fetched += 1
        try:
            history = client.history(int(key))
        except TicketError:
            history = []
        names = submitter_names(history, staff_id)
        if len(names) == 1:
            name = names.pop()
            desired[key][_FIELDS["submitter"]] = name
            cache[key] = {"staff_id": staff_id, "name": name}
            filled += 1
        else:
            cache[key] = {"staff_id": staff_id, "retry_at": now + 3600}
        changed = True
    if changed:
        support._write_state(cache_path, cache)
    return {"fetched": fetched, "filled": filled, "pending": len(desired) - filled - unavailable,
            "deferred": deferred, "unavailable": unavailable}


def _enrich(cfg, desired, state_dir):
    cache_path = state_dir / "ticket-summaries.json"
    cache = support._read_state(cache_path) if cache_path.exists() else {}
    if not isinstance(cache, dict):
        raise ValueError("工单摘要缓存无法读取。")
    client = TicketClient(cfg.qiyu)
    next_cache = {}
    names = None
    names_loaded = False
    fetched = failures = deferred = 0
    started = monotonic()
    for key, row in desired.items():
        stamp = [row[_FIELDS["created"]], row[_FIELDS["updated"]], row[_FIELDS["title"]]]
        previous = cache.get(key)
        if isinstance(previous, dict) and previous.get("stamp") == stamp and isinstance(previous.get("fields"), dict):
            row.update({name: value for name, value in previous["fields"].items()
                        if name in _DETAIL.values() and isinstance(value, str)})
            next_cache[key] = previous
            continue
        if fetched >= DETAIL_LIMIT or monotonic() - started >= DETAIL_BUDGET_SECONDS:
            deferred += 1
            continue
        fetched += 1
        try:
            source = client.detail(int(key))
        except TicketError:
            failures += 1
            continue
        if not names_loaded:
            names = _private_names(cfg, {})
            names_loaded = True
        columns = _summary(source, names)
        row.update(columns)
        next_cache[key] = {"stamp": stamp, "fields": columns}
    if next_cache != cache:
        support._write_state(cache_path, next_cache)
    return {"fetched": fetched, "failed": failures, "deferred": deferred}


def _record_pages(target, data):
    seen = set()
    params = {"page_size": 100}
    while True:
        response = sync._call(target, "POST", "/records/search", params=params, data=data)
        rows = response.get("items")
        more = response.get("has_more")
        if rows is None and more is False and response.get("total", 0) == 0 and not seen:
            rows = []
        if not isinstance(rows, list) or type(more) is not bool:
            raise sync.SyncError("飞书工单分页返回不完整。")
        yield rows
        if not more:
            return
        cursor = response.get("page_token")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise sync.SyncError("飞书工单分页无法继续。")
        seen.add(cursor)
        params = {"page_size": 100, "page_token": cursor}


def _feedback(fields):
    cells = {role: sync._value(fields.get(name)) for role, name in _INPUT.items()}
    if any(not isinstance(value, str) for value in cells.values()):
        raise sync.SyncError("飞书技术回复字段格式不匹配。", 409)
    return {role: value.strip() for role, value in cells.items()}


def _technical_rows(cfg, target):
    records = {}
    ids = set()
    columns = [_FIELDS["key"], _FIELDS["source"], *_INPUT.values(), *_OUTPUT.values()]
    query = {"field_names": columns, "filter": {"conjunction": "or", "conditions": [
        {"field_name": name, "operator": "isNotEmpty", "value": []} for name in _INPUT.values()
    ]}}
    for page in _record_pages(target, query):
        for row in page:
            record_id = row.get("record_id") if isinstance(row, dict) else None
            fields = row.get("fields") if isinstance(row, dict) else None
            if not isinstance(record_id, str) or not sync._RECORD_ID.fullmatch(record_id) or not isinstance(fields, dict):
                raise sync.SyncError("飞书技术回复记录格式无法核对。")
            feedback = _feedback(fields)
            if not any(feedback.values()):
                continue
            key = sync._value(fields.get(_FIELDS["key"]))
            if (not isinstance(key, str) or not _ID.fullmatch(key) or key in records or record_id in ids
                    or not _source_matches(cfg, key, fields.get(_FIELDS["source"]))):
                raise sync.SyncError("飞书技术回复无法唯一匹配七鱼工单，请核对工单ID及语海详情。", 409)
            ids.add(record_id)
            records[key] = record_id
    return records


def _import_feedback(cfg, target):
    rows = _technical_rows(cfg, target)
    keys = list(rows)
    lookup = replace(target, fields={"key": _FIELDS["key"], "source": _FIELDS["source"],
                                    **{"input_" + key: name for key, name in _INPUT.items()},
                                    **{"output_" + key: name for key, name in _OUTPUT.items()}})
    imported = uncertain = failed = 0
    for offset in range(0, len(keys), MATCH_SIZE):
        group = keys[offset:offset + MATCH_SIZE]
        verified = sync._existing(lookup, dict.fromkeys(group))
        if set(verified) != set(group) or any(verified[key]["record_id"] != rows[key] for key in group):
            raise sync.SyncError("飞书技术回复所在记录已变动，请重试。", 409)
        updates = []
        for key in group:
            fields = verified[key]["fields"]
            if not _source_matches(cfg, key, fields.get(_FIELDS["source"])):
                raise sync.SyncError("飞书技术回复的语海详情已变动，请核对来源。", 409)
            feedback = _feedback(fields)
            if not any(feedback.values()):
                continue
            record_id = rows[key]
            result = ticket_feedback.ingest(cfg, ticket_id=key, record_id=record_id,
                                            feedback=feedback, feishu_url=target.destination)
            imported += int(result.get("changed") is True)
            uncertain += int(result["status"] == "待核对七鱼回复")
            failed += int(result["status"] == "回传失败")
            error = result.get("error") or ""
            values = {_OUTPUT["status"]: result["status"], _OUTPUT["returned_at"]: result["returned_at"],
                      _OUTPUT["error"]: error[:300]}
            changes = {name: value for name, value in values.items()
                       if sync._value(fields.get(name)) != sync._value(value)}
            if changes:
                updates.append({"record_id": record_id, "fields": changes})
        sync._batch(target, "update", updates)
    return {"checked": len(rows), "imported": imported, "awaiting_review": uncertain, "failed": failed}


def run(state_dir):
    """Pull technical responses even for older tickets and export recent Qiyu work."""
    support._safe_state(state_dir)
    cfg = settings()
    if maintenance(cfg):
        return {"status": "maintenance"}
    if cfg.runtime.mock:
        raise ValueError("生产侧工单数据未配置。")
    report = support.target_table(state_dir)
    inserted, changed = 0, 0
    with sync._write_lock(report.lock_path):
        target, submitter_field_id = _table(report)
        setup_path = state_dir / "ticket-table-setup.json"
        setup = support._read_state(setup_path) if setup_path.exists() else {}
        if not isinstance(setup, dict) or setup.get("table_id") != target.table_id:
            setup = {"table_id": target.table_id}
        try:
            feedback = _import_feedback(cfg, target)
        except _SYNC_ERRORS as exc:
            message = str(exc) if type(exc) is ValueError or isinstance(exc, sync.SyncError) else "飞书技术回复暂无法核对。"
            feedback = {"status": "failed", "message": message}
        try:
            desired, coverage, staff_ids = _recent(cfg, datetime.now(_ZONE).date())
        except TicketError as exc:
            raise sync.SyncError("无法读取七鱼工单，请核对七鱼接口状态或权限。", exc.http_status) from None
        if maintenance(cfg):
            return {"status": "maintenance"}
        try:
            submitters = _submitters(cfg, desired, staff_ids, state_dir)
        except (OSError, ValueError):
            submitters = {"status": "failed", "message": "工单提报人缓存暂不可核对。"}
        try:
            details = _enrich(cfg, desired, state_dir)
        except (OSError, ValueError, TypeError, KeyError):
            details = {"status": "failed", "message": "工单摘要缓存暂不可核对。"}
        keys = sorted(desired, key=int)
        for offset in range(0, len(keys), MATCH_SIZE):
            group = {key: desired[key] for key in keys[offset:offset + MATCH_SIZE]}
            added, adjusted = _upsert(target, group)
            inserted += added
            changed += adjusted
        layout_error = ""
        if setup.get("positioned_field_id") != submitter_field_id:
            try:
                _position_submitter(target)
                setup["positioned_field_id"] = submitter_field_id
                support._write_state(setup_path, setup)
            except _SYNC_ERRORS as exc:
                layout_error = str(exc) if isinstance(exc, sync.SyncError) else "工单视图设置暂不可核对。"
        migrated = 0
        if setup.get("guest_links_migrated") is not True:
            migrated = _migrate_old_links(cfg, target, desired)
            setup["guest_links_migrated"] = True
            support._write_state(setup_path, setup)
    status = "partial" if (
        "linked" in (coverage["created_scope"], coverage["updated_scope"])
        or feedback.get("status") == "failed" or feedback.get("failed") or feedback.get("awaiting_review")
        or details.get("status") == "failed"
        or submitters.get("status") == "failed" or submitters.get("pending")
        or layout_error
    ) else "published"
    return {"status": status, "tickets": len(desired), "created": inserted, "updated": changed,
            "url": target.destination, "historical_guest_links_updated": migrated,
            "details": details, "feedback": feedback, "submitters": submitters,
            "submitter_column_positioned": not layout_error,
            **({"submitter_column_error": layout_error} if layout_error else {}), **coverage}
