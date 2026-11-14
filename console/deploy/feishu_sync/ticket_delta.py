"""Read a complete, bounded window of ticket changes from the Qiyu OpenAPI."""

from src.config import Settings
from src.qiyu.ticket import PAGE_SIZE, TicketClient, TicketError, _id
from src.web.tickets import _linked_sessions, _millis, _public


_MAX_WINDOW_MS = 90 * 24 * 60 * 60 * 1000
_SLICE_PAGES = 4


def _read_window(client: TicketClient, bounds: dict[str, int], first_page: tuple[int, list[dict]],
                 max_pages: int | None) -> dict[int, dict] | None:
    records: dict[int, dict] = {}
    expected_total, rows = first_page
    total = expected_total
    pages = 1
    while True:
        offset = len(records)
        if total != expected_total or offset + len(rows) > total or (offset < total and not rows):
            raise TicketError("七鱼工单增量分页数量不一致，稍后重试。")
        for row in rows:
            identifier = _id(row.get("id"))
            if identifier is None or identifier in records:
                raise TicketError("七鱼工单增量分页存在无法唯一匹配的编号，稍后重试。")
            records[identifier] = row
        if len(records) == total:
            return records
        if max_pages is not None and pages >= max_pages:
            return None
        total, rows = client.search(offset=len(records), limit=PAGE_SIZE, bounds=bounds)
        pages += 1


def read(cfg: Settings, start_ms: int, end_ms: int, *, max_pages: int | None = None) -> tuple[list[dict], dict]:
    """Collect created and operated tickets. The caller owns the sync checkpoint."""
    if (type(start_ms) is not int or type(end_ms) is not int or _millis(start_ms) is None
            or _millis(end_ms) is None or not 0 <= end_ms - start_ms < _MAX_WINDOW_MS):
        raise ValueError("七鱼工单增量时间范围无效。")
    if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
        raise ValueError("七鱼工单增量分页预算无效。")

    client = TicketClient(cfg.qiyu)
    while True:
        # A single millisecond cannot be subdivided, so drain all its pages.
        page_budget = max_pages if end_ms > start_ms else None
        creation_bounds = {"start": start_ms, "end": end_ms}
        operation_bounds = {"opStart": start_ms, "opEnd": end_ms}
        first_created = client.search(offset=0, limit=PAGE_SIZE, bounds=creation_bounds)
        if page_budget is not None and first_created[0] > page_budget * PAGE_SIZE:
            end_ms = (start_ms + end_ms) // 2
            continue
        first_operated = client.search(offset=0, limit=PAGE_SIZE, bounds=operation_bounds)
        if page_budget is not None and first_operated[0] > page_budget * PAGE_SIZE:
            end_ms = (start_ms + end_ms) // 2
            continue
        created = _read_window(client, creation_bounds, first_created, page_budget)
        if created is not None:
            operated = _read_window(client, operation_bounds, first_operated, page_budget)
            if operated is not None:
                break
        end_ms = (start_ms + end_ms) // 2

    records = dict(created)
    for identifier, row in operated.items():
        previous = records.get(identifier)
        if previous is None or (_millis(row.get("modifyTime")) or 0) >= (_millis(previous.get("modifyTime")) or 0):
            records[identifier] = row

    links = _linked_sessions(cfg, set(records))
    items = []
    for identifier, row in records.items():
        item = _public(row, len(links.get(identifier, ())), submitter="")
        item["submitter_id"] = _id(row.get("staffId")) or _id(created.get(identifier, {}).get("staffId"))
        items.append(item)
    return items, {
        "created_period": [start_ms, end_ms], "updated_period": [start_ms, end_ms],
        "created_scope": "qiyu", "updated_scope": "qiyu",
        "created_matches": len(created), "updated_matches": len(operated),
    }


def read_slice(cfg: Settings, start_ms: int, end_ms: int) -> tuple[list[dict], dict]:
    """Collect a complete earliest slice and report its inclusive end in coverage."""
    return read(cfg, start_ms, end_ms, max_pages=_SLICE_PAGES)
