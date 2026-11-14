"""Create and verify the ranking table within the configured Feishu Base."""

from dataclasses import replace
import json
import re
import subprocess
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from src import voc_feishu_sync as sync


TABLE_NAME = "高频问题 Top 100"
_VIEW_ID = re.compile(r"vew[A-Za-z0-9]{4,77}")
_TABLE_ID = re.compile(r"tbl[A-Za-z0-9]{4,77}")


def _cli(target: sync.Target, command: str, *args: str) -> dict:
    argv = [target.cli, "base", command, "--base-token", target.base_token,
            "--as", "bot", "--format", "json", *args]
    try:
        result = subprocess.run(argv, env=sync._cli_environment(), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise sync.SyncError("飞书数据表操作暂时无法完成。", 503) from None
    value = sync._parse_json(result.stdout)
    if result.returncode or value.get("ok") is not True or value.get("identity") != "bot":
        raise sync._cli_error(sync._parse_json(result.stderr) or value)
    data = value.get("data")
    if not isinstance(data, dict):
        raise sync.SyncError("飞书数据表响应不完整。")
    return data


def _find_table(original: sync.Target, table_name: str) -> str | None:
    result = _cli(original, "+table-list")
    tables = result.get("tables")
    if (not isinstance(tables, list) or result.get("total") != len(tables)
            or not all(isinstance(table, dict) for table in tables)
            or not any(table.get("id") == original.table_id for table in tables)):
        raise sync.SyncError("无法核对当前多维表格及原高频问题表。")
    matches = [table.get("id") for table in tables if table.get("name") == table_name]
    if not matches:
        return None
    if (len(matches) != 1 or not isinstance(matches[0], str)
            or not _TABLE_ID.fullmatch(matches[0]) or matches[0] == original.table_id):
        raise sync.SyncError(f"飞书中存在无法唯一识别的{table_name}数据表。", 409)
    return matches[0]


def _create_table(original: sync.Target, table_name: str, fields: list[dict]) -> str:
    creation_error = None
    try:
        _cli(original, "+table-create", "--name", table_name, "--fields", json.dumps(fields, ensure_ascii=False))
    except sync.SyncError as error:
        creation_error = error
    # A timed-out command may already have created the table. Reconcile by name
    # before any subsequent run can issue another create.
    for delay in (0, 1, 2):
        if delay:
            time.sleep(delay)
        table = _find_table(original, table_name)
        if table:
            return table
    if creation_error:
        raise creation_error
    raise sync.SyncError(f"飞书暂未返回刚创建的{table_name}数据表。", 503)


def _destination(original: sync.Target, table_id: str, view_id: str = "") -> str:
    url = urlsplit(original.destination)
    query = {"table": table_id}
    if view_id:
        query["view"] = view_id
    return urlunsplit(url._replace(query=urlencode(query)))


def _field_rows(target: sync.Target, expected: dict[str, int]) -> dict[str, dict]:
    rows = sync._paginated(target, "GET", "/fields")
    fields = {row.get("field_name"): row for row in rows if isinstance(row, dict)}
    if any(fields.get(name, {}).get("type") != kind for name, kind in expected.items()):
        raise sync.SyncError("高频问题 Top 100 数据表的字段名称或类型与预期不一致。", 409)
    if fields[target.fields["title"]].get("is_primary") is not True:
        raise sync.SyncError("高频问题 Top 100 数据表的主字段应为问题标题。", 409)
    return fields


def _view_id(target: sync.Target) -> str:
    args = ("--table-id", target.table_id, "--limit", "200")
    result = _cli(target, "+view-list", *args)
    rows = result.get("views")
    if not isinstance(rows, list) or not rows or result.get("total") != len(rows):
        raise sync.SyncError("无法核对高频问题 Top 100 数据表的默认视图。")
    first = rows[0]
    view_id = first.get("id") if isinstance(first, dict) else None
    if not isinstance(view_id, str) or not _VIEW_ID.fullmatch(view_id) or first.get("type") != "grid":
        raise sync.SyncError("高频问题 Top 100 的默认视图不是可排序表格。", 409)
    return view_id


def _sorts_by_rank(target: sync.Target, args: tuple[str, ...], rank: dict) -> bool:
    current = _cli(target, "+view-get-sort", *args)
    entries = current.get("sort")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        return False
    entry = entries[0]
    field = entry.get("field") or entry.get("field_id") or entry.get("field_name")
    return bool(field and field in (target.fields["rank"], rank.get("field_id"))
                and entry.get("desc") is False)


def ensure_top(original_target: sync.Target, *, apply: bool) -> sync.Target | None:
    """Find or create the Top 100 table, then confirm its schema and rank sort."""
    original = original_target
    fields = {role: original.fields[role] for role in ("title", "detail", "source", "summary")}
    fields.update(rank="排名", consultations="咨询次数", period="统计区间")
    expected = {name: (2 if role in ("rank", "consultations") else 1) for role, name in fields.items()}
    table_id = _find_table(original, TABLE_NAME)
    if not table_id:
        if not apply:
            return None
        definitions = [{"name": name, "type": "number" if kind == 2 else "text"} for name, kind in expected.items()]
        table_id = _create_table(original, TABLE_NAME, definitions)

    target = replace(original, table_id=table_id, destination=_destination(original, table_id), fields=fields)
    rows = _field_rows(target, expected)
    view_id = _view_id(target)
    args = ("--table-id", table_id, "--view-id", view_id)
    if not _sorts_by_rank(target, args, rows[fields["rank"]]) and apply:
        sort = {"sort_config": [{"field": fields["rank"], "desc": False}]}
        _cli(target, "+view-set-sort", *args, "--json", json.dumps(sort, ensure_ascii=False))
        if not _sorts_by_rank(target, args, rows[fields["rank"]]):
            raise sync.SyncError("飞书未确认高频问题 Top 100 默认视图按排名升序。")
    return replace(target, destination=_destination(original, table_id, view_id))
