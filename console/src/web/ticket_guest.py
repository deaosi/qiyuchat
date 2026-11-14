"""Read-only, opaque links for a single Qiyu ticket."""

from __future__ import annotations

from collections import OrderedDict
from hashlib import sha256
from html import escape
from pathlib import Path, PurePosixPath
import re
from secrets import token_urlsafe
import sqlite3
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from .. import ticket_feedback
from ..config import Settings
from ..qiyu.ticket import TicketClient, TicketError, _id
from ..voc_assets import connection, transaction
from . import case_media, case_senders, guest_evidence, ticket_media, tickets


PUBLIC_PATH = "/guest/ticket/"
TOKEN_PATTERN = guest_evidence.TOKEN_PATTERN
IMAGE_PATTERN = r"[a-f0-9]{64}"
_TOKEN = re.compile(TOKEN_PATTERN)
_IMAGE = re.compile(IMAGE_PATTERN)
_TEST_PLACEHOLDER = re.compile(r"(?:测试|test|demo|sample)(?:[-_ ]?[0-9]{1,3})?", re.I)
_SOFTWARE_VERSION = re.compile(r"[vV]?\d{1,4}(?:\.\d{1,4}){1,3}")
_REPORT_VALUES = {"是", "否", "已点击", "未点击", "yes", "no", "true", "false"}
_PRIVATE_LABEL = re.compile(
    r"密码|口令|密钥|验证码|签名|联系(?:电话|方式|人)|手机号|地址|身份证|账号|(?:access[_ -]?)?token\b|"
    r"api[_ -]?key\b|(?:技术回复|内部备注|技术处理状态|回传状态|回传失败原因)$",
    re.I,
)
_STAFF_FIELDS = {"后台负责人", "负责人", "处理人", "提报人", "提交人"}
_SYNC_MARKER = re.compile(r"〔语海同步\s+[0-9a-f]+〕", re.I)
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
_MAX_MEDIA = 40
_MAX_LINKED_SESSIONS = 20
_TICKET_TIMELINE_ACTIONS = frozenset({1, 2, 5, 6, 10, 15, 25, 40, 45, 50, 55, 60, 61, 65, 100})
_CACHE_SECONDS = 30
_CACHE_LIMIT = 128
_CACHE_LOCK = Lock()
_cache = OrderedDict()
_cache_generation = 0
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


def invalidate(cfg: Settings, identifier: int | str) -> None:
    global _cache_generation
    path, ticket_id = _registry(cfg), _id(identifier)
    with _CACHE_LOCK:
        _cache_generation += 1
        for key in tuple(_cache):
            if key[0] == path and key[2] == ticket_id:
                _cache.pop(key)


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


def _visible_history(logs: list[dict]) -> list[dict]:
    visible = []
    for entry in logs:
        shared = entry.get("customerSee")
        explicit = shared in (True, 1, "1", "true", "True")
        action_type = _id(entry.get("actionType"))
        reply = action_type in (20, 101) or "回复" in str(entry.get("action") or entry.get("actionLang") or "")
        if any(_SYNC_MARKER.search(str(item.get("content") or "")) for item in tickets._attachments(entry.get("info"))):
            continue
        if reply and not explicit:
            continue
        if shared in (False, 0, "0", "false", "False"):
            if action_type in _TICKET_TIMELINE_ACTIONS:
                visible.append({key: entry.get(key) for key in ("id", "action", "actionLang", "time")})
            continue
        visible.append(entry)
    return visible


def _public_raw_fields(value, *, history=False):
    fields = []
    for field in tickets._attachments(value):
        label = tickets._text(field.get("title") or field.get("titleLang") if history else field.get("name"), 100)
        if not _PRIVATE_LABEL.search(label):
            fields.append(field)
    return fields


def _shared_feedback(cfg: Settings, identifier: int, names) -> dict | None:
    try:
        feedback = ticket_feedback.get(cfg, str(identifier))
    except (OSError, ValueError, sqlite3.Error):
        return None
    if not feedback:
        return None
    state = feedback.get("writeback_status")
    return {
        "status": _guest_text(feedback.get("status"), names),
        "reply": _guest_text(feedback.get("reply"), names),
        "resolved_version": _guest_text(feedback.get("resolved_version"), names),
        "received_at": tickets._millis(feedback.get("received_at")),
        "writeback_status": state if state in ticket_feedback._STATES else "",
        "written_at": tickets._millis(feedback.get("written_at")),
    }


def _shared_sessions(cfg: Settings, identifier: int, names, scoped_links) -> list[dict]:
    try:
        linked = tickets._linked_sessions(cfg, {identifier}).get(identifier, [])
    except (OSError, sqlite3.Error):
        return []
    sessions = []
    for item in linked[:_MAX_LINKED_SESSIONS]:
        display = {}
        case = guest_evidence._case(cfg, item["qa_id"], "", full=True, include_sender_names=True, display=display)
        if not case:
            continue
        _, context, _, _, sources = case
        images = {}
        for position, url in sources.values():
            if case_media._safe_image_url(url):
                assets, _ = scoped_links([{"url": url, "name": "会话截图", "inline_image": True}])
                images.setdefault(position, []).extend(image["url"] for image in assets)
        messages = []
        for position, item_message in enumerate(context):
            text = _guest_text(item_message.get("text"), names)
            media = images.get(position)
            if not text and not media:
                continue
            message = {"text": text, "time": item_message["time"], "role": item_message["role"]}
            display_name = item_message.get("sender_name") or item_message.get("sender_label")
            if sender := _guest_text(display_name, None):
                message["sender_name"] = sender
            if media:
                message["images"] = media
            messages.append(message)
        if messages:
            sessions.append({"started_at": item["started_at"], "customer_name": _guest_text(display.get("customer_name"), None),
                             "messages": messages})
    return sessions


def _project(raw: dict, token: str, *, cfg: Settings | None = None, logs: list[dict] | None = None) -> tuple[dict, dict[str, tuple[str, str, bool]]]:
    custom = tickets._attachments(raw.get("custom"))
    names, display_fields = _identities(raw, custom)
    records = []
    media: dict[str, tuple[str, str, bool]] = {}

    def scoped_links(items: list[dict]) -> tuple[list[dict], list[dict]]:
        images, attachments = [], []
        for upstream, original_name, is_image in ticket_media.source_items(items):
            parts = urlsplit(upstream)
            # The provider can rotate a signed query without changing the underlying attachment.
            if is_image:
                identity = f"{token}/{parts.netloc.lower()}{parts.path}"
            else:
                identity = f"{token}/attachment/{parts.netloc.lower()}{parts.path}"
            file_id = sha256(identity.encode()).hexdigest()
            if file_id not in media:
                if len(media) >= _MAX_MEDIA:
                    continue
                suffix = PurePosixPath(original_name).suffix.lower()
                suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""
                safe_stem = _guest_text(PurePosixPath(original_name).stem, names)
                name = (safe_stem or ("工单截图" if is_image else "工单附件")) + suffix
                media[file_id] = upstream, name, is_image
            kind = "images" if is_image else "attachments"
            (images if is_image else attachments).append({"name": media[file_id][1], "url": f"{PUBLIC_PATH}{token}/{kind}/{file_id}"})
        return images, attachments

    def public_fields(fields: list[dict]) -> list[dict]:
        result = []
        seen_labels = set()
        for field in fields:
            label = field["label"]
            if _PRIVATE_LABEL.search(label):
                continue
            value = field["value"]
            business = _DISPLAY_LABELS.get(label)
            if business:
                if business in seen_labels:
                    continue
                seen_labels.add(business)
                value = display_fields.get(business, "")
                label = business
            else:
                typed = ((label == "是否已点击报告问题" and value.casefold() in _REPORT_VALUES)
                         or (label == "软件版本" and _SOFTWARE_VERSION.fullmatch(value) is not None)
                         or label in _STAFF_FIELDS)
                value = _guest_text(value, None if typed else names)
            if value or field.get("images") or field.get("attachments"):
                result.append({**field, "label": _guest_text(label, None), "value": value})
        return result

    source_logs = logs or []
    history = [{**entry, "info": _public_raw_fields(entry.get("info"), history=True)}
               for entry in _visible_history(source_logs)]
    submitter = tickets.ticket_submitter(raw, source_logs)
    shared = {**raw, "custom": _public_raw_fields(custom)}
    data = tickets.project_ticket(cfg, shared, scoped_links, submitter=submitter)
    data.pop("owner", None)
    data.pop("linked_session_count", None)
    data["title"] = _guest_text(data["title"], names) or "工单详情"
    data["description"] = _guest_text(data["description"], names)
    data["category"] = _guest_text(data["category"], names)
    data["template"] = _guest_text(data["template"], names)
    data["submitter"] = _guest_text(submitter, None)
    data["updated_at"] = tickets.ticket_updated_at(raw, source_logs)
    data["fields"] = public_fields(data["fields"])
    visible_labels = {_DISPLAY_LABELS.get(field["label"], field["label"]) for field in data["fields"]}
    data["fields"].extend({"label": label, "value": name} for label, name in display_fields.items()
                          if label not in visible_labels)
    for index, event in enumerate(tickets.project_history(history, scoped_links), start=1):
        event["id"] = f"event-{index}"
        event["action"] = _guest_text(event["action"], names) or "工单操作"
        event["operator"] = _guest_text(event["operator"], None)
        event["details"] = public_fields(event["details"])
        records.append(event)
    payload = {"kind": "ticket", "ticket": data, "history": records, "history_available": logs is not None,
               "sessions": [], "technical_feedback": None}
    if cfg is not None and (identifier := _id(raw.get("id"))) is not None:
        payload["sessions"] = _shared_sessions(cfg, identifier, names, scoped_links)
        payload["technical_feedback"] = _shared_feedback(cfg, identifier, names)
    return payload, media


def _snapshot(cfg: Settings, token: str) -> tuple[dict, dict[str, tuple[str, str, bool]]] | None:
    identifier = _ticket_id(cfg, token)
    if identifier is None:
        return None
    key = (_registry(cfg), token, identifier)
    now = monotonic()
    with _CACHE_LOCK:
        generation = _cache_generation
        hit = _cache.get(key)
        if hit and hit[0] > now:
            _cache.move_to_end(key)
            return hit[1]
    try:
        client = TicketClient(cfg.qiyu)
        detail = client.detail(identifier)
    except TicketError:
        return None
    try:
        logs = client.history(identifier)
    except TicketError:
        logs = None
    result = _project(detail, token, cfg=cfg, logs=logs)
    with _CACHE_LOCK:
        if generation == _cache_generation:
            _cache[key] = (now + _CACHE_SECONDS, result)
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
    return result


def resolve_payload(cfg: Settings, token: str) -> dict | None:
    found = _snapshot(cfg, token)
    return found[0] if found else None


def fetch_image(cfg: Settings, token: str, image_id: str) -> tuple[bytes, str, str]:
    media = _guest_media(cfg, token, image_id)
    if not media or not media[2]:
        raise case_media.ImageFetchError("工单截图不存在。", 404)
    upstream, _, _ = media
    data, mime = case_media.fetch_bytes(upstream, image_only=True, ticket_attachment=True)
    mime = case_media.actual_image_type(data, mime)
    if not mime:
        raise case_media.ImageFetchError("图片格式无法读取。", 502)
    return data, mime, "ticket-image." + case_media._IMAGE_FORMATS[mime]


def _guest_media(cfg: Settings, token: str, media_id: str) -> tuple[str, str, bool] | None:
    snapshot = _snapshot(cfg, token) if _IMAGE.fullmatch(media_id) else None
    return snapshot[1].get(media_id) if snapshot else None


def fetch_attachment(cfg: Settings, token: str, attachment_id: str) -> tuple[bytes, str, str]:
    media = _guest_media(cfg, token, attachment_id)
    if not media or media[2]:
        raise case_media.ImageFetchError("工单附件不存在。", 404)
    upstream, name, _ = media
    data, _ = case_media.fetch_bytes(upstream, image_only=False, ticket_attachment=True)
    return data, "application/octet-stream", name


def render_payload(payload: dict) -> bytes:
    ticket = payload["ticket"]
    title = escape(ticket["title"])
    details = "".join(f'<dt>{escape(field["label"])}</dt><dd>{escape(field["value"])}</dd>'
                      for field in ticket["fields"] if field["value"])
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title} · 语海Voc</title><main><p>语海Voc · 工单访客阅览</p><h1>{title}</h1>'
            f'<p>{escape(ticket["status"])}</p><p>{escape(ticket["description"])}</p><dl>{details}</dl></main></html>').encode("utf-8")
