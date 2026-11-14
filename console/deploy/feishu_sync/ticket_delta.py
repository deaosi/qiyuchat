"""Read a complete, bounded window of ticket changes from the Qiyu OpenAPI."""

from src.config import Settings
from src.qiyu.ticket import PAGE_SIZE, TicketClient, TicketError, _id
from src.web.tickets import _linked_sessions, _millis, _public


_MAX_WINDOW_MS = 90 * 24 * 60 * 60 * 1000


def _read_window(client: TicketClient, bounds: dict[str, int]) -> dict[int, dict]:
    records: dict[int, dict] = {}
    expected_total = None
    while True:
        offset = len(records)
        total, rows = client.search(offset=offset, limit=PAGE_SIZE, bounds=bounds)
        if expected_total is None:
            expected_total = total
        if total != expected_total or offset + len(rows) > total or (offset < total and not rows):
            raise TicketError("七鱼工单增量分页数量不一致，稍后重试。")
        for row in rows:
            identifier = _id(row.get("id"))
            if identifier is None or identifier in records:
                raise TicketError("七鱼工单增量分页存在无法唯一匹配的编号，稍后重试。")
            records[identifier] = row
        if len(records) == total:
            return records


def read(cfg: Settings, start_ms: int, end_ms: int) -> tuple[list[dict], dict]:
    """Collect created and operated tickets. The caller owns the sync checkpoint."""
    if (type(start_ms) is not int or type(end_ms) is not int or _millis(start_ms) is None
            or _millis(end_ms) is None or not 0 <= end_ms - start_ms < _MAX_WINDOW_MS):
        raise ValueError("七鱼工单增量时间范围无效。")

    client = TicketClient(cfg.qiyu)
    created = _read_window(client, {"start": start_ms, "end": end_ms})
    operated = _read_window(client, {"opStart": start_ms, "opEnd": end_ms})
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
