"""Backfill the Qiyu detail columns on existing Feishu ticket records."""

from dataclasses import replace

from deploy.feishu_sync import support, ticket_archive, ticket_history, tickets, top
from src import voc_feishu_sync as sync
from src.qiyu.ticket import TicketClient, TicketError, _id


def _page(target, limit, after):
    key_column = tickets._FIELDS["key"]
    query = {
        "field_names": [key_column, tickets._FIELDS["source"]],
        "sort": [{"field_name": key_column, "desc": False}],
        "filter": {"conjunction": "and", "conditions": [
            {"field_name": ticket_archive.CLOSED_AT, "operator": "isEmpty", "value": []},
        ]},
    }
    params = {"page_size": limit}
    if after is not None:
        params["page_token"] = after
    result = sync._call(target, "POST", "/records/search", params=params, data=query)
    rows = result.get("items")
    more = result.get("has_more")
    if rows is None and more is False and result.get("total", 0) == 0 and after is None:
        rows = []
    cursor = result.get("page_token") if more else None
    if (not isinstance(rows, list) or len(rows) > limit or type(more) is not bool
            or more and (not rows or not isinstance(cursor, str) or not cursor or cursor == after)):
        raise sync.SyncError("飞书工单补齐分页无法核对。", 409)
    return rows, cursor


def run(state_dir, *, limit=20, after=None):
    """Refresh one page of existing, unarchived tickets without creating records."""
    if type(limit) is not int or not 1 <= limit <= tickets.MATCH_SIZE:
        raise ValueError("飞书工单补齐单页数量无效。")
    if after is not None and (not isinstance(after, str) or not after or len(after) > 4096):
        raise ValueError("飞书工单补齐分页游标无效。")
    support._safe_state(state_dir)
    cfg = tickets.settings()
    if tickets.maintenance(cfg):
        return {"status": "maintenance"}
    if cfg.runtime.mock:
        raise ValueError("生产侧工单数据未配置。")
    report = support.target_table(state_dir)
    with sync._write_lock(report.lock_path.with_name("ticket-table.lock")):
        table_id = top._find_table(report, tickets.TABLE_NAME)
        if table_id is None:
            raise sync.SyncError("找不到配置中的七鱼工单表。", 409)
        key_column, source_column = tickets._FIELDS["key"], tickets._FIELDS["source"]
        fields = {"key": key_column, "source": source_column, "closed": ticket_archive.CLOSED_AT, **tickets._DETAIL}
        target = replace(report, table_id=table_id, fields=fields, destination=top._destination(report, table_id))
        rows, cursor = _page(target, limit, after)
        candidates = {}
        record_ids = set()
        for row in rows:
            cells = row.get("fields") if isinstance(row, dict) else None
            record_id = row.get("record_id") if isinstance(row, dict) else None
            if (not isinstance(cells, dict) or not isinstance(record_id, str)
                    or not sync._RECORD_ID.fullmatch(record_id) or record_id in record_ids):
                raise sync.SyncError("飞书补齐工单记录无法核对。", 409)
            record_ids.add(record_id)
            key = sync._value(cells.get(key_column))
            source = sync._value(cells.get(source_column))
            if (not isinstance(key, str) or not tickets._ID.fullmatch(key) or _id(key) is None
                    or not isinstance(source, dict) or not tickets.ticket_guest.matches(cfg, key, source.get("link"))):
                continue
            if key in candidates:
                raise sync.SyncError("飞书补齐工单编号重复。", 409)
            candidates[key] = record_id, source["link"]

        client = TicketClient(cfg.qiyu)
        names = tickets._private_names(cfg, {}) if candidates else None
        desired = {}
        missing = failed = 0
        for key, (_, link) in candidates.items():
            try:
                raw = client.detail(int(key))
                logs = client.history(int(key))
            except TicketError as error:
                missing += error.http_status == 404
                failed += error.http_status != 404
                continue
            desired[key] = {**tickets._summary(raw, names, source_link=link),
                            **ticket_history.project(key, logs, submitter_id=raw.get("staffId"))}
        if tickets.maintenance(cfg):
            return {"status": "maintenance"}

        existing = sync._existing(target, desired) if desired else {}
        selected = {}
        for key, detail in desired.items():
            expected_id, expected_link = candidates[key]
            current = existing.get(key)
            if not current or current["record_id"] != expected_id:
                continue
            cells = current["fields"]
            source = sync._value(cells.get(source_column))
            if (sync._value(cells.get(ticket_archive.CLOSED_AT)) or not isinstance(source, dict)
                    or source.get("link") != expected_link):
                continue
            selected[key] = detail
        _, updated = tickets._upsert(target, selected, existing=existing)
        return {
            "status": "partial" if failed else "published", "inspected": len(rows), "updated": updated,
            "unchanged": len(selected) - updated, "skipped": len(rows) - len(selected) - missing - failed,
            "missing": missing, "failed": failed, "retry": bool(failed), "next_cursor": after if failed else cursor,
            "url": target.destination,
        }
