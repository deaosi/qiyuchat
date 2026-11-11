"""Project recognized Qiyu attachments for the conversation timeline."""
from __future__ import annotations

import html
import json
from typing import Any, Iterator
from urllib.parse import urlsplit

from ..extract.text import _attachment, _containers, _structured, clean_visible_text


_IMAGE_HOSTS = frozenset({'ysf.nosdn.127.net'})
_RASTER_EXTENSIONS = frozenset({'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'avif'})
_IMAGE_EXTENSIONS = _RASTER_EXTENSIONS | {'svg'}


def _safe_image_url(value: Any) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 4096:
        return ''
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == '\\' for char in value):
        return ''
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ''
    extension = parsed.path.rsplit('.', 1)[-1].lower()
    if (parsed.scheme != 'https' or parsed.netloc.lower() not in _IMAGE_HOSTS
            or not parsed.path.startswith('/') or parsed.fragment or extension not in _RASTER_EXTENSIONS):
        return ''
    return value


def safe_images(items: Any) -> list[dict[str, str]]:
    """Keep only public image fields and canonical HTTPS CDN destinations."""
    if not isinstance(items, list):
        return []
    urls = dict.fromkeys(_safe_image_url(item.get('url')) for item in items if isinstance(item, dict))
    return [{'url': url} for url in urls if url]


def _attachments(value: Any, depth: int = 0) -> Iterator[tuple[bool, str]]:
    if depth > 8:
        return
    if isinstance(value, str):
        try:
            yield from _attachments(json.loads(value), depth + 1)
        except (ValueError, RecursionError):
            pass
        return
    if not _structured(value, depth)[0]:
        return
    if isinstance(value, list):
        for item in value:
            # Literal strings in rich-text arrays are conversation text.
            if not isinstance(item, str):
                yield from _attachments(item, depth + 1)
    elif isinstance(value, dict):
        keys = {str(key).lower(): item for key, item in value.items()}
        if _attachment(value):
            kind = str(keys.get('type') or keys.get('msgtype') or '').lower()
            mime = str(keys.get('mimetype') or keys.get('mime') or keys.get('contenttype') or '').lower()
            image = kind in ('image', 'picture', 'photo') or mime.startswith('image/')
            if not kind and not mime:
                suffixes = [str(keys.get(name) or '').lower().split('?', 1)[0].rsplit('.', 1)[-1]
                            for name in ('ext', 'url', 'downloadurl', 'name', 'filename')]
                image = any(ext in _IMAGE_EXTENSIONS for ext in suffixes)
            url = ''
            if image:
                url = _safe_image_url(keys.get('url')) or _safe_image_url(keys.get('downloadurl'))
            yield image, url
        else:
            for key in ('attach', 'attachment', 'attachments'):
                if key in keys:
                    yield from _attachments(keys[key], depth + 1)


def _recognized_payloads(text: str) -> Iterator[Any]:
    stripped = text.strip()
    if stripped.startswith(('"{', '"[')):
        try:
            decoded = json.loads(stripped)
            payload = json.loads(decoded) if isinstance(decoded, str) else None
        except (ValueError, RecursionError):
            payload = None
        if _structured(payload)[0]:
            yield payload
            return
    for start, end in _containers(text):
        fragment = text[start:end]
        if '&quot;' in fragment:
            fragment = html.unescape(fragment)
        try:
            payload = json.loads(fragment)
        except (ValueError, RecursionError):
            continue
        if _structured(payload)[0]:
            yield payload


def project(value: Any) -> tuple[str, list[dict[str, str]]]:
    """Return cleaned text and renderable images without transport metadata."""
    raw = str(value or '')
    content = clean_visible_text(raw)
    if '{' not in raw and '[' not in raw:
        return content, []
    attachments = [attachment for payload in _recognized_payloads(raw) for attachment in _attachments(payload)]
    if not content and attachments:
        content = '[图片]' if all(image for image, _ in attachments) else '[附件]'
    urls = dict.fromkeys(url for _, url in attachments if url)
    return content, [{'url': url} for url in urls]
