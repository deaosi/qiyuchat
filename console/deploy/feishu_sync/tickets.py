"""Sync Qiyu tickets and technical responses through their Feishu Base table."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
from time import monotonic, time

from deploy.feishu_sync import support, ticket_archive, ticket_delta, ticket_reply_delta, ticket_work, top
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
_MINUTE = 60_000
_RECONCILE = 10 * _MINUTE
_DAY = 24 * 60 * _MINUTE
_FEEDBACK_QUIET_MS = 15_000
_SCHEMA_VERSION = 3
_WORK_SECONDS = 9
_ARCHIVE_START_BUDGET = 12
_ZONE = timezone(timedelta(hours=8))
_ID = re.compile(r"[1-9][0-9]{0,18}")
_FIELDS = {"key": "工单ID", "title": "工单标题", "status": "状态", "priority": "优先级", "submitter": "提报人",
           "created": "创建时间", "updated": "更新时间", "sessions": "关联会话数", "source": "语海详情"}
_TYPES = {"key": 1, "title": 1, "status": 1, "priority": 1, "created": 5, "updated": 5, "sessions": 2, "source": 15}
_FORM = {"group": "客户群名称", "owner": "后台负责人", "platform_version": "平台&版本", "shop": "店铺名",
         "software": "软件版本", "reported": "是否已点击报告问题", "request": "问题需求", "attachment": "附件/截图"}
_DETAIL = {"summary": "工单描述", "platform": "平台", **_FORM}
_INPUT = {"status": "技术处理状态", "reply": "技术回复", "version": "解决版本"}
_OUTPUT = {"status": "回传状态", "returned_at": "回传时间", "error": "回传失败原因"}
_SYNC_ERRORS = (ValueError, TypeError, KeyError, OSError, sync.SyncError)


def _column_specs() -> dict:
    names = (_FIELDS["submitter"], *_DETAIL.values(), *_INPUT.values(), *_OUTPUT.values())
    specs = {name: {"name": name, "type": "text"} for name in names}
    specs[_FORM["attachment"]]["style"] = {"type": "url"}
    specs[_OUTPUT["returned_at"]].update(type="datetime", style={"format": "yyyy-MM-dd HH:mm"})
    return specs


def _table(report: sync.Target, *, incremental=False) -> tuple[sync.Target, str]:
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
    if incremental:
        name = ticket_reply_delta.TIME_FIELD
        definitions[name] = {"name": name, "type": "updated_at", "style": {"format": "yyyy-MM-dd HH:mm"}}
        definitions[ticket_archive.CLOSED_AT] = {"name": ticket_archive.CLOSED_AT, "type": "datetime",
                                                 "style": {"format": "yyyy-MM-dd HH:mm"}}
    for name, definition in definitions.items():
        if name not in fields:
            top._cli(target, "+field-create", "--table-id", table_id, "--json", json.dumps(definition, ensure_ascii=False))
    if not set(definitions) <= fields.keys():
        fields = {row.get("field_name"): row for row in sync._paginated(target, "GET", "/fields") if isinstance(row, dict)}
    for name in definitions:
        expected = {_FORM["attachment"]: 15, _OUTPUT["returned_at"]: 5, ticket_archive.CLOSED_AT: 5,
                    ticket_reply_delta.TIME_FIELD: ticket_reply_delta.TIME_FIELD_TYPE}.get(name, 1)
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
    source = _FIELDS["source"]
    if (not isinstance(current, list) or not all(isinstance(name, str) for name in current)
            or len(set(current)) != len(current) or priority not in current or source not in current):
        raise sync.SyncError("无法核对飞书工单视图的详情列。", 409)
    form_columns = list(_FORM.values())
    order = [name for name in current if name != submitter and name not in form_columns]
    order.insert(order.index(priority) + 1, submitter)
    index = order.index(source) + 1
    order[index:index] = form_columns
    if current != order:
        top._cli(target, "+view-set-visible-fields", *args,
                 "--json", json.dumps({"visible_fields": order}, ensure_ascii=False))
        if top._cli(target, "+view-get-visible-fields", *args).get("visible_fields") != order:
            raise sync.SyncError("飞书工单视图的提报字段位置未能回读确认。", 409)


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


def _upsert(target, desired, *, existing=None):
    if existing is None:
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


def _attachment_link(row, source_link):
    prefix = PUBLIC_ORIGIN + ticket_guest.PUBLIC_PATH
    if not isinstance(source_link, str) or not source_link.startswith(prefix):
        return None
    token = source_link[len(prefix):]
    if not re.fullmatch(ticket_guest.TOKEN_PATTERN, token):
        return None
    snapshot, _ = ticket_guest._project(row, token)
    ticket = snapshot["ticket"]
    files = {item["url"]: item["name"] for section in (ticket, *ticket["fields"])
             for column in ("images", "attachments") for item in section.get(column, [])}
    if len(files) == 1:
        path, name = next(iter(files.items()))
        return {"text": name, "link": PUBLIC_ORIGIN + path}
    if files:
        return {"text": f"查看 {len(files)} 个附件", "link": source_link + "#ticket-form-heading"}
    return None


def _summary(row, names, *, source_link=None):
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
    request = fields.get("问题需求", original)
    platform = fields.get("平台&版本", fields.get("平台"))
    form = {label: ticket_guest._guest_text(fields.get(label), None) for role, label in _FORM.items()
            if role not in {"platform_version", "request", "attachment"}}
    return {
        _DETAIL["summary"]: _public_text(request, redaction)[:1500],
        _DETAIL["platform"]: _public_text(platform, redaction)[:120],
        **form,
        _FORM["platform_version"]: ticket_guest._guest_text(platform, None),
        _FORM["request"]: ticket_guest._guest_text(request, None),
        _FORM["attachment"]: _attachment_link(row, source_link),
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


def _enrich(cfg, desired, state_dir, *, incremental=False):
    if not desired:
        return {"fetched": 0, "failed": 0, "deferred": 0}
    cache_path = state_dir / "ticket-summaries.json"
    cache = support._read_state(cache_path) if cache_path.exists() else {}
    if not isinstance(cache, dict):
        raise ValueError("工单摘要缓存无法读取。")
    client = TicketClient(cfg.qiyu)
    next_cache = dict(cache) if incremental else {}
    names = None
    names_loaded = False
    fetched = failures = deferred = 0
    started = monotonic()
    for key, row in desired.items():
        stamp = [row[_FIELDS["created"]], row[_FIELDS["updated"]], row[_FIELDS["title"]]]
        previous = cache.get(key)
        cached = previous.get("fields") if isinstance(previous, dict) else None
        if isinstance(cached, dict) and previous.get("stamp") == stamp and set(cached) == set(_DETAIL.values()):
            row.update(cached)
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
        source_cell = sync._value(row.get(_FIELDS["source"]))
        columns = _summary(source, names, source_link=source_cell.get("link") if isinstance(source_cell, dict) else None)
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


def _technical_rows(cfg, target, *, window=None, exclude_closed=False, batch_state=None):
    records = {}
    ids = set()
    columns = [_FIELDS["key"], _FIELDS["source"], *_INPUT.values()]
    query = {"field_names": columns, "filter": {"conjunction": "or", "conditions": [
        {"field_name": name, "operator": "isNotEmpty", "value": []} for name in _INPUT.values()
    ]}}
    if exclude_closed:
        columns.append(ticket_archive.CLOSED_AT)
        # The v1 filter API only supports flat conditions. Prioritize excluding archived rows
        # on the server; look for technical edits on the projected active rows.
        query["filter"] = {"conjunction": "and", "conditions": [
            {"field_name": ticket_archive.CLOSED_AT, "operator": "isEmpty", "value": []}
        ]}
    if window is None:
        rows = (row for page in _record_pages(target, query) for row in page)
    elif batch_state is None:
        rows = ticket_reply_delta.read(target, since_ms=window[0], until_ms=window[1],
                                       field_names=columns, filters=query["filter"])
    else:
        rows, continuation = ticket_reply_delta.read_batch(
            target, since_ms=window[0], until_ms=window[1], field_names=columns,
            filters=query["filter"], cursor=batch_state.get("cursor"),
        )
        batch_state["cursor"] = continuation
    for row in rows:
        record_id = row.get("record_id") if isinstance(row, dict) else None
        fields = row.get("fields") if isinstance(row, dict) else None
        if not isinstance(record_id, str) or not sync._RECORD_ID.fullmatch(record_id) or not isinstance(fields, dict):
            raise sync.SyncError("飞书技术回复记录格式无法核对。")
        if exclude_closed and sync._value(fields.get(ticket_archive.CLOSED_AT)):
            continue
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


def _import_feedback(cfg, target, *, window=None, retry=False, pending=None, exclude_closed=False):
    if pending is not None:
        rows = pending
    else:
        rows = _technical_rows(cfg, target, window=window) if window else _technical_rows(cfg, target)
    # Durable pending or ambiguous writes remain eligible after the time window has moved on.
    retries = ticket_feedback.retry_records(cfg) if retry else {}
    for key, record in retries.items():
        if key in rows and rows[key] != record:
            raise sync.SyncError("飞书技术回复的记录关联已变化。", 409)
        rows.setdefault(key, record)
    keys = list(rows)
    lookup = replace(target, fields={"key": _FIELDS["key"], "source": _FIELDS["source"],
                                    **{"input_" + key: name for key, name in _INPUT.items()},
                                    **{"output_" + key: name for key, name in _OUTPUT.items()},
                                    **({"closed": ticket_archive.CLOSED_AT} if exclude_closed else {}),
                                    **({"modified": ticket_reply_delta.TIME_FIELD} if window else {})})
    imported = uncertain = failed = 0
    waiting = {}
    for offset in range(0, len(keys), MATCH_SIZE):
        group = keys[offset:offset + MATCH_SIZE]
        verified = sync._existing(lookup, dict.fromkeys(group))
        if any((key not in verified and key not in retries and pending is None)
               or (key in verified and verified[key]["record_id"] != rows[key]) for key in group):
            raise sync.SyncError("飞书技术回复所在记录已变动，请重试。", 409)
        updates = []
        for key in group:
            if key not in verified:
                continue
            fields = verified[key]["fields"]
            if exclude_closed and sync._value(fields.get(ticket_archive.CLOSED_AT)):
                continue
            if window is not None:
                modified = fields.get(ticket_reply_delta.TIME_FIELD)
                if type(modified) is not int:
                    raise sync.SyncError("无法核对飞书工单的最近修改时间。", 409)
                if modified > window[1]:
                    waiting[key] = modified + _FEEDBACK_QUIET_MS
                    continue
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
            if pending is not None and result["status"] in ("回传失败", "待核对七鱼回复"):
                waiting[key] = window[1] + _FEEDBACK_QUIET_MS + _RECONCILE
            error = result.get("error") or ""
            values = {_OUTPUT["status"]: result["status"], _OUTPUT["returned_at"]: result["returned_at"],
                      _OUTPUT["error"]: error[:300]}
            changes = {name: value for name, value in values.items()
                       if sync._value(fields.get(name)) != sync._value(value)}
            if changes:
                updates.append({"record_id": record_id, "fields": changes})
        sync._batch(target, "update", updates)
    return {"checked": len(rows), "imported": imported, "awaiting_review": uncertain, "failed": failed,
            **({"waiting": waiting} if pending is not None else {})}


def _incremental_projection(cfg, start, end):
    items, coverage = ticket_delta.read_slice(cfg, start, end)
    desired = _projection(cfg, items, _private_names(cfg, {}) if items else None)
    return desired, coverage, {item["id"]: item["submitter_id"] for item in items}


def _current_export(target, desired):
    lookup = replace(target, fields={**target.fields, "closed": ticket_archive.CLOSED_AT})
    existing = sync._existing(lookup, desired)
    selected = {}
    for key, fields in desired.items():
        actual = existing.get(key, {}).get("fields", {})
        if (not sync._value(actual.get(ticket_archive.CLOSED_AT))
                and (_time(actual.get(_FIELDS["updated"])) or 0) <= (_time(fields.get(_FIELDS["updated"])) or 0)):
            selected[key] = fields
    return selected, existing


def _feedback_work(cfg, target, queue, lane, now, window, *, batch_state, retry_after=None):
    rows = _technical_rows(cfg, target, window=window, exclude_closed=True, batch_state=batch_state)
    retries = {}
    if retry_after is not None:
        retries = ticket_feedback.retry_records(cfg, limit=100, after=retry_after)
        for key, record in retries.items():
            rows.setdefault(key, record)
    ticket_work.enqueue(queue, lane, {key: {"record_id": record} for key, record in rows.items()}, now_ms=now)
    return next(reversed(retries), "") if len(retries) == 100 else ""


def _drain_feedback(cfg, target, queue, lane, now, window):
    batch = ticket_work.due(queue, lane, now_ms=now, limit=10)
    if not batch:
        return {"checked": 0, "imported": 0, "awaiting_review": 0, "failed": 0}
    try:
        result = _import_feedback(cfg, target, window=window, exclude_closed=True,
                                  pending={key: item["record_id"] for key, item in batch.items()})
    except _SYNC_ERRORS:
        ticket_work.defer(queue, lane, batch, until_ms=now + _RECONCILE)
        raise
    waiting = result.pop("waiting")
    for key, until in waiting.items():
        ticket_work.defer(queue, lane, [key], until_ms=until)
    ticket_work.finish(queue, lane, batch.keys() - waiting.keys())
    return result


def _drain_exports(cfg, target, queue, state_dir, lane, detail_lane, now, deadline):
    inserted = updated = 0
    while monotonic() < deadline:
        batch = ticket_work.due(queue, lane, now_ms=now, limit=MATCH_SIZE)
        if not batch:
            break
        try:
            desired, existing = _current_export(target, {key: item["fields"] for key, item in batch.items()})
            if desired:
                created, changed = _upsert(target, desired, existing=existing)
                inserted += created
                updated += changed
                ticket_work.enqueue(queue, detail_lane, {key: batch[key] for key in desired}, now_ms=now)
        except _SYNC_ERRORS:
            ticket_work.defer(queue, lane, batch, until_ms=now + 2 * _MINUTE)
            raise
        ticket_work.finish(queue, lane, batch)

    details = {"fetched": 0, "failed": 0, "deferred": 0}
    submitters = {"fetched": 0, "filled": 0, "pending": 0, "deferred": 0, "unavailable": 0}
    if monotonic() >= deadline:
        return inserted, updated, details, submitters
    batch = ticket_work.due(queue, detail_lane, now_ms=now, limit=3)
    if not batch:
        return inserted, updated, details, submitters
    postponed = set()
    try:
        desired, existing = _current_export(target, {key: item["fields"] for key, item in batch.items()})
        if desired:
            staff_ids = {key: batch[key]["staff_id"] for key in desired}
            submitters = _submitters(cfg, desired, staff_ids, state_dir)
            details = _enrich(cfg, desired, state_dir, incremental=True)
            _upsert(target, desired, existing=existing)
            postponed = {key for key, fields in desired.items() if not set(_DETAIL.values()) <= fields.keys()
                         or (staff_ids[key] is not None and _FIELDS["submitter"] not in fields)}
    except _SYNC_ERRORS:
        postponed = set(batch)
        details = {"status": "failed", "message": "本轮工单资料稍后重试。"}
    if postponed:
        ticket_work.defer(queue, detail_lane, postponed, until_ms=now + _RECONCILE)
    ticket_work.finish(queue, detail_lane, batch.keys() - postponed)
    return inserted, updated, details, submitters


def _archive_inactive(cfg, target, state, now):
    client = TicketClient(cfg.qiyu)
    cutoff = ticket_archive.six_month_cutoff(now)

    def eligible(key, fields):
        identifier = _id(key)
        if identifier is None or not _source_matches(cfg, key, fields.get(_FIELDS["source"])):
            return False
        total, found = client.search(offset=0, limit=2, bounds={"ticketId": identifier})
        if total != 1 or len(found) != 1 or _id(found[0].get("id")) != identifier:
            return False
        source = found[0]
        created = _time(source.get("createTime"))
        updated = _time(source.get("modifyTime"))
        if created is None or ("modifyTime" not in source):
            return False
        return max(created, updated or created) < cutoff

    def pending(fields):
        if sync._value(fields.get(_OUTPUT["status"])) in ("待回传", "回传失败", "待核对七鱼回复"):
            return True
        feedback = _feedback(fields)
        return (any(feedback.values()) and
                not ticket_feedback.confirmed(cfg, sync._value(fields.get(_FIELDS["key"])), feedback))

    return ticket_archive.archive(
        target, now_ms=now, key_field=_FIELDS["key"], status_field=_FIELDS["status"],
        created_field=_FIELDS["created"], updated_field=_FIELDS["updated"], source_field=_FIELDS["source"],
        feedback_fields=tuple(_INPUT.values()), writeback_field=_OUTPUT["status"],
        is_pending=pending, eligible=eligible, cutoff_ms=cutoff, scan_cursor=state.get("archive_cursor"),
    )


def _recent_checkpoint(state, key, upper, ordinary, replay=None):
    previous = state.get(key)
    if type(previous) is not int or previous < 0 or previous > upper:
        return max(0, upper - (replay or ordinary))
    return max(0, min(previous - ordinary, upper - replay) if replay else previous - ordinary)


def run(state_dir, *, incremental=False):
    """Pull technical responses even for older tickets and export recent Qiyu work."""
    support._safe_state(state_dir)
    cfg = settings()
    if maintenance(cfg):
        return {"status": "maintenance"}
    if cfg.runtime.mock:
        raise ValueError("生产侧工单数据未配置。")
    report = support.target_table(state_dir)
    inserted, changed = 0, 0
    lock = report.lock_path.with_name("ticket-table.lock")
    with sync._write_lock(lock):
        started = monotonic()
        now = int(time() * 1000)
        setup_path = state_dir / "ticket-table-setup.json"
        setup = support._read_state(setup_path) if setup_path.exists() else {}
        if not isinstance(setup, dict):
            raise ValueError("工单表的设置记录无效。")
        state_path = state_dir / "ticket-incremental.json"
        state = support._read_state(state_path) if incremental and state_path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError("工单同步时间记录无效。")
        schema_at = state.get("schema_at", 0)
        table_id = state.get("table_id")
        if (incremental and state.get("schema_version") == _SCHEMA_VERSION
                and type(schema_at) is int and 0 <= now - schema_at < _RECONCILE
                and isinstance(table_id, str) and top._TABLE_ID.fullmatch(table_id)
                and setup.get("table_id") == table_id and setup.get("positioned_field_id")):
            target = replace(report, table_id=table_id, destination=top._destination(report, table_id),
                             fields={**_FIELDS, **_DETAIL})
            submitter_field_id = setup["positioned_field_id"]
        else:
            target, submitter_field_id = _table(report, incremental=True) if incremental else _table(report)
            if state.get("table_id") != target.table_id:
                state = {"table_id": target.table_id}
            state["schema_at"] = now
            state["schema_version"] = _SCHEMA_VERSION
        if setup.get("table_id") != target.table_id:
            setup = {"table_id": target.table_id}
        reconcile = incremental and now - state.get("reconciled_at", 0) >= _RECONCILE
        retry_feedback = incremental and now - state.get("feedback_retry_at", 0) >= _RECONCILE
        daily = incremental and now - state.get("daily_at", 0) >= _DAY
        feedback_end = now - _FEEDBACK_QUIET_MS
        feedback_start = _recent_checkpoint(
            state, "feishu_until", feedback_end, _MINUTE, _RECONCILE * 2 if retry_feedback else None)
        queue = state_dir / "ticket-work.db"
        inbound, outbound, details_lane = (f"{direction}:{target.table_id}"
                                           for direction in ("feedback", "records", "details"))
        try:
            if incremental:
                pending_scan = state.get("feedback_scan")
                scan = dict(pending_scan) if isinstance(pending_scan, dict) else {
                    "start": feedback_start, "end": feedback_end,
                }
                retry_after = state.get("feedback_retry_cursor", "") if retry_feedback else None
                next_retry = _feedback_work(cfg, target, queue, inbound, now, (scan["start"], scan["end"]),
                                            retry_after=retry_after, batch_state=scan)
                if scan["cursor"] is None:
                    state.pop("feedback_scan", None)
                    state["feishu_until"] = scan["end"]
                else:
                    state["feedback_scan"] = scan
                if retry_feedback:
                    state["feedback_retry_at"] = now
                    state["feedback_retry_cursor"] = next_retry
                support._write_state(state_path, state)
                feedback = _drain_feedback(cfg, target, queue, inbound, now, (feedback_start, feedback_end))
            else:
                feedback = _import_feedback(cfg, target)
        except _SYNC_ERRORS as exc:
            message = str(exc) if type(exc) is ValueError or isinstance(exc, sync.SyncError) else "飞书技术回复暂无法核对。"
            feedback = {"status": "failed", "message": message}
        previous_qiyu = state.get("qiyu_until", 0)
        try:
            if incremental:
                start = _recent_checkpoint(state, "qiyu_until", now, _MINUTE,
                                           _RECONCILE * 2 if reconcile else None)
                upper = min(now, start + _DAY)
                desired, coverage, staff_ids = _incremental_projection(cfg, start, upper)
                end = coverage["created_period"][1]
                if type(previous_qiyu) is int and end < upper and end <= previous_qiyu < upper:
                    desired, coverage, staff_ids = _incremental_projection(cfg, previous_qiyu + 1, upper)
                    end = coverage["created_period"][1]
            else:
                end = now
                desired, coverage, staff_ids = _recent(cfg, datetime.now(_ZONE).date())
        except TicketError as exc:
            raise sync.SyncError("无法读取七鱼工单，请核对七鱼接口状态或权限。", exc.http_status) from None
        if maintenance(cfg):
            return {"status": "maintenance"}
        if incremental:
            ticket_work.enqueue(queue, outbound, {key: {"fields": fields, "staff_id": staff_ids[key]}
                                                 for key, fields in desired.items()}, now_ms=now)
            state["qiyu_until"] = max(end, previous_qiyu) if type(previous_qiyu) is int else end
            if reconcile:
                state["reconciled_at"] = now
            support._write_state(state_path, state)
            inserted, changed, details, submitters = _drain_exports(
                cfg, target, queue, state_dir, outbound, details_lane, now, monotonic() + _WORK_SECONDS,
            )
        else:
            submitters = _submitters(cfg, desired, staff_ids, state_dir)
            details = _enrich(cfg, desired, state_dir)
            for offset in range(0, len(desired), MATCH_SIZE):
                keys = list(desired)[offset:offset + MATCH_SIZE]
                group = {key: desired[key] for key in keys}
                added, adjusted = _upsert(target, group)
                inserted += added
                changed += adjusted
        layout_error = ""
        if setup.get("positioned_field_id") != submitter_field_id or setup.get("form_layout") != _SCHEMA_VERSION:
            try:
                _position_submitter(target)
                setup["positioned_field_id"] = submitter_field_id
                setup["form_layout"] = _SCHEMA_VERSION
                support._write_state(setup_path, setup)
            except _SYNC_ERRORS as exc:
                layout_error = str(exc) if isinstance(exc, sync.SyncError) else "工单视图设置暂不可核对。"
        migrated = 0
        if setup.get("guest_links_migrated") is not True:
            migrated = _migrate_old_links(cfg, target, desired)
            setup["guest_links_migrated"] = True
            support._write_state(setup_path, setup)
        archive = {}
        if (incremental and monotonic() - started < _ARCHIVE_START_BUDGET
                and now - state.get("history_reconciled_at", 0) >= _DAY
                and not desired and not feedback.get("checked")
                and feedback.get("status") != "failed"):
            try:
                # Review older updates in a bounded slice without advancing the live cursor.
                history_start = state.get("history_until", 0) or (now - _DAY)
                older, history_coverage, older_staff = _incremental_projection(cfg, history_start, now)
                ticket_work.enqueue(queue, outbound, {
                    key: {"fields": fields, "staff_id": older_staff[key]} for key, fields in older.items()
                }, now_ms=now)
                history_end = history_coverage["created_period"][1]
                state["history_until"] = history_end + 1 if history_end < now else 0
                if history_end == now:
                    state["history_reconciled_at"] = now
                support._write_state(state_path, state)
            except (*_SYNC_ERRORS, TicketError):
                state["history_reconciled_at"] = now - _DAY + _RECONCILE
                support._write_state(state_path, state)
        if (incremental and (daily or state.get("archive_pending"))
                and now >= state.get("archive_retry_at", 0)
                and monotonic() - started < _ARCHIVE_START_BUDGET
                and feedback.get("status") != "failed"):
            try:
                archive = _archive_inactive(cfg, target, state, now)
                state["archive_cursor"] = archive.get("next_cursor")
                state["archive_pending"] = archive["more_work"]
                state["daily_at"] = now
                state.pop("archive_retry_at", None)
                support._write_state(state_path, state)
            except (*_SYNC_ERRORS, TicketError):
                archive = {"status": "failed", "message": "本轮未能完成工单关闭核验。"}
                state["archive_pending"] = True
                state["archive_retry_at"] = now + _RECONCILE
                support._write_state(state_path, state)
    status = "partial" if (
        "linked" in (coverage["created_scope"], coverage["updated_scope"])
        or feedback.get("status") == "failed" or feedback.get("failed") or feedback.get("awaiting_review")
        or details.get("status") == "failed"
        or submitters.get("status") == "failed" or submitters.get("pending")
        or layout_error or archive.get("status") == "failed"
    ) else "published"
    return {"status": status, "tickets": len(desired), "created": inserted, "updated": changed,
            "url": target.destination, "historical_guest_links_updated": migrated,
            "details": details, "feedback": feedback, "submitters": submitters,
            "submitter_column_positioned": not layout_error,
            **({"submitter_column_error": layout_error} if layout_error else {}),
            **({"incremental": True, "reconciled": reconcile, "daily": daily,
                "archive": {key: value for key, value in archive.items() if key != "next_cursor"},
                "queued": {name: ticket_work.count(queue, lane) for name, lane in
                           (("feedback", inbound), ("records", outbound), ("details", details_lane))},
                "qiyu_lag_seconds": round((now - end) / 1000, 3),
                "duration_seconds": round((time() * 1000 - now) / 1000, 3)} if incremental else {}),
            **coverage}
