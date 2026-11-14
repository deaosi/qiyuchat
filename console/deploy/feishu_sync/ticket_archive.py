"""Close inactive Feishu ticket copies while preserving their history."""

from calendar import monthrange
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import re

from deploy.feishu_sync import ticket_reply_delta
from src import voc_feishu_sync as sync


CLOSED_AT = "自动关闭时间"
CLOSED_STATUS = "已关闭"
PAGE_SIZE = 5
BUSINESS_ZONE = timezone(timedelta(hours=8))
_TICKET_ID = re.compile(r"[1-9][0-9]{0,18}")
_MAX_TIME_MS = 253_402_300_800_000


def six_month_cutoff(now_ms):
    now = datetime.fromtimestamp(now_ms / 1000, BUSINESS_ZONE)
    year, month = divmod(now.year * 12 + now.month - 1 - 6, 12)
    month += 1
    day = min(now.day, monthrange(year, month)[1])
    return int(now.replace(year=year, month=month, day=day).timestamp() * 1000)


def _stamp(value):
    return type(value) is int and 0 < value < _MAX_TIME_MS


def _inactive(fields, cutoff, *, status_field, created_field, updated_field, modified_field, source_field):
    created = fields.get(created_field)
    updated = fields.get(updated_field)
    modified = fields.get(modified_field)
    source = sync._value(fields.get(source_field))
    status = sync._value(fields.get(status_field))
    return (isinstance(status, str) and bool(status.strip()) and status != CLOSED_STATUS
            and isinstance(source, dict) and isinstance(source.get("link"), str) and bool(source["link"])
            and sync._value(fields.get(CLOSED_AT)) == ""
            and _stamp(created) and created < cutoff and _stamp(modified) and modified < cutoff
            and (updated is None or updated == "" or _stamp(updated) and updated < cutoff))


def archive(target, *, now_ms, key_field, status_field, created_field, updated_field, source_field,
            feedback_fields, writeback_field, is_pending, eligible=None, cutoff_ms=None,
            modified_field=ticket_reply_delta.TIME_FIELD, scan_cursor=None):
    """Inspect one page and close verified records. The caller holds the Base write lock."""
    if not _stamp(now_ms):
        raise ValueError("飞书工单关闭时间无效。")
    cutoff = six_month_cutoff(now_ms) if cutoff_ms is None else cutoff_ms
    if not _stamp(cutoff) or cutoff >= now_ms:
        raise ValueError("飞书工单关闭范围无效。")

    fields = {"key": key_field, "status": status_field, "created": created_field, "updated": updated_field,
              "modified": modified_field, "source": source_field, "closed": CLOSED_AT,
              "writeback": writeback_field}
    fields.update({f"feedback_{index}": name for index, name in enumerate(feedback_fields)})
    lookup = replace(target, fields=fields)
    projected = list(dict.fromkeys(fields.values()))
    # Feishu's date predicate rounds by day. Include the cutoff day and compare exact timestamps locally.
    coarse_end = (datetime.fromtimestamp(cutoff / 1000, BUSINESS_ZONE) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    query = {"field_names": projected, "automatic_fields": True,
             "sort": [{"field_name": modified_field, "desc": False}, {"field_name": key_field, "desc": False}],
             "filter": {"conjunction": "and", "conditions": [
                 {"field_name": CLOSED_AT, "operator": "isEmpty", "value": []},
                 {"field_name": modified_field, "operator": "isLess",
                  "value": ["ExactDate", str(int(coarse_end.timestamp() * 1000))]},
             ]}}
    params = {"page_size": PAGE_SIZE}
    if scan_cursor:
        params["page_token"] = scan_cursor
    try:
        response = sync._call(target, "POST", "/records/search", params=params, data=query)
    except sync.SyncError as exc:
        if not scan_cursor or exc.status != 502:
            raise
        # A saved Feishu cursor can expire between runs.
        scan_cursor = None
        response = sync._call(target, "POST", "/records/search", params={"page_size": PAGE_SIZE}, data=query)

    rows = response.get("items")
    more = response.get("has_more")
    if rows is None and more is False and response.get("total", 0) == 0 and not scan_cursor:
        rows = []
    cursor = response.get("page_token") if more else None
    if (not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(more) is not bool
            or more and (not rows or not isinstance(cursor, str) or not cursor or cursor == scan_cursor)):
        raise sync.SyncError("飞书工单关闭候选分页无法核对。")

    def inactive(cells):
        return _inactive(cells, cutoff, status_field=status_field, created_field=created_field,
                         updated_field=updated_field, modified_field=modified_field, source_field=source_field)

    candidates = {}
    seen_keys = set()
    seen_ids = set()
    for row in rows:
        cells = row.get("fields") if isinstance(row, dict) else None
        record_id = row.get("record_id") if isinstance(row, dict) else None
        if (not isinstance(cells, dict) or not isinstance(record_id, str)
                or not sync._RECORD_ID.fullmatch(record_id) or record_id in seen_ids):
            raise sync.SyncError("飞书工单关闭候选记录无法核对。", 409)
        seen_ids.add(record_id)
        key = sync._value(cells.get(key_field))
        if not isinstance(key, str) or not _TICKET_ID.fullmatch(key) or int(key) > 2 ** 63 - 1:
            continue
        if key in seen_keys:
            raise sync.SyncError("飞书关闭候选存在重复工单编号。", 409)
        seen_keys.add(key)
        if (row.get("last_modified_time") != cells.get(modified_field) or not inactive(cells)
                or is_pending(cells) is not False or (eligible is not None and eligible(key, cells) is not True)):
            continue
        candidates[key] = row

    if not candidates:
        return {"closed": 0, "skipped": len(rows), "inspected": len(rows), "next_cursor": cursor,
                "more_work": more}

    current = sync._existing(lookup, candidates)
    expected = {}
    changes = []
    for key, candidate in candidates.items():
        fresh = current.get(key)
        if fresh is None or fresh["record_id"] != candidate["record_id"]:
            continue
        candidate_cells = candidate["fields"]
        fresh_cells = fresh["fields"]
        if any(sync._value(candidate_cells.get(name)) != sync._value(fresh_cells.get(name)) for name in projected):
            continue
        expected[key] = fresh["record_id"]
        changes.append({"record_id": fresh["record_id"], "fields": {status_field: CLOSED_STATUS, CLOSED_AT: now_ms}})

    if changes:
        sync._batch(target, "update", changes)
        verified = sync._existing(lookup, expected)
        if any(key not in verified or verified[key]["record_id"] != record_id
               or sync._value(verified[key]["fields"].get(status_field)) != CLOSED_STATUS
               or sync._value(verified[key]["fields"].get(CLOSED_AT)) != now_ms
               for key, record_id in expected.items()):
            raise sync.SyncError("飞书未回读到完整工单关闭结果，请重试。")
        # A write removes rows from the filtered result. Start from its new first page next time.
        cursor = None

    return {"closed": len(changes), "skipped": len(rows) - len(changes), "inspected": len(rows),
            "next_cursor": cursor, "more_work": more}
