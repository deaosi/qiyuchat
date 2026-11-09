"""HTTP 封装：标准库 urllib，带超时、重试、错误码归一。

不用 requests，避免任何 pip 依赖 —— 服务器上 python3 就能直接跑。
"""

from __future__ import annotations

import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from .log import get

log = get("http")


def _force_ipv4() -> None:
    """强制只解析 IPv4。

    为什么需要：七鱼的 IP 白名单只认 IPv4。如果服务器同时有 IPv6（腾讯云/电信
    常见 2402: 段），glibc 会优先返回 AAAA 记录，请求就走 IPv6 出去，白名单
    永远匹配不上，报 14008。IPV4_ONLY=1 时把 AAAA 结果过滤掉。
    """
    if os.environ.get("IPV4_ONLY", "1") not in ("1", "true", "yes"):
        return
    _orig = socket.getaddrinfo

    def _v4_only(host, port, family=0, type=0, proto=0, flags=0):
        infos = _orig(host, port, socket.AF_INET, type, proto, flags)
        return infos

    socket.getaddrinfo = _v4_only  # type: ignore[assignment]
    log.debug("已强制 IPv4 解析")


_force_ipv4()


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} {url} -> {body[:300]}")


def request(
    method: str,
    url: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    retries: int = 3,
    raw: bool = False,
) -> Any:
    """发一个请求。

    body 为 dict/list 时自动 JSON 序列化（separators 固定，保证签名可复现）。
    raw=True 时返回 bytes，否则尝试解析 JSON。
    """
    hdrs = {"User-Agent": "qiyu-feishu-pipeline/1.0"}
    payload: bytes | None = None

    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            payload = bytes(body)
        elif isinstance(body, str):
            # 已经是序列化好的字符串：原样发出，保证与签名时算 md5 的内容完全一致
            payload = body.encode("utf-8")
            hdrs["Content-Type"] = "application/json;charset=utf-8"
        else:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            hdrs["Content-Type"] = "application/json;charset=utf-8"
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if raw:
                    return data
                text = data.decode("utf-8", errors="replace")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"_raw": text}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            # 4xx 除 429 外不重试，避免把鉴权错误刷成频控
            if 400 <= e.code < 500 and e.code != 429:
                raise HttpError(e.code, detail, url) from None
            last_err = HttpError(e.code, detail, url)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e

        if attempt < retries - 1:
            sleep = (2 ** attempt) + random.uniform(0, 0.5)
            log.warning("请求失败(%s/%s) %s %s: %s，%.1fs 后重试",
                        attempt + 1, retries, method, url.split("?")[0], last_err, sleep)
            time.sleep(sleep)

    raise RuntimeError(f"请求最终失败 {method} {url.split('?')[0]}: {last_err}") from last_err
