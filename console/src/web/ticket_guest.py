"""Read-only, opaque links for a single Qiyu ticket."""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from html import escape
from pathlib import Path
import re
from secrets import token_urlsafe
import sqlite3
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from ..config import Settings
from ..qiyu.ticket import TicketClient, TicketError, _id
from ..voc_assets import connection, transaction
from . import case_media, case_senders, guest_evidence, ticket_media, tickets


PUBLIC_PATH = "/guest/ticket/"
TOKEN_PATTERN = guest_evidence.TOKEN_PATTERN
IMAGE_PATTERN = r"[a-f0-9]{64}"
_TOKEN = re.compile(TOKEN_PATTERN)
_IMAGE = re.compile(IMAGE_PATTERN)
_FIELDS = {"平台&版本", "平台", "软件版本", "是否已点击报告问题", "问题需求", "附件/截图", "附件／截图"}
_TEST_PLACEHOLDER = re.compile(r"(?:测试|test|demo|sample)(?:[-_ ]?[0-9]{1,3})?", re.I)
_SOFTWARE_VERSION = re.compile(r"[vV]?\d{1,4}(?:\.\d{1,4}){1,3}")
_REPORT_VALUES = {"是", "否", "已点击", "未点击", "yes", "no", "true", "false"}
_BEARER_TOKEN = re.compile(r"\b(Bearer)[ \t]+[A-Za-z0-9_./+~=-]{8,}", re.I)
_DISPLAY_LABELS = {"客户群": "客户群名称", "客户群名称": "客户群名称", "店铺名": "店铺名",
                   "店铺名称": "店铺名", "商家名称": "商家名称"}
_IDENTIFIER_KEYS = ("id", "userId", "externalUserId", "staffId", "holderId", "creatorId", "applyStaffId",
                    "groupId", "staffAccount", "userMobile", "userEmail", "crmId", "crmForeignId",
                    "crmUserEmail", "crmUserPhone", "connectionId")
_USER_ACCOUNT = re.compile(r"(?<![a-z0-9_-])(?:wxid_[a-z0-9_-]+|w[mo][a-z0-9_-]{16,})(?![a-z0-9_-])", re.I)
_LABELED_SECRET = re.compile(
    r"""(密码|口令|密钥|验证码|token\b|api[_ -]?key\b)\s*[:：=]\s*(?:"[^"\n]+"|'[^'\n]+'|[^\s，。；！？、<>'"]+)""",
    re.I,
)
_REDACTION = re.compile(r"\[(?:名称|链接|标识)已隐藏\]|\[(?:详细地址)?已脱敏\]")
_MAX_IMAGES = 16
_CACHE_SECONDS = 30
_CACHE_LIMIT = 128
_CACHE_LOCK = Lock()
_cache = OrderedDict()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_guest_link (
    ticket_id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE
);
"""


def _registry(cfg: Settings) -> Path:
    return Path(cfg.runtime.state_dir).resolve() / "voc-ticket-guest.db"


def url_for(cfg: Settings, ticket_id: str) -> str:
    """Publish a stable capability URL for a ticket already selected by the caller."""
    number = _id(ticket_id)
    if number is None or str(number) != ticket_id:
        raise ValueError("工单访客链接的工单 ID 无效。")
    path = _registry(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError("工单访客链接存储路径无效。")
        path.touch(mode=0o600, exist_ok=True)
        with connection(path, write=True) as db:
            db.execute(_SCHEMA)
            with transaction(db):
                db.execute("INSERT INTO ticket_guest_link VALUES (?, ?) ON CONFLICT(ticket_id) DO NOTHING",
                           (ticket_id, token_urlsafe(32)))
                token = db.execute("SELECT token FROM ticket_guest_link WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
    except (sqlite3.Error, OSError):
        raise ValueError("暂时无法生成工单访客链接。") from None
    if not _TOKEN.fullmatch(token):
        raise ValueError("已保存的工单访客链接格式无效。")
    return guest_evidence.PUBLIC_ORIGIN + PUBLIC_PATH + token


def _ticket_id(cfg: Settings, token: str) -> int | None:
    if not _TOKEN.fullmatch(token):
        return None
    try:
        with connection(_registry(cfg)) as db:
            row = db.execute("SELECT ticket_id FROM ticket_guest_link WHERE token=?", (token,)).fetchone()
    except (sqlite3.Error, OSError):
        return None
    return _id(row[0]) if row else None


def matches(cfg: Settings, ticket_id: str, url: str) -> bool:
    prefix = guest_evidence.PUBLIC_ORIGIN + PUBLIC_PATH
    if not isinstance(url, str) or not url.startswith(prefix):
        return False
    number = _ticket_id(cfg, url[len(prefix):])
    return number is not None and str(number) == ticket_id


def _name_pattern(values: set[str]) -> re.Pattern[str] | None:
    pieces = []
    for value in sorted(values, key=lambda value: (-len(value), value)):
        pattern = re.escape(value)
        if value.isascii() and value.isalnum():
            pattern = r"(?<![a-z0-9])" + pattern + r"(?![a-z0-9])"
        pieces.append(pattern)
    return re.compile("|".join(pieces), re.I) if pieces else None


def _guest_text(value: object, names: re.Pattern[str] | None) -> str:
    if isinstance(value, str):
        value = _BEARER_TOKEN.sub(r"\1 [已脱敏]", value)
        value = _LABELED_SECRET.sub(r"\1 [已脱敏]", value)
        value = _USER_ACCOUNT.sub("[标识已隐藏]", value)
    text = guest_evidence._clean_projection(value, names)
    if _REDACTION.search(text) and not any(char.isalnum() for char in _REDACTION.sub("", text)):
        return ""
    return text


def _identities(raw: dict, custom: list[dict]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    identifiers = tuple(raw.get(key) for key in _IDENTIFIER_KEYS)
    values = {str(value).strip() for value in identifiers if type(value) in (int, str) and len(str(value).strip()) >= 4}
    if ticket_id := _id(raw.get("id")):
        values.add(str(ticket_id))
    for key in ("staffName", "holderName", "creatorName", "applyStaffName"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            values.add(value.strip())
    candidates = []
    for field in custom:
        label = field.get("name")
        if not isinstance(label, str):
            continue
        if label in _DISPLAY_LABELS:
            display, _ = tickets._field_value(field.get("value"), 300)
            candidates.append((_DISPLAY_LABELS[label], display))
        elif any(part in label for part in ("客户群", "负责人", "店铺名", "商家名称", "联系")):
            value, _ = tickets._field_value(field.get("value"), 180)
            if value and not _TEST_PLACEHOLDER.fullmatch(value):
                values.add(value)
    candidates.extend((("客户名称", raw.get("userName")), ("客户群名称", raw.get("groupName"))))

    display_candidates = []
    internal = _name_pattern(values)
    for label, raw_name in candidates:
        name = case_senders._guest_nickname(raw_name, identifiers)
        if name and "***" not in name and not _REDACTION.search(name) and _guest_text(name, internal) == name:
            display_candidates.append((label, name))
        elif (isinstance(raw_name, str) and raw_name.strip()
              and not case_senders._SOURCE_PLACEHOLDER.fullmatch(raw_name.strip())):
            values.add(raw_name.strip())

    names = _name_pattern(values)
    display_fields = {}
    for label, name in display_candidates:
        if _guest_text(name, names) == name:
            display_fields.setdefault(label, name)
    return names, display_fields


def _project(raw: dict, token: str) -> tuple[dict, dict[str, str]]:
    custom = tickets._attachments(raw.get("custom"))
    names, display_fields = _identities(raw, custom)
    media = {}

    def snapshots(items: list[dict]) -> list[dict]:
        images = []
        for upstream, _, is_image in ticket_media.source_items(items):
            if not is_image or len(media) >= _MAX_IMAGES:
                continue
            parts = urlsplit(upstream)
            # The provider can rotate a signed query without changing the underlying attachment.
            identity = f"{token}/{parts.netloc.lower()}{parts.path}"
            image_id = sha256(identity.encode()).hexdigest()
            if image_id in media:
                continue
            media[image_id] = upstream
            images.append({"name": f"工单截图 {len(media)}", "url": f"{PUBLIC_PATH}{token}/images/{image_id}"})
        return images

    fields = [{"label": label, "value": name} for label, name in display_fields.items()]
    for field in custom:
        label = field.get("name")
        if not isinstance(label, str) or label not in _FIELDS:
            continue
        value, candidates = tickets._field_value(field.get("value"), 4000)
        typed_value = ((label == "是否已点击报告问题" and value.casefold() in _REPORT_VALUES)
                       or (label == "软件版本" and _SOFTWARE_VERSION.fullmatch(value) is not None))
        value = _guest_text(value, None if typed_value else names)
        images = snapshots(candidates)
        if value or images:
            fields.append({"label": label, "value": value, **({"images": images} if images else {})})

    text, embedded = tickets._field_value(raw.get("content"), 8000, rich=raw.get("isRichText") in (True, 1, "1"))
    title, status, priority, created, updated = (
        _guest_text(raw.get("title"), names)[:300],
        tickets.TICKET_STATUSES.get(_id(raw.get("status")), ""),
        tickets.TICKET_PRIORITIES.get(_id(raw.get("priority")), ""),
        tickets._millis(raw.get("createTime")),
        tickets._millis(raw.get("modifyTime")),
    )
    data = {"title": title or "工单详情", "status": status, "priority": priority, "created_at": created,
            "updated_at": updated, "description": _guest_text(text, names),
            "fields": fields, "images": snapshots(embedded)}
    return {"kind": "ticket", "ticket": data}, media


def _snapshot(cfg: Settings, token: str) -> tuple[dict, dict[str, str]] | None:
    identifier = _ticket_id(cfg, token)
    if identifier is None:
        return None
    key = (_registry(cfg), token, identifier)
    now = monotonic()
    with _CACHE_LOCK:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            _cache.move_to_end(key)
            return hit[1]
    try:
        result = _project(TicketClient(cfg.qiyu).detail(identifier), token)
    except TicketError:
        return None
    with _CACHE_LOCK:
        _cache[key] = (now + _CACHE_SECONDS, result)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_LIMIT:
            _cache.popitem(last=False)
    return result


def resolve_payload(cfg: Settings, token: str) -> dict | None:
    found = _snapshot(cfg, token)
    return found[0] if found else None


def fetch_image(cfg: Settings, token: str, image_id: str) -> tuple[bytes, str, str]:
    snapshot = _snapshot(cfg, token) if _IMAGE.fullmatch(image_id) else None
    upstream = snapshot[1].get(image_id) if snapshot else None
    if not upstream:
        raise case_media.ImageFetchError("工单截图不存在。", 404)
    data, mime = case_media.fetch_bytes(upstream, image_only=True, ticket_attachment=True)
    mime = case_media.actual_image_type(data, mime)
    if not mime:
        raise case_media.ImageFetchError("图片格式无法读取。", 502)
    return data, mime, "ticket-image." + case_media._IMAGE_FORMATS[mime]


def render_payload(payload: dict) -> bytes:
    ticket = payload["ticket"]
    title = escape(ticket["title"])
    details = "".join(f'<dt>{escape(field["label"])}</dt><dd>{escape(field["value"])}</dd>'
                      for field in ticket["fields"] if field["value"])
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} · 语海Voc</title><main><p>语海Voc · 工单访客阅览</p><h1>{title}</h1>'
            f'<p>{escape(ticket["status"])}</p><p>{escape(ticket["description"])}</p><dl>{details}</dl></main></html>').encode("utf-8")
