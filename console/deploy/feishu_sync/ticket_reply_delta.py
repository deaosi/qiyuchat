"""Read Feishu ticket records by their system modification time."""

from src import voc_feishu_sync as sync


TIME_FIELD = "飞书最后修改时间"
TIME_FIELD_TYPE = 1002
PAGE_SIZE = 100


def read(target, *, since_ms, until_ms, field_names, time_field=TIME_FIELD, filters=None):
    """Yield records in the inclusive time window. The caller checks the field type."""
    if type(since_ms) is not int or type(until_ms) is not int or not 0 <= since_ms <= until_ms:
        raise ValueError("飞书工单修改时间范围无效。")
    data = {
        "field_names": list(dict.fromkeys([*field_names, time_field])),
        "automatic_fields": True,
        "sort": [{"field_name": time_field, "desc": True}],
    }
    if filters is not None:
        data["filter"] = filters
    params = {"page_size": PAGE_SIZE}
    seen_cursors = set()
    seen_records = set()
    previous = None
    while True:
        response = sync._call(target, "POST", "/records/search", params=params, data=data)
        rows = response.get("items")
        more = response.get("has_more")
        if rows is None and more is False and response.get("total", 0) == 0 and not seen_cursors:
            rows = []
        if not isinstance(rows, list) or type(more) is not bool or (not rows and more):
            raise sync.SyncError("飞书工单修改时间分页返回不完整。")

        page = []
        for row in rows:
            fields = row.get("fields") if isinstance(row, dict) else None
            record_id = row.get("record_id") if isinstance(row, dict) else None
            stamp = row.get("last_modified_time") if isinstance(row, dict) else None
            field_stamp = fields.get(time_field) if isinstance(fields, dict) else None
            if (not isinstance(record_id, str) or not sync._RECORD_ID.fullmatch(record_id)
                    or type(stamp) is not int or stamp <= 0 or type(field_stamp) is not int or field_stamp != stamp):
                raise sync.SyncError("无法核对飞书工单系统修改时间。", 409)
            if record_id in seen_records or (previous is not None and stamp > previous):
                raise sync.SyncError("飞书工单分页顺序发生变化，请重试。", 409)
            seen_records.add(record_id)
            previous = stamp
            if since_ms <= stamp <= until_ms:
                page.append(row)

        yield from page
        # Several pages can share one timestamp. Only stop after the cutoff is crossed.
        if not more or (previous is not None and previous < since_ms):
            return
        cursor = response.get("page_token")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise sync.SyncError("飞书工单修改时间分页无法继续。")
        seen_cursors.add(cursor)
        params = {"page_size": PAGE_SIZE, "page_token": cursor}
