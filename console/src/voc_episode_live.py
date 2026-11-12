"""Incrementally analyze verified, complete official support conversations."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from . import voc, voc_assets, voc_episode, voc_outcome
from .clean.rules import mask_pii
from .config import Settings
from .extract.text import clean_visible_text
from .model_protocol import ModelProtocolError
from .voc_episode_replay import _alias, _question_anchors, _segments
from .voc_semantics import SemanticError


_PIPELINE = voc_episode.VERSION + "/official-v1"
_VISITOR_PREFIXES = ("qiyu-external:", "qiyu-external-sha256:", "qiyu-user:")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS episode_live_segment (
 segment_key TEXT PRIMARY KEY, session_key TEXT NOT NULL, session_id TEXT NOT NULL,
 official_version TEXT NOT NULL, fingerprint TEXT NOT NULL, pipeline TEXT NOT NULL,
 state TEXT NOT NULL, episodes TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL, retry_at REAL NOT NULL, owner TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS episode_live_session ON episode_live_segment(session_key);
"""


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _gather(cfg: Settings, *, filters: dict | None = None, session_ids: set[str] | None = None) -> list[dict]:
    path = Path(cfg.runtime.db_path).resolve()
    if not path.is_file() or session_ids == set():
        return []
    records = []
    observed_at = time.time()
    with voc_assets.connection(path) as conn:
        conn.execute("BEGIN")
        if not all(_table(conn, name) for name in ("push_sync_state", "raw_session", "raw_message", "qa_origin", "qa_pair")):
            return []
        selected = set(voc._scope(conn, filters, include_messages=False)) if filters else None
        if "source_message_id" not in {row[1] for row in conn.execute("PRAGMA table_info(raw_message)")}:
            return []
        if not {"source_json", "source_version", "version"} <= {row[1] for row in conn.execute("PRAGMA table_info(push_sync_state)")}:
            return []
        session_clause = "" if session_ids is None else " AND state.session_id IN (SELECT value FROM json_each(?))"
        params = () if session_ids is None else (voc_assets.dump(sorted(session_ids)),)
        rows = conn.execute("""SELECT state.session_id, state.source_version, state.source_json,
                session.user_name, session.staff_name, session.from_group
            FROM push_sync_state AS state
            JOIN raw_session AS session ON state.session_id=session.session_id
            WHERE state.status='completed' AND state.source_json<>'' AND state.source_version=state.version
                AND session.end_ts>0""" + session_clause + " ORDER BY session.end_ts DESC, state.session_id ASC", params)
        for row in rows:
            sid = row["session_id"]
            anchors = _question_anchors(conn, sid)
            if not anchors or selected is not None and not any(qa in selected for qa, _ in anchors):
                continue
            try:
                snapshot = voc_outcome._snapshot(conn, sid)
            except (ValueError, KeyError, TypeError, OverflowError):
                continue
            qa_owners = dict(conn.execute("SELECT qa_id,customer_id FROM qa_pair WHERE session_id=?", (sid,)))
            session_key = "s_" + voc_assets.digest(["raw-session", sid])
            names = sorted({name for key in ("user_name", "staff_name", "from_group")
                            if len(name := mask_pii((row[key] or "").strip())) >= 2}, key=lambda name: (-len(name), name))
            proof = voc_assets.digest(row["source_json"])
            by_id = {message["source_message_id"]: message for message in snapshot["messages"]
                     if message.get("source_message_id")}
            for segment in _segments(snapshot, session_key, names):
                source_ids = segment.pop("source_ids")
                owners = {by_id[mid]["_owner"] for mid in source_ids}
                if len(owners) != 1:
                    continue
                customer_id = next(iter(owners))
                if not customer_id or not customer_id.startswith(_VISITOR_PREFIXES):
                    continue
                customer_messages = {mid for mid in source_ids if by_id[mid]["role"] == "user" and by_id[mid]["_is_text"]}
                segment_anchors = {qa: ids for qa, ids in anchors if qa_owners.get(qa) == customer_id and ids <= customer_messages}
                if not segment_anchors or selected is not None and not selected.intersection(segment_anchors):
                    continue
                messages = segment["messages"]
                original = {_alias("message", mid): by_id[mid] for mid in source_ids}
                if len(original) != len(messages):
                    continue
                segment_key = "seg_" + voc_assets.digest([sid, customer_id, original[messages[0]["id"]]["source_message_id"]])[:28]
                records.append({**segment, "session_id": sid, "customer_id": customer_id, "segment_key": segment_key,
                                "official_version": row["source_version"], "qa_anchors": segment_anchors,
                                "fingerprint": voc_assets.digest([_PIPELINE, row["source_version"], proof, messages]),
                                "observed_at": observed_at,
                                "original": original})
    return records


def _stored(cfg: Settings, session_keys: set[str] | None = None) -> dict[str, dict]:
    path = voc_assets.store_path(cfg)
    if not path.is_file() or session_keys == set():
        return {}
    with voc_assets.connection(path) as conn:
        if not _table(conn, "episode_live_segment"):
            return {}
        query = "SELECT * FROM episode_live_segment WHERE pipeline=?"
        args = (_PIPELINE,)
        if session_keys is not None:
            query += " AND session_key IN (SELECT value FROM json_each(?))"
            args += (voc_assets.dump(sorted(session_keys)),)
        return {row["segment_key"]: dict(row) for row in conn.execute(query, args)}


def _matches(row: dict | None, segment: dict) -> bool:
    return bool(row and row["fingerprint"] == segment["fingerprint"] and row["pipeline"] == _PIPELINE)


def _due(row: dict | None, segment: dict, now: float) -> bool:
    return (not _matches(row, segment)
            or row["state"] in ("running", "retry") and row["retry_at"] <= now)


def status(cfg: Settings) -> dict:
    result = {"pending": 0, "segments": 0, "episodes": 0, "assigned": 0, "errors": 0, "unsupported": 0}
    if cfg.runtime.voc_domain != "zhihuige" or cfg.runtime.mock:
        return result
    stored = _stored(cfg)
    now = time.time()
    for segment in _gather(cfg):
        row = stored.get(segment["segment_key"])
        result["pending"] += int(_due(row, segment, now))
        if not _matches(row, segment):
            continue
        if row["state"] == "complete":
            episodes = json.loads(row["episodes"])
            result["segments"] += 1
            result["episodes"] += len(episodes)
            result["assigned"] += sum(bool(episode.get("topic_id")) for episode in episodes)
        elif row["state"] == "retry":
            result["errors"] += 1
        elif row["state"] == "unsupported":
            result["unsupported"] += 1
    return result


def pending(cfg: Settings, filters: dict | None = None) -> bool:
    if cfg.runtime.voc_domain != "zhihuige" or not voc.configured(cfg):
        return False
    stored = _stored(cfg)
    now = time.time()
    return any(_due(stored.get(segment["segment_key"]), segment, now) for segment in _gather(cfg, filters=filters))


def load_current(cfg: Settings, session_keys: set[str], *, include_messages: bool = False) -> list[dict]:
    """Read only current, independently checked segment publications."""
    if not session_keys or cfg.runtime.voc_domain != "zhihuige" or cfg.runtime.mock:
        return []
    stored = {key: row for key, row in _stored(cfg, session_keys).items() if row["state"] == "complete"}
    records = []
    for segment in _gather(cfg, session_ids={row["session_id"] for row in stored.values()}):
        row = stored.get(segment["segment_key"])
        if _matches(row, segment):
            fields = ("session_key", "session_id", "customer_id", "customer_key", "segment_key", "official_version", "qa_anchors")
            record = {**{field: segment[field] for field in fields}, "episodes": json.loads(row["episodes"])}
            if include_messages:
                record["messages"] = [_original_message(message, segment["original"][message["id"]])
                                      for message in segment["messages"]]
            records.append(record)
    return records


def _original_message(message: dict, raw: dict) -> dict:
    return {"id": raw["source_message_id"], "role": message["role"], "kind": message["kind"],
            "ts": message["ts"], "text": mask_pii(clean_visible_text(raw["content"] or ""))}


def _claim(path: Path, segment: dict, lease_seconds: int) -> tuple[str, int] | None:
    now = time.time()
    owner = uuid4().hex
    key = segment["segment_key"]
    with voc_assets.connection(path, write=True) as conn, voc_assets.transaction(conn):
        prior = conn.execute("SELECT * FROM episode_live_segment WHERE segment_key=?", (key,)).fetchone()
        if (not _due(prior, segment, now)
                or prior is not None and prior["updated_at"] > segment["observed_at"]):
            return None
        attempts = prior["attempts"] + 1 if _matches(prior, segment) else 1
        conn.execute("""INSERT INTO episode_live_segment(segment_key,session_key,session_id,official_version,fingerprint,
            pipeline,state,attempts,retry_at,owner,updated_at) VALUES(?,?,?,?,?,?,'running',?,?,?,?)
            ON CONFLICT(segment_key) DO UPDATE SET official_version=excluded.official_version,
              fingerprint=excluded.fingerprint,pipeline=excluded.pipeline,state='running',episodes='[]',error='',
              attempts=excluded.attempts,retry_at=excluded.retry_at,owner=excluded.owner,updated_at=excluded.updated_at""",
            (key, segment["session_key"], segment["session_id"], segment["official_version"], segment["fingerprint"],
             _PIPELINE, attempts, now + lease_seconds, owner, now))
    return owner, attempts


def _write_state(conn: sqlite3.Connection, segment: dict, claim: tuple[str, int], *,
                 episodes: list[dict] | None = None, error: str = "", unsupported: bool = False) -> bool:
    now = time.time()
    owner, attempts = claim
    state = "complete" if episodes is not None else "unsupported" if unsupported else "retry"
    retry_at = now + min(3600, 60 * 2 ** min(attempts - 1, 6)) if state == "retry" else 0
    changed = conn.execute("""UPDATE episode_live_segment SET state=?,episodes=?,error=?,retry_at=?,owner='',updated_at=?
        WHERE segment_key=? AND fingerprint=? AND owner=? AND state='running'""",
        (state, voc_assets.dump(episodes or []), error, retry_at, now,
         segment["segment_key"], segment["fingerprint"], owner)).rowcount
    return changed == 1


def _finish(path: Path, segment: dict, claim: tuple[str, int], *, error: str, unsupported: bool = False) -> bool:
    with voc_assets.connection(path, write=True) as conn, voc_assets.transaction(conn):
        return _write_state(conn, segment, claim, error=error, unsupported=unsupported)


def _current(cfg: Settings, segment: dict) -> bool:
    return any(item["segment_key"] == segment["segment_key"] and item["fingerprint"] == segment["fingerprint"]
               for item in _gather(cfg, session_ids={segment["session_id"]}))


def _ground(episode: dict, segment: dict) -> dict:
    original = segment["original"]

    def reference(ref: dict) -> dict:
        row = original[ref["message_id"]]
        visible = mask_pii(clean_visible_text(row["content"] or ""))
        quote = ref["quote"]
        start = visible.find(quote)
        exact = start >= 0 and visible.find(quote, start + 1) < 0
        return {"message_id": row["source_message_id"], "quote": quote,
                "start": start if exact else None, "end": start + len(quote) if exact else None}

    return {**episode, "message_ids": [original[mid]["source_message_id"] for mid in episode["message_ids"]],
            "question": reference(episode["question"]), "answers": [reference(ref) for ref in episode["answers"]],
            "actions": [{**action, "instruction": reference(action["instruction"]),
                         "feedback": reference(action["feedback"]) if action["feedback"] else None}
                        for action in episode["actions"]]}


def _legacy_candidates(cfg: Settings, segment: dict, episodes: list[dict]) -> dict:
    from . import voc_engine

    sources = {}
    for qa_id in segment["qa_anchors"]:
        sources.update(voc_engine._sources(cfg, {}, only_qa_id=qa_id))
    events = voc_engine._events(cfg, {}, _current=sources)
    if not events:
        return {}
    topics = {topic["id"]: topic for topic in voc_engine._stored_topics(voc_assets.store_path(cfg), domain_mode="zhihuige")}
    matches = defaultdict(dict)
    for episode in episodes:
        ref = episode["question"]
        if ref["start"] is None:
            continue
        for event in events:
            source = event["source"]
            if source["_session_key"] != segment["session_key"] or event["topic_id"] not in topics:
                continue
            span = event["data"]["question_span"]
            for part in source["_question_parts"]:
                if (part["message_id"] == ref["message_id"] and span.get("alignment") == "exact"
                        and span["start"] == part["start"] + ref["start"] and span["end"] == part["start"] + ref["end"]):
                    matches[episode["id"]][event["topic_id"]] = {**topics[event["topic_id"]], "quote": span["quote"]}
    return {episode_id: list(values.values()) for episode_id, values in matches.items()}


def _reservation(messages: list[dict]) -> int:
    payload = json.dumps({"messages": messages}, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    prompt_bytes = 3 * len(voc_episode.TASK.encode()) + len(payload.encode())
    return prompt_bytes + voc_episode.MAX_OUTPUT_TOKENS + 384


def run(cfg: Settings, *, max_calls: int, max_tokens: int, filters: dict | None = None,
        embedder=None, rerank=None, extract: Callable | None = None) -> dict:
    """Extract and publish bounded episodes without holding SQLite transactions during inference."""
    result = {"calls": 0, "usage_tokens": 0, "reserved_tokens": 0, "segments_processed": 0,
              "episodes": 0, "assigned": 0, "errors": {}}
    if (type(max_calls) is not int or type(max_tokens) is not int or max_calls <= 0
            or max_tokens < voc_episode.MAX_OUTPUT_TOKENS or cfg.runtime.voc_domain != "zhihuige" or not voc.configured(cfg)):
        return result
    extract = extract or voc_episode.extract
    segments = _gather(cfg, filters=filters)
    stored = _stored(cfg)
    now = time.time()
    segments = [segment for segment in segments if _due(stored.get(segment["segment_key"]), segment, now)]
    # Spend the same hourly budget on potential handling evidence first.
    segments.sort(key=lambda segment: not any(m["role"] == "staff" and m["kind"] == "text" for m in segment["messages"]))
    if not segments:
        return result
    from . import voc_episode_catalog

    path = voc_assets.store_path(cfg)
    with voc_assets.connection(path, write=True) as conn:
        conn.executescript("PRAGMA journal_mode=WAL;\n" + _SCHEMA)
    errors: Counter = Counter()
    for segment in segments:
        if result["calls"] >= max_calls:
            break
        messages = segment["messages"]
        reservation = _reservation(messages)
        try:
            voc_episode._prepare(messages)
        except voc_episode.EpisodeError as exc:
            claim = _claim(path, segment, 60)
            if claim and _finish(path, segment, claim, error=exc.code, unsupported=True):
                errors[exc.code] += 1
            continue
        if max(result["reserved_tokens"], result["usage_tokens"]) + reservation > max_tokens:
            continue
        claim = _claim(path, segment, max(300, cfg.llm.timeout * 2 + 120))
        if not claim:
            continue
        if not _current(cfg, segment):
            _finish(path, segment, claim, error="source_changed")
            errors["source_changed"] += 1
            continue
        result["calls"] += 1
        result["reserved_tokens"] += reservation
        try:
            output = extract(cfg, messages)
            result["usage_tokens"] += output["usage_tokens"]
            episodes = [_ground(episode, segment) for episode in output["episodes"]]
            if not _current(cfg, segment):
                _finish(path, segment, claim, error="source_changed")
                errors["source_changed"] += 1
                continue
            grounded = [episode for episode in episodes if episode["question"]["start"] is not None
                        and sum(episode["question"]["message_id"] in ids for ids in segment["qa_anchors"].values()) == 1]
            publication_error = "claim_lost"

            def publish(conn, assigned):
                nonlocal publication_error
                if not _current(cfg, segment):
                    publication_error = "source_changed"
                    return False
                published = [{**episode, **({"topic_id": assigned[episode["id"]]} if episode["id"] in assigned else {})}
                             for episode in episodes]
                return _write_state(conn, segment, claim, episodes=published)

            try:
                assigned = voc_episode_catalog.assign(cfg, grounded, embedder=embedder, rerank=rerank,
                    legacy_candidates=_legacy_candidates(cfg, segment, grounded), publish=publish)
            except voc_episode_catalog.CatalogPublishRejected:
                _finish(path, segment, claim, error=publication_error)
                errors[publication_error] += 1
                continue
            result["segments_processed"] += 1
            result["episodes"] += len(episodes)
            result["assigned"] += len(assigned)
        except voc_episode.EpisodeError as exc:
            _finish(path, segment, claim, error=exc.code)
            errors[exc.code] += 1
        except ModelProtocolError as exc:
            code = f"gateway_{exc.status}"
            _finish(path, segment, claim, error=code)
            errors[code] += 1
            if exc.status in (400, 401, 403, 429):
                break
        except SemanticError:
            _finish(path, segment, claim, error="semantic_unavailable")
            errors["semantic_unavailable"] += 1
            break
    result["errors"] = dict(errors)
    return result
