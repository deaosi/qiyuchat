"""Access to the official Qiyu ticket OpenAPI."""

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
REPLY = "/openapi/v2/ticket/reply"
MODIFY = "/openapi/v2/ticket/modify"
TEMPLATE_FIELDS = "/openapi/v2/ticket/template/fields"
PAGE_SIZE = 50
_request_lock = threading.Lock()
_next_request = 0.0
_REPLY_REJECTED = frozenset((14001, 14002, 14003, 14004, 14009, 14100, 14103, 14104, 14106, 14107))
_REPLY_HTTP_REJECTED = frozenset((400, 401, 403, 404, 405, 422, 429))


class TicketError(RuntimeError):
    def __init__(self, detail: str, *, http_status: int = 502, upstream_code: int | None = None) -> None:
        super().__init__(detail)
        self.http_status = http_status
        self.upstream_code = upstream_code


class TicketReplyUnconfirmed(TicketError):
    """The reply may have been posted. Reconcile through history without resending."""


class TicketReplyVisible(TicketReplyUnconfirmed):
    """Qiyu recorded the reply as customer-visible."""


class TicketModifyUnconfirmed(TicketError):
    """Qiyu may have applied the change. Fetch the current ticket before retrying."""


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
            raise TicketError(f"七鱼工单接口不可用（HTTP {error.status}）", upstream_code=error.status) from None
        except (RuntimeError, OSError, ValueError) as error:
            if isinstance(error.__cause__, HttpError) and error.__cause__.status == 429:
                raise TicketError("七鱼工单接口请求过于频繁，请稍后重试", http_status=503, upstream_code=429) from None
            # Transport errors can contain a signed URL. Only return a safe message.
            raise TicketError("七鱼工单接口暂时无法连接") from None
        if not isinstance(result, dict):
            raise TicketError("七鱼工单接口返回了无法识别的数据")
        code = _id(result.get("code"))
        if code == 14106:
            raise TicketError("七鱼工单不存在", http_status=404, upstream_code=code)
        if code == 14009:
            raise TicketError("七鱼工单接口请求过于频繁，请稍后重试", http_status=503, upstream_code=code)
        if code != 200:
            if code is not None:
                raise TicketError(f"七鱼工单接口返回错误（{code}）", upstream_code=code)
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

    def template_fields(self, template_id: int) -> list[dict]:
        identifier = _id(template_id)
        if identifier is None:
            raise ValueError("七鱼工单模板编号无效")
        value = self._post(TEMPLATE_FIELDS, {"templateId": identifier}, "message")
        if not isinstance(value, list):
            raise TicketError("七鱼工单模板字段格式无效")
        for field in value:
            if (not isinstance(field, dict) or _id(field.get("fieldId")) is None
                    or not isinstance(field.get("name"), str) or type(field.get("type")) is not int
                    or type(field.get("status")) is not int or field.get("required") not in (0, 1, None)):
                raise TicketError("七鱼工单模板字段格式无效")
        return value

    def modify(self, ticket_id: int, staff_id: int, *, change_type: int, value: str,
               field_id: int = 0, custom_id: int = 0) -> None:
        """Update one field using the current holder's ID. The caller verifies the saved value."""
        identifier, holder = _id(ticket_id), _id(staff_id)
        if (identifier is None or holder is None or type(change_type) is not int
                or change_type not in (0, 1, 2, 3) or not isinstance(value, str)):
            raise ValueError("七鱼工单修改参数无效")
        if any(type(field) is not int or (field != 0 and _id(field) is None) for field in (field_id, custom_id)):
            raise ValueError("七鱼工单自定义字段编号无效")
        if (change_type == 0) != bool(field_id or custom_id):
            raise ValueError("七鱼工单自定义字段编号与修改类型不符")
        if (change_type == 1 and not value.strip()) or (change_type in (2, 3) and _id(value) is None):
            raise ValueError("七鱼工单修改值无效")
        try:
            accepted = self._post(MODIFY, {"ticketId": identifier, "staffId": holder, "type": change_type,
                                           "customId": custom_id, "fieldId": field_id, "value": value}, "message")
        except TicketError as error:
            code = error.upstream_code
            if code in (None, 408, 14500, 16001) or (500 <= code < 600):
                raise TicketModifyUnconfirmed("七鱼工单修改结果暂未确认，请刷新工单核对。", http_status=503) from None
            if code in (14004, 14100, 14101, 14110, 14200):
                raise TicketError("七鱼未接受本次工单修改", http_status=409, upstream_code=code) from None
            raise
        if accepted is False:
            raise TicketError("七鱼未接受本次工单修改", http_status=409)
        if accepted is not True:
            raise TicketModifyUnconfirmed("七鱼工单修改结果暂未确认，请刷新工单核对。", http_status=503)

    def has_feedback(self, ticket_id: int, text: str) -> bool:
        """Look for the exact tagged reply in the native ticket history."""
        identifier = _id(ticket_id)
        if identifier is None or not isinstance(text, str) or not text.strip():
            raise ValueError("七鱼工单回复参数无效")
        return _has_reply(self.history(identifier), text)

    def post_feedback(self, ticket_id: int, text: str) -> bool:
        """Append one native reply and confirm it in the ticket history."""
        identifier = _id(ticket_id)
        if identifier is None:
            raise ValueError("七鱼工单回复参数无效")
        if self.has_feedback(identifier, text):
            return False
        detail = self.detail(identifier)
        holder = _id(detail.get("holderId"))
        if detail.get("isCrossCorp") not in (None, 0, "0") or holder is None:
            raise TicketError("七鱼工单没有可用于回写的本企业受理客服", http_status=409)
        if _id(detail.get("status")) != 10:
            raise TicketError("七鱼工单尚未受理或已完结，当前无法添加回复", http_status=409)
        try:
            self._post(REPLY, {
                "ticketId": identifier, "staffId": holder, "comment": text, "attachments": [],
            }, "message")
        except TicketError as write_error:
            if write_error.upstream_code in _REPLY_REJECTED or write_error.upstream_code in _REPLY_HTTP_REJECTED:
                raise
        # An accepted reply may be written before the response or log becomes available.
        try:
            if self.has_feedback(identifier, text):
                return True
        except TicketReplyVisible:
            raise
        except TicketError:
            pass
        raise TicketReplyUnconfirmed("七鱼回复尚未在工单日志中确认，等待回读", http_status=503)


def _has_reply(history: list[dict], text: str) -> bool:
    expected = text.replace("\r\n", "\n").strip()
    matched = False
    for event in history:
        fields = event.get("info")
        if _id(event.get("actionType")) not in (20, 101) or not isinstance(fields, list):
            continue
        for field in fields:
            content = field.get("content") if isinstance(field, dict) else None
            if isinstance(content, str) and content.replace("\r\n", "\n").strip() == expected:
                if event.get("customerSee") in (True, 1, "1", "true", "True"):
                    raise TicketReplyVisible("七鱼将该回复记录为客户可见，请核对工单", http_status=409)
                matched = True
    return matched


def _id(value: Any) -> int | None:
    if type(value) is int:
        identifier = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        identifier = int(value)
    else:
        return None
    return identifier if 0 < identifier <= 2**63 - 1 else None
