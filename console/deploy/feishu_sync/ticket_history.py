"""Project Qiyu replies into display-only Feishu columns."""

from datetime import datetime, timedelta, timezone
import re
from sys import maxsize
from urllib.parse import urlsplit

from src.extract.text import clean_visible_text
from src.qiyu.ticket import _id
from src.web import ticket_guest, ticket_media, tickets


COLUMNS = {"latest": "最新七鱼跟进", "history": "工单往来记录", "attachments": "点击查看跟进回复",
           "context": "跟进回复上下文"}
_ZONE = timezone(timedelta(hours=8))
_REPLY_ACTION = re.compile(r"(?:(?:工单|追加|补充)?回复(?:了?工单)?|repl(?:y|ied)(?: to (?:the )?ticket)?)", re.I)
_REPLY_FIELD = re.compile(r"(?:(?:工单)?回复(?:内容|工单)?|comment|reply)", re.I)
_RICH = re.compile(r"</?(?:p|div|br|li|ol|ul|span|strong|em|b|a|img|script|style)(?:\s|/?>)", re.I)
_FEISHU_ECHO = re.compile(r"语海 Voc · 飞书技术反馈\s*\n(.*?)\n\s*〔语海同步 [0-9a-f]{16}〕", re.S)
_MAX_REPLIES = 12
_MAX_HISTORY = 8_000
_MAX_REPLY = 4_000
_MAX_LATEST = 1_200
_MAX_CONTEXT = 100_000
_CONTEXT_NOTICE = "回复超出飞书单元格容量，完整内容请查看七鱼工单日志。"


def _is_reply(entry, fields):
    action_type = _id(entry.get("actionType"))
    if action_type == 101:
        return True
    if action_type != 20:
        return False
    actions = (entry.get("action"), entry.get("actionLang"))
    labels = (field.get("title") or field.get("titleLang") for field in fields)
    return (any(isinstance(action, str) and _REPLY_ACTION.fullmatch(action.strip()) for action in actions)
            or any(isinstance(label, str) and _REPLY_FIELD.fullmatch(label.strip()) for label in labels))


def _reply_content(fields, attachments):
    parts = []
    from_feishu = False
    for field in fields:
        label = field.get("title") or field.get("titleLang") or ""
        if isinstance(label, str) and (label.strip().casefold() in tickets._PRIVATE_LABELS
                                      or ticket_guest._PRIVATE_LABEL.search(label)):
            continue
        content = field.get("content")
        if not isinstance(content, str):
            continue
        echo = _FEISHU_ECHO.fullmatch(content.strip())
        if echo:
            from_feishu = True
            content = echo[1]
        value, media = tickets._field_value(content, maxsize, rich=bool(_RICH.search(content)))
        attachments.extend(media)
        if not echo and (echo := _FEISHU_ECHO.fullmatch(value)):
            from_feishu = True
            value = echo[1]
        visible = ticket_guest._guest_text(clean_visible_text(value), None)
        if visible:
            parts.append(visible)
    message = "\n".join(parts)
    return message, from_feishu


def _shorten(text, limit):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _attached_count(attachments):
    paths = set()
    for url, _, _ in ticket_media.source_items(attachments):
        parts = urlsplit(url)
        paths.add((parts.netloc.lower(), parts.path))
    return len(paths)


def _context_text(entries, length, truncated):
    if not truncated:
        return "\n\n".join(text for text, _ in reversed(entries))
    available = _MAX_CONTEXT - len(_CONTEXT_NOTICE) - 2
    while length > available and len(entries) > 1:
        _, dropped = entries.pop()
        length -= dropped + 2
    if length > available:
        text, _ = entries[0]
        prefix = text.encode("utf-16-le")[:2 * (available - 1)].decode("utf-16-le", errors="ignore")
        return _CONTEXT_NOTICE + "\n\n" + prefix.rstrip() + "…"
    return "\n\n".join([_CONTEXT_NOTICE, *(text for text, _ in reversed(entries))])


def project(cfg, ticket_id: str, logs: list[dict], *, submitter_id=None) -> dict:
    """Read chronological replies and preserve their direction without sending feedback."""
    identifier = _id(ticket_id)
    if identifier is None or str(identifier) != ticket_id:
        raise ValueError("工单往来记录的工单 ID 无效。")
    creator_ids = {_id(entry.get("operatorId")) for entry in logs
                   if _id(entry.get("actionType")) in tickets._CREATE_ACTIONS}
    creator_ids.discard(None)
    if not creator_ids and (creator := _id(submitter_id)) is not None:
        creator_ids.add(creator)
    ordered = sorted(logs,
                     key=lambda entry: (tickets._millis(entry.get("time")) or 0, _id(entry.get("id")) or 0),
                     reverse=True)
    seen = set()
    entries = []
    context = []
    context_length = 0
    context_omitted = False
    latest = ""
    has_attachments = False
    has_native_reply = False
    history_length = 0
    omitted = False
    for entry in ordered:
        fields = tickets._attachments(entry.get("info"))
        event_id = _id(entry.get("id"))
        if event_id in seen or not _is_reply(entry, fields):
            continue
        if event_id is not None:
            seen.add(event_id)
        attachments = tickets._attachments(entry.get("attachments"))
        message, from_feishu = _reply_content(fields, attachments)
        count = _attached_count(attachments)
        if not message and not count:
            continue

        timestamp = tickets._millis(entry.get("time"))
        when = datetime.fromtimestamp(timestamp // 1000, _ZONE).strftime("%Y-%m-%d %H:%M:%S") if timestamp else "时间未记录"
        actor = _id(entry.get("operatorId"))
        if _id(entry.get("actionType")) == 101:
            role = "修改回复"
        else:
            role = "提报人补充" if len(creator_ids) == 1 and actor in creator_ids else "工单回复"
        direction = "飞书 → 七鱼 · 技术反馈" if from_feishu else f"七鱼 → 飞书 · {role}"
        sender = "" if from_feishu else ticket_guest._guest_text(entry.get("operator"), None)
        header = " · ".join(part for part in (when, direction, _shorten(sender, 120)) if part)
        marker = f"附件 {count} 个" if count else ""
        if not from_feishu:
            has_attachments |= bool(_attached_count(ticket_guest.followup_media(entry)))
            if not has_native_reply and message:
                has_native_reply = bool(ticket_guest.native_followup_text(entry))
            if not latest:
                latest = "\n".join(part for part in (header, _shorten(message, _MAX_LATEST), marker) if part)
        if not context_omitted:
            full_entry = "\n".join(part for part in (header, message, marker) if part)
            units = len(full_entry.encode("utf-16-le")) // 2
            combined_length = context_length + units + (2 if context else 0)
            if not context or combined_length <= _MAX_CONTEXT:
                context.append((full_entry, units))
                context_length = combined_length
            context_omitted = combined_length > _MAX_CONTEXT
        summary = "\n".join(part for part in (header, _shorten(message, _MAX_REPLY), marker) if part)
        if omitted or len(entries) >= _MAX_REPLIES or history_length + len(summary) + 2 > _MAX_HISTORY:
            omitted = True
            if context_omitted and latest and has_attachments and has_native_reply:
                break
            continue
        entries.append(summary)
        history_length += len(summary) + 2

    history = list(reversed(entries))
    if omitted:
        history.insert(0, f"以下为最近 {len(entries)} 条回复")
    reply_link = None
    if has_native_reply:
        reply_link = {"text": "查看跟进回复", "link": ticket_guest.reply_url_for(cfg, ticket_id)}
    elif has_attachments:
        reply_link = {"text": "查看跟进回复", "link": ticket_guest.followup_url_for(cfg, ticket_id)}
    return {
        COLUMNS["latest"]: latest,
        COLUMNS["history"]: "\n\n".join(history),
        COLUMNS["attachments"]: reply_link,
        COLUMNS["context"]: _context_text(context, context_length, context_omitted),
    }
