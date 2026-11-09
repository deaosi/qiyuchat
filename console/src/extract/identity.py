"""Internal sender-control rows produced by the verified source projector.

Chat text is never parsed as a control row: the dedicated role is required.
These rows must be omitted from displayed transcripts and model context.
"""
from __future__ import annotations

import json
import re

SOURCE_IDENTITY_ROLE = "source_identity"
HIDDEN_ROLES = frozenset(("boundary", SOURCE_IDENTITY_ROLE))
_FIELDS = {"version", "session_id", "customer_id", "customer_name"}


def encode_identity(session_id: str, customer_id: str, customer_name: str) -> str:
    value = {"version": 1, "session_id": session_id, "customer_id": customer_id, "customer_name": customer_name}
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if decode_identity(text, session_id) is None:
        raise ValueError("来源访客控制记录格式无效")
    return text


def decode_identity(content: str, session_id: str) -> dict[str, str] | None:
    if not isinstance(content, str) or len(content) > 5000:
        return None
    try:
        value = json.loads(content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != _FIELDS or type(value["version"]) is not int or value["version"] != 1:
        return None
    if not isinstance(value["session_id"], str) or value["session_id"] != session_id:
        return None
    cid, name = value["customer_id"], value["customer_name"]
    if not isinstance(cid, str) or not 1 <= len(cid) <= 1000 or not cid.startswith(("qiyu-external:", "qiyu-external-sha256:", "qiyu-user:", "qiyu-session:")):
        return None
    if any(ord(char) < 32 for char in cid):
        return None
    prefix, identifier = cid.split(":", 1)
    if not identifier:
        return None
    if prefix == "qiyu-external-sha256" and not re.fullmatch("[0-9a-f]{64}", identifier):
        return None
    if prefix == "qiyu-user" and not re.fullmatch(r"-?[1-9][0-9]{0,18}", identifier):
        return None
    if prefix == "qiyu-session" and identifier != session_id:
        return None
    if not isinstance(name, str) or len(name) > 300:
        return None
    return {"customer_id": cid, "customer_name": name}


def sender_context(messages: list[dict], session_id: str, customer_id: str) -> list[dict]:
    """Only a visitor's own controlled segments, with their following replies.

    Legacy transcripts have no identity controls and retain their old context.
    Anonymous controlled identities do not prove cross-turn identity, so omit
    their optional context instead of joining potentially different visitors.
    """
    if not any(message.get("role") == SOURCE_IDENTITY_ROLE for message in messages):
        return [message for message in messages if message.get("role") not in HIDDEN_ROLES]
    if customer_id.startswith("qiyu-session:"):
        return []
    owner = None
    result = []
    for message in messages:
        role = message.get("role")
        if role == SOURCE_IDENTITY_ROLE:
            identity = decode_identity(message.get("content"), session_id)
            owner = identity["customer_id"] if identity else None
        elif role == "boundary":
            owner = None
        elif owner == customer_id:
            result.append(message)
    return result
