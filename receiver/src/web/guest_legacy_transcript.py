"""Recover only the original messages of a retained legacy question and answer."""
from __future__ import annotations

from ..extract import extract_pairs
from ..extract.identity import SOURCE_IDENTITY_ROLE


def match_pair(pair: dict, rows: list[dict]) -> tuple[list[dict], int] | None:
    """Return the original rows and the sequence of the first question message."""
    asked_at = pair.get("asked_at")
    if type(asked_at) is not int or asked_at <= 0:
        return None

    originals = {}
    replay = []
    previous_seq = -1
    for row in rows:
        seq = row.get("context_seq")
        if row.get("role") == SOURCE_IDENTITY_ROLE or type(seq) is not int or seq <= previous_seq:
            return None
        previous_seq = seq
        anchor = str(seq)
        originals[anchor] = row
        replay.append({**row, "source_message_id": anchor})

    session = {"session_id": pair.get("session_id"), "user_id": pair.get("customer_id")}
    matching = [candidate for candidate in extract_pairs(replay, session)
                if (candidate["asked_at"], candidate["question"], candidate["answer"])
                == (asked_at, pair.get("question"), pair.get("answer"))]
    if len(matching) != 1:
        return None

    question_ids = matching[0]["question_message_ids"]
    answer_ids = matching[0]["answer_message_ids"]
    return [originals[anchor] for anchor in question_ids + answer_ids], int(question_ids[0])
