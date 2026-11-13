"""Incremental VOC v2 shadow engine and bounded synchronous service contract.

The business database is opened read-only. All v2 data lives in a sibling derived
database. No legacy topic, knowledge asset, raw message or QA row is modified.
External inference and local embedding run without an open SQLite connection.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
import importlib.util
from itertools import zip_longest
import json
import logging
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from . import voc, voc_assets as assets, voc_quality as quality, voc_variants as variants
from . import voc_episode_projection as episode_projection
from . import voc_topic_insights as insights
from .clean.rules import mask_pii
from .config import Settings, load_settings
from .extract.text import clean_visible_text
from .extract.identity import SOURCE_IDENTITY_ROLE, sender_context
from .knowledge_model import _staff_answer
from .voc_extract import (VERSION, MAX_OUTPUT_TOKENS, MAX_SOURCE_CHARS, GatewayProvider,
                          LangExtractAligner, canonical, request_payload, validate, output_token_limit, correction_feedback)
from .voc_model import VocModelError
from .voc_outcome import _visitor_owners
from .voc_semantics import LocalBGE, SemanticError, retrieve
from .voc_search import (STAFF_SYMPTOMS, has_local_evidence_terms, has_staff_reply,
                         rank_local_evidence, rank_materials)

PIPELINE_VERSION = VERSION + "/" + quality.VERSION
_RANK_FAMILIES = {
    ("configuration_guidance", "scene_reply_setup"): (
        "场景与话术配置",
        ("话术配置", "场景话术配置", "场景话术设置", "场景配置", "场景设置", "场景条件配置",
         "话术设置", "回复配置", "回复话术配置", "回复内容配置"),
    ),
    ("configuration_guidance", "data_sync"): (
        "数据同步",
        ("同步操作", "同步设置", "同步配置", "知识同步", "知识库同步", "表格同步", "商品数据同步", "商品同步"),
    ),
    ("functional_anomaly", "data_sync"): (
        "数据同步异常",
        ("同步异常", "同步失败", "同步问题", "知识同步异常", "知识库同步异常", "表格同步异常",
         "商品数据同步异常", "商品同步异常", "商品同步"),
    ),
    ("functional_anomaly", "robot_reply"): (
        "回复异常",
        ("机器人回复异常",),
    ),
}


class EngineError(voc.VocError):
    pass


def path(cfg: Settings) -> Path:
    return assets.store_path(cfg)


def _when(stamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else ""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _anchored_text(conn: sqlite3.Connection, session_id: str, ids: list[str], role: str) -> tuple[str, list[dict]]:
    if not ids or len(set(ids)) != len(ids):
        return "", []
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT source_message_id,role,content,ts FROM raw_message WHERE session_id=? AND source_message_id IN ({marks}) ORDER BY seq",
        (session_id, *ids),
    ).fetchall()
    if [row["source_message_id"] for row in rows] != ids or any(row["role"] != role for row in rows):
        return "", []
    chunks, parts, offset = [], [], 0
    for row in rows:
        text = _staff_answer(row["content"]) if role == "staff" else mask_pii(clean_visible_text(row["content"] or ""))
        if text:
            chunks.append(text)
            parts.append({"message_id": row["source_message_id"], "start": offset, "end": offset + len(text), "ts": row["ts"]})
            offset += len(text) + 1
    return "\n".join(chunks), parts


def _sources(cfg: Settings, filters: dict, *, only_qa_id: int | None = None,
             identify_merchants: bool = False, time_bounds: tuple[int, int] | None = None,
             qa_ids: set[int] | None = None) -> dict[int, dict]:
    source_path = Path(cfg.runtime.db_path).resolve()
    if not source_path.is_file():
        return {}
    conn = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")  # Consistent read snapshot, closed before any model work.
        source = voc._scope(conn, filters, qa_id=only_qa_id, include_messages=False, _bounds_override=time_bounds,
                            _qa_ids=qa_ids)
        tables = _tables(conn)
        official_sessions = set()
        if identify_merchants and "push_sync_state" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(push_sync_state)")}
            if {"session_id", "status", "source_json", "source_version", "version"} <= columns:
                official_sessions = {row[0] for row in conn.execute("""SELECT session_id FROM push_sync_state
                    WHERE status='completed' AND source_json<>'' AND source_version=version""")}
        anchored_column = "raw_message" in tables and "source_message_id" in {row[1] for row in conn.execute("PRAGMA table_info(raw_message)")}
        identity_sessions = {}
        source_columns = "seq,role,content,ts,source_message_id" if anchored_column else "role,content,ts"

        @lru_cache(maxsize=32)
        def session_messages(session_id: str) -> list[dict]:
            return [dict(message) for message in conn.execute(
                f"SELECT {source_columns} FROM raw_message WHERE session_id=? ORDER BY seq", (session_id,))]

        @lru_cache(maxsize=32)
        def visible_messages(session_id: str, customer_id: str) -> list[dict]:
            return sender_context(session_messages(session_id), session_id, customer_id)

        for qa_id in list(source):
            row = source[qa_id]
            row["_domain_mode"] = variants.mode(cfg)
            original = conn.execute("SELECT session_id,customer_id FROM qa_pair WHERE qa_id=?", (qa_id,)).fetchone()
            raw_sid, raw_customer = original["session_id"], original["customer_id"] or ""
            row["_session_key"] = "s_" + assets.digest(["raw-session", raw_sid])
            row["answer"] = _staff_answer(row["answer"])
            origin = conn.execute("SELECT * FROM qa_origin WHERE qa_id=?", (qa_id,)).fetchone() if "qa_origin" in tables else None
            if origin is not None and not origin["active"]:
                del source[qa_id]
                continue
            question_ids, answer_ids = [], []
            if origin and origin["session_id"] == raw_sid:
                try:
                    question_ids = json.loads(origin["question_message_ids"])
                    answer_ids = json.loads(origin["answer_message_ids"])
                except (ValueError, TypeError):
                    question_ids, answer_ids = [], []
                if not all(isinstance(value, list) and all(isinstance(item, str) and item for item in value) for value in (question_ids, answer_ids)):
                    question_ids, answer_ids = [], []
            row.update(question_message_ids=question_ids, answer_message_ids=answer_ids,
                       source_kind="evidence-key-reference" if origin else "legacy-reference",
                       _evidence_key=origin["evidence_key"] if origin and question_ids else
                       "legacy:" + assets.digest([row["_session_key"], row["customer_id"], qa_id]))
            row["_origin_fingerprint"] = [origin["fingerprint"], origin["revision"]] if origin else None
            row["_question_parts"] = []
            if question_ids and anchored_column:
                question, question_parts = _anchored_text(conn, raw_sid, question_ids, "user")
                if question == row["question"] and question_parts:
                    row["_question_parts"] = question_parts
                    row["source_kind"] = "source-anchored"
                    if row["_domain_mode"] and raw_customer:
                        row["_episode_customer_key"] = episode_projection.customer_key(raw_customer)
            if identify_merchants:
                known = (bool(row["_question_parts"]) and raw_sid in official_sessions
                         and raw_customer.startswith(("qiyu-external:", "qiyu-external-sha256:", "qiyu-user:")))
                row["_known_merchant"] = assets.digest(["verified-merchant", raw_customer]) if known else ""
            row["_answer_parts"] = []
            row["_staff_verified"] = False
            has_identity = False
            if row["_domain_mode"] and anchored_column and row["_question_parts"]:
                if raw_sid not in identity_sessions:
                    messages = session_messages(raw_sid)
                    identity_sessions[raw_sid] = (
                        any(message["role"] == SOURCE_IDENTITY_ROLE for message in messages),
                        len(_visitor_owners(messages, raw_sid)) > 1,
                    )
                has_identity = identity_sessions[raw_sid][0]
            mixed_visitors = identity_sessions.get(raw_sid, (False, False))[1]
            if mixed_visitors:
                # Keep the official conversation intact. Its ambiguous replies cannot support VOC claims.
                row["answer"] = ""
                row["answer_message_ids"] = []
            elif row["answer"] and row["_question_parts"] and answer_ids and anchored_column:
                answer, answer_parts = _anchored_text(conn, raw_sid, answer_ids, "staff")
                if answer == row["answer"] and answer_parts:
                    row["_answer_parts"], row["_staff_verified"] = answer_parts, True
            elif row["answer"] and origin is None and "raw_message" in tables:
                visible = visible_messages(raw_sid, raw_customer)
                staff = "\n".join(_staff_answer(message.get("content")) for message in visible if message.get("role") == "staff")
                row["_staff_verified"] = row["answer"] in staff
            row["_context"] = []
            if has_identity:
                visible = visible_messages(raw_sid, raw_customer)
                if mixed_visitors:
                    visible = [message for message in visible if message["role"] == "user"]
                anchors = {part["message_id"] for part in row["_question_parts"] + row["_answer_parts"]}
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
            if origin and row["source_kind"] != "source-anchored":
                fingerprint_data["incomplete_source_anchors"] = True
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
        return _read_events(cfg, filters, current, conn)


def _read_events(cfg: Settings, filters: dict, current: dict, conn: sqlite3.Connection, *,
                 restrict_qa_ids: bool = False) -> list[dict]:
    if "v2_intent" not in _tables(conn):
        return []
    query = """SELECT i.* FROM v2_intent i JOIN v2_topic t ON t.id=i.topic_id
      JOIN v2_source s ON s.qa_id=i.qa_id AND s.fingerprint=i.fingerprint
      WHERE i.active=1 AND t.status!='ignored' AND s.pipeline=? AND s.state='done'"""
    params = [variants.pipeline(cfg)]
    if restrict_qa_ids:
        query += " AND i.qa_id IN (SELECT value FROM json_each(?))"
        params.append(assets.dump(sorted(current)))
    if filters.get("focus_topic"):
        query += " AND i.topic_id=?"
        params.append(filters["focus_topic"])
    result = []
    for raw in conn.execute(query, params):
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


def _episode_anchors(cfg: Settings, qa_ids: set[int]) -> tuple[dict[int, dict], list[dict]]:
    sources = _sources(cfg, {}, qa_ids=qa_ids)
    store = path(cfg)
    if not sources or not store.is_file():
        return sources, []
    with assets.connection(store) as conn:
        return sources, _read_events(cfg, {}, sources, conn, restrict_qa_ids=True)


def _topic_definitions(cfg: Settings) -> dict[str, dict]:
    current = {item["id"]: item for item in _stored_topics(path(cfg), domain_mode=variants.mode(cfg))}
    if variants.mode(cfg) == "zhihuige":
        from . import voc_episode_catalog

        for item in voc_episode_catalog.topics(cfg):
            current.setdefault(item["id"], item)
    return current


def _topic_view(cfg: Settings, filters: dict, current: dict | None = None) -> list[dict]:
    if getattr(getattr(cfg, "runtime", None), "voc_domain", "") != "zhihuige":
        return _events(cfg, filters) if current is None else _events(cfg, filters, _current=current)
    current = _sources(cfg, filters) if current is None else current
    legacy = _events(cfg, episode_projection.source_filters(filters), _current=current)
    return episode_projection.project(cfg, current, legacy, filters, set(_topic_definitions(cfg)))


def _topic_evidence(row: dict) -> dict:
    evidence = quality.evidence(row)
    if row.get("episode_id"):
        evidence["episode_id"] = row["episode_id"]
        if domain := evidence.get("domain"):
            # An episode can be derived without a classified legacy event.
            # Give its public evidence the same shape without changing its proof fingerprint.
            empty_lists = ("platforms", "modules", "reproduction_context", "prerequisites", "operation_path", "actions", "tags")
            diagnosis = {"cause_code": "", "cause": None, "evidence": [], **domain["diagnosis"]}
            evidence["domain"] = {**{key: [] for key in empty_lists}, **domain, "diagnosis": diagnosis}
    return evidence


def _original_topic_events(rows: list[dict], all_events: list[dict]) -> list[dict]:
    ids = episode_projection.legacy_event_ids(rows)
    return [row for row in all_events if row["id"] in ids]


def _topic_knowledge_assets(cfg: Settings, current: dict, all_events: list[dict], rows: list[dict]) -> list[dict]:
    return [item for topic_id in sorted({row["topic_id"] for row in rows})
            for item in _latest_assets(cfg, _current=current, topic_id=topic_id,
                                       _plans=_knowledge_plans(cfg, all_events, topic_id=topic_id))]


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


def _topic_items(cfg: Settings, events: list[dict], definitions: dict | None = None) -> list[dict]:
    by_topic = defaultdict(list)
    for row in events:
        by_topic[row["topic_id"]].append(row)
    definitions = _topic_definitions(cfg) if definitions is None else definitions
    items = [_public_topic(topic, by_topic[topic_id]) for topic_id, topic in definitions.items() if topic_id in by_topic]
    return sorted(items, key=lambda item: (-item["consultation_count"], -item["event_count"], item["id"]))


def _last_synced(cfg: Settings) -> str:
    source = Path(cfg.runtime.db_path).resolve()
    if not source.is_file():
        return ""
    with assets.connection(source) as conn:
        if "push_sync_state" not in _tables(conn):
            return ""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(push_sync_state)")}
        if not {"updated_at", "status", "source_json", "source_version", "version"} <= columns:
            return ""
        stamp = conn.execute("""SELECT MAX(updated_at) FROM push_sync_state
            WHERE status='completed' AND source_json<>'' AND source_version=version""").fetchone()[0]
    return datetime.fromtimestamp(stamp, insights.ZONE).isoformat(timespec="seconds") if stamp else ""


def _source_filters(filters: dict) -> dict:
    return {key: filters[key] for key in ("group", "staff", "q", "replied", "cluster", "customer") if filters.get(key)}


def _workbench_data(cfg: Settings, filters: dict, *, definitions: dict | None = None) -> tuple[list[dict], list[dict], set[str], dict]:
    current, previous = insights.periods(filters)
    start, stop = current.bounds()
    comparison_start = previous.bounds()[0] if previous else start
    sources = _sources(cfg, _source_filters(filters), identify_merchants=True, time_bounds=(comparison_start, stop))
    current_sources = current.rows(sources)
    previous_sources = previous.rows(sources) if previous else {}
    store = path(cfg)
    events, states, observed_before = [], {}, set()
    domain = variants.mode(cfg) == "zhihuige"
    if sources and store.is_file():
        with assets.connection(store) as conn:
            conn.execute("BEGIN")
            if "v2_source" in _tables(conn):
                states = {row["qa_id"]: row["state"] for row in conn.execute(
                    """SELECT qa_id,fingerprint,state FROM v2_source
                        WHERE pipeline=? AND qa_id IN (SELECT value FROM json_each(?))""",
                    (variants.pipeline(cfg), assets.dump(sorted(sources))))
                    if row["fingerprint"] == sources[row["qa_id"]]["_fingerprint"]}
                read_filters = episode_projection.source_filters(filters) if domain else filters
                events = _read_events(cfg, read_filters, sources, conn, restrict_qa_ids=True)
                if previous and events:
                    # Historical snapshots can suppress a false "first seen" label, but only
                    # current fingerprints contribute to counts, evidence, or answers.
                    observed_before = {row[0] for row in conn.execute("""SELECT DISTINCT i.topic_id
                        FROM v2_intent i JOIN v2_source s ON s.qa_id=i.qa_id AND s.fingerprint=i.fingerprint
                        WHERE i.active=1 AND s.state='done' AND s.pipeline=? AND
                        CASE WHEN json_valid(s.snapshot) THEN CAST(json_extract(s.snapshot,'$.asked_at') AS INTEGER) END
                        BETWEEN 1 AND ?""", (variants.pipeline(cfg), start - 1))}
    if domain:
        legacy_events = events
        view_filters = {key: value for key, value in filters.items() if key != "focus_topic"}
        active_topics = _topic_definitions(cfg) if definitions is None else definitions
        events = episode_projection.project(cfg, sources, events, view_filters, set(active_topics))
        states.update({row["qa_id"]: "done" for row in events if row.get("episode_id")})
    current_events = [row for row in events if row["qa_id"] in current_sources]
    previous_events = [row for row in events if row["qa_id"] in previous_sources]
    if domain and filters.get("focus_topic"):
        current_events = episode_projection.focus_rows(current_events,
            [row for row in legacy_events if row["qa_id"] in current_sources], filters)
        previous_events = episode_projection.focus_rows(previous_events,
            [row for row in legacy_events if row["qa_id"] in previous_sources], filters)
    comparison = insights.comparison(current.public(current_sources, states),
                                     previous.public(previous_sources, states) if previous else None, _last_synced(cfg))
    comparison["episode"] = episode_projection.coverage(current_events)
    return current_events, previous_events, observed_before, comparison


def _topic_insight(topic: dict, rows: list[dict], prior: list[dict], earlier: bool, scope: dict, *, category: bool = False) -> dict:
    known, unknown = insights.participants(rows)
    previous_count = _consultations(prior, category=category)
    trend = insights.trend(topic["consultation_count"], previous_count, earlier, scope)
    if trend["status"] == "new" and any(row.get("episode_id") for row in rows):
        # Earlier episodes may have been published outside the compared periods.
        trend["status"] = "unknown"
    return {**topic, "platforms": insights.platforms(rows), "merchant_count": known,
            "unknown_merchant_sessions": unknown,
            "trend": trend}


def _consultations(rows: list[dict], *, category: bool) -> int:
    parts = 2 if category else 3
    return len({variants.consultation_key(row)[:parts] for row in rows})


def _rank_label(label: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", label).casefold().split())


_RANK_ALIASES = {(intent, _rank_label(label)): family
                 for (intent, family), (canonical_label, aliases) in _RANK_FAMILIES.items()
                 for label in (canonical_label, *aliases)}


def _rank_category(topic: dict) -> tuple[str, ...]:
    label = _rank_label(topic["theme"])
    intent = topic.get("primary_intent", "")
    if family := _RANK_ALIASES.get((intent, label)):
        return ("family", intent, family)
    if label and intent in ("functional_anomaly", "configuration_guidance"):
        return ("theme", intent, label)
    return ("topic", topic["id"])


def _rank_categories(definitions: dict, items: list[dict], rows: list[dict], prior: list[dict],
                     earlier: set[str], comparison: dict) -> list[dict]:
    categories_by_topic = {identity: _rank_category(topic) for identity, topic in definitions.items()}
    categories_by_topic.update({item["id"]: _rank_category(item) for item in items})
    members, current_rows, prior_rows = defaultdict(list), defaultdict(list), defaultdict(list)
    for item in items:
        members[categories_by_topic[item["id"]]].append(item)
    for row in rows:
        if (key := categories_by_topic.get(row["topic_id"])) in members:
            current_rows[key].append(row)
    for row in prior:
        if (key := categories_by_topic.get(row["topic_id"])) in members:
            prior_rows[key].append(row)
    seen = {categories_by_topic[identity] for identity in earlier if identity in categories_by_topic}

    ranked = []
    for key, children in members.items():
        top = children[0]
        current = current_rows[key]
        topic = {**_public_topic(top, current), "consultation_count": _consultations(current, category=True)}
        if key[0] == "family":
            topic["theme"] = _RANK_FAMILIES[(key[1], key[2])][0]
            # The representative's topic ID may change with the selected period.
            # A publication can use this stable family key independently.
            topic["category_key"] = "t_" + assets.digest(("voc-rank-category", key))[:24]
        if key[0] == "family" or len(children) > 1:
            topic["theme"] = topic["theme"].strip()
            topic["members"] = [{field: item[field] for field in ("id", "question", "consultation_count", "theme")}
                                for item in children]
        ranked.append(_topic_insight(topic, current, prior_rows[key], key in seen, comparison, category=True))
    return sorted(ranked, key=lambda item: (-item["consultation_count"], -item["event_count"], item["id"]))


def topics(cfg: Settings, filters: dict, *, workbench: bool = False, trend: str = "all") -> dict:
    if workbench:
        definitions = _topic_definitions(cfg)
        rows, previous, earlier, comparison = _workbench_data(cfg, filters, definitions=definitions)
        items = _topic_items(cfg, rows, definitions)
        if filters.get("focus_topic"):
            grouped, prior = defaultdict(list), defaultdict(list)
            for row in rows:
                grouped[row["topic_id"]].append(row)
            for row in previous:
                prior[row["topic_id"]].append(row)
            items = [_topic_insight(item, grouped[item["id"]], prior[item["id"]], item["id"] in earlier, comparison)
                     for item in items]
        else:
            items = _rank_categories(definitions, items, rows, previous, earlier, comparison)
        category_total = len(items)
        category_consultation_count = sum(item["consultation_count"] for item in items)
        if trend != "all":
            items = [item for item in items if item["trend"]["status"] == trend]
        return {"items": items, "total": len(items), "category_total": category_total,
                "category_consultation_count": category_consultation_count, "comparison": comparison,
                "event_count": len(rows)}
    items = _topic_items(cfg, _topic_view(cfg, filters))
    return {"items": items, "total": len(items)}


def feishu_topics(cfg: Settings, filters: dict, *, topic_ids: set[str] | None = None, grouped: bool = False) -> dict:
    """Rank the scoped export and retain its exact case rows for guest publication."""
    rows, previous, earlier, comparison = _workbench_data(cfg, filters)
    if topic_ids is not None:
        rows = [row for row in rows if row["topic_id"] in topic_ids]
    groups, prior = defaultdict(list), defaultdict(list)
    for row in previous:
        if topic_ids is None or row["topic_id"] in topic_ids:
            prior[row["topic_id"]].append(row)
    candidates: dict[str, dict[tuple[int, str], tuple]] = defaultdict(dict)
    for row in rows:
        groups[row["topic_id"]].append(row)
        source = row["source"]
        qa_id = source["qa_id"]
        if type(qa_id) is not int or qa_id <= 0:
            continue
        span = row["data"]["question_span"]
        anchor_id = next((part["message_id"] for part in source.get("_question_parts", [])
                          if part["start"] < span["end"] and span["start"] < part["end"]
                          and isinstance(part["message_id"], str) and part["message_id"]), "")
        rank = (not anchor_id, not source.get("_staff_verified", False),
                not source.get("answer"), -int(source.get("asked_at") or 0), qa_id, anchor_id)
        key = (qa_id, anchor_id)
        by_source = candidates[row["topic_id"]]
        by_source[key] = min(rank, by_source.get(key, rank))
    anchors = {topic_id: sorted(ranked, key=ranked.__getitem__)[:8]
               for topic_id, ranked in candidates.items()}
    definitions = _topic_definitions(cfg)
    items = _topic_items(cfg, rows, definitions)
    details = {item["id"]: _topic_insight(item, groups[item["id"]], prior[item["id"]],
                                         item["id"] in earlier, comparison)
               for item in items if item["id"] in groups}
    ranked = _rank_categories(definitions, items, rows, previous, earlier, comparison) if grouped else items
    return {"items": ranked, "comparison": comparison, "guest_anchors": anchors,
            "guest_rows": groups, "guest_topics": details}


def topic_detail(cfg: Settings, filters: dict, topic_id: str, *, workbench: bool = False,
                 exchange_only: bool = False) -> dict:
    if filters.get("focus_topic") and filters["focus_topic"] != topic_id:
        raise EngineError("当前范围内没有该问题。", 404)
    definitions = _topic_definitions(cfg)
    topic = definitions.get(topic_id)
    if workbench:
        if topic is None:
            raise EngineError("当前范围内没有该问题。", 404)
        rows, previous, earlier, comparison = _workbench_data(
            cfg, {**filters, "focus_topic": topic_id}, definitions=definitions)
        if not rows:
            raise EngineError("当前范围内没有该问题。", 404)
        evidence = [{**_topic_evidence(row), "workbench": insights.case_details(row)} for row in rows]
        public = _topic_insight(_public_topic(topic, rows), rows, previous, topic_id in earlier, comparison)
        if exchange_only:
            return {"topic": public, "events": evidence, "total": len(rows),
                    "workbench": {"comparison": comparison, "knowledge": [], "gaps": []}}
        global_sources = _sources(cfg, {})
        all_events = _events(cfg, {}, _current=global_sources)
        original = _original_topic_events(rows, all_events)
        latest = _topic_knowledge_assets(cfg, global_sources, all_events, original)
        knowledge = _knowledge_for_events(cfg, latest, original, all_events)
        knowledge = [{**item, "reuse_level": insights.reuse_level(item)} for item in knowledge]
        return {"topic": public, "events": evidence, "evidence": evidence, "total": len(rows),
                "workbench": {"comparison": comparison, "knowledge": knowledge,
                              "gaps": insights.gaps(rows, knowledge, latest)}}
    rows = _topic_view(cfg, {**filters, "focus_topic": topic_id})
    if topic is None or not rows:
        raise EngineError("当前范围内没有该问题。", 404)
    evidence = [_topic_evidence(row) for row in rows]
    return {"topic": _public_topic(topic, rows), "events": evidence, "evidence": evidence, "total": len(rows)}


def event_details(cfg: Settings, filters: dict, event_ids: set[str] | list[str]) -> dict[str, dict]:
    """Resolve requested events from one scoped source read and projection."""
    requested = set(event_ids)
    if not requested:
        return {}

    episode_ids = {identity for identity in requested if identity.startswith("episode_")}
    legacy_ids = requested - episode_ids
    current = _sources(cfg, filters)
    projected = _topic_view(cfg, filters, current=current) if episode_ids else None
    found = {row["id"]: row for row in projected or () if row["id"] in episode_ids}

    if legacy_ids:
        found.update({row["id"]: row for row in _events(cfg, filters, _current=current) if row["id"] in legacy_ids})
        missing = legacy_ids - found.keys()
        if missing and filters.get("focus_topic") and variants.mode(cfg) == "zhihuige":
            if projected is None:
                projected = _topic_view(cfg, filters, current=current)
            aliases = missing & episode_projection.legacy_event_ids(projected)
            if aliases:
                originals = _events(cfg, episode_projection.source_filters(filters), _current=current)
                found.update({row["id"]: row for row in originals if row["id"] in aliases})

    return {identity: {**_topic_evidence(row), "topic_id": row["topic_id"], "revision": row["revision"]}
            for identity, row in found.items()}


def event_detail(cfg: Settings, filters: dict, event_id: str) -> dict:
    event = event_details(cfg, filters, {event_id}).get(event_id)
    if event is None:
        raise EngineError("当前范围内没有该咨询事件，来源可能已更新。", 404)
    return {"event": event}


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


def _knowledge_plans(cfg: Settings, events: list[dict], *, topic_id: str = "") -> list[dict]:
    duplicates = _duplicate_context(events)
    by_topic = defaultdict(list)
    for row in events:
        by_topic[row["topic_id"]].append(row)
    result = []
    for topic in _stored_topics(path(cfg), domain_mode=variants.mode(cfg)):
        if topic_id and topic["id"] != topic_id:
            continue
        rows = by_topic.get(topic["id"])
        if not rows:
            continue
        for plan in variants.plans(cfg, topic, rows):
            links = duplicates.get(topic["id"], [])
            value = _compile_input(topic, plan["rows"], links)
            if variants.mode(cfg):
                value = assets.digest([value, variants.pipeline(cfg), plan["key"], plan.get("conflict_links", [])])
            result.append({**plan, "topic": topic, "links": links, "compile_input": value})
    return result


def _latest_assets(cfg: Settings, *, _current: dict | None = None, _plans: list[dict] | None = None,
                   topic_id: str = "") -> list[dict]:
    store = path(cfg)
    if not store.is_file():
        return []
    current = _sources(cfg, {}) if _current is None else _current
    plans = _knowledge_plans(cfg, _events(cfg, {}, _current=current), topic_id=topic_id) if _plans is None else _plans
    expected_plans = {(plan["topic"]["id"], plan["key"]): plan for plan in plans}
    quality_version = variants.quality_version(cfg)
    with assets.connection(store) as conn:
        if "v2_asset" not in _tables(conn):
            return []
        result = []
        has_variants = "variant_key" in {row[1] for row in conn.execute("PRAGMA table_info(v2_asset)")}
        by_variant = " AND b.variant_key=a.variant_key" if has_variants else ""
        query = "SELECT a.* FROM v2_asset a JOIN v2_topic t ON t.id=a.topic_id WHERE t.status!='ignored' AND a.version=(SELECT MAX(b.version) FROM v2_asset b WHERE b.topic_id=a.topic_id" + by_variant + ")"
        if topic_id:
            query += " AND a.topic_id=?"
        for row in conn.execute(query, (topic_id,) if topic_id else ()):
            branch = row["variant_key"] if has_variants else ""
            plan = expected_plans.get((row["topic_id"], branch))
            expected_input = plan["compile_input"] if plan else None
            data = json.loads(row["payload"])
            restored = False
            if expected_input and (data.get("quality_version") != quality_version or data.get("compile_input") != expected_input):
                # The compiler deduplicates an exact prior input, so a newer
                # version with different evidence must not mask that result.
                clause = " AND variant_key=?" if has_variants else ""
                args = (row["topic_id"], row["version"], expected_input, quality_version)
                if has_variants:
                    args += (branch,)
                prior = conn.execute(
                    "SELECT * FROM v2_asset WHERE topic_id=? AND version<? "
                    "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.compile_input') END=? "
                    "AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.quality_version') END=?"
                    + clause + " ORDER BY version DESC,id DESC LIMIT 1", args,
                ).fetchone()
                if prior is not None:
                    row = prior
                    data = json.loads(row["payload"])
                    restored = True
            item = dict(row)
            item.pop("payload")
            item.update(data)
            item["status"] = row["status"]
            if restored and item["status"] == "ready" and _same_evidence_manually_ignored(conn, row["topic_id"], plan["rows"]):
                item["status"] = "ignored"
            item["id"] = str(row["id"])
            item["updated_at"] = _when(row["updated_at"])
            item["created_at"] = _when(row["created_at"])
            item["variant_key"] = branch
            # Cross-database updates cannot be atomic. Revalidate every citation
            # at read time so a concurrently changed/retired QA is never ready.
            item["needs_compile"] = (expected_input is None or item.get("quality_version") != quality_version
                                     or item.get("compile_input") != expected_input)
            item["stale"] = item["needs_compile"] or any(not current.get(citation["qa_id"]) or current[citation["qa_id"]]["_fingerprint"] != citation["source_fingerprint"]
                                                        for citation in item["evidence"])
            result.append(item)
        return result


def _with_conversation_verification(cfg: Settings, items: list[dict], rows: list[dict], *, events: list[dict] | None = None) -> list[dict]:
    # Dynamic read-only certificates do not change extraction/compile identity.
    from . import voc_knowledge_reuse, voc_outcome
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
        reuse = voc_knowledge_reuse.evaluate(cfg, item, cited, certificate, all_events)
        result.append({**item, "conversation_verification": certificate, "knowledge_reuse": reuse})
    return result


def _knowledge_for_events(cfg: Settings, candidates: list[dict], rows: list[dict], all_events: list[dict]) -> list[dict]:
    visible = {row["topic_id"] for row in rows}
    event_ids = {row["id"] for row in rows}
    items = [item for item in candidates
             if item["status"] == "ready" and not item["stale"] and item["topic_id"] in visible
             and any(citation["event_id"] in event_ids for citation in item["evidence"])]
    return _with_conversation_verification(cfg, items, rows, events=all_events)


def knowledge(cfg: Settings, filters: dict, knowledge_id: str = "") -> dict:
    current = _sources(cfg, {})
    all_events = _events(cfg, {}, _current=current)
    focus = filters.get("focus_topic", "")
    if focus and variants.mode(cfg) == "zhihuige":
        rows = _original_topic_events(_topic_view(cfg, filters), all_events)
        candidates = _topic_knowledge_assets(cfg, current, all_events, rows)
    else:
        rows = _events(cfg, filters) if any(value for key, value in filters.items() if key != "source") else all_events
        plans = _knowledge_plans(cfg, all_events, topic_id=focus)
        candidates = _latest_assets(cfg, _current=current, _plans=plans, topic_id=focus)
    items = _knowledge_for_events(cfg, candidates, rows, all_events)
    if knowledge_id:
        item = next((item for item in items if item["id"] == str(knowledge_id)), None)
        if item is None:
            raise EngineError("当前范围内没有可用的该知识版本。", 404)
        return {"knowledge": item, "evidence": item["evidence"]}
    return {"items": items, "total": len(items)}


def sops(cfg: Settings, filters: dict, identity: str = "", *, _current: dict | None = None) -> dict:
    """Read workflows against the same official proof used by the episode ledger."""
    from . import voc_episode_live, voc_sops

    source = _sources(cfg, episode_projection.source_filters(filters)) if _current is None else _current
    sessions = {row["_session_key"] for row in source.values() if row.get("_question_parts")}
    items = []
    for segment in voc_episode_live.load_current(cfg, sessions, include_messages=True):
        for episode in segment["episodes"]:
            if filters.get("focus_topic") and episode.get("topic_id") != filters["focus_topic"]:
                continue
            intent = episode_projection._INTENTS.get(episode["kind"])
            diagnosis = "not_applicable" if intent == "configuration_guidance" else "reported"
            if filters.get("support_intent") not in (None, "", "all", intent):
                continue
            if filters.get("diagnosis_status") not in (None, "", "all", diagnosis):
                continue
            matching = [qa_id for qa_id in segment["qa_anchors"] if qa_id in source
                        and episode_projection._question_span(source[qa_id], episode["question"]) is not None]
            if len(matching) != 1:
                continue
            case = voc_sops.compile_case(episode, segment["messages"], matching[0])
            if case is not None and (not identity or case["id"] == identity):
                items.append(case)
    if identity:
        if len(items) != 1:
            raise EngineError("当前范围内没有可用的该处理流程。", 404)
        return {"item": items[0]}
    order = {"verified": 0, "guidance": 1, "incomplete": 2}
    items.sort(key=lambda item: (order[item["status"]], -item["problem"]["ts"], item["id"]))
    return {"items": items, "total": len(items)}


def _read_issue_pilot(cfg: Settings, filters: dict) -> dict:
    from . import voc_issue_pilot

    if variants.mode(cfg) != "zhihuige" or cfg.runtime.mock:
        return voc_issue_pilot.build([])
    source = _sources(cfg, episode_projection.source_filters(filters), identify_merchants=True)
    return voc_issue_pilot.build(_topic_view(cfg, filters, current=source))


def issue_pilot(cfg: Settings, filters: dict) -> dict:
    result = _read_issue_pilot(cfg, filters)
    return {**result, "items": [{key: value for key, value in item.items() if not key.startswith("_")}
                                for item in result["items"]]}


def _playbook_branch_key(branch: dict) -> tuple:
    if branch.get("id"):
        return (branch["id"],)
    return branch["condition"], tuple(step["text"] for step in branch["steps"]), branch["retest"]


def _recheck_playbook_branches(scoped: dict, global_result: dict) -> dict:
    """Keep evidence within the requested view without hiding counterevidence."""
    global_items = {item["id"]: item for item in global_result["items"]}
    updated = []
    for item in scoped["items"]:
        if not item["branches"]:
            updated.append(item)
            continue
        global_item = global_items.get(item["id"], {})
        current_branches = {_playbook_branch_key(branch): branch for branch in global_item.get("branches", [])}
        retained = []
        for branch in item["branches"]:
            current = current_branches.get(_playbook_branch_key(branch))
            if current is None:
                continue
            if branch.get("status") == "verified" and current.get("status") != "verified":
                branch = {**branch, "status": "guidance", "result_evidence": []}
            retained.append(branch)
        missing = list(item["missing"])
        if len(retained) < len(item["branches"]):
            reason = "conflicting_case" if "conflicting_case" in global_item.get("missing", []) else "case_evidence"
            if reason not in missing:
                missing.append(reason)
        status = ("verified" if retained and all(branch.get("status") == "verified" for branch in retained)
                  else "guidance" if retained else "investigation")
        if status != "verified" and "verified_outcome" not in missing:
            missing.append("verified_outcome")
        updated.append({**item, "branches": retained, "status": status, "missing": missing})
    return {**scoped, "items": updated}


def _build_playbooks(cfg: Settings, filters: dict, *, include_case_paths: bool = True) -> dict:
    from . import voc_case_paths, voc_issue_pilot, voc_playbooks, voc_solution_sop, voc_solution_store

    if variants.mode(cfg) != "zhihuige" or cfg.runtime.mock:
        return {"items": [], "total": 0, "case_paths": []}
    current = _sources(cfg, episode_projection.source_filters(filters), identify_merchants=True)
    events = _topic_view(cfg, filters, current=current)
    issues = voc_issue_pilot.build(events)["items"]
    if not issues and not include_case_paths:
        return {"items": [], "total": 0, "case_paths": []}
    path_sources = current
    if any(filters.get(key) not in (None, "", "all") for key in ("focus_topic", "support_intent", "diagnosis_status")):
        matching = {event["qa_id"] for event in events}
        path_sources = {qa: source for qa, source in current.items() if qa in matching}
    proof_by_qa = {}
    case_paths = voc_solution_store.enrich(cfg, path_sources, voc_case_paths.build(cfg, path_sources),
                                          proof_by_qa=proof_by_qa)
    if not issues:
        return {"items": [], "total": 0, "case_paths": case_paths}
    current_sops = sops(cfg, filters, _current=current)["items"]
    extra_sops = voc_solution_sop.compile_supported(issues, case_paths, proof_by_qa, current_sops)
    superseded = {voc_solution_sop.case_key(case) for case in extra_sops}
    retained = [case for case in current_sops if voc_solution_sop.case_key(case) not in superseded]
    result = voc_playbooks.build(issues, [*retained, *extra_sops])
    return {**result, "case_paths": case_paths if include_case_paths else []}


def _check_playbook_conflicts(cfg: Settings, filters: dict, result: dict) -> dict:
    if any(item["branches"] for item in result["items"]) and any(
            value not in (None, "", 0, "all", "local") for value in filters.values()):
        return _recheck_playbook_branches(result, _build_playbooks(cfg, {}, include_case_paths=False))
    return result


def playbooks(cfg: Settings, filters: dict) -> dict:
    return _check_playbook_conflicts(cfg, filters, _build_playbooks(cfg, filters))


def _complete_sop_candidates(items: list[dict]) -> list[dict]:
    candidates = []
    for item in items:
        branches = []
        for branch in item["branches"]:
            if branch["status"] != "verified" or not branch["condition"] or not branch["steps"] or not branch["retest"]:
                continue
            witnesses = {ref["qa_id"] for ref in branch["result_evidence"]}
            evidence_groups = [branch["condition_evidence"], branch["retest_evidence"],
                               *(step["evidence"] for step in branch["steps"])]
            for evidence in evidence_groups:
                witnesses.intersection_update(ref["qa_id"] for ref in evidence)
            if not witnesses:
                continue

            def sourced(evidence):
                return [ref for ref in evidence if ref["qa_id"] in witnesses]

            escalation = sourced(branch["escalation_evidence"])
            branches.append({**branch, "case_count": len(witnesses),
                             "condition_evidence": sourced(branch["condition_evidence"]),
                             "steps": [{**step, "evidence": sourced(step["evidence"])} for step in branch["steps"]],
                             "retest_evidence": sourced(branch["retest_evidence"]),
                             "result_evidence": sourced(branch["result_evidence"]),
                             "escalation": branch["escalation"] if escalation else "",
                             "escalation_evidence": escalation})
        if branches:
            candidates.append({**item, "branches": branches})
    return candidates


def complete_sops(cfg: Settings, filters: dict) -> dict:
    """Publish only procedures with a complete witness and no conflicting case."""
    scoped = _build_playbooks(cfg, filters, include_case_paths=False)
    candidates = _complete_sop_candidates(scoped["items"])
    if not candidates:
        return {"items": [], "total": 0}
    current = _check_playbook_conflicts(cfg, filters, {"items": candidates, "total": len(candidates)})
    items = []
    for item in current["items"]:
        branches = [branch for branch in item["branches"] if branch["status"] == "verified"]
        if not branches:
            continue
        checked = {ref["qa_id"] for branch in branches for ref in branch["result_evidence"]}
        items.append({key: item[key] for key in ("id", "issue_id", "title", "family", "platform", "module", "condition")}
                     | {"status": "verified", "checked_cases": len(checked), "branches": branches,
                        "triage": [], "missing": [], "evidence": []})
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
    topic_events = _topic_view(cfg, filters, source) if variants.mode(cfg) == "zhihuige" else events
    processed = 0
    context_needed = 0
    latest_job = None
    if path(cfg).is_file():
        with assets.connection(path(cfg)) as conn:
            if "v2_source" in _tables(conn):
                states = Counter(
                    row["state"] for row in conn.execute("SELECT qa_id,fingerprint,state FROM v2_source WHERE pipeline=?", (variants.pipeline(cfg),))
                    if row["qa_id"] in source and row["fingerprint"] == source[row["qa_id"]]["_fingerprint"]
                )
                context_needed = states["context_needed"]
                processed = states["done"] + states["excluded"] + context_needed
                row = conn.execute("SELECT * FROM v2_job ORDER BY id DESC LIMIT 1").fetchone()
                latest_job = _public_job(row) if row else None
    global_source = _sources(cfg, {}) if any(value for key, value in filters.items() if key != "source") else source
    all_events = events if global_source is source else _events(cfg, {}, _current=global_source)
    if filters.get("focus_topic") and variants.mode(cfg) == "zhihuige":
        events = _original_topic_events(topic_events, all_events)
    visible = {row["topic_id"] for row in events}
    plans = _knowledge_plans(cfg, all_events)
    latest = _latest_assets(cfg, _current=global_source, _plans=plans)
    by_branch = {(item["topic_id"], item["variant_key"]): item for item in latest}
    pending_topics = {plan["topic"]["id"] for plan in plans if plan["topic"]["id"] in visible
                      and ((plan["topic"]["id"], plan["key"]) not in by_branch
                           or by_branch[(plan["topic"]["id"], plan["key"])]["needs_compile"])}
    needs_compile = len(pending_topics)
    event_ids = {row["id"] for row in events}
    know = [item for item in latest if item["status"] == "ready" and not item["stale"] and item["topic_id"] in visible
            and any(citation["event_id"] in event_ids for citation in item["evidence"])]
    know = _with_conversation_verification(cfg, know, events, events=all_events)
    reuse_counts = Counter(item["knowledge_reuse"]["level"] for item in know)
    problems = _exception_items(cfg, source, visible, {item["fingerprint"]: item for item in latest}, filters=filters, event_ids=event_ids)
    return {"total_events": len(topic_events), "topic_count": len({row["topic_id"] for row in topic_events}),
            "ready_knowledge": len(know), "auto_reusable_knowledge": reuse_counts["verified"] + reuse_counts["guidance"],
            "verified_effect_knowledge": reuse_counts["verified"], "guidance_knowledge": reuse_counts["guidance"],
            "exception_count": len(problems), "needs_compile": needs_compile,
            "answered_events": sum(bool(row["source"]["answer"]) for row in topic_events), "source_pairs": len(source),
            "consultation_count": len({variants.consultation_key(row) for row in topic_events}),
            "coverage": {"processed_pairs": processed, "pending_pairs": len(source) - processed, "context_needed_pairs": context_needed,
                         "anchored_events": sum(row["source"]["source_kind"] == "source-anchored" for row in topic_events),
                         "legacy_events": sum(row["source"]["source_kind"] != "source-anchored" for row in topic_events),
                         "answered_pairs": sum(bool(pair["answer"]) for pair in source.values()),
                         "resolved_events": sum(row["data"]["answer_kind"] == "resolved_answer" for row in topic_events),
                         "collect_info_events": sum(row["data"]["answer_kind"] == "collect_info" for row in topic_events),
                         "episode": episode_projection.coverage(topic_events),
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
    def __init__(self, message: str, *, after_failure: bool = False, cannot_fit: bool = False):
        super().__init__(message)
        self.after_failure = after_failure
        self.cannot_fit = cannot_fit


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
        if reservation > max_tokens and feedback is None:
            raise _ExtractionBudgetStop("本轮总预算不足以提取当前来源，请提高预算后重试。", cannot_fit=True)
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
            # Fresh work stays ahead of retries. Alternate verified answers
            # with other consultations so both outputs advance under the same
            # model budget, keeping the existing order within each group.
            pending.sort(key=lambda item: item[:4])
            with_reply, other, retries = [], [], []
            for retry, _, _, _, pair in pending:
                if retry:
                    retries.append(pair)
                elif pair["_staff_verified"] and pair["answer"]:
                    with_reply.append(pair)
                else:
                    other.append(pair)
            fresh = [pair for group in zip_longest(with_reply, other) for pair in group if pair is not None]
            selected = (fresh + retries)[:limit]
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
                latest = _sources(cfg, {}, only_qa_id=pair["qa_id"]).get(pair["qa_id"])
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
                if not job["error"] or not error.cannot_fit:
                    job["error"] = str(error)
                if error.cannot_fit:
                    job["skipped"] += 1
                    with assets.connection(path(cfg), write=True) as conn:
                        assets.put_exception(conn, kind="extraction_failed", fingerprint=pair["_fingerprint"], qa_id=pair["qa_id"],
                                             payload={"question": pair["question"][:120], "reason": str(error)})
                    continue
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


def relevant_items(cfg: Settings, items: list[dict], query: str, *, kind: str = 'knowledge',
                   limit: int | None = None, require_staff: bool = False) -> list[dict]:
    """Rank an already scoped/gated complete list, without inventing facts."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
        raise EngineError("检索词须为 1–1000 字。")
    try:
        return rank_materials(path(cfg), _semantic(cfg), query.strip(), items,
                              kind=kind, limit=limit, require_staff=require_staff)
    except SemanticError as error:
        raise EngineError(str(error), 503) from None


def relevant_topics(cfg: Settings, filters: dict, query: str) -> list[dict]:
    """All relevant scoped topics, not a top-N sample used as a false total."""
    return relevant_items(cfg, topics(cfg, filters)['items'], query, kind='topic')


def search_staff_symptom(cfg: Settings, filters: dict, symptom: str, limit: int) -> dict:
    # Match the customer's symptom before selecting the source-bound staff reply.
    pattern = STAFF_SYMPTOMS[symptom]
    rows = _topic_view(cfg, filters)
    topics_by_id = {row['id']: row for row in _topic_items(cfg, rows)}
    events = []
    for row in rows:
        if row['topic_id'] not in topics_by_id or not row['source'].get('_staff_verified'):
            continue
        event = _topic_evidence(row)
        actual = ((event.get('domain') or {}).get('actual') or {}).get('quote') or ''
        if (pattern.search(event.get('question_quote') or event['question']) or pattern.search(actual)) and has_staff_reply(event):
            events.append({**event, 'topic_id': row['topic_id']})
    events.sort(key=lambda row: (-(row.get('asked_at') or 0), row['id']))
    events = events[:limit]
    return {'events': events, 'topics': [topics_by_id[identity] for identity in
                                         dict.fromkeys(row['topic_id'] for row in events)]}


def search_local_evidence(cfg: Settings, filters: dict, query: str, limit: int = 3) -> list[dict]:
    """Retrieve source excerpts in the current scope without invoking a model."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000 or type(limit) is not int or not 1 <= limit <= 10:
        raise EngineError("检索词须为 1–2000 字，结果数为 1–10。")
    if not has_local_evidence_terms(query):
        return []
    events = [{**_topic_evidence(row), 'topic_id': row['topic_id'], 'revision': row['revision']}
              for row in _topic_view(cfg, filters)]
    return rank_local_evidence(query, events, limit)


def search(cfg: Settings, filters: dict, query: str, limit: int = 5, *, require_staff: bool = False) -> dict:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000 or type(limit) is not int or not 1 <= limit <= 10:
        raise EngineError("检索词须为 1–1000 字，结果数为 1–10。")
    if require_staff:
        rows = _topic_view(cfg, filters)
        topics_by_id = {item['id']: item for item in _topic_items(cfg, rows)}
        event_items = [{**_topic_evidence(row), 'topic_id': row['topic_id']} for row in rows
                       if row['topic_id'] in topics_by_id and row['source'].get('_staff_verified')]
        events = relevant_items(cfg, event_items, query, kind='event', limit=limit, require_staff=True) if event_items else []
        if events:
            selected = [topics_by_id[identity] for identity in dict.fromkeys(row['topic_id'] for row in events)]
        else:
            selected = relevant_items(cfg, list(topics_by_id.values()), query, kind='topic', limit=limit)
        return {"topics": selected, "knowledge": [], "events": events}

    candidates = topics(cfg, filters)["items"]
    if not candidates:
        return {"topics": [], "knowledge": [], "events": []}
    ids = {item['id'] for item in candidates}
    rows = [row for row in _topic_view(cfg, filters) if row['topic_id'] in ids]
    links = defaultdict(set)
    for row in rows:
        for original_id in row.get('legacy_event_ids', ()):
            links[original_id].add(row['topic_id'])

    def topic_ids(item):
        destinations = {item['topic_id']} & ids
        for citation in item.get('evidence', []):
            destinations.update(links.get(citation.get('event_id'), ()))
        return destinations

    # Topic labels are a navigation aid, not a proxy for which answer matches.
    # Rank the complete, scoped material before truncating it to the UI budget.
    know = relevant_items(cfg, [item for item in knowledge(cfg, filters)['items'] if topic_ids(item)],
                          query, kind='knowledge', limit=limit)
    event_items = [{**_topic_evidence(row), 'topic_id': row['topic_id']} for row in rows]
    events = relevant_items(cfg, event_items, query, kind='event', limit=limit)
    selected = relevant_items(cfg, candidates, query, kind='topic')
    # A precise answer can be relevant when the broad topic title is not.
    material_scores = {}
    material_semantics = {}
    for item in [*know, *events]:
        for topic_id in topic_ids(item):
            material_scores[topic_id] = max(material_scores.get(topic_id, 0.), item['retrieval']['score'])
            material_semantics[topic_id] = max(material_semantics.get(topic_id, 0.), item['similarity'])
    selected_ids = {item['id'] for item in selected}
    selected += [{**item, 'similarity': material_semantics[item['id']]}
                 for item in candidates if item['id'] in material_scores and item['id'] not in selected_ids]
    selected.sort(key=lambda item: (-max(material_scores.get(item['id'], 0.), item.get('retrieval', {}).get('score', item.get('similarity', 0.))), item['id']))
    selected = selected[:limit]
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
