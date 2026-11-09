"""Readable conversation text, without transport metadata from media attachments.

Raw messages stay intact in storage. Only recognized attachment objects, forwarded
chat cards and rich-text blocks are projected to text; ordinary JSON and links survive.
"""

from __future__ import annotations

import html
import json
import re
from functools import lru_cache
from typing import Any, Iterator

_MEDIA_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "avif",
    "mp3", "wav", "amr", "aac", "ogg", "m4a", "mp4", "mov", "avi", "webm",
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", "rar", "7z",
}
_MEDIA_TYPES = {"image", "picture", "photo", "audio", "voice", "video", "file", "attachment"}
_METADATA = {"ext", "size", "thumb", "origin", "w", "h", "width", "height", "md5", "name", "filename", "filesize", "duration"}
_TEXT_FIELDS = ("text", "caption", "textcontent")
_ATTACHMENT_FIELDS = _METADATA | set(_TEXT_FIELDS) | {
    "type", "msgtype", "mimetype", "mime", "contenttype", "url", "downloadurl",
    "fileid", "noskey", "bucket", "scene", "filetype", "dur", "expire", "tt",
}
_NON_TEXT_PLACEHOLDER = re.compile(r"\[非文本消息\s+mType=\d+\]", re.IGNORECASE)
_MERGED_FIELDS = {"type", "preview", "cmd", "sessionid", "title"}
_MERGED_MEDIA_PLACEHOLDERS = {"[图片]", "[视频]", "[语音]", "[文件]", "[表情]"}


def _attachment(value: dict[str, Any]) -> bool:
    keys = {str(key).lower(): val for key, val in value.items()}
    if not set(keys).issubset(_ATTACHMENT_FIELDS):
        return False
    ext = str(keys.get("ext") or "").lower().lstrip(".")
    kind = str(keys.get("type") or keys.get("msgtype") or "").lower()
    mime = str(keys.get("mimetype") or keys.get("mime") or keys.get("contenttype") or "").lower()
    locator = any(keys.get(key) for key in ("url", "downloadurl", "name", "filename"))
    metadata_count = len(_METADATA.intersection(keys))
    suffix_hint = any(str(keys.get(key) or "").lower().split("?", 1)[0].rsplit(".", 1)[-1] in _MEDIA_EXTENSIONS
                      for key in ("url", "downloadurl", "name", "filename"))
    return bool(
        (ext in _MEDIA_EXTENSIONS and locator and metadata_count >= 2)
        or (keys.get("url") and metadata_count >= 2
            and (suffix_hint or {"thumb", "origin", "md5"}.intersection(keys)))
        or (kind in _MEDIA_TYPES and locator)
        or (mime.startswith(("image/", "audio/", "video/")) and locator)
    )


def _structured(value: Any, depth: int = 0) -> tuple[bool, str]:
    if depth > 8:
        return False, ""
    if isinstance(value, dict):
        keys = {str(key).lower(): val for key, val in value.items()}
        # Qiyu forwarded-chat cards contain readable lines in preview. The card
        # title, command and session ID describe transport, not conversation.
        preview = keys.get("preview")
        if (keys.get("type") == "merge" and keys.get("cmd") == 123
                and "sessionid" in keys and set(keys).issubset(_MERGED_FIELDS)
                and isinstance(preview, list) and all(isinstance(line, str) for line in preview)):
            lines = [clean_visible_text(line) for line in preview
                     if line.strip() not in _MERGED_MEDIA_PLACEHOLDERS]
            return True, "\n".join(line for line in lines if line)
        if _attachment(value):
            captions = [clean_visible_text(keys[key]) for key in _TEXT_FIELDS if isinstance(keys.get(key), str)]
            return True, "\n".join(part for part in captions if part)
        if str(keys.get("type") or "").lower() == "text" and set(keys).issubset({"type", "text", "content"}):
            content = keys.get("text", keys.get("content"))
            if isinstance(content, str):
                return True, clean_visible_text(content)
        # Explicit attachment envelopes: do not infer media from arbitrary nesting.
        for key in ("attach", "attachment", "attachments"):
            envelope_fields = _METADATA | set(_TEXT_FIELDS) | {"attach", "attachment", "attachments", "type", "msgtype", "url", "downloadurl"}
            if not set(keys).issubset(envelope_fields):
                continue
            child = keys.get(key)
            if isinstance(child, str):
                try:
                    child = json.loads(child)
                except (ValueError, RecursionError):
                    continue
            recognized, text = _structured(child, depth + 1)
            if recognized:
                captions = [clean_visible_text(keys[field]) for field in _TEXT_FIELDS if isinstance(keys.get(field), str)]
                return True, "\n".join(part for part in [*captions, text] if part)
    elif isinstance(value, list) and value:
        parts: list[str] = []
        recognized_any = False
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            recognized, text = _structured(item, depth + 1)
            if not recognized:
                return False, ""
            recognized_any = True
            parts.append(text)
        if recognized_any:
            return True, "\n".join(part for part in parts if part)
    return False, ""


def _containers(text: str) -> Iterator[tuple[int, int]]:
    """Find complete outer JSON-shaped spans in one pass, respecting quoted braces."""
    start = -1
    stack: list[str] = []
    quoted = escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char in "{[":
                start = index
                stack = [char]
                quoted = escaped = False
            continue
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            expected = "{" if char == "}" else "["
            if not stack or stack[-1] != expected:
                start = -1
                stack = []
            else:
                stack.pop()
                if not stack:
                    yield start, index + 1
                    start = -1


def _clean(text: str) -> str:
    stripped = text.strip()
    # Some importers have encoded the entire attachment JSON a second time.
    if stripped.startswith(('"{', '"[')):
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, str):
                recognized, value = _structured(json.loads(decoded))
                if recognized:
                    return value.strip()
        except (ValueError, RecursionError):
            pass
    parts: list[str] = []
    cursor = 0
    changed = False
    for start, end in _containers(text):
        fragment = text[start:end]
        candidate = html.unescape(fragment) if "&quot;" in fragment else fragment
        try:
            value = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        recognized, content = _structured(value)
        if not recognized:
            continue
        parts.append(text[cursor:start])
        # A removed attachment remains a separator, never joining adjacent words.
        parts.append(content if content else "\n")
        cursor = end
        changed = True
    if not changed:
        return text.strip()
    parts.append(text[cursor:])
    result = "".join(parts)
    return "\n".join(line.strip() for line in result.splitlines() if line.strip()).strip()


@lru_cache(maxsize=16_384)
def _cached(text: str) -> str:
    return _clean(text)


def clean_visible_text(text: str) -> str:
    """Remove known media payloads while retaining the actual conversation words."""
    if not text:
        return ""
    text = str(text)
    if _NON_TEXT_PLACEHOLDER.fullmatch(text.strip()):
        return ""
    if "{" not in text and "[" not in text:
        return text.strip()
    # Do not retain very large transcript bodies in the process-wide cache.
    return _cached(text) if len(text) <= 32_768 else _clean(text)
