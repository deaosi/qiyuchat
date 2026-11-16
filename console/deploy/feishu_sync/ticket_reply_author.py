"""Attribute a forwarded reply to the operator recorded in Feishu field history."""

import re
import subprocess
from time import monotonic
from typing import Callable

from deploy.feishu_sync import ticket_feedback_media, ticket_fields
from src import voc_feishu_sync as sync


_PAGE_SIZE = 30
_MAX_PAGES = 2
_TIMEOUT_SECONDS = 8
_IDENTIFIER = re.compile(r"(?:ou|on|oc|u|cli)_[A-Za-z0-9_-]+|\d{8,}", re.IGNORECASE)


def _valid_field_id(value):
    return isinstance(value, str) and ticket_fields._FIELD_ID.fullmatch(value)


def _text_fields(target, changed_text):
    fields = {}
    for name, value in changed_text.items():
        spec = getattr(target, "approved", {}).get(name)
        if not spec and name == getattr(target, "investigation_field", None):
            spec = (getattr(target, "investigation_id", None), 1)
        if (not isinstance(spec, tuple) or len(spec) != 2 or spec[1] not in (1, 3)
                or not _valid_field_id(spec[0])
                or spec[0] in fields or not isinstance(value, str)):
            raise ValueError("Unbound or ambiguous feedback field")
        fields[spec[0]] = value.replace("\r\n", "\n").strip()
    return fields


def _operator_name(value):
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or len(name) > 120 or not name.isprintable() or _IDENTIFIER.fullmatch(name):
        return ""
    return name


def _history_pages(target, record_id):
    base_args = [target.cli, "base", "+record-history-list", "--base-token", target.base_token,
                 "--table-id", target.table_id, "--record-id", record_id,
                 "--as", "bot", "--format", "json", "--page-size", str(_PAGE_SIZE)]
    deadline = monotonic() + _TIMEOUT_SECONDS
    cursor = None
    previous_revision = None
    for _ in range(_MAX_PAGES):
        remaining = deadline - monotonic()
        if remaining <= 0:
            return
        args = base_args if cursor is None else base_args + ["--max-version", str(cursor)]
        result = subprocess.run(args, env=sync._cli_environment(), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=remaining, check=False)
        response = sync._parse_json(result.stdout)
        if result.returncode or response.get("ok") is not True or response.get("identity") != "bot":
            return
        data = response.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) > _PAGE_SIZE:
            return
        for item in items:
            revision = item.get("rev") if isinstance(item, dict) else None
            if type(revision) is not int or revision <= 0:
                return
            if revision == previous_revision:
                continue
            if previous_revision is not None and revision > previous_revision:
                return
            previous_revision = revision
            yield item
        if data.get("has_more") is not True:
            return
        next_cursor = data.get("next_max_version")
        if type(next_cursor) is not int or next_cursor <= 0 or (cursor is not None and next_cursor >= cursor):
            return
        cursor = next_cursor


def _file_map(value):
    return {file["file_token"]: file for file in ticket_feedback_media.parse_cell(value)}


def _resolve(target, record_id, changed_text, files, at_ms, normalize_reply):
    text = _text_fields(target, changed_text)
    attachments = _file_map(files)
    attachment_id = getattr(target, "attachment_id", None)
    if attachments and (not _valid_field_id(attachment_id) or attachment_id in text):
        return ""
    if not text and not attachments:
        return ""

    author = ""
    for entry in _history_pages(target, record_id):
        when = entry.get("create_time")
        if type(when) is not int or when <= 0:
            return ""
        # Base history uses seconds; record search returns milliseconds.
        if when * 1000 > at_ms:
            continue
        changes = entry.get("field_changes")
        if not isinstance(changes, list):
            return ""
        matched = False
        for change in changes:
            field = change.get("field_id") if isinstance(change, dict) else None
            if not isinstance(field, str):
                return ""
            if field in text:
                if "after" not in change:
                    return ""
                after = normalize_reply(change["after"]).replace("\r\n", "\n").strip()
                # An older identical value cannot prove this snapshot's author.
                if after != text[field]:
                    return ""
                del text[field]
                matched = True
            elif attachments and field == attachment_id:
                if "before" not in change and entry.get("activity_type") != "create":
                    return ""
                after = _file_map(change.get("after"))
                if any(after.get(token) != file for token, file in attachments.items()):
                    return ""
                added = after.keys() - _file_map(change.get("before")).keys()
                for token in added & attachments.keys():
                    del attachments[token]
                    matched = True
        if matched:
            name = _operator_name(entry.get("operator"))
            if not name or (author and name != author):
                return ""
            author = name
            if not text and not attachments:
                return author
    return ""


def resolve(target, record_id: str, *, changed_text: dict[str, str], files: list[dict],
            at_ms: int, normalize_reply: Callable[[object], str]) -> str:
    """Return a display name only when each forwarded change proves the same author."""
    if (not isinstance(record_id, str) or not sync._RECORD_ID.fullmatch(record_id)
            or type(at_ms) is not int or at_ms <= 0):
        return ""
    try:
        return _resolve(target, record_id, changed_text, files, at_ms, normalize_reply)
    except (OSError, subprocess.SubprocessError, sync.SyncError, TypeError, ValueError, AttributeError):
        return ""
