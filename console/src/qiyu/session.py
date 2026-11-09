"""会话记录导出：POST /openapi/export/session

注意：这个接口返回的是**会话级元数据**（谁、什么时候、哪个客服、多久、满意度、
分类、关联工单……），不含逐句聊天正文。要正文只能走质检导出。
"""

from __future__ import annotations

from typing import Any

from ..config import QiyuConfig
from ..http import request
from ..log import get
from .common import ensure_ok
from .sign import signed_query

log = get("qiyu.session")

ENDPOINT = "/openapi/export/session"


class SessionExporter:
    def __init__(self, cfg: QiyuConfig):
        self.cfg = cfg

    def export(self, start_ms: int, end_ms: int, type_: int = 0) -> list[dict[str, Any]]:
        """拉一个时间片的会话记录。官方限制：跨度最大 5 天。"""
        span_days = (end_ms - start_ms) / 86_400_000
        if span_days > self.cfg.session_window_days + 0.01:
            raise ValueError(f"时间跨度 {span_days:.2f} 天超过上限 {self.cfg.session_window_days} 天")

        body = {"start": int(start_ms), "end": int(end_ms), "type": int(type_)}
        qs, raw = signed_query(self.cfg.app_key, self.cfg.app_secret, body)
        url = f"{self.cfg.base_url}{ENDPOINT}?{qs}"

        log.info("拉取会话记录 %s → %s (type=%s)",
                 _fmt(start_ms), _fmt(end_ms), type_)
        resp = request("POST", url, body=raw, timeout=self.cfg.timeout)
        data = ensure_ok(resp, ENDPOINT)

        rows = self._extract_rows(data)
        log.info("本次返回 %d 条会话", len(rows))
        return rows

    @staticmethod
    def _extract_rows(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            for key in ("session", "sessions", "list", "rows", "records"):
                v = data.get(key)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, dict)]
        return []


def _fmt(ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
