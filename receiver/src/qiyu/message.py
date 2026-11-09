"""Validate the observed official single-session source-message schema."""
from __future__ import annotations

from typing import Any


def source_integer(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and (0 < value < 2**63 if positive else -(2**63) <= value < 2**63)


def validate_messages(data: Any, session_id: int) -> list[dict[str, Any]]:
    """Keep original content/metadata; never guess unverified wrapper keys.

    Unknown directions/statuses remain source records. The consumer must not
    turn them into customer or human staff messages.
    """
    if not source_integer(session_id, positive=True):
        raise ValueError("会话 ID 须为正整数")
    if not isinstance(data, list) or len(data) > 50_000:
        raise ValueError("单会话消息响应须为官方消息数组")
    seen: set[int] = set()
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("单会话消息记录格式无效")
        if not source_integer(row.get("id"), positive=True) or row["id"] in seen:
            raise ValueError("单会话消息 ID 无效或重复")
        if type(row.get("sessionId")) is not int or row["sessionId"] != session_id:
            raise ValueError("单会话消息的来源会话不一致")
        if any(not source_integer(row.get(k)) for k in ("from", "staffId", "mType", "status")):
            raise ValueError("单会话消息方向、身份、类型或状态字段无效")
        if not source_integer(row.get("time"), positive=True):
            raise ValueError("单会话消息时间字段无效")
        if not isinstance(row.get("msg"), str) or len(row["msg"]) > 2_000_000:
            raise ValueError("单会话消息正文格式无效或过长")
        if "userId" in row and not source_integer(row["userId"]):
            raise ValueError("单会话消息访客身份字段无效")
        if "externalUserId" in row and not isinstance(row["externalUserId"], str):
            raise ValueError("单会话消息外部访客身份字段无效")
        seen.add(row["id"])
    return data
