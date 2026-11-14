"""Read-only access to the official Qiyu ticket OpenAPI."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from ..config import QiyuConfig
from ..http import HttpError, request
from .sign import signed_query


SEARCH = "/openapi/v2/ticket/search"
DETAIL = "/openapi/v2/ticket/new/detail"
HISTORY = "/openapi/v2/ticket/log"
PAGE_SIZE = 50
_request_lock = threading.Lock()
_next_request = 0.0


class TicketError(RuntimeError):
    def __init__(self, detail: str, *, http_status: int = 502) -> None:
        super().__init__(detail)
        self.http_status = http_status


class TicketClient:
    def __init__(self, config: QiyuConfig) -> None:
        self.config = config

    def _post(self, path: str, body: dict[str, Any], result_key: str) -> Any:
        global _next_request
        if not self.config.app_key or not self.config.app_secret:
            raise TicketError("七鱼工单接口尚未配置", http_status=503)
        with _request_lock:
            delay = _next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            _next_request = time.monotonic() + 0.25
        query, payload = signed_query(self.config.app_key, self.config.app_secret, body)
        try:
            result = request("POST", f"{self.config.base_url.rstrip('/')}{path}?{query}",
                             body=payload, timeout=min(self.config.timeout, 12), retries=1)
        except HttpError as error:
            if error.status == 429:
                raise TicketError("七鱼工单接口请求过于频繁，请稍后重试", http_status=503) from None
            raise TicketError(f"七鱼工单接口不可用（HTTP {error.status}）") from None
        except (RuntimeError, OSError, ValueError):
            # Transport errors can contain a signed URL. Only return a safe message.
            raise TicketError("七鱼工单接口暂时无法连接") from None
        if not isinstance(result, dict):
            raise TicketError("七鱼工单接口返回了无法识别的数据")
        code = result.get("code")
        if code in (14106, "14106"):
            raise TicketError("七鱼工单不存在", http_status=404)
        if code in (14009, "14009"):
            raise TicketError("七鱼工单接口请求过于频繁，请稍后重试", http_status=503)
        if code not in (200, "200"):
            if type(code) is int:
                raise TicketError(f"七鱼工单接口返回错误（{code}）")
            raise TicketError("七鱼工单接口返回了无法识别的数据")
        other_key = "message" if result_key == "data" else "data"
        value = result.get(result_key, result.get(other_key))
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (ValueError, RecursionError):
                raise TicketError("七鱼工单接口返回了无法识别的数据") from None
        return value

    def search(self, *, offset: int, limit: int, bounds: dict[str, int]) -> tuple[int, list[dict]]:
        value = self._post(SEARCH, {"offset": offset, "limit": limit, "sortBy": "ct", "order": "desc",
                                   "withCustomField": False, **bounds}, "message")
        if not isinstance(value, dict):
            raise TicketError("七鱼工单列表格式无效")
        total, tickets = value.get("total"), value.get("tickets")
        if type(total) is not int or total < 0 or not isinstance(tickets, list) or len(tickets) > limit:
            raise TicketError("七鱼工单列表格式无效")
        if any(not isinstance(ticket, dict) or _id(ticket.get("id")) is None for ticket in tickets):
            raise TicketError("七鱼工单列表缺少有效工单编号")
        if offset < total and not tickets:
            raise TicketError("七鱼工单列表返回不完整，请刷新重试")
        return total, tickets

    def detail(self, ticket_id: int) -> dict:
        value = self._post(DETAIL, {"ticketId": ticket_id}, "data")
        if not isinstance(value, dict) or _id(value.get("id")) != ticket_id:
            raise TicketError("七鱼工单详情与请求编号不一致")
        return value

    def history(self, ticket_id: int) -> list[dict]:
        value = self._post(HISTORY, {"ticketId": ticket_id}, "data")
        if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
            raise TicketError("七鱼工单处理记录格式无效")
        return value


def _id(value: Any) -> int | None:
    if type(value) is int:
        identifier = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        identifier = int(value)
    else:
        return None
    return identifier if 0 < identifier <= 2**63 - 1 else None
