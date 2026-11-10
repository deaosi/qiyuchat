"""Incremental VOC v2 shadow engine and bounded synchronous service contract.

The business database is opened read-only. All v2 data lives in a sibling derived
database. No legacy topic, knowledge asset, raw message or QA row is modified.
External inference and local embedding run without an open SQLite connection.
"""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import voc, voc_assets as assets, voc_quality as quality, voc_variants as variants
from .clean.rules import mask_pii
from .config import Settings, load_settings
from .extract.text import clean_visible_text
from .extract.identity import SOURCE_IDENTITY_ROLE, sender_context
from .knowledge_model import _staff_answer
from .voc_extract import (VERSION, MAX_OUTPUT_TOKENS, MAX_SOURCE_CHARS, GatewayProvider,
                          LangExtractAligner, canonical, request_payload, validate, output_token_limit, correction_feedback)
from .voc_model import VocModelError
from .voc_semantics import LocalBGE, SemanticError, retrieve

PIPELINE_VERSION = VERSION + "/" + quality.VERSION


class EngineError(voc.VocError):
    pass


def path(cfg: Settings) -> Path:
    return assets.store_path(cfg)


def _when(stamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else ""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _sources(cfg: Settings, filters: dict) -> dict[int, dict]:
    source_path = Path(cfg.runtime.db_path).resolve()
    if not source_path.is_file():
        return {}
    conn = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")  # Consistent read snapshot, closed before any model work.
        source = voc._scope(conn, filters)
        tables = _tables(conn)
        anchored_column = "raw_message" in tables and "source_message_id" in {row[1] for row in conn.execute("PRAGMA table_info(raw_message)")}
        for qa_id in list(source):
            row = source[qa_id]
            row["_domain_mode"] = variants.mode(cfg)
            original = conn.execute("SELECT session_id,customer_id FROM qa_pair WHERE qa_id=?", (qa_id,)).fetchone()
            raw_sid, raw_customer = original["session_id"], original["customer_id"] or ""
            row["_session_key"] = "s_" + assets.digest(["raw-session", raw_sid])
            row.pop("messages", None)  # Control rows/context never enter v2 prompts.
            row["answer"] = _staff_answer(row["answer"])
            origin = conn.execute("SELECT * FROM qa_origin WHERE qa_id=?", (qa_id,)).fetchone() if "qa_origin" in tables else None
            if origin is not None and not origin["active"]:
                del source[qa_id]
                continue
            question_ids, answer_ids = [], []
            if origin:
                try:
                    question_ids = json.loads(origin["question_message_ids"])
                    answer_ids = json.loads(origin["answer_message_ids"])
                except (ValueError, TypeError):
                    question_ids, answer_ids = [], []
                if not all(isinstance(value, list) and all(isinstance(item, str) and item for item in value) for value in (question_ids, answer_ids)):
                    question_ids, answer_ids = [], []
            row.update(question_message_ids=question_ids, answer_message_ids=answer_ids,
                       source_kind="source-anchored" if question_ids and origin else "legacy-reference",
                       _evidence_key=origin["evidence_key"] if origin and question_ids else
                       "legacy:" + assets.digest([row["_session_key"], row["customer_id"], qa_id]))
            row["_origin_fingerprint"] = [origin["fingerprint"], origin["revision"]] if origin else None
            parts = []
            if question_ids and anchored_column:
                marks = ",".join("?" for _ in question_ids)
                messages = {message["source_message_id"]: dict(message) for message in conn.execute(
                    f"SELECT source_message_id,role,content FROM raw_message WHERE session_id=? AND source_message_id IN ({marks})",
                    (origin["session_id"] if origin else row["session_id"], *question_ids))}
                chunks, offset = [], 0
                for message_id in question_ids:
                    message = messages.get(message_id, {})
                    if message.get("role") != "user":
                        break
                    content = mask_pii(clean_visible_text(str(message.get("content") or "").strip()))
                    chunks.append(content)
                    parts.append({"message_id": message_id, "start": offset, "end": offset + len(content)})
                    offset += len(content) + 1
                if "\n".join(chunks).strip() != row["question"]:
                    parts = []
            row["_question_parts"] = parts
            if question_ids and not parts:
                row["source_kind"] = "evidence-key-reference"
            row["_answer_parts"] = []
            row["_staff_verified"] = False
            if row["answer"] and answer_ids and anchored_column:
                marks = ",".join("?" for _ in answer_ids)
                answer_messages = {message["source_message_id"]: dict(message) for message in conn.execute(
                    f"SELECT source_message_id,role,content FROM raw_message WHERE session_id=? AND source_message_id IN ({marks})",
                    (raw_sid, *answer_ids))}
                chunks, answer_parts, offset = [], [], 0
                for message_id in answer_ids:
                    message = answer_messages.get(message_id, {})
                    if message.get("role") != "staff":
                        break
                    content = _staff_answer(message.get("content"))
                    if content:
                        chunks.append(content)
                        answer_parts.append({"message_id": message_id, "start": offset, "end": offset + len(content)})
                        offset += len(content) + 1
                if "\n".join(chunks) == row["answer"]:
                    row["_answer_parts"], row["_staff_verified"] = answer_parts, True
            elif row["answer"] and "raw_message" in tables:
                messages = [dict(message) for message in conn.execute(
                    "SELECT role,content,ts FROM raw_message WHERE session_id=? ORDER BY seq", (raw_sid,))]
                visible = sender_context(messages, raw_sid, raw_customer)
                staff = "\n".join(_staff_answer(message.get("content")) for message in visible if message.get("role") == "staff")
                row["_staff_verified"] = row["answer"] in staff
            row["_context"] = []
            if row["_domain_mode"] and anchored_column and question_ids:
                raw_context = [dict(message) for message in conn.execute(
                    "SELECT seq,role,content,ts,source_message_id FROM raw_message WHERE session_id=? ORDER BY seq", (raw_sid,))]
                if any(message["role"] == SOURCE_IDENTITY_ROLE for message in raw_context):
                    visible = sender_context(raw_context, raw_sid, raw_customer)
                    anchors = {message_id for message_id in question_ids + answer_ids}
                    current = [index for index, message in enumerate(visible) if message.get("source_message_id") in anchors]
                    if current:
                        near = visible[max(0, min(current) - 3):max(current) + 4]
                        remaining = 1200
                        for message in near:
                            mid = message.get("source_message_id")
                            if not mid or mid in anchors or message["role"] not in ("user", "staff"):
                                continue
                            text = mask_pii(clean_visible_text(str(message.get("content") or "")))
                            if not text or len(text) > min(400, remaining):
                                continue
                            row["_context"].append({"source_ref": "ctx_" + assets.digest(mid)[:20],
                                "role": "merchant" if message["role"] == "user" else "staff", "content": text,
                                "comparison_only": True})
                            remaining -= len(text)
            fingerprint_data = {key: row[key] for key in (
                "session_id", "customer_id", "question", "answer", "asked_at", "group_name", "staff_name",
                "question_message_ids", "answer_message_ids", "_evidence_key", "_staff_verified", "_origin_fingerprint", "_session_key")}
            if row["_domain_mode"]:
                fingerprint_data.update(domain_pipeline=variants.pipeline(cfg), context=row["_context"])
            row["_fingerprint"] = assets.digest(fingerprint_data)
        return source
    finally:
        conn.close()


def _event_identity(source: dict, span: dict) -> str:
    refs = [{"message_id": part["message_id"], "start": max(span["start"], part["start"]) - part["start"],
             "end": min(span["end"], part["end"]) - part["start"]}
            for part in source["_question_parts"] if part["start"] < span["end"] and span["start"] < part["end"]]
    anchor = refs or [source["_evidence_key"], span["start"], span["end"]]
    return "i_" + assets.digest([quality.session_key(source), source["customer_id"], anchor])[:28]


def _snapshot(source: dict) -> dict:
    return {key: value for key, value in source.items() if not key.startswith("_")}


def _stored_topics(store: Path, *, include_ignored: bool = False, domain_mode: str | None = None) -> list[dict]:
    if not store.is_file():
        return []
    with assets.connection(store) as conn:
        if "v2_topic" not in _tables(conn):
            return []
        result = []
        has_domain = "domain" in {row[1] for row in conn.execute("PRAGMA table_info(v2_topic)")}
        for row in conn.execute("SELECT * FROM v2_topic ORDER BY id"):
            if domain_mode is not None and (row["domain"] if has_domain else "") != domain_mode:
                continue
            if row["status"] == "ignored" and not include_ignored:
                continue
            item = dict(row)
            item["scope_evidence"] = json.loads(item.pop("scope_json"))
            result.append(item)
        return result


def _candidate_examples(cfg: Settings, candidates: list[dict]) -> list[dict]:
    """At most two live evidence examples per TopK topic, comparison only.

    Never consult historical v2_source snapshots. _events revalidates current
    source fingerprints and roles; incomplete/long staff text is omitted rather
    than cut into a potentially unqualified factual fragment.
    """
    if not candidates:
        return candidates
    samples: dict[str, list] = {candidate["id"]: [] for candidate in candidates}
    for row in _events(cfg, {}):
        bucket = samples.get(row["topic_id"])
        if bucket is None or len(bucket) >= 2:
            continue
        data, source = row["data"], row["source"]
        question = data["question_span"]["quote"]
        if len(question) > 200:
            continue
        facts = "\n".join(claim["quote"] for claim in data["claims"])
        complete = (data.get("claim_context", {}).get("context_complete")
                    and quality.claim_context_checks(source["answer"], [data])[0]["full_sentences"])
        verified = source.get("_staff_verified") and complete and len(facts) <= 400
        bucket.append({"question": question, "staff_facts": [facts] if facts and verified else [],
                       "comparison_only": True})
    return [{**candidate, "examples": samples[candidate["id"]]} for candidate in candidates]


def _events(cfg: Settings, filters: dict, *, _current: dict | None = None) -> list[dict]:
    current, store = _sources(cfg, filters) if _current is None else _current, path(cfg)
    if not store.is_file():
        return []
    with assets.connection(store) as conn:
        if "v2_intent" not in _tables(conn):
            return []
        result = []
        for raw in conn.execute("""SELECT i.* FROM v2_intent i JOIN v2_topic t ON t.id=i.topic_id
          JOIN v2_source s ON s.qa_id=i.qa_id AND s.fingerprint=i.fingerprint
          WHERE i.active=1 AND t.status!='ignored' AND s.pipeline=? AND s.state='done'""", (variants.pipeline(cfg),)):
            source = current.get(raw["qa_id"])
            if source and source["_fingerprint"] == raw["fingerprint"]:
                data = json.loads(raw["payload"])
                domain = data.get("domain", {})
                if variants.mode(cfg):
                    if filters.get("support_intent", "all") not in ("", "all", domain.get("primary_intent")):
                        continue
                    if filters.get("diagnosis_status", "all") not in ("", "all", domain.get("diagnosis", {}).get("status")):
                        continue
                result.append({**dict(raw), "data": data, "source": source})
        return sorted(result, key=lambda row: (-row["source"]["asked_at"], row["id"]))


def _public_topic(topic: dict, events: list[dict]) -> dict:
    kinds = {row["data"].get("domain", {}).get("primary_intent", "") for row in events}
    return {"id": topic["id"], "question": topic["question"], "theme": topic["theme"],
            "boundary": topic["boundary"], "revision": topic["revision"],
            "event_count": len(events), "session_count": len({quality.session_key(row["source"]) for row in events}),
            "consultation_count": len({variants.consultation_key(row) for row in events}),
            "primary_intent": "functional_anomaly" if "functional_anomaly" in kinds else "configuration_guidance" if "configuration_guidance" in kinds else "unclear" if "unclear" in kinds else "",
            "customer_count": len({row["source"]["customer_id"] for row in events}),
            "mention_count": len(events), "answered_count": sum(bool(row["source"]["answer"]) for row in events),
            "last_seen": max((row["source"]["asked_at_text"] for row in events if row["source"]["asked_at"]), default=""),
            "samples": list(dict.fromkeys(row["data"]["question_span"]["quote"] for row in events))[:5]}


def _topic_items(cfg: Settings, events: list[dict]) -> list[dict]:
    items = [_public_topic(topic, [row for row in events if row["topic_id"] == topic["id"]]) for topic in _stored_topics(path(cfg), domain_mode=variants.mode(cfg))]
    return sorted([item for item in items if item["event_count"]], key=lambda item: (-item["consultation_count"], -item["event_count"], item["id"]))


def topics(cfg: Settings, filters: dict) -> dict:
    items = _topic_items(cfg, _events(cfg, filters))
    return {"items": items, "total": len(items)}


def topic_detail(cfg: Settings, filters: dict, topic_id: str) -> dict:
    topic = next((item for item in _stored_topics(path(cfg), domain_mode=variants.mode(cfg)) if item["id"] == topic_id), None)
    rows = [row for row in _events(cfg, filters) if row["topic_id"] == topic_id]
    if topic is None or not rows:
        raise EngineError("当前范围内没有该问题。", 404)
    evidence = [quality.evidence(row) for row in rows]
    return {"topic": _public_topic(topic, rows), "events": evidence, "evidence": evidence, "total": len(rows)}


def event_detail(cfg: Settings, filters: dict, event_id: str) -> dict:
    row = next((row for row in _events(cfg, filters) if row["id"] == event_id), None)
    if row is None:
        raise EngineError("当前范围内没有该咨询事件，来源可能已更新。", 404)
    return {"event": {**quality.evidence(row), "topic_id": row["topic_id"], "revision": row["revision"]}}


def defects(cfg: Settings, filters: dict) -> dict:
    items = [{**quality.evidence(row), "topic_id": row["topic_id"], "revision": row["revision"]}
             for row in _events(cfg, filters) if row["data"].get("domain", {}).get("diagnosis", {}).get("status") == "defect_confirmed"]
    return {"items": items, "total": len(items)}


def _duplicate_context(events: list[dict]) -> dict[str, list]:
    links: dict[str, list] = {}
    for row in events:
        for target in row["data"].get("possible_duplicate", []):
            link = [row["id"], row["fingerprint"], row["topic_id"], target]
            for topic_id in {row["topic_id"], target}:
                links.setdefault(topic_id, []).append(link)
    return {topic_id: sorted(rows) for topic_id, rows in links.items()}


def _compile_input(topic: dict, rows: list[dict], duplicate_links: list) -> str:
    return assets.digest([topic["id"], topic["revision"],
                          sorted((row["id"], row["fingerprint"], row["revision"]) for row in rows), duplicate_links])


def _knowledge_plans(cfg: Settings, events: list[dict]) -> list[dict]:
    duplicates = _duplicate_context(events)
    result = []
    for topic in _stored_topics(path(cfg), domain_mode=variants.mode(cfg)):
        rows = [row for row in events if row["topic_id"] == topic["id"]]
        if not rows:
            continue
        for plan in variants.plans(cfg, topic, rows):
            links = duplicates.get(topic["id"], [])
            value = _compile_input(topic, plan["rows"], links)
            if variants.mode(cfg):
                value = assets.digest([value, variants.pipeline(cfg), plan["key"], plan.get("conflict_links", [])])
            result.append({**plan, "topic": topic, "links": links, "compile_input": value})
    return result


def _latest_assets(cfg: Settings, *, _current: dict | None = None) -> list[dict]:
    store, current = path(cfg), _sources(cfg, {}) if _current is None else _current
    if not store.is_file():
        return []
    events = _events(cfg, {}, _current=current)
    expected_inputs = {(plan["topic"]["id"], plan["key"]): plan["compile_input"] for plan in _knowledge_plans(cfg, events)}
    with assets.connection(store) as conn:
        if "v2_asset" not in _tables(conn):
            return []
        result = []
        has_variants = "variant_key" in {row[1] for row in conn.execute("PRAGMA table_info(v2_asset)")}
        by_variant = " AND b.variant_key=a.variant_key" if has_variants else ""
        for row in conn.execute("SELECT a.* FROM v2_asset a JOIN v2_topic t ON t.id=a.topic_id WHERE t.status!='ignored' AND a.version=(SELECT MAX(b.version) FROM v2_asset b WHERE b.topic_id=a.topic_id" + by_variant + ")"):
            item = dict(row)
            data = json.loads(item.pop("payload"))
            item.update(data)
            item["status"] = row["status"]
            item["id"] = str(row["id"])
            item["updated_at"] = _when(row["updated_at"])
            item["created_at"] = _when(row["created_at"])
            item["variant_key"] = row["variant_key"] if has_variants else ""
            # Cross-database updates cannot be atomic. Revalidate every citation
            # at read time so a concurrently changed/retired QA is never ready.
            item["needs_compile"] = (item.get("quality_version") != variants.quality_version(cfg)
                                     or item.get("compile_input") != expected_inputs.get((row["topic_id"], item["variant_key"])))
            item["stale"] = item["needs_compile"] or any(not current.get(citation["qa_id"]) or current[citation["qa_id"]]["_fingerprint"] != citation["source_fingerprint"]
                                                        for citation in item["evidence"])
            result.append(item)
        return result


def _with_conversation_verification(cfg: Settings, items: list[dict], rows: list[dict], *, events: list[dict] | None = None) -> list[dict]:
    # Dynamic read-only certificates do not change extraction/compile identity.
    from . import voc_outcome
    all_events = events if events is not None else rows
    by_event = {row["id"]: row for row in all_events}
    result = []
    for item in items:
        cited = [by_event[citation["event_id"]] for citation in item["evidence"]
                 if citation["event_id"] in by_event and by_event[citation["event_id"]]["fingerprint"] == citation["source_fingerprint"]]
        certificate = voc_outcome.evaluate(cfg, cited, events=all_events)
        if len(cited) != len(item["evidence"]):
            certificate.update(status="unknown", acceptance="unknown", effect="unknown", auto_reusable=False)
            certificate["reason_codes"] = sorted(set(certificate["reason_codes"]) | {"incomplete_branch_citations"})
            missing = len(item["evidence"]) - len(cited)
            certificate["coverage"]["sources"] += missing
            certificate["coverage"]["incomplete_sources"] += missing
            certificate["fingerprint"] = assets.digest([certificate["fingerprint"], "incomplete_branch_citations", item["evidence"]])
        result.append({**item, "conversation_verification": certificate})
    return result


def knowledge(cfg: Settings, filters: dict, knowledge_id: str = "") -> dict:
    rows = _events(cfg, filters)
    visible = {row["topic_id"] for row in rows}
    event_ids = {row["id"] for row in rows}
    items = [item for item in _latest_assets(cfg) if item["status"] == "ready" and not item["stale"] and item["topic_id"] in visible
             and any(citation["event_id"] in event_ids for citation in item["evidence"])]
    items = _with_conversation_verification(cfg, items, rows, events=_events(cfg, {}))
    if knowledge_id:
        item = next((item for item in items if item["id"] == str(knowledge_id)), None)
        if item is None:
            raise EngineError("当前范围内没有可用的该知识版本。", 404)
        return {"knowledge": item, "evidence": item["evidence"]}
    return {"items": items, "total": len(items)}


def exceptions(cfg: Settings, filters: dict) -> dict:
    current = _sources(cfg, filters)
    rows = _events(cfg, filters, _current=current)
    visible = {row["topic_id"] for row in rows}
    latest = {item["fingerprint"]: item for item in _latest_assets(cfg)}
    items = _exception_items(cfg, current, visible, latest, filters=filters, event_ids={row["id"] for row in rows})
    return {"items": items, "total": len(items)}


def _exception_items(cfg: Settings, current: dict, visible: set, latest: dict, *, filters: dict | None = None, event_ids: set | None = None) -> list[dict]:
    store = path(cfg)
    if not store.is_file():
        return []
    with assets.connection(store) as conn:
        if "v2_exception" not in _tables(conn):
            return []
        items = []
        for row in conn.execute("SELECT * FROM v2_exception WHERE state='open' ORDER BY updated_at DESC,id"):
            data = json.loads(row["payload"])
            if row["topic_id"]:
                asset = latest.get(row["fingerprint"])
                if row["topic_id"] not in visible or not asset or asset["fingerprint"] != row["fingerprint"]:
                    continue
                if event_ids is not None and not any(item["event_id"] in event_ids for item in asset["evidence"]):
                    continue
            elif row["qa_id"] not in current or current[row["qa_id"]]["_fingerprint"] != row["fingerprint"]:
                continue
            if variants.mode(cfg) and data.get("domain"):
                metadata = data["domain"]
                if (filters or {}).get("support_intent", "all") not in ("", "all", metadata.get("primary_intent")):
                    continue
                if (filters or {}).get("diagnosis_status", "all") not in ("", "all", metadata.get("diagnosis", {}).get("status")):
                    continue
            items.append({"id": row["id"], "kind": row["kind"], "topic_id": row["topic_id"],
                          "qa_id": row["qa_id"], "question": data.get("question", ""),
                          "reason": data.get("reason", "待核对"), "created_at": _when(row["updated_at"]),
                          **({"domain": data["domain"]} if data.get("domain") else {})})
    return items


def _public_job(row: sqlite3.Row | dict) -> dict:
    keys = ("status", "total", "processed", "failed", "skipped", "calls", "usage_tokens", "reserved_tokens", "model", "pipeline", "error")
    return {"id": str(row["id"]), **{key: row[key] for key in keys},
            "created_at": _when(row["created_at"]), "updated_at": _when(row["updated_at"])}


def _semantic(cfg: Settings) -> LocalBGE:
    return LocalBGE(Path(cfg.runtime.state_dir).resolve() / "models")


def summary(cfg: Settings, filters: dict) -> dict:
    source = _sources(cfg, filters)
    events = _events(cfg, filters, _current=source)
    processed = 0
    context_needed = 0
    latest_job = None
    if path(cfg).is_file():
        with assets.connection(path(cfg)) as conn:
            if "v2_source" in _tables(conn):
                processed = sum(bool((row := conn.execute("SELECT fingerprint,pipeline,state FROM v2_source WHERE qa_id=?", (qa_id,)).fetchone())
                                and row["fingerprint"] == pair["_fingerprint"] and row["pipeline"] == variants.pipeline(cfg) and row["state"] in ("done", "excluded", "context_needed"))
                                for qa_id, pair in source.items())
                context_needed = sum(bool((row := conn.execute("SELECT fingerprint,pipeline,state FROM v2_source WHERE qa_id=?", (qa_id,)).fetchone())
                                          and row["fingerprint"] == pair["_fingerprint"] and row["pipeline"] == variants.pipeline(cfg) and row["state"] == "context_needed")
                                     for qa_id, pair in source.items())
                row = conn.execute("SELECT * FROM v2_job ORDER BY id DESC LIMIT 1").fetchone()
                latest_job = _public_job(row) if row else None
    topic_items = _topic_items(cfg, events)
    visible = {item["id"] for item in topic_items}
    global_source = _sources(cfg, {}) if any(value for key, value in filters.items() if key != "source") else source
    latest = _latest_assets(cfg, _current=global_source)
    by_branch = {(item["topic_id"], item["variant_key"]): item for item in latest}
    plans = _knowledge_plans(cfg, _events(cfg, {}, _current=global_source))
    pending_topics = {plan["topic"]["id"] for plan in plans if plan["topic"]["id"] in visible
                      and ((plan["topic"]["id"], plan["key"]) not in by_branch
                           or by_branch[(plan["topic"]["id"], plan["key"])]["needs_compile"])}
    needs_compile = len(pending_topics)
    event_ids = {row["id"] for row in events}
    know = [item for item in latest if item["status"] == "ready" and not item["stale"] and item["topic_id"] in visible
            and any(citation["event_id"] in event_ids for citation in item["evidence"])]
    know = _with_conversation_verification(cfg, know, events, events=_events(cfg, {}, _current=global_source))
    problems = _exception_items(cfg, source, visible, {item["fingerprint"]: item for item in latest}, filters=filters, event_ids=event_ids)
    return {"total_events": len(events), "topic_count": len(topic_items),
            "ready_knowledge": len(know), "auto_reusable_knowledge": sum(item["conversation_verification"]["auto_reusable"] for item in know),
            "exception_count": len(problems), "needs_compile": needs_compile,
            "answered_events": sum(bool(row["source"]["answer"]) for row in events), "source_pairs": len(source),
            "consultation_count": len({variants.consultation_key(row) for row in events}),
            "coverage": {"processed_pairs": processed, "pending_pairs": len(source) - processed, "context_needed_pairs": context_needed,
                         "anchored_events": sum(row["source"]["source_kind"] == "source-anchored" for row in events),
                         "legacy_events": sum(row["source"]["source_kind"] != "source-anchored" for row in events),
                         "answered_pairs": sum(bool(pair["answer"]) for pair in source.values()),
                         "resolved_events": sum(row["data"]["answer_kind"] == "resolved_answer" for row in events),
                         "collect_info_events": sum(row["data"]["answer_kind"] == "collect_info" for row in events),
                         "needs_compile": needs_compile,
                         "pipeline_version": variants.pipeline(cfg) + "/" + variants.quality_version(cfg),
                         "domain": variants.mode(cfg)},
            "semantics": _semantic(cfg).availability(), "latest_job": latest_job}


def check(cfg: Settings, filters: dict) -> dict:
    result = summary(cfg, filters)
    return {**result, "configured": voc.configured(cfg), "shadow": True,
            "derived_database_exists": path(cfg).is_file(),
            "alignment_ready": importlib.util.find_spec("langextract") is not None,
            "pipeline_version": variants.pipeline(cfg) + "/" + variants.quality_version(cfg)}


def _busy_legacy(cfg: Settings) -> bool:
    source_path = Path(cfg.runtime.db_path).resolve()
    if not source_path.is_file():
        return False
    conn = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        tables = _tables(conn)
        for table in ("voc_job", "knowledge_job"):
            if table in tables and any(voc._owner_alive(row[0]) for row in conn.execute(
                    f"SELECT owner FROM {table} WHERE status IN ('queued','running')")):
                return True
        return False
    finally:
        conn.close()


def _assign(conn: sqlite3.Connection, data: dict, candidates: list[dict]) -> str:
    domain_mode = "zhihuige" if "domain" in data else ""
    primary = data.get("domain", {}).get("primary_intent", "")
    if data["topic_id"] is not None:
        topic = conn.execute("SELECT * FROM v2_topic WHERE id=?", (data["topic_id"],)).fetchone()
        candidate = next((item for item in candidates if item["id"] == data["topic_id"]), None)
        if not topic or topic["status"] == "ignored" or not candidate or topic["revision"] != candidate["revision"] or topic["domain"] != domain_mode:
            raise EngineError("主题已由人工更新，请重新整理本条来源。", 409)
        prior_scope = () if domain_mode else quality.hard_scope_key({"scope": json.loads(topic["scope_json"])})
        current_scope = () if domain_mode else quality.hard_scope_key(data)
        if prior_scope == current_scope and topic["primary_intent"] == primary:
            return topic["id"]
        # Different products/versions/audiences must not widen an asset. Other
        # condition/time differences stay together and become quality conflicts,
        # rather than being split away so contradictory policies look consistent.
    scope = [] if domain_mode else [{"kind": kind, "quote": quote} for kind, quote in quality.scope_key(data)]
    hard_scope = [] if domain_mode else [{"kind": kind, "quote": quote} for kind, quote in quality.hard_scope_key(data)]
    question_key = canonical(data["question"])
    # Null does not grant permission to create a second identity for the same
    # standard question. Soft conditions/durations must meet in one topic so
    # incompatible policy evidence remains visible to conflict checks.
    for prior in conn.execute("SELECT * FROM v2_topic WHERE domain=? AND primary_intent=? ORDER BY created_at,id", (domain_mode, primary)):
        if (canonical(prior["question"]) == question_key
                and (domain_mode or quality.hard_scope_key({"scope": json.loads(prior["scope_json"])}) == quality.hard_scope_key(data))):
            if prior["status"] == "ignored":
                raise EngineError("该主题已由人工忽略，未自动恢复。", 409)
            return prior["id"]
    topic_id = assets.topic_id(domain_mode + ":" + primary + ":" + question_key if domain_mode else question_key, hard_scope)
    existing = conn.execute("SELECT status FROM v2_topic WHERE id=?", (topic_id,)).fetchone()
    if existing and existing["status"] == "ignored":
        raise EngineError("该主题已由人工忽略，未自动恢复。", 409)
    now = time.time()
    conn.execute("""INSERT OR IGNORE INTO v2_topic(id,question,theme,boundary,scope_json,domain,primary_intent,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?)""", (topic_id, data["question"], data["theme"], data["boundary"], assets.dump(scope), domain_mode, primary, now, now))
    return topic_id


def _knowledge_question(cfg: Settings, topic: dict, rows: list[dict], result: dict) -> str:
    """A branch may not inherit another incident's reproduction background."""
    if not variants.mode(cfg) or result.get("status") != "ready":
        return topic["question"]
    cited = {item["event_id"] for item in result.get("evidence", [])}
    local = [(row["id"], row["data"].get("question", "").strip()) for row in rows
             if row["id"] in cited and row["source"].get("_staff_verified")]
    local = [(event_id, question) for event_id, question in local if 3 <= len(question) <= 120]
    if not local:
        return "相关软件使用问题"
    frequency = Counter(canonical(question) for _, question in local)
    # Prefer the most represented local wording, then retain the most complete
    # bounded phrasing. Event identity makes ties deterministic across reads.
    return min(local, key=lambda item: (-frequency[canonical(item[1])], -len(canonical(item[1])), item[0]))[1]


def _same_evidence_manually_ignored(conn, topic_id: str, rows: list[dict]) -> bool:
    identity = {(row["id"], row["fingerprint"]) for row in rows}
    for prior in conn.execute("SELECT * FROM v2_asset WHERE topic_id=? AND manual=1 ORDER BY updated_at DESC,id DESC", (topic_id,)):
        old = json.loads(prior["payload"])
        if {(item["event_id"], item["source_fingerprint"]) for item in old.get("evidence", [])} == identity:
            # Includes explicit reactivation: a newer human ready decision wins
            # over a historical ignored branch with identical evidence.
            return prior["status"] == "ignored"
    return False


def _compile(cfg: Settings, embedder: Any) -> None:
    events = _events(cfg, {})
    for plan in _knowledge_plans(cfg, events):
        topic, rows, links, compile_input = plan["topic"], plan["rows"], plan["links"], plan["compile_input"]
        fingerprint = assets.digest([variants.pipeline(cfg), variants.quality_version(cfg), embedder.model, compile_input])
        with assets.connection(path(cfg)) as conn:
            if conn.execute("SELECT 1 FROM v2_asset WHERE topic_id=? AND fingerprint=?", (topic["id"], fingerprint)).fetchone():
                continue
        result = variants.evaluate(cfg, path(cfg), embedder, topic, plan)
        result["compile_input"] = compile_input
        if links:
            result.update(status="exception", kind="possible_duplicate",
                          reason="相近问题未能明确归并，或存在条件分歧，相关主题均暂不自动生成可用知识。",
                          related_topic_ids=sorted({link[2] for link in links} | {link[3] for link in links}))
        asset_topic = {**topic, "question": _knowledge_question(cfg, topic, rows, result)}
        if variants.mode(cfg) and result.get("status") == "ready":
            result["knowledge_question"] = asset_topic["question"]
        with assets.connection(path(cfg), write=True) as conn:
            with assets.transaction(conn):
                # A human's same evidence disposition stays immutable; new
                # evidence becomes a new version rather than overwriting it.
                inherited_ignore = _same_evidence_manually_ignored(conn, topic["id"], rows)
                asset_id = assets.put_asset(conn, topic=asset_topic, fingerprint=fingerprint, status="ignored" if inherited_ignore else result["status"],
                                            answer=result["answer"], scope=result["scope"], payload=result, variant_key=plan["key"])
                if inherited_ignore:
                    conn.execute("UPDATE v2_asset SET manual=1 WHERE id=?", (asset_id,))
                if result["status"] != "ready" and not inherited_ignore:
                    assets.put_exception(conn, kind=result["kind"], fingerprint=fingerprint, topic_id=topic["id"],
                                         payload={"question": topic["question"], "reason": result["reason"], "variant_id": result.get("variant_id", "")})


def _save_job(cfg: Settings, job: dict, progress: Callable | None) -> None:
    job["updated_at"] = time.time()
    with assets.connection(path(cfg), write=True) as conn:
        conn.execute("""UPDATE v2_job SET status=?,processed=?,failed=?,skipped=?,calls=?,usage_tokens=?,
          reserved_tokens=?,error=?,updated_at=? WHERE id=?""",
          (job["status"], job["processed"], job["failed"], job["skipped"], job["calls"], job["usage_tokens"],
           job["reserved_tokens"], job["error"], job["updated_at"], job["id"]))
    if progress:
        try:
            progress(_public_job(job))
        except Exception:
            # A UI/progress observer is not allowed to interrupt source commits
            # or strand an otherwise synchronous owned job in running state.
            pass


class _ExtractionBudgetStop(Exception):
    def __init__(self, message: str, *, after_failure: bool = False):
        super().__init__(message)
        self.after_failure = after_failure


def _extract_source(cfg, pair, candidates, provider, aligner, job, max_calls, max_tokens, progress):
    """At most two serial full extractions; each attempt owns its accounting."""
    feedback = None
    for attempt in (1, 2):
        if job["calls"] >= max_calls:
            raise _ExtractionBudgetStop("已达到本轮模型调用上限；未完成的来源保留待整理，可在下一轮继续。", after_failure=feedback is not None)
        if job["usage_tokens"] >= max_tokens:
            raise _ExtractionBudgetStop("已达到本轮模型用量上限；未完成的来源保留待整理，可在下一轮继续。", after_failure=feedback is not None)
        request_source = {**pair, **({"_extraction_feedback": feedback} if feedback else {})}
        output_budget = output_token_limit(pair)
        request = request_payload(cfg.llm, request_source, candidates, output_budget)
        reservation = len(assets.dump(request).encode("utf-8")) + output_budget
        if job["reserved_tokens"] + reservation > max_tokens:
            raise _ExtractionBudgetStop("本轮保守用量预算不足以完成下一次提取；来源保留待整理，可在下一轮继续。", after_failure=feedback is not None)
        job["calls"] += 1
        job["reserved_tokens"] += reservation
        try:
            _save_job(cfg, job, progress)  # Closed before inference; durable admission.
        except Exception:
            job["calls"] -= 1
            job["reserved_tokens"] -= reservation
            raise
        tokens = 0
        try:
            result = provider.extract(cfg.llm, request_source, candidates, output_budget)
            tokens = result.get("usage_tokens", 0)
            if type(tokens) is not int or tokens < 0:
                tokens = 0
                raise VocModelError("模型网关用量字段格式无效。")
            extracted = validate(result["payload"], pair, candidates, aligner)
            for data in extracted["intents"]:
                data["extraction_attempts"] = attempt
            return extracted
        except VocModelError as error:
            if not tokens:
                amount = getattr(error, "usage_tokens", 0)
                tokens = amount if type(amount) is int and amount >= 0 else 0
            if attempt == 2 or not error.retryable:
                raise
            feedback = correction_feedback(error)
        finally:
            job["usage_tokens"] += tokens
            _save_job(cfg, job, progress)


def run(cfg: Settings, filters: dict, limit: int = 20, max_calls: int = 20,
        max_tokens: int = 400000, shadow: bool = True, progress: Callable | None = None,
        *, embedder: Any = None, provider: Any = None, aligner: Any = None) -> dict:
    """Synchronous, one inference at a time, bounded by calls AND token reservation.

    max_tokens reserves UTF-8 input bytes + requested maximum output before each
    inference (a conservative tokenizer-independent bound). Reported token usage
    is tracked separately, including failed responses. Capability fallback may
    issue one unbilled HTTP format-negotiation retry inside the shared gateway.
    Each source has at most one retry after retryable model/validation failure;
    no unvalidated old output becomes evidence. shadow is retained in the public
    contract; even False never edits old data.
    """
    if type(limit) is not int or not 1 <= limit <= 500 or type(max_calls) is not int or not 1 <= max_calls <= 500:
        raise EngineError("每轮来源数和模型调用上限须为 1–500。")
    if type(max_tokens) is not int or not 4000 <= max_tokens <= 2_000_000 or type(shadow) is not bool:
        raise EngineError("每轮模型预算须为 4000–2000000 tokens。")
    if not voc.configured(cfg):
        raise EngineError("请先配置并启用整理模型。")
    if _busy_legacy(cfg):
        raise EngineError("已有整理任务正在运行，请稍后重试。", 409)
    embedder, provider, aligner = embedder or _semantic(cfg), provider or GatewayProvider(), aligner or LangExtractAligner()
    # Loading the real model now fails before any inference, even with no prior
    # topics (which would otherwise bypass retrieval on the first source).
    embedder.encode(["客户咨询意图"])
    source = _sources(cfg, filters)
    assets.initialize(path(cfg))
    with assets.connection(path(cfg), write=True) as conn:
        with assets.transaction(conn):
            for old in conn.execute("SELECT id,owner FROM v2_job WHERE status='running'").fetchall():
                if voc._owner_alive(old["owner"]):
                    raise EngineError("已有VOC整理任务正在运行。", 409)
                conn.execute("UPDATE v2_job SET status='failed',failed=total-processed-skipped,error='整理进程中断，可重新运行',updated_at=? WHERE id=?", (time.time(), old["id"]))
            pending = []
            for pair in sorted(source.values(), key=lambda item: (item["asked_at"], item["qa_id"])):
                old = conn.execute("SELECT * FROM v2_source WHERE qa_id=?", (pair["qa_id"],)).fetchone()
                if not old or old["fingerprint"] != pair["_fingerprint"] or old["pipeline"] != variants.pipeline(cfg) or old["state"] not in ("done", "excluded", "context_needed"):
                    attempt = conn.execute("SELECT MAX(updated_at) FROM v2_exception WHERE qa_id=? AND fingerprint=? AND kind='extraction_failed'",
                                           (pair["qa_id"], pair["_fingerprint"])).fetchone()[0]
                    pending.append((bool(attempt), attempt or 0, pair["asked_at"], pair["qa_id"], pair))
            # A malformed/oversized source cannot starve all later new sources.
            # Fresh work comes first; failed sources rotate by oldest attempt.
            pending.sort(key=lambda item: item[:4])
            selected = [item[4] for item in pending[:limit]]
            now = time.time()
            job_id = conn.execute("INSERT INTO v2_job(status,total,model,pipeline,owner,filters,created_at,updated_at) VALUES ('running',?,?,?,?,?,?,?)",
                (len(selected), cfg.llm.model_main, variants.pipeline(cfg) + "/" + variants.quality_version(cfg), voc._OWNER, assets.dump(filters), now, now)).lastrowid
            job = dict(conn.execute("SELECT * FROM v2_job WHERE id=?", (job_id,)).fetchone())
    _save_job(cfg, job, progress)
    interrupted = False
    failure_streak = 0
    try:
        for pair in selected:
            if failure_streak >= 3:
                job["error"] = "连续3条来源整理失败，已停止本轮调用；失败详情保留在异常中，其余来源留待下一轮。"
                break
            if job["calls"] >= max_calls:
                job["error"] = "已达到本轮模型调用上限，剩余来源留待下一轮。"
                break
            if job["usage_tokens"] >= max_tokens:
                job["error"] = "已达到本轮模型用量上限，剩余来源留待下一轮。"
                break
            try:
                if len(pair["question"]) + len(pair["answer"]) > MAX_SOURCE_CHARS:
                    raise VocModelError("来源超过单次完整文本预算，未截断或遗漏其余意图，请拆分核对。", retryable=False)
                candidates = retrieve(path(cfg), embedder, pair["question"], _stored_topics(path(cfg), domain_mode=variants.mode(cfg)), top_k=5)
                candidates = _candidate_examples(cfg, candidates)
                extracted = _extract_source(cfg, pair, candidates, provider, aligner, job, max_calls, max_tokens, progress)
                for data in extracted["intents"]:
                    data["candidate_context"] = [{"id": candidate["id"], "question": candidate["question"],
                        "similarity": candidate.get("similarity"), "scope": candidate.get("scope_evidence", []),
                        "examples": candidate.get("examples", [])} for candidate in candidates]
                    if data["topic_id"] is None:
                        compatible = [candidate for candidate in candidates
                                      if (candidate.get("primary_intent", "") == data.get("domain", {}).get("primary_intent", "") if variants.mode(cfg)
                                          else quality.hard_scope_key({"scope": candidate.get("scope_evidence", [])}) == quality.hard_scope_key(data))
                                      and canonical(candidate["question"]) != canonical(data["question"])]
                        near = retrieve(path(cfg), embedder, data["question"], compatible, top_k=5, minimum=0.86)
                        if near:
                            data["possible_duplicate"] = [candidate["id"] for candidate in near]
                latest = _sources(cfg, {}).get(pair["qa_id"])
                if not latest or latest["_fingerprint"] != pair["_fingerprint"]:
                    job["skipped"] += 1
                else:
                    with assets.connection(path(cfg), write=True) as conn:
                        with assets.transaction(conn):
                            conn.execute("UPDATE v2_intent SET active=0 WHERE qa_id=?", (pair["qa_id"],))
                            context_items = []
                            for data in extracted["intents"]:
                                if variants.mode(cfg) and data["domain"].get("needs_context"):
                                    context_items.append(data["domain"])
                                    continue
                                topic_id = _assign(conn, data, candidates)
                                span = data["question_span"]
                                assets.save_intent(conn, {"id": _event_identity(pair, span), "qa_id": pair["qa_id"],
                                    "fingerprint": pair["_fingerprint"], "topic_id": topic_id, "quote": span["quote"],
                                    "start": span["start"], "end": span["end"], "data": data})
                            conn.execute("""INSERT INTO v2_source VALUES (?,?,?,?,?,?) ON CONFLICT(qa_id) DO UPDATE SET
                              fingerprint=excluded.fingerprint,pipeline=excluded.pipeline,snapshot=excluded.snapshot,state=excluded.state,updated_at=excluded.updated_at""",
                              (pair["qa_id"], pair["_fingerprint"], variants.pipeline(cfg), assets.dump(_snapshot(pair)),
                               "context_needed" if context_items and len(context_items) == len(extracted["intents"]) else "excluded" if extracted["excluded"] else "done", time.time()))
                            if context_items:
                                assets.put_exception(conn, kind="context_needed", fingerprint=pair["_fingerprint"], qa_id=pair["qa_id"],
                                    payload={"question": pair["question"][:120], "reason": "当前片段仅能确定复现背景，尚缺明确的软件主诉关联；未生成商品FAQ。", "domain": context_items[0]})
                            conn.execute("UPDATE v2_exception SET state='resolved' WHERE qa_id=? AND fingerprint=? AND kind='extraction_failed' AND state='open'",
                                         (pair["qa_id"], pair["_fingerprint"]))
                    job["processed"] += 1
                failure_streak = 0
            except _ExtractionBudgetStop as error:
                job["error"] = str(error)
                if error.after_failure:
                    with assets.connection(path(cfg), write=True) as conn:
                        assets.put_exception(conn, kind="extraction_failed", fingerprint=pair["_fingerprint"], qa_id=pair["qa_id"],
                                             payload={"question": pair["question"][:120], "reason": "首次提取未通过校验，本轮预算不足以执行纠错；来源仍待整理。"})
                break
            except (VocModelError, EngineError, SemanticError) as error:
                job["failed"] += 1
                failure_streak += 1
                safe = str(error)[:240]
                if not job["error"]:
                    job["error"] = safe
                with assets.connection(path(cfg), write=True) as conn:
                    assets.put_exception(conn, kind="extraction_failed", fingerprint=pair["_fingerprint"], qa_id=pair["qa_id"],
                                         payload={"question": pair["question"][:120], "reason": safe})
            except Exception:
                job["failed"] += 1
                failure_streak += 1
                if not job["error"]:
                    job["error"] = "部分来源整理异常，可查看异常列表并重试。"
                with assets.connection(path(cfg), write=True) as conn:
                    assets.put_exception(conn, kind="extraction_failed", fingerprint=pair["_fingerprint"], qa_id=pair["qa_id"],
                                         payload={"question": pair["question"][:120], "reason": "整理服务异常，本条未标为完成，可重试。"})
            finally:
                _save_job(cfg, job, progress)
        _compile(cfg, embedder)
    except KeyboardInterrupt:
        interrupted = True
        job["error"] = "整理已中断，未完成来源可在下一轮重试。"
    except Exception:
        interrupted = True
        job["error"] = "知识质量检查未完成，已保留成功抽取的来源，可重新运行。"
    finally:
        job["skipped"] += job["total"] - job["processed"] - job["failed"] - job["skipped"]
        job["status"] = "failed" if interrupted or job["failed"] and not job["processed"] else "partial" if job["failed"] or job["skipped"] else "completed"
        _save_job(cfg, job, progress)
    return {"job": _public_job(job), "summary": summary(cfg, filters), "shadow": shadow}


def search(cfg: Settings, filters: dict, query: str, limit: int = 5) -> dict:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000 or type(limit) is not int or not 1 <= limit <= 10:
        raise EngineError("检索词须为 1–1000 字，结果数为 1–10。")
    candidates = topics(cfg, filters)["items"]
    if not candidates:
        return {"topics": [], "knowledge": [], "events": []}
    try:
        selected = retrieve(path(cfg), _semantic(cfg), query.strip(), candidates, top_k=limit)
    except SemanticError as error:
        raise EngineError(str(error), 503) from None
    ids = {item["id"] for item in selected}
    know = [item for item in knowledge(cfg, filters)["items"] if item["topic_id"] in ids][:limit]
    know = [{**item, "evidence": item["evidence"][:5]} for item in know]
    events = [quality.evidence(row) for row in _events(cfg, filters) if row["topic_id"] in ids][:limit]
    return {"topics": selected, "knowledge": know, "events": events}


def review(cfg: Settings, *, kind: str, item_id: str, status: str, revision: int) -> dict:
    """Optional admin adapter; identical evidence is not recreated after ignore."""
    if kind not in ("topic", "knowledge") or status not in ("ignored", "active", "ready") or type(revision) is not int:
        raise EngineError("审核操作无效。")
    table = "v2_topic" if kind == "topic" else "v2_asset"
    if kind == "topic" and status == "ready" or kind == "knowledge" and status == "active":
        raise EngineError("审核状态无效。")
    with assets.connection(path(cfg), write=True) as conn:
        with assets.transaction(conn):
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise EngineError("审核对象不存在。", 404)
            if row["revision"] != revision:
                raise EngineError("内容已更新，请刷新后审核。", 409)
            if kind == "knowledge" and status == "ready" and (not row["answer"].strip() or json.loads(row["payload"])["status"] != "ready"):
                raise EngineError("当前版本未通过知识质量门控，请先补充或核对来源。")
            conn.execute(f"UPDATE {table} SET status=?,manual=1,revision=revision+1,updated_at=? WHERE id=?", (status, time.time(), item_id))
            conn.execute("INSERT INTO v2_audit(kind,asset_id,detail,created_at) VALUES (?,?,?,?)", (kind, item_id, assets.dump({"status": status, "previous_revision": revision}), time.time()))
    return {"id": item_id, "status": status, "revision": revision + 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="增量VOC与知识影子整理，不修改原业务数据")
    parser.add_argument("--check", action="store_true", help="严格只读，不下载模型、不建表、不调模型")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--max-tokens", type=int, default=400000)
    parser.add_argument("--days", type=int, default=0)
    parser.add_argument("--group", default="")
    parser.add_argument("--staff", default="")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        cfg = load_settings()
        filters = {"days": args.days, "group": args.group, "staff": args.staff}
        result = check(cfg, filters) if args.check else run(cfg, filters, args.limit, args.max_calls, args.max_tokens)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if args.check or result["job"]["status"] == "completed" else 1
    except (EngineError, VocModelError, SemanticError) as error:
        print(json.dumps({"status": "failed", "error": str(error)[:240]}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"status": "failed", "error": "VOC整理服务异常，请检查本机配置。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
