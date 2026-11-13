"""Shared validation and recovery helpers for the scheduled full VOC export."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from http.client import HTTPException, HTTPSConnection
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from src import voc_feishu_sync as sync
from src.web import guest_evidence


KINDS = ("topics", "sops")
MAX_JOURNAL = 8_000_000
MAX_GUEST_BYTES = 5_000_000
GUEST_DATA = re.compile(rb'<script id="voc-guest-data" type="application/json">(.*?)</script>')


def target_table(state_dir: Path) -> sync.Target:
    target = sync.configured_target()
    metadata_path = os.environ.get("YUHAI_FEISHU_SYNC_TARGET_METADATA", "").strip()
    metadata = _read_state(Path(metadata_path) if metadata_path else state_dir / "target-metadata.json")
    if target is None or (target.base_token, target.table_id) != (metadata["base_token"], metadata["table_id"]):
        raise ValueError("飞书配置与已核对的多维表格目标不一致。")
    return target



def _safe_state(path):
    if path.is_symlink():
        raise ValueError("同步状态目录不能是链接。")
    if path.exists() and not path.is_dir():
        raise ValueError("同步状态目录不可用。")

def _write_state(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("同步状态文件不能是链接。")
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_JOURNAL:
        raise ValueError("同步状态内容超出本次写入上限。")
    fd, name = tempfile.mkstemp(prefix=".voc-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name != "nt":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def _read_state(path: Path):
    if path.is_symlink() or path.stat().st_size > MAX_JOURNAL:
        raise ValueError("同步状态文件不可用。")
    return read_json(path)

def _publication(value: dict, *, full_links=True):
    if not isinstance(value, dict) or value.get("filters") != {"days": 7} or not isinstance(value.get("documents"), dict):
        raise ValueError("仅同步已授权的近七天全部客户群和客服。")
    for kind in KINDS:
        doc = value["documents"].get(kind)
        if not isinstance(doc, dict):
            raise ValueError("缺少发布清单。")
        records = doc.get("records")
        if not isinstance(records, list) or len(records) != doc.get("count") or not 0 <= len(records) <= 20:
            raise ValueError("发布清单条数无效。")
        if kind == "topics" and len(records) != 20:
            raise ValueError("高频排行尚不足 20 项，已保留飞书中上次确认的结果。")
        keys = set()
        for row in records:
            if (not isinstance(row, dict) or not isinstance(row.get("key"), str)
                    or not sync._SOURCE_ID[kind].fullmatch(row["key"]) or row["key"] in keys
                    or not isinstance(row.get("url"), str) or (full_links or row["url"]) and not sync._GUEST_URL.fullmatch(row["url"])):
                raise ValueError("发布清单包含无效编号或访客链接。")
            keys.add(row["key"])
    topic, sop = [value["documents"][kind] for kind in KINDS]
    if (topic["start"], topic["end"]) != (sop["start"], sop["end"]):
        raise ValueError("高频和 SOP 发布范围不一致。")
    if "marker" in value and not (_valid_marker(value["marker"]) or _old_marker(value["marker"])):
        raise ValueError("小时整理标记无法核对。")
    for field in ("topic_row_ids", "previous_topic_row_ids"):
        if field in value:
            ids = value[field]
            if (not isinstance(ids, dict) or len(ids) != 20 or any(
                    not isinstance(item, str) or not sync._RECORD_ID.fullmatch(item) for item in ids.values())
                    or len(set(ids.values())) != len(ids)):
                raise ValueError("飞书高频问题的记录 ID 清单无效。")
    topic_document(value)
    return value

def _valid_marker(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:(?:00|30)\+08:00", value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True

def _old_marker(value):
    # State from the earlier invocation-based runner is still readable. Its
    # marker never counts as a completed half-hour slot.
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f-]{36}:[0-9a-f]{32}", value))

def _schema(target):
    fields = sync._paginated(target, "GET", "/fields")
    types = {row.get("field_name"): row.get("type") for row in fields if isinstance(row, dict)}
    if all(types.get(name) in sync._FIELD_TYPES.get(role, (1,)) for role, name in target.fields.items()):
        return types
    roles = {role for role, name in target.fields.items() if name in types}
    if roles == {"title", "detail", "summary", "source"} and all(
            types[target.fields[role]] in sync._FIELD_TYPES.get(role, (1,)) for role in roles):
        return types
    raise ValueError("飞书字段与已核对的同步表结构不一致。")

def _compact(target, schema):
    return target.fields["key"] not in schema

def _table_fields(doc, kind, filters, target, schema, *, compact_text=False):
    if not doc["records"]:
        return {}
    rows = sync._rows(doc, kind, filters, target, schema)
    if not _compact(target, schema):
        return rows
    detail = target.fields["detail"]
    if compact_text and kind == "topics":
        scope = sync._scope_key(filters, doc)
        for record in doc["records"]:
            fields = rows[f'yuhai-voc:{record["key"]}:{scope}']
            _, _, questions = fields[detail].partition("\n具体问法\n")
            question = record.get("representative_question")
            if not isinstance(question, str) or question == record["title"]:
                question = ""
            fields[detail] = questions or question or "具体咨询与会话上下文见访客页。"
    return {key: {name: item for name, item in fields.items() if name in schema} for key, fields in rows.items()}

def _mapped(value, kind, target, schema):
    return _table_fields(value["documents"][kind], kind, value["filters"], target, schema,
                         compact_text=value.get("compact_text") is True)

def _published_sops(value, target, schema):
    inventory = value.get("published_sops")
    if inventory is None:
        return _mapped(value, "sops", target, schema)
    if not isinstance(inventory, list):
        raise ValueError("SOP 发布记录无法核对。")
    result = {}
    for doc in inventory:
        if not isinstance(doc, dict) or doc.get("count") != 1:
            raise ValueError("SOP 发布记录无法核对。")
        result.update(_table_fields(doc, "sops", value["filters"], target, schema))
    return result

def _sop_inventory(last, stage):
    inventory = {}
    for publication in (last, stage):
        doc = publication["documents"]["sops"]
        for item in publication.get("published_sops", []):
            inventory[item["records"][0]["key"]] = item
        for row in doc["records"]:
            inventory[row["key"]] = {"count": 1, "start": doc["start"], "end": doc["end"], "records": [row]}
    return list(inventory.values())

def _period(value):
    return value["documents"]["topics"]["start"], value["documents"]["topics"]["end"]

def _matches(target, expected, current):
    return all(_same(target, field, current.get(field), value) for field, value in expected.items())

def _sop_links(document):
    links = set()
    for record in document["records"]:
        link = record["url"]
        if link in links:
            raise ValueError("SOP 访客链接重复。")
        payload = guest_page(link)
        expected = guest_evidence._entry({**record, "kind": "sops", "start": document["start"],
                                               "end": document["end"]}, None)
        if expected is None or not isinstance(payload, dict) or guest_evidence._entry(payload.get("entry"), None) != expected:
            raise ValueError("SOP 访客链接未通过免登录与发布范围校验。")
        links.add(link)
    return len(links)

def _source_link(target, fields):
    value = sync._value(fields.get(target.fields["source"]))
    return value.get("link") if isinstance(value, dict) else value

def _compact_matches(target, expected, actual, *, legacy=False):
    if legacy and not sync._value(actual.get(target.fields["title"])):
        expected = {**expected, target.fields["title"]: ""}
    return _matches(target, expected, actual)

def _slot_checked(last, marker, refresh):
    previous = last.get("marker")
    return _valid_marker(previous) and (previous > marker or (previous == marker and not refresh))


def read_json(path):
    with path.open("rb") as handle:
        body = handle.read(MAX_JOURNAL + 1)
    if len(body) > MAX_JOURNAL:
        raise ValueError("输入文件大小超出本次操作范围。")
    return json.loads(body.decode("utf-8-sig"))

def topic_document(value):
    filters = value["filters"]
    if filters != {"days": 7}:
        raise ValueError("本次替换只使用原发布任务的近 7 天、全部客户群和客服范围。")
    document = value["documents"]["topics"]
    start, end = (date.fromisoformat(document[key]) for key in ("start", "end"))
    if (end - start).days != 6 or [start.isoformat(), end.isoformat()] != [document["start"], document["end"]]:
        raise ValueError("发布文档未提供有效的 7 天日期区间。")
    records = document["records"]
    if not isinstance(records, list) or not 1 <= len(records) <= 20 or document["count"] != len(records):
        raise ValueError("发布文档应包含 1 至 20 条高频问题。")
    return filters, document

def guest_page(url):
    parsed = urlsplit(url)
    for attempt in range(2):
        client = HTTPSConnection(parsed.hostname, timeout=15)
        try:
            client.request("GET", parsed.path, headers={"Accept-Encoding": "identity"})
            with client.getresponse() as response:
                if (response.status == 200 and not response.getheader("Set-Cookie") and not response.getheader("Location")
                        and response.getheader("Referrer-Policy", "").split(",")[-1].strip() == "no-referrer"
                        and response.getheader("Cache-Control") == "no-store"
                        and "default-src 'none'" in response.getheader("Content-Security-Policy", "")):
                    body = response.read(MAX_GUEST_BYTES + 1)
                    match = GUEST_DATA.search(body) if len(body) <= MAX_GUEST_BYTES else None
                    return json.loads(match[1]) if match else None
                if response.status not in (429, 502, 503, 504):
                    return None
        except (OSError, HTTPException):
            pass
        finally:
            client.close()
        if attempt == 0:
            time.sleep(0.6)
    return None

def verify_guest_links(document):
    records = document["records"]
    links = [record["url"] for record in records]
    if len(set(links)) != len(links) or any(not sync._GUEST_URL.fullmatch(link) for link in links):
        raise ValueError("本次成果的访客链接重复或格式不正确。")

    def check(record):
        expected = guest_evidence._entry({**record, "kind": "topics", "start": document["start"],
                                          "end": document["end"]}, None)
        try:
            payload = guest_page(record["url"])
            if (expected is None or not isinstance(payload, dict)
                    or guest_evidence._entry(payload.get("entry"), None) != expected):
                return False
            cases, detail, variants = payload["cases"], payload["topic_detail"], payload["topic_variants"]
            if (not isinstance(cases, list) or not isinstance(variants, list) or not variants
                    or detail["topic"]["consultation_count"] != record["consultations"]):
                return False
            used = set()
            for variant in variants:
                indices = variant["case_indices"]
                projection = variant["topic_detail"]
                if (any(type(i) is not int or not 0 <= i < len(cases) or i in used for i in indices)
                        or [event["id"] for event in projection["events"]] != [f"guest-case-{i}" for i in indices]
                        or not projection["topic"]["question"] or projection["topic"]["consultation_count"] < 1
                        or (projection["workbench"]["comparison"]["current"]["start"],
                            projection["workbench"]["comparison"]["current"]["end"]) != (document["start"], document["end"])):
                    return False
                used.update(indices)
            return used == set(range(len(cases)))
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        checked = sum(pool.map(check, records))
    if checked != len(records):
        raise ValueError(f"仅 {checked}/{len(records)} 条访客链接通过免登录、标题及日期范围校验。")
    return checked

def confirm_unchanged(rows, expected):
    if any(record_id not in rows or rows[record_id] != fields for record_id, fields in expected.items()):
        raise ValueError("飞书中有不在更新计划内的记录变化，需重新核对。")


def _same(target, field, first, second):
    values = [sync._value(value) for value in (first, second)]
    if field == target.fields["source"]:
        # A text-column URL may round-trip as a rich-link segment. The
        # visitor URL itself is authoritative; avoid rewriting the same URL.
        values = [value.get("link") if isinstance(value, dict) else value for value in values]
    return values[0] == values[1]
