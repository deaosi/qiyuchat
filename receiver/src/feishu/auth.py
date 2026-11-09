"""tenant_access_token 缓存。

有效期 2 小时，提前 20 分钟刷新。不要每次现取 —— 有频控。
"""

from __future__ import annotations

import threading
import time

from ..http import request
from ..log import get

log = get("feishu.auth")

BASE = "https://open.feishu.cn/open-apis"


class FeishuAuth:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._expire_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expire_at:
                return self._token

            resp = request(
                "POST",
                f"{BASE}/auth/v3/tenant_access_token/internal",
                body={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=20,
            )
            if int(resp.get("code") or 0) != 0:
                raise RuntimeError(f"获取 tenant_access_token 失败: {resp}")
            self._token = resp["tenant_access_token"]
            expire = int(resp.get("expire") or 7200)
            self._expire_at = time.time() + max(60, expire - 1200)
            log.info("tenant_access_token 已刷新，%d 秒后过期", expire)
            return self._token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def call(self, method: str, path: str, body=None, **kw):
        resp = request(method, f"{BASE}{path}", body=body, headers=self.headers(), **kw)
        code = int(resp.get("code") or 0) if isinstance(resp, dict) else 0
        if code != 0:
            raise RuntimeError(f"飞书接口失败 {path}: code={code} msg={resp.get('msg')}")
        return resp.get("data", resp)
