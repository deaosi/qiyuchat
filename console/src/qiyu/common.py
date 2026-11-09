"""七鱼接口的公共响应处理与错误码。"""

from __future__ import annotations

from typing import Any

from ..log import get

log = get("qiyu")

ERROR_HINTS = {
    14001: "appKey 不正确 —— 核对后台 系统管理→扩展与集成→开发者ID",
    14002: "checksum 校验失败 —— md5 必须是对实际发出的 body 字符串做的，注意字段顺序和空格",
    14003: "time 与七鱼服务器偏差超过 5 分钟 —— 服务器需要 NTP 对时",
    14008: "请求来源 IP 不在白名单 —— 把服务器公网出口 IP 加到七鱼白名单",
    14009: "请求频率超限 —— 单接口 5 QPS / 单租户 20 QPS，加退避",
    14400: "企业级禁止调用该接口 —— 该租户未开通此开放接口权限，需联系七鱼商务/客服开通（与 AppKey、签名、IP 白名单无关）",
    14403: "任务尚未完成（Wait...）—— 继续轮询",
}


class QiyuError(RuntimeError):
    def __init__(self, code: int, message: str, endpoint: str = ""):
        self.code = code
        self.message = message
        hint = ERROR_HINTS.get(code, "")
        super().__init__(f"七鱼接口错误 {code} @{endpoint}: {message}" + (f"\n  → {hint}" if hint else ""))


def ensure_ok(resp: Any, endpoint: str = "") -> Any:
    """统一判定 code，返回 data（或整个 resp）。"""
    if not isinstance(resp, dict):
        return resp
    code = resp.get("code")
    if code is None:
        return resp
    try:
        code = int(code)
    except (TypeError, ValueError):
        return resp
    if code in (0, 200):
        return resp.get("data", resp)
    raise QiyuError(code, str(resp.get("message") or resp.get("msg") or ""), endpoint)
