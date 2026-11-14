"""Authenticated access to media issued by the Qiyu ticket API."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from hmac import compare_digest, digest
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote, unquote, urlsplit

from ..clean.rules import mask_pii
from . import case_media


_QIYU_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)?qiyukf\.net")
_QIYU_OBJECT = re.compile(r"/[0-9a-fA-F]{32}(?:\.[a-zA-Z0-9]{1,10})?")
_TOKEN = re.compile(r"([A-Za-z0-9_-]{1,4500})\.([a-f0-9]{64})")
_IMAGE_EXTENSIONS = frozenset(case_media._IMAGE_FORMATS.values()) | {"jpeg"}


def _safe_source(value: object) -> str:
    if case_media._safe_image_url(value):
        return value
    if not isinstance(value, str) or len(value) > 2048 or not value.isascii():
        return ""
    if any(char.isspace() or ord(char) < 33 or ord(char) == 127 or char == "\\" for char in value):
        return ""
    try:
        url = urlsplit(value)
    except ValueError:
        return ""
    if (url.scheme == "https" and not url.fragment and _QIYU_HOST.fullmatch(url.netloc.lower())
            and _QIYU_OBJECT.fullmatch(url.path)):
        return value
    return ""


def _signature(secret: str, encoded: str) -> str:
    return digest(secret.encode(), b"qiyu-ticket-media\0" + encoded.encode("ascii"), sha256).hex()


def links(items: list[dict], secret: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    images, attachments = [], []
    seen = set()
    for item in items[:40]:
        uploaded = item.get("response")
        upstream = _safe_source(item.get("url")) or _safe_source(uploaded.get("url") if isinstance(uploaded, dict) else None)
        if not upstream or upstream in seen or not secret:
            continue
        seen.add(upstream)
        name = item.get("name")
        name = mask_pii(name.strip())[:180] if isinstance(name, str) else ""
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(char for char in name if char.isprintable()) or "工单附件"
        extension = PurePosixPath(name).suffix.lower().lstrip(".")
        if not extension:
            extension = PurePosixPath(unquote(urlsplit(upstream).path)).suffix.lower().lstrip(".")
        image = extension in _IMAGE_EXTENSIONS or item.get("inline_image") is True
        encoded = urlsafe_b64encode(json.dumps([upstream, name, image], ensure_ascii=False).encode()).decode().rstrip("=")
        if len(encoded) > 4500:
            continue
        public = {"name": name, "url": f"/api/tickets/media?token={encoded}.{_signature(secret, encoded)}"}
        (images if image else attachments).append(public)
    return images, attachments


def fetch(token: str, secret: str, *, download: bool = False) -> tuple[bytes, str, str]:
    match = _TOKEN.fullmatch(token)
    if not secret or not match or not compare_digest(_signature(secret, match[1]), match[2]):
        raise case_media.ImageFetchError("工单附件不存在，请刷新详情。", 404)
    encoded = match[1]
    try:
        url, name, image = json.loads(urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError, RecursionError):
        raise case_media.ImageFetchError("工单附件不存在，请刷新详情。", 404) from None
    if not _safe_source(url) or not isinstance(name, str) or type(image) is not bool:
        raise case_media.ImageFetchError("工单附件不存在，请刷新详情。", 404)
    data, mime = case_media.fetch_bytes(url, image_only=image, ticket_attachment=True)
    mode = "attachment"
    if image:
        mime = case_media.actual_image_type(data, mime)
        if not mime:
            raise case_media.ImageFetchError("图片格式无法读取。", 502)
        name = f"{PurePosixPath(name).stem or '工单截图'}.{case_media._IMAGE_FORMATS[mime]}"
        if not download:
            mode = "inline"
    disposition = f"{mode}; filename*=UTF-8''{quote(name, safe='')}"
    return data, mime if image else "application/octet-stream", disposition
