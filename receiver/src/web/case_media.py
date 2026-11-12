"""Project recognized Qiyu attachments for the conversation timeline."""
from __future__ import annotations

import html
import json
import re
import time
from http.client import HTTPException
from io import BytesIO
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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


_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_IMAGE_IDLE_TIMEOUT = 5
_IMAGE_TOTAL_TIMEOUT = 15
_IMAGE_FORMATS = {
    'image/jpeg': 'jpg', 'image/png': 'png', 'image/gif': 'gif',
    'image/webp': 'webp', 'image/bmp': 'bmp', 'image/heic': 'heic',
    'image/avif': 'avif',
}
_IMAGE_MIME_ALIASES = {'image/x-ms-bmp': 'image/bmp', 'image/heif': 'image/heic'}
_IMAGE_SIGNATURES = {
    'image/jpeg': (b'\xff\xd8\xff',),
    'image/png': (b'\x89PNG\r\n\x1a\n',),
    'image/gif': (b'GIF87a', b'GIF89a'),
    'image/bmp': (b'BM',),
}
_IMAGE_BRANDS = {
    'image/avif': {b'avif', b'avis'},
    'image/heic': {b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'hevm', b'hevs'},
}


class ImageFetchError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class _NoImageRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Use a direct HTTPS request, with no ambient proxies, cookies or authorization.
_IMAGE_OPENER = build_opener(ProxyHandler({}), _NoImageRedirect())


def _matches_image_signature(mime: str, data: bytes) -> bool:
    prefixes = _IMAGE_SIGNATURES.get(mime)
    if prefixes:
        return data.startswith(prefixes)
    if mime == 'image/webp':
        return data.startswith(b'RIFF') and data[8:12] == b'WEBP' and data[12:16] in (b'VP8 ', b'VP8L', b'VP8X')
    if len(data) < 16 or data[4:8] != b'ftyp':
        return False
    size = int.from_bytes(data[:4], 'big')
    if not 16 <= size <= len(data) or size % 4:
        return False
    brands = _IMAGE_BRANDS[mime]
    return data[8:12] in brands or any(data[index:index + 4] in brands for index in range(16, size, 4))


def fetch_image(url: str) -> tuple[bytes, str, str]:
    """Fetch the attachment's exact bytes from its recorded CDN URL."""
    if not _safe_image_url(url):
        raise ImageFetchError('图片地址无效。', 400)
    unavailable = '暂时无法读取会话图片。'
    unsupported = '图片格式无法读取。'
    too_large = '图片超过 20 MB，无法加载。'
    timeout = '图片加载超时，请重试。'
    deadline = time.monotonic() + _IMAGE_TOTAL_TIMEOUT
    request = Request(url, headers={'Accept-Encoding': 'identity'})
    try:
        with _IMAGE_OPENER.open(request, timeout=_IMAGE_IDLE_TIMEOUT) as response:
            if response.status != 200:
                raise ImageFetchError(unavailable, 502)
            headers = response.headers
            mime = headers.get_content_type().lower()
            mime = _IMAGE_MIME_ALIASES.get(mime, mime)
            encoding = headers.get('Content-Encoding', 'identity').strip().lower()
            if mime not in _IMAGE_FORMATS or encoding != 'identity':
                raise ImageFetchError(unsupported, 502)
            declared = headers.get_all('Content-Length', [])
            if len(declared) > 1:
                raise ImageFetchError(unavailable, 502)
            expected = int(declared[0]) if declared else None
            if expected is not None and expected > _IMAGE_MAX_BYTES:
                raise ImageFetchError(too_large, 413)
            with BytesIO() as output:
                while True:
                    if time.monotonic() >= deadline:
                        raise ImageFetchError(timeout, 504)
                    chunk = response.read1(min(64 * 1024, _IMAGE_MAX_BYTES + 1 - output.tell()))
                    if not chunk:
                        break
                    output.write(chunk)
                    if output.tell() > _IMAGE_MAX_BYTES:
                        raise ImageFetchError(too_large, 413)
                data = output.getvalue()
            if time.monotonic() >= deadline:
                raise ImageFetchError(timeout, 504)
            if expected is not None and expected != len(data):
                raise ImageFetchError(unavailable, 502)
    except HTTPError as exc:
        exc.close()
        if exc.code in (404, 410):
            raise ImageFetchError('会话图片已失效或暂不可用。', 502) from None
        raise ImageFetchError(unavailable, 502) from None
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ImageFetchError(timeout, 504) from None
        raise ImageFetchError(unavailable, 502) from None
    except TimeoutError:
        raise ImageFetchError(timeout, 504) from None
    except (OSError, HTTPException, ValueError):
        raise ImageFetchError(unavailable, 502) from None

    # Qiyu's CDN sometimes labels PNG files as JPEG, including for .jpg URLs.
    if not _matches_image_signature(mime, data):
        mime = next((candidate for candidate in _IMAGE_FORMATS
                     if candidate != mime and _matches_image_signature(candidate, data)), '')
    if not mime:
        raise ImageFetchError(unsupported, 502)
    leaf = urlsplit(url).path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    basename = re.sub(r'[^A-Za-z0-9_.-]+', '_', unquote(leaf))[:100].strip('._-')
    return data, mime, f"{basename or 'conversation-image'}.{_IMAGE_FORMATS[mime]}"
