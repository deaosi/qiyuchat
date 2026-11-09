"""签名工具。

关键点：md5 算的是**实际发出去的字节对应的字符串**。所以这里返回序列化后的
raw 字符串，调用方必须把它原样作为 body 发送，不能重新 json.dumps 一遍。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from typing import Any


def _serialize(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def build_signature(app_secret: str, body: Any = None, ts: int | None = None) -> tuple[int, str, str]:
    """返回 (time, checksum, raw_body)。raw_body 必须原样发出。"""
    ts = int(ts if ts is not None else time.time())
    raw = _serialize(body)
    md5 = hashlib.md5(raw.encode("utf-8")).hexdigest()
    checksum = hashlib.sha1(f"{app_secret}{md5}{ts}".encode("utf-8")).hexdigest()
    return ts, checksum, raw


def signed_query(app_key: str, app_secret: str, body: Any = None) -> tuple[str, str]:
    """生成 query 串和 raw_body。"""
    ts, checksum, raw = build_signature(app_secret, body)
    qs = urllib.parse.urlencode({"appKey": app_key, "time": ts, "checksum": checksum})
    return qs, raw
