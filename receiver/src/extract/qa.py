"""从逐句对话里抽出「客户问题 → 客服回答」配对。

规则：
- 连续多条客户消息合并成一个问题（客户常分几段发）
- 遇到客服消息就闭合这一问，客服的连续多条也合并成一段回答
- 客户问了但一直没人回 → 也记录，answer 为空（这类恰恰是最该看的）
- 纯情绪/确认类短句丢弃，不算问题
"""

from __future__ import annotations

from typing import Any

from .normalize import is_info_not_question, is_noise_question, normalize
from .text import clean_visible_text
from .identity import SOURCE_IDENTITY_ROLE, decode_identity

# 消息无时间时可用已知会话时间；显式 0 表示时间未知，不估算时间间隔。


def extract_pairs(
    messages: list[dict[str, Any]],
    session: dict[str, Any],
    min_len: int = 3,
) -> list[dict[str, Any]]:
    """返回 qa_pair 记录列表。"""
    if not messages:
        return []

    customer_id = str(session.get("user_id") or session.get("userId") or "")
    customer_name = session.get("user_name") or session.get("userName") or ""
    group_name = session.get("from_group") or session.get("fromGroup") or ""
    staff_name = session.get("staff_name") or session.get("staffName") or ""
    base_ts = int(session.get("start_ts") or session.get("startTime") or 0)

    pairs: list[dict[str, Any]] = []
    q_buf: list[str] = []
    a_buf: list[str] = []
    q_ts: int = 0
    idx = 0
    staff_turn_seen = False

    def flush(answer: str = "") -> None:
        nonlocal q_buf, a_buf, q_ts, idx, staff_turn_seen
        if not q_buf:
            a_buf = []
            staff_turn_seen = False
            return
        question = "\n".join(q_buf).strip()
        ok = (question
              and not is_noise_question(question)
              and not is_info_not_question(question)
              and len(normalize(question)) >= min_len)
        if ok:
            idx += 1
            pairs.append({
                "idx": idx,
                "session_id": str(session.get("session_id") or session.get("id") or ""),
                "customer_id": customer_id,
                "customer_name": customer_name,
                "group_name": group_name,
                "staff_name": staff_name,
                "asked_at": q_ts,
                "question": question,
                "answer": (answer or "\n".join(a_buf)).strip(),
                "q_norm": normalize(question),
            })
        q_buf = []
        a_buf = []
        staff_turn_seen = False

    for m in messages:
        role = m.get("role", "")
        if role == SOURCE_IDENTITY_ROLE:
            flush()
            identity = decode_identity(m.get("content"), str(session.get("session_id") or session.get("id") or ""))
            customer_id = identity["customer_id"] if identity else ""
            customer_name = identity["customer_name"] if identity else "身份未知访客"
            continue
        if role == "boundary":
            flush()
            continue
        raw_content = (m.get("content") or "").strip()
        if role == "staff" and raw_content and q_buf:
            staff_turn_seen = True
        content = clean_visible_text(raw_content)
        if not content:
            continue
        ts = int(m.get("ts", base_ts) or 0)

        if role == "user":
            if a_buf or staff_turn_seen:  # A media-only staff turn still closes Q1.
                flush()
            if is_noise_question(content) or is_info_not_question(content):
                continue
            if not q_buf:
                q_ts = ts
            q_buf.append(content)
        elif role == "staff":
            if q_buf:
                a_buf.append(content)
        # bot / system 消息忽略，不影响配对

    flush()                               # 最后一问可能没被回答
    return pairs
