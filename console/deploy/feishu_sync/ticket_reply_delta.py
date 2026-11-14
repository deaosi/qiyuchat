"""Read Feishu ticket records by their system modification time."""

from src import voc_feishu_sync as sync


TIME_FIELD = "飞书最后修改时间"
TIME_FIELD_TYPE = 1002
PAGE_SIZE = 100
PAGE_BUDGET = 4


def _query(since_ms, until_ms, field_names, time_field, filters):
    if type(since_ms) is not int or type(until_ms) is not int or not 0 <= since_ms <= until_ms:
        raise ValueError("飞书工单修改时间范围无效。")
    data = {
        "field_names": list(dict.fromkeys([*field_names, time_field])),
        "automatic_fields": True,
        "sort": [{"field_name": time_field, "desc": True}],
    }
    if filters is not None:
        data["filter"] = filters
    return data


def _page(response, first_page):
    rows = response.get("items")
    more = response.get("has_more")
    if rows is None and more is False and response.get("total", 0) == 0 and first_page:
        rows = []
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(more) is not bool or (not rows and more):
        raise sync.SyncError("飞书工单修改时间分页返回不完整。")
    return rows, more


def _window(rows, since_ms, until_ms, time_field, previous, seen_records):
    page = []
    ids = []
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
        ids.append(record_id)
        previous = stamp
        if since_ms <= stamp <= until_ms:
            page.append(row)
    return page, previous, ids


def _next_token(response, seen_cursors):
    token = response.get("page_token")
    if not isinstance(token, str) or not token or token in seen_cursors:
        raise sync.SyncError("飞书工单修改时间分页无法继续。")
    seen_cursors.add(token)
    return token


def read(target, *, since_ms, until_ms, field_names, time_field=TIME_FIELD, filters=None):
    """Yield records in the inclusive time window. The caller checks the field type."""
    data = _query(since_ms, until_ms, field_names, time_field, filters)
    params = {"page_size": PAGE_SIZE}
    seen_cursors = set()
    seen_records = set()
    previous = None
    while True:
        response = sync._call(target, "POST", "/records/search", params=params, data=data)
        rows, more = _page(response, first_page=not seen_cursors)
        page, previous, _ = _window(rows, since_ms, until_ms, time_field, previous, seen_records)
        yield from page
        # Several pages can share one timestamp. Only stop after the cutoff is crossed.
        if not more or (previous is not None and previous < since_ms):
            return
        params = {"page_size": PAGE_SIZE, "page_token": _next_token(response, seen_cursors)}


def _validated_cursor(cursor, since_ms, until_ms):
    if not isinstance(cursor, dict) or cursor.get("bounds") != [since_ms, until_ms]:
        return None
    token, stamp, ids = cursor.get("token"), cursor.get("stamp"), cursor.get("ids")
    total = cursor.get("total")
    if (not isinstance(token, str) or not token or len(token) > 1024 or type(stamp) is not int
            or stamp <= 0 or stamp < since_ms or not isinstance(ids, list) or not 1 <= len(ids) <= PAGE_SIZE
            or any(not isinstance(row_id, str) or not sync._RECORD_ID.fullmatch(row_id) for row_id in ids)
            or len(set(ids)) != len(ids) or total is not None and (type(total) is not int or total < len(ids))):
        return None
    return cursor


def read_batch(target, *, since_ms, until_ms, field_names, time_field=TIME_FIELD, filters=None, cursor=None):
    """Read up to four pages. Persist the returned cursor only after the rows are processed."""
    data = _query(since_ms, until_ms, field_names, time_field, filters)
    resume = _validated_cursor(cursor, since_ms, until_ms)
    token = resume["token"] if resume else None
    previous = resume["stamp"] if resume else None
    ids = resume["ids"] if resume else []
    total = resume.get("total") if resume else None
    seen_cursors = {token} if token else set()
    seen_records = set(ids)
    result = []
    for _ in range(PAGE_BUDGET):
        params = {"page_size": PAGE_SIZE}
        if token:
            params["page_token"] = token
        try:
            response = sync._call(target, "POST", "/records/search", params=params, data=data)
            rows, more = _page(response, first_page=not token)
            next_total = response.get("total")
            if next_total is not None and (type(next_total) is not int or next_total < len(rows)):
                raise sync.SyncError("飞书工单修改时间记录总数无法核对。")
            if total is not None and next_total != total:
                raise sync.SyncError("飞书工单修改时间分页范围发生变化，请重试。", 409)
            page, previous, ids = _window(rows, since_ms, until_ms, time_field, previous, seen_records)
            if not more or (previous is not None and previous < since_ms):
                return result + page, None
            token = _next_token(response, seen_cursors)
        except sync.SyncError as exc:
            if resume is None or exc.status not in (409, 502):
                raise
            # Retry a rejected or detached saved cursor from the original window's start.
            resume = token = previous = total = None
            seen_records.clear()
            seen_cursors.clear()
            continue
        resume = None
        total = next_total
        result.extend(page)
    return result, {"bounds": [since_ms, until_ms], "token": token, "stamp": previous, "ids": ids, "total": total}
