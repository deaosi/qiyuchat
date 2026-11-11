"""Read-only conversation closure certificates, separate from source compilation.

Only a completed, reproducible official message snapshot can support acceptance.
Silence/END never means success. No inference, external network or business write.
This conservative recognizer deliberately leaves unbound natural language unknown.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .clean.rules import mask_pii
from .extract.identity import SOURCE_IDENTITY_ROLE, decode_identity
from .extract.text import clean_visible_text
from .qiyu.message import source_auto_reply

VERSION = "conversation-outcome-v2"
WEAK_THRESHOLD = 2
MAX_WINDOW_MESSAGES = 80
MAX_WINDOW_MS = 30 * 60 * 1000

_NEGATIVE = re.compile(r"(?:还是|仍然|依然|仍|又|并没有|并未|没有|没|不)(?:能|法|有|太|起|成功|解决|生效|同步|正常|恢复|触发|显示|变化|作用|行|对|懂|明白)|(?:仍|还是|依然).{0,8}(?:报错|失败|问题)|(?:操作|测试|试了).{0,8}(?:失败|不行)|(?:不行|未解决|没解决|没成功|失败了|报错了|还有问题|没明白)")
_SUCCESS = re.compile(r"(?:已经|现在|这次|目前|刚才|终于|这下|可以|能|已|好|正常|成功|恢复|解决).{0,8}(?:成功了|可以了|正常了|恢复了|解决了|同步了|触发了|生效了|显示了|能用了)|^(?:好了|可以了|成功了|正常了|恢复了|解决了|能用了)[，。！!\s]*(?:谢谢|感谢)?[。！!\s]*$")
_UNDERSTOOD = re.compile(r"^(?:(?:好的?|嗯|哦|收到)[，,。\s]*)?(?:明白了|了解了|懂了|知道怎么操作了|知道了|清楚了|没有疑问了|没问题了)(?:[，,。\s]*(?:谢谢|感谢|好的?))?[。！!\s]*$")
_COURTESY = re.compile(r"^(?:好的?|嗯嗯?|收到|谢谢(?:您|你)?|感谢(?:您|你)?|了解|OK|ok|没别的了|没有其他问题了|没有其它问题了|再见)(?:[，,。！!\s]*(?:好的?|谢谢(?:您|你)?|感谢|再见))*[。！!\s]*$")
_QUESTION = re.compile(r"[?？]|[吗么呢][。！!\s]*$|(?:怎么|如何|为什么|为啥|能否|是否|可不可以|还有什么|什么时候|需要多久)")
_PROSPECTIVE = re.compile(r"希望|应该|可能|大概|似乎|估计|如果|要是|假如|的话|准备|打算|试试|试一下|试试看|等会|待会|稍后|将会|但|不过|可是")
_STAFF_CLOSING = re.compile(r"^(?:不客气|不用谢|好的?|收到|有问题随时联系(?:我们|我)?|有问题随时咨询)(?:[，,。！!\s]+(?:不客气|不用谢|好的?|有问题随时联系(?:我们|我)?|有问题随时咨询))*[。！!\s]*$")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def unknown(reason: str = "source_unavailable") -> dict:
    return {"status": "unknown", "acceptance": "unknown", "effect": "unknown", "auto_reusable": False,
            "evidence": [], "coverage": {"sources": 0, "complete_sources": 0, "incomplete_sources": 0,
            "accepted_sources": 0, "negative_sources": 0, "weak_sources": 0, "unknown_sources": 0,
            "independent_weak_supports": 0, "weak_threshold": WEAK_THRESHOLD},
            "reason_codes": [reason], "version": VERSION, "fingerprint": _digest([VERSION, reason])}


def _text(value: str) -> str:
    return mask_pii(clean_visible_text(value)).strip()


def _snapshot(conn, sid: str) -> dict:
    """Reproduce the official role/identity projection, never trust staffId alone."""
    from .push_sync import _project, PushSyncError, VERSION as PROJECTION_VERSION
    state = conn.execute("SELECT * FROM push_sync_state WHERE session_id=?", (sid,)).fetchone()
    if not state or state["status"] != "completed" or not state["source_json"] or state["source_version"] != state["version"]:
        raise ValueError("incomplete_ledger")
    snapshot = json.loads(state["source_json"])
    if not isinstance(snapshot, dict) or snapshot.get("projection_version") != PROJECTION_VERSION:
        raise ValueError("unverified_projection")
    metadata, official = snapshot["session"], snapshot["messages"]
    if not isinstance(metadata, dict) or not isinstance(official, list):
        raise ValueError("invalid_snapshot_shape")
    try:
        projected = _project(metadata, official, metadata, int(sid))
    except (PushSyncError, TypeError, KeyError):
        raise ValueError("invalid_official_projection") from None
    raw = [dict(row) for row in conn.execute("SELECT role,content,ts,source_message_id FROM raw_message WHERE session_id=? ORDER BY seq", (sid,))]
    fields = ("role", "content", "ts", "source_message_id")
    if [[r.get(k) for k in fields] for r in raw] != [[r.get(k) for k in fields] for r in projected]:
        raise ValueError("projection_mismatch")
    # Equal timestamps do not establish whether feedback followed the answer.
    metadata_end = metadata["endTime"]
    if any(r["time"] > metadata_end for r in official):
        raise ValueError("invalid_end_coverage")
    kinds = {f"source:{sid}:{r['id']}": r["mType"] for r in official}
    automated = {f"source:{sid}:{r['id']}" for r in official if r["from"] == 0 and source_auto_reply(r)}
    owner, segment = None, 0
    for message in raw:
        if message["role"] == SOURCE_IDENTITY_ROLE:
            identity = decode_identity(message["content"], sid)
            owner = identity["customer_id"] if identity else None
            segment += 1
        elif message["role"] == "boundary":
            owner = None
            segment += 1
        message["_owner"], message["_segment"] = owner, segment
        source_id = message.get("source_message_id")
        message["_is_text"] = kinds.get(source_id) == 1
        message["_automated"] = source_id in automated
    return {"messages": raw, "version": state["source_version"], "end": metadata_end}


def _ref(message: dict, qa_id: int, kind: str) -> dict:
    quote = _text(message["content"])
    return {"quote": quote, "role": "merchant", "message_id": message["source_message_id"],
            "ts": message["ts"], "qa_id": qa_id, "kind": kind,
            "span_coordinate": "clean_visible_text",
            "source_refs": [{"message_id": message["source_message_id"], "start": 0, "end": len(quote)}]}


def _assess(conn, row: dict, all_events: list[dict], snapshots: dict, matches_success=None) -> dict:
    qa_id = int(row["qa_id"])
    result = {"status": "unknown", "acceptance": "unknown", "effect": "unknown", "complete": False,
              "evidence": [], "reasons": [], "key": str(qa_id), "fingerprint": ""}
    original = conn.execute("SELECT session_id,customer_id FROM qa_pair WHERE qa_id=?", (qa_id,)).fetchone()
    origin = conn.execute("SELECT * FROM qa_origin WHERE qa_id=?", (qa_id,)).fetchone()
    if not original or not origin or not origin["active"] or not row.get("source", {}).get("_staff_verified"):
        result["reasons"].append("unverified_current_answer")
        return result
    sid, cid = str(original["session_id"]), original["customer_id"] or ""
    result["key"] = _digest([sid, cid])
    expected_origin = row["source"].get("_origin_fingerprint")
    if expected_origin and list(expected_origin) != [origin["fingerprint"], origin["revision"]]:
        result["reasons"].append("source_changed")
        return result
    if cid.startswith("qiyu-session:"):
        result["reasons"].append("visitor_identity_unknown")
        return result
    try:
        if sid not in snapshots:
            snapshots[sid] = _snapshot(conn, sid)
        snap = snapshots[sid]
        if isinstance(snap, Exception):
            raise snap
        messages = snap["messages"]
        qids, aids = json.loads(origin["question_message_ids"]), json.loads(origin["answer_message_ids"])
        if not qids or not aids or not isinstance(qids, list) or not isinstance(aids, list):
            raise ValueError("missing_source_anchors")
        by_id = {m.get("source_message_id"): (i, m) for i, m in enumerate(messages) if m.get("source_message_id")}
        questions, answers = [by_id[key] for key in qids], [by_id[key] for key in aids]
        segment = answers[-1][1]["_segment"]
        if any(m["role"] != "user" or m["_owner"] != cid or m["_segment"] != segment or not m["_is_text"] for _,m in questions):
            raise ValueError("question_identity_unbound")
        if any(m["role"] != "staff" or m["_owner"] != cid or m["_segment"] != segment
               or not m["_is_text"] or m["_automated"] for _,m in answers):
            raise ValueError("answer_role_unbound")
        if max(i for i,_ in questions) >= min(i for i,_ in answers):
            raise ValueError("invalid_anchor_order")
        result["complete"] = True
        start, last = max(answers, key=lambda p:p[0])
        result["fingerprint"] = _digest([snap["version"], origin["fingerprint"], origin["revision"], messages[start:], row.get("id")])
    except (ValueError, TypeError, KeyError, OverflowError):
        result["reasons"].append("incomplete_source_coverage")
        return result

    # A generic acknowledgement cannot be allocated to several intents sharing
    # one QA/answer. Keep unknown rather than accepting every extracted intent.
    siblings = [e for e in all_events if e.get("qa_id") == qa_id
                or set(e.get("source", {}).get("answer_message_ids", [])) & set(aids)]
    if row.get("data", {}).get("source_intent_count", 1) > 1 or len({e.get("id") for e in siblings}) > 1:
        result["reasons"].append("ambiguous_multiple_intents")
        return result
    topic_id = row.get("topic_id")
    question_topics = {}
    for event in all_events:
        event_source = event.get("source", {})
        for anchor in event_source.get("question_message_ids", []):
            question_topics.setdefault(anchor, set()).add(event.get("topic_id"))
    intervened, changed_topic, unbound_question, partial = False, False, False, False
    accepted, courtesy = None, None
    relevant = 0
    for index,message in enumerate(messages[start + 1:], start + 1):
        if message["_owner"] != cid or message["_segment"] != segment:
            # Do not join disjoint visitor segments, even if a customer reenters.
            break
        if message["role"] in ("bot", "boundary") or not message["_is_text"] or message["_automated"]:
            partial = True
            break
        relevant += 1
        if relevant > MAX_WINDOW_MESSAGES or message["ts"] - last["ts"] > MAX_WINDOW_MS:
            partial = True
            break
        if message["role"] == "staff":
            if _STAFF_CLOSING.fullmatch(_text(message["content"])):
                continue
            intervened = True
            accepted, courtesy = None, None
            continue
        if message["role"] != "user":
            continue
        if message["ts"] <= last["ts"]:
            partial = True
            break
        text = _text(message["content"])
        if len(text) > 1000:
            partial = True
            break
        topics = question_topics.get(message.get("source_message_id"), set())
        if topics and topic_id not in topics:
            changed_topic = True
            # A topic transition is not permanent closure if the same visitor
            # later reopens this goal. Do not allocate later success backwards.
            for later in messages[index+1:]:
                if later.get("_owner") == cid and later["role"] == "user" and topic_id in question_topics.get(later.get("source_message_id"), set()):
                    result["reasons"].append("same_topic_reopened_after_transition")
                    result["evidence"].append(_ref(later,qa_id,"reopened_goal"))
                    result.update(acceptance="continuing",effect="not_established")
                    return result
            break
        # A later same-goal failure revokes an earlier acknowledgement, even
        # after another human intervention. It is not a global method verdict.
        if _NEGATIVE.search(text):
            result.update(status="negative", acceptance="continuing", effect="customer_reported_failure")
            result["evidence"].append(_ref(message, qa_id, "negative_feedback"))
            result["reasons"].append("customer_continues_or_reports_failure")
            return result
        if _QUESTION.search(text):
            unbound_question = True
            result["evidence"].append(_ref(message, qa_id, "continuing_question"))
            continue
        if intervened or unbound_question:
            # Later success belongs to the new intervention, not this answer.
            continue
        positive = re.sub(r"[，,。！!\s]*(?:谢谢|感谢)(?:您|你)?[。！!\s]*$", "", text).strip("，,。！! ")
        if not _PROSPECTIVE.search(text) and (_SUCCESS.fullmatch(positive) or
                                               matches_success is not None and matches_success(row, text)):
            accepted = ("resolved", "customer_reported_success", message)
        elif _UNDERSTOOD.fullmatch(text):
            accepted = ("understood", "not_established", message)
        elif _COURTESY.fullmatch(text):
            courtesy = message
        elif text:
            # May be a store name, parameter answer, or a fresh goal. An
            # unclassified reply is never silently interpreted as agreement.
            unbound_question = True
    if partial:
        result["complete"] = False
        result["reasons"].append("feedback_window_incomplete")
        return result
    if unbound_question:
        result.update(acceptance="continuing", effect="not_established")
        result["reasons"].append("continuing_or_unbound_reply")
        return result
    if accepted:
        result.update(status="accepted", acceptance=accepted[0], effect=accepted[1])
        result["evidence"].append(_ref(accepted[2], qa_id, accepted[0]))
        result["reasons"].append("explicit_customer_" + accepted[0])
    elif courtesy and not intervened:
        result.update(status="weak", acceptance="weak_closure", effect="not_established")
        result["evidence"].append(_ref(courtesy, qa_id, "weak_acknowledgement"))
        result["reasons"].append("acknowledgement_then_topic_change" if changed_topic else "customer_closing_acknowledgement")
    else:
        result["reasons"].append("new_intervention_without_bound_acceptance" if intervened else "no_customer_acceptance_evidence")
    return result


def evaluate(cfg, rows: list[dict], *, events: list[dict] | None = None, matches_success=None) -> dict:
    """Evaluate every current cited source in this branch/scope, never a sample.

    Explicit acceptance can support a bounded reusable answer; 'understood' is
    not operational success. Two independent weak closures are the declared
    weaker policy. Any observed negative or incomplete source withholds reuse.
    """
    result = unknown("no_current_evidence")
    if not rows:
        return result
    db = Path(cfg.runtime.db_path).resolve()
    if not db.is_file():
        return unknown()
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"qa_pair", "qa_origin", "raw_message", "push_sync_state"} <= tables:
            return unknown("complete_message_ledger_missing")
        snapshots = {}
        unique = {row.get("id", str(row["qa_id"])): row for row in rows}
        assessed = [_assess(conn, row, events if events is not None else rows, snapshots, matches_success)
                    for row in unique.values()]
    except (sqlite3.Error, ValueError, TypeError, KeyError, RecursionError):
        return unknown("source_verification_unavailable")
    finally:
        conn.close()
    counts = Counter(r["status"] for r in assessed)
    complete = sum(r["complete"] for r in assessed)
    weak = len({r["key"] for r in assessed if r["status"] == "weak"})
    result["coverage"] = {"sources": len(assessed), "complete_sources": complete,
        "incomplete_sources": len(assessed)-complete, "accepted_sources": counts["accepted"],
        "negative_sources": counts["negative"], "weak_sources": counts["weak"], "unknown_sources": counts["unknown"],
        "independent_weak_supports": weak, "weak_threshold": WEAK_THRESHOLD,
        "max_window_messages": MAX_WINDOW_MESSAGES, "max_window_minutes": MAX_WINDOW_MS//60000}
    result["evidence"] = list({(e["message_id"], e["kind"]):e for r in assessed for e in r["evidence"]}.values())
    result["reason_codes"] = sorted({reason for r in assessed for reason in r["reasons"]})
    result["fingerprint"] = _digest([VERSION, sorted((r["key"], r["fingerprint"], r["status"]) for r in assessed)])
    if counts["negative"]:
        result.update(status="negative", acceptance="continuing", effect="customer_reported_failure")
    elif complete != len(assessed):
        result["reason_codes"].append("incomplete_branch_coverage")
    elif any(r["acceptance"] == "continuing" for r in assessed):
        result.update(acceptance="continuing", effect="not_established")
        result["reason_codes"].append("branch_has_unclosed_followup")
    elif counts["accepted"] or weak >= WEAK_THRESHOLD:
        resolved = any(r["acceptance"] == "resolved" for r in assessed)
        result.update(status="accepted", auto_reusable=True,
                      acceptance="resolved" if resolved else "understood" if counts["accepted"] else "weak_closure",
                      effect="customer_reported_success" if resolved else "not_established")
        if not counts["accepted"]:
            result["reason_codes"].append("repeated_independent_weak_closure")
    elif counts["weak"]:
        result.update(status="weak", acceptance="weak_closure", effect="not_established")
    return result
