"""Publish current VOC output to one configured Feishu table through lark-cli."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from uuid import UUID


_CONFIG_ENV = "YUHAI_FEISHU_SYNC_CONFIG"
_REQUIRED = {"key", "title", "detail"}
_OPTIONAL = {"summary", "source", "kind", "period", "consultations"}
_FIELD_TYPES = {"source": (1, 15), "consultations": (2,)}
_TOKEN = re.compile(r"[A-Za-z0-9]{8,80}")
_RECORD_ID = re.compile(r"rec[A-Za-z0-9]{4,77}")
_SOURCE_ID = {"topics": re.compile(r"t_[0-9a-f]{24}"), "sops": re.compile(r"sop_[0-9a-f]{24}")}
_LABEL = {"topics": "高频问题", "sops": "知识 SOP"}


class SyncError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    cli: str
    base_token: str
    table_id: str
    destination: str
    fields: dict[str, str]
    lock_path: Path

    @property
    def api_path(self) -> str:
        return f"/open-apis/bitable/v1/apps/{self.base_token}/tables/{self.table_id}"


def configured_target() -> Target | None:
    raw_path = os.environ.get(_CONFIG_ENV, "").strip()
    if not raw_path:
        return None
    message = "飞书同步配置无效，请检查 CLI 路径、目标数据表和字段映射。"
    try:
        path = Path(raw_path).resolve(strict=True)
        if path.stat().st_size > 16 * 1024:
            raise ValueError
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(cfg, dict):
            raise ValueError
        cli, base, table, destination, fields = (cfg.get(k) for k in (
            "cli", "base_token", "table_id", "destination", "fields"))
        if not all(isinstance(x, str) for x in (cli, base, table, destination)):
            raise ValueError
        binary = shutil.which(cli)
        if not binary or (os.name == "nt" and Path(binary).suffix.lower() != ".exe"):
            raise ValueError
        if not _TOKEN.fullmatch(base) or not (_TOKEN.fullmatch(table) and table.startswith("tbl")):
            raise ValueError
        parsed = urlsplit(destination)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host.endswith((".feishu.cn", ".larksuite.com"))
                or parsed.netloc != host or not re.fullmatch(r"/(?:wiki|base)/[A-Za-z0-9]{8,80}", parsed.path)
                or parse_qs(parsed.query).get("table") != [table] or parsed.fragment
                or any(char.isspace() or ord(char) < 32 for char in destination)):
            raise ValueError
        if parsed.path.startswith("/base/") and parsed.path.split("/")[-1] != base:
            raise ValueError
        if (not isinstance(fields, dict) or not _REQUIRED <= set(fields) <= _REQUIRED | _OPTIONAL
                or any(not isinstance(name, str) or not name.strip() or len(name) > 100 for name in fields.values())
                or len(set(fields.values())) != len(fields)):
            raise ValueError
        return Target(str(Path(binary).resolve(strict=True)), base, table, destination, dict(fields),
                      path.with_name(path.name + ".lock"))
    except (OSError, ValueError, TypeError, RecursionError):
        raise SyncError(message, 503) from None


@contextmanager
def _write_lock(path: Path):
    """Serialize web and scheduled writes on the same host and config."""
    try:
        lock = path.open("a+b")
    except OSError:
        raise SyncError("飞书同步配置目录不可写，无法锁定发布任务。", 503) from None
    with lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SyncError("已有飞书同步正在运行，请稍后重试。", 409) from None
        yield


def _cli_environment() -> dict[str, str]:
    # Inherit the OS keychain/profile location, not the console's model or admin credentials.
    names = {"PATH", "HOME", "USER", "LOGNAME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
             "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
             "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
             "LARKSUITE_CLI_CONFIG_DIR"}
    return {key: value for key, value in os.environ.items() if key.upper() in names}


def _parse_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _cli_error(envelope: dict) -> SyncError:
    error = envelope.get("error")
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    category = error.get("type")
    if code == 99991672 or category == "authorization":
        return SyncError("飞书应用权限不足，请核对字段读取、记录读取和写入权限及目标表权限。", 403)
    if category in ("authentication", "config"):
        return SyncError("飞书 CLI 应用身份不可用，请检查运行账户的飞书 CLI 配置。", 503)
    if code in (1254290, 1254291, 1255040) or category in ("rate_limit", "network"):
        return SyncError("飞书暂时无法同步，请稍后重新发起；系统会重新匹配已有记录。", 503)
    return SyncError("飞书 CLI 返回失败，请检查应用权限和目标表配置。")


def _call(target: Target, method: str, resource: str, *, params=None, data=None) -> dict:
    args = [target.cli, "api", method, target.api_path + resource, "--as", "bot", "--format", "json"]
    with TemporaryDirectory(prefix="yuhai-feishu-") as folder:
        for flag, value in (("--params", params), ("--data", data)):
            if value is not None:
                name = "params.json" if flag == "--params" else "body.json"
                (Path(folder) / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                args.extend([flag, "@" + name])
        try:
            process = subprocess.run(args, cwd=folder, env=_cli_environment(), stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=25, check=False)
        except subprocess.TimeoutExpired:
            raise SyncError("飞书响应超时，请稍后重试；系统会重新匹配已有记录。", 504) from None
        except OSError:
            raise SyncError("飞书 CLI 启动失败，请检查同步配置。", 503) from None
    result = _parse_json(process.stdout)
    if process.returncode or result.get("ok") is not True or result.get("identity") != "bot":
        raise _cli_error(_parse_json(process.stderr) or result)
    result = result.get("data")
    if not isinstance(result, dict):
        raise SyncError("飞书响应不完整，请核对记录后重试。")
    return result


def _paginated(target: Target, method: str, resource: str, *, data=None) -> list:
    rows = []
    page_token = None
    for _ in range(10):
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        page = _call(target, method, resource, params=params, data=data)
        items = page.get("items", [])
        if not isinstance(items, list) or not isinstance(page.get("has_more"), bool):
            break
        rows.extend(items)
        if not page["has_more"]:
            return rows
        token = page.get("page_token")
        if not isinstance(token, str) or not token or token == page_token:
            break
        page_token = token
    raise SyncError("飞书分页结果不完整，无法确认目标记录。")


def _schema(target: Target) -> dict[str, int]:
    fields = _paginated(target, "GET", "/fields")
    types = {row.get("field_name"): row.get("type") for row in fields if isinstance(row, dict)}
    if any(types.get(name) not in _FIELD_TYPES.get(role, (1,)) for role, name in target.fields.items()):
        raise SyncError("飞书字段映射与目标表不一致。咨询次数须使用数字字段，来源可使用文本或超链接字段，其余使用文本字段。", 409)
    return types


def _scope_key(filters: dict, document: dict) -> str:
    days = filters.get("days")
    period = (["days", days] if type(days) is int and days > 0 and not filters.get("start") and not filters.get("end")
              else ["dates", document["start"], document["end"]])
    scope = [period, filters.get("group") or "", filters.get("staff") or ""]
    return sha256(json.dumps(scope, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]


def _rows(document: dict, kind: str, filters: dict, target: Target, types: dict) -> dict[str, dict]:
    if kind not in _SOURCE_ID:
        raise SyncError("未知飞书成果类型。", 400)
    records = document.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 20 or document.get("count") != len(records):
        raise SyncError("当前范围没有可发布的成果，或预览已失效。", 409)
    period = f'{document["start"]} 至 {document["end"]}'
    scope = _scope_key(filters, document) if kind == "topics" else ""
    mapped = {}
    for item in records:
        if (not isinstance(item, dict)
                or any(not isinstance(item.get(field), str) for field in ("key", "title", "detail", "summary", "url"))
                or not _SOURCE_ID[kind].fullmatch(item["key"]) or not item["title"] or not item["detail"]):
            raise SyncError("成果数据不完整，请重新预览。", 409)
        key = "yuhai-voc:" + item["key"] + (":" + scope if scope else "")
        if key in mapped:
            raise SyncError("成果中存在重复编号，请重新预览。", 409)
        values = {**item, "key": key, "kind": _LABEL[kind], "period": period, "source": item["url"]}
        if "consultations" in target.fields:
            count = item.get("consultations") if kind == "topics" else None
            if kind == "topics" and (type(count) is not int or count < 1):
                raise SyncError("高频问题缺少有效咨询次数，请重新预览。", 409)
            values["consultations"] = count
        if "source" in target.fields and types[target.fields["source"]] == 15:
            values["source"] = {"text": "查看来源", "link": item["url"]} if item["url"] else None
        mapped[key] = {name: values[role] for role, name in target.fields.items()}
    return mapped


def _value(cell):
    if isinstance(cell, list) and len(cell) == 1 and isinstance(cell[0], dict) and "link" in cell[0]:
        return _value(cell[0])
    if isinstance(cell, list) and all(isinstance(part, dict) and isinstance(part.get("text"), str) for part in cell):
        return "".join(part["text"] for part in cell)
    if isinstance(cell, dict) and "link" in cell:
        return {name: cell.get(name) for name in ("text", "link")}
    return cell if cell is not None else ""


def _existing(target: Target, desired: dict[str, dict]) -> dict[str, dict]:
    key_name = target.fields["key"]
    rows = _paginated(target, "POST", "/records/search", data={
        "field_names": list(target.fields.values()),
        "filter": {"conjunction": "or", "conditions": [
            {"field_name": key_name, "operator": "is", "value": [key]} for key in desired
        ]},
    })
    found = {}
    for row in rows:
        fields = row.get("fields") if isinstance(row, dict) else None
        record_id = row.get("record_id") if isinstance(row, dict) else None
        key = _value(fields.get(key_name)) if isinstance(fields, dict) else None
        if (not isinstance(key, str) or key not in desired or not isinstance(record_id, str)
                or not _RECORD_ID.fullmatch(record_id)):
            raise SyncError("飞书返回了无法确认来源的记录，已停止同步。", 409)
        if key in found:
            raise SyncError("飞书表内存在重复的语海Voc编号，请合并重复记录后再同步。", 409)
        found[key] = row
    return found


def _batch(target: Target, operation: str, rows: list[dict]) -> None:
    if not rows:
        return
    body = {"records": rows}
    params = None
    if operation == "create":
        # An identical create retry also uses Feishu's request-level idempotency.
        payload = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        fingerprint = sha256(f"{target.api_path}\n{payload}".encode()).digest()
        params = {"client_token": str(UUID(bytes=fingerprint[:16], version=4))}
    result = _call(target, "POST", "/records/batch_" + operation, params=params, data=body)
    records = result.get("records")
    if (not isinstance(records, list) or len(records) != len(rows)
            or any(not isinstance(row, dict) or not isinstance(row.get("record_id"), str)
                   or not _RECORD_ID.fullmatch(row["record_id"]) for row in records)
            or len({row["record_id"] for row in records}) != len(rows)
            or operation == "update" and {row["record_id"] for row in records} != {row["record_id"] for row in rows}):
        raise SyncError("飞书未确认全部记录，请重试以重新核对现有记录。")


def publish(document: dict, kind: str, filters: dict, *, target: Target | None = None) -> dict:
    target = target or configured_target()
    if not target:
        raise SyncError("飞书同步尚未配置。", 503)
    with _write_lock(target.lock_path):
        desired = _rows(document, kind, filters, target, _schema(target))
        found = _existing(target, desired)
        inserts, updates = [], []
        for key, fields in desired.items():
            previous = found.get(key)
            if previous is None:
                inserts.append({"fields": fields})
            else:
                changes = {name: value for name, value in fields.items()
                           if _value(previous["fields"].get(name)) != _value(value)}
                if changes:
                    updates.append({"record_id": previous["record_id"], "fields": changes})
        _batch(target, "update", updates)
        _batch(target, "create", inserts)
        if inserts or updates:
            confirmed = _existing(target, desired)
            if set(confirmed) != set(desired) or any(
                _value(confirmed[key]["fields"].get(name)) != _value(value)
                for key, fields in desired.items() for name, value in fields.items()
            ):
                raise SyncError("飞书尚未回读到完整成果，请重试以重新核对现有记录。")
    return {"url": target.destination, "title": document["title"], "reused": not inserts and not updates,
            "count": len(desired), "created": len(inserts), "updated": len(updates)}


def main(argv: list[str] | None = None) -> int:
    from .config import settings
    from .voc import VocError
    from .voc_feishu_content import build_document

    parser = argparse.ArgumentParser(description="Publish a VOC snapshot to the configured Feishu Base table.")
    parser.add_argument("--kind", choices=_LABEL, required=True)
    parser.add_argument("--days", type=int)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--apply", action="store_true", help="Write to Feishu (default: local preview only)")
    args = parser.parse_args(argv)
    if bool(args.start) != bool(args.end) or (args.start and args.days is not None):
        parser.error("choose --days or both --start and --end")
    filters = {"days": 0 if args.start else (7 if args.days is None else args.days),
               "start": args.start or "", "end": args.end or ""}
    try:
        document = build_document(settings(), filters, args.kind)
        result = (publish(document, args.kind, filters) if args.apply and document["count"] else
                  {"title": document["title"], "start": document["start"], "end": document["end"],
                   "count": document["count"], "total": document["total"], "published": False})
    except (SyncError, VocError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
