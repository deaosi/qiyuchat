"""Select short, scoped Qiyu examples for the VOC data assistant."""

from __future__ import annotations

from datetime import datetime
import re

from . import voc_answer
from .clean.rules import mask_pii
from .voc_knowledge_reuse import _confirmed_procedure
from .voc_search import verified_pair_reply
from .voc_service import DISPLAY_ZONE


_MAX_REFERENCES = 10
_MAX_CASE_READS = 8
_MAX_EXAMPLES = 5
_PRIVATE = re.compile(
    r"https?://[^\s，。；！？]+|qiyu-[\w-]+:[^\s，。；！？]+|"
    r"(?<![a-z0-9])(?:episode_|playbook_|case_path_|issue_|branch_|sop_|ctx_|t_|i_|k_)[a-f0-9]{12,}(?![a-z0-9])",
    re.I,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_PLACEHOLDERS = {"客户", "客服", "匿名客户", "未分组", "未记录客服", "来源访客", "未知"}


def _identity(value) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= 200 else ""


def _event_id(row: dict) -> str:
    return _identity(row.get("event_id") or row.get("id"))


def _bindings(row: dict) -> tuple | None:
    identity, qa_id, fingerprint = _event_id(row), row.get("qa_id"), row.get("source_fingerprint")
    if identity and type(qa_id) is int and qa_id > 0 and isinstance(fingerprint, str) and fingerprint:
        return identity, qa_id, fingerprint
    return None


def _exact_quote(value, source) -> str:
    if not isinstance(value, dict) or value.get("alignment") != "exact" or not isinstance(source, str):
        return ""
    start, end, quote = value.get("start"), value.get("end"), value.get("quote")
    if (type(start) is int and type(end) is int and 0 <= start < end <= len(source)
            and isinstance(quote, str) and quote.strip() and source[start:end] == quote):
        return quote
    return ""


def exact_case_pair(row: dict) -> tuple[str, str] | None:
    """Return a source-aligned question and verified reply for the same event."""
    question = _exact_quote(row.get("question_span"), row.get("question"))
    if (not question or row.get("question_quote") != question or row.get("staff_source_verified") is not True):
        return None
    claims = row.get("claims")
    if not isinstance(claims, list):
        return None
    if not claims:
        # A full anchored answer is safe only when this event covers the
        # original question. A sub-intent may have an unrelated paired reply.
        reply = verified_pair_reply(row) if question.strip() == row["question"].strip() else ""
        return (question, reply) if reply else None

    quotes = []
    ids = row.get("answer_message_ids")
    answer_ids = set(ids) if isinstance(ids, list) and all(isinstance(mid, str) for mid in ids) else set()
    anchored = row.get("source_kind") == "source-anchored"
    for claim in claims:
        quote = _exact_quote(claim, row.get("answer"))
        if not quote or claim.get("role") != "staff":
            return None
        if anchored:
            refs = claim.get("source_refs")
            if not isinstance(refs, list) or not refs or any(
                    not isinstance(ref, dict) or not isinstance(ref.get("message_id"), str)
                    or ref["message_id"] not in answer_ids for ref in refs):
                return None
        quotes.append(quote)
    return question, "\n".join(dict.fromkeys(quotes))


def _clean_quote(text: str, row: dict, limit: int) -> str:
    names = {value.strip() for key in ("customer_name", "staff_name", "group_name")
             if isinstance((value := row.get(key)), str) and len(value.strip()) >= 2} - _PLACEHOLDERS
    if names:
        pattern = "|".join(re.escape(name) for name in sorted(names, key=lambda value: (-len(value), value)))
        text = re.sub(pattern, "[名称已隐藏]", text)
    text = mask_pii(_PRIVATE.sub("[已隐藏]", _CONTROL.sub("", text))).strip()
    return text if len(text) <= limit else ""


def _current_event(engine, cfg, scoped: dict, candidate: dict) -> dict | None:
    key = _bindings(candidate)
    if not key:
        return None
    try:
        result = engine.event_detail(cfg, scoped, key[0])
    except ValueError:
        return None
    row = result.get("event") if isinstance(result, dict) else None
    if (not isinstance(row, dict) or _bindings(row) != key
            or any(row.get(field) != candidate.get(field)
                   for field in ("question_span", "question_quote", "answer", "claims", "staff_source_verified"))):
        return None
    return row


def _knowledge_cases(row: dict, identity: str) -> list[dict]:
    cases = row.get("evidence")
    if (str(row.get("id")) != identity or row.get("status") != "ready" or row.get("stale")
            or not isinstance(cases, list)):
        return []
    return [case for case in cases if isinstance(case, dict)]


def _outcomes(references: list) -> dict:
    selected = {}
    for citation, row in references:
        if citation.get("kind") != "knowledge" or voc_answer.knowledge_reuse(row)["level"] != "verified":
            continue
        cases = _knowledge_cases(row, _identity(citation.get("id")))
        decision = row.get("knowledge_reuse")
        steps = row.get("steps")
        if (not isinstance(decision, dict) or not isinstance(decision.get("evidence"), list)
                or not isinstance(steps, list) or not steps or not all(isinstance(step, str) for step in steps)):
            continue
        for proof in decision["evidence"]:
            # knowledge_reuse has already checked exact quote coordinates and
            # verified reporter identity. Recheck that it states a real effect.
            quote, stamp = proof["quote"], proof["ts"]
            if not _confirmed_procedure(steps, quote):
                continue
            keys = {key for case in cases if case.get("qa_id") == proof["qa_id"] and (key := _bindings(case))}
            if len(keys) != 1:
                continue
            key, = keys
            if key not in selected or stamp > selected[key]["ts"]:
                selected[key] = proof
    return selected


def _topic_cases(engine, cfg, scoped: dict, identity: str) -> list[dict]:
    if scoped.get("focus_topic") not in (None, "", identity):
        return []
    try:
        result = engine.topic_detail(cfg, scoped, identity)
    except ValueError:
        return []
    if not isinstance(result, dict):
        return []
    topic, cases = result.get("topic"), result.get("events")
    if not isinstance(topic, dict) or topic.get("id") != identity or not isinstance(cases, list):
        return []
    return [row for row in cases if isinstance(row, dict)]


def _display_case(row: dict, key: tuple, outcomes: dict) -> dict | None:
    pair = exact_case_pair(row)
    if pair is None:
        return None
    question, reply = _clean_quote(pair[0], row, 500), _clean_quote(pair[1], row, 1200)
    if not question or not reply:
        return None
    result = {"citation": {"kind": "event", "id": key[0], "title": question[:120]},
              "question": question, "reply": reply}
    stamp = row.get("asked_at")
    if type(stamp) is int and stamp > 0:
        try:
            result["time"] = datetime.fromtimestamp(stamp / 1000, DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
        proof = outcomes.get(key)
        if (proof and proof["ts"] > stamp
                and not any(proof["message_id"] in (row.get(field) or [])
                            for field in ("question_message_ids", "answer_message_ids"))):
            outcome = _clean_quote(proof["quote"], row, 400)
            if outcome:
                result["outcome"] = outcome
    return result


def build_examples(engine, cfg, scoped: dict, references, limit: int = 2) -> list[dict]:
    """Read representative cases only from the exact sources used for this answer."""
    if type(limit) is not int or limit <= 0:
        return []
    limit = min(limit, _MAX_EXAMPLES)
    references = [(citation, row) for citation, row in references[:_MAX_REFERENCES]
                  if isinstance(citation, dict) and isinstance(row, dict) and _identity(citation.get("id"))]
    outcomes = _outcomes(references)
    examples, seen, used_qa, used_replies = [], set(), set(), set()
    reads = 0
    # Prefer the actual event cited by the answer to an expanded topic example.
    for kind in ("event", "knowledge", "topic"):
        for citation, reference in references:
            if citation.get("kind") != kind:
                continue
            identity = citation["id"]
            if kind == "event":
                candidates = [reference] if _event_id(reference) == identity else []
            elif kind == "knowledge":
                candidates = _knowledge_cases(reference, identity)
            else:
                candidates = _topic_cases(engine, cfg, scoped, identity)
            for candidate in candidates:
                key = _bindings(candidate)
                if not key or key[0] in seen or key[1] in used_qa or exact_case_pair(candidate) is None:
                    continue
                if kind != "event":
                    if reads >= _MAX_CASE_READS:
                        return examples
                    reads += 1
                    current = _current_event(engine, cfg, scoped, candidate)
                    if current is None:
                        continue
                else:
                    current = candidate
                if (example := _display_case(current, key, outcomes)) is not None:
                    reply = " ".join(example["reply"].split())
                    if reply in used_replies:
                        continue
                    examples.append(example)
                    seen.add(key[0])
                    used_qa.add(key[1])
                    used_replies.add(reply)
                    if len(examples) == limit:
                        return examples
    return examples
