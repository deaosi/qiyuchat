"""Persist source-backed episode topics and reuse their identities across batches."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import sqlite3
from typing import Callable

from . import voc_assets as assets
from . import voc_episode_topics as matching
from . import voc_variants as variants
from .voc_semantics import Embedder


_PRIMARY = {"configuration": "configuration_guidance", "anomaly": "functional_anomaly"}
_FIELDS = ("module", "platform", "goal", "symptom")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS voc_episode_topic (
    domain TEXT NOT NULL,
    id TEXT NOT NULL,
    question TEXT NOT NULL,
    theme TEXT NOT NULL,
    boundary TEXT NOT NULL,
    primary_intent TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    legacy INTEGER NOT NULL,
    representative TEXT NOT NULL,
    PRIMARY KEY (domain,id)
);
CREATE TABLE IF NOT EXISTS voc_episode_assignment (
    domain TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    topic_id TEXT NOT NULL,
    issue TEXT NOT NULL,
    PRIMARY KEY (domain,episode_id)
);
"""


class CatalogPublishRejected(RuntimeError):
    """The prepared segment could not be published with its assignments."""


@dataclass(frozen=True)
class _Snapshot:
    topics: dict[str, dict]
    assignments: dict[str, dict]
    legacy: dict[str, dict]


def _snapshot(conn: sqlite3.Connection, domain: str) -> _Snapshot:
    topics = {row["id"]: dict(row) for row in conn.execute(
        "SELECT * FROM voc_episode_topic WHERE domain=? ORDER BY id", (domain,))}
    assignments = {row["episode_id"]: dict(row) for row in conn.execute(
        "SELECT * FROM voc_episode_assignment WHERE domain=? ORDER BY episode_id", (domain,))}
    legacy = {}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(v2_topic)")}
    if {"id", "domain", "primary_intent", "status", "revision"} <= columns:
        legacy = {row["id"]: dict(row) for row in conn.execute(
            "SELECT id,primary_intent,status,revision FROM v2_topic WHERE domain=?", (domain,))}
    return _Snapshot(topics, assignments, legacy)


def _source_issues(episodes: list[dict]) -> list[dict]:
    seen, issues = set(), []
    for episode in episodes:
        identity = episode.get("id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("VOC 咨询事件编号缺失或重复。")
        seen.add(identity)
        kind = episode.get("kind")
        if kind not in _PRIMARY:
            continue
        quote = episode.get("question", {}).get("quote")
        if not isinstance(quote, str) or not matching._key(quote):
            continue
        details = {key: episode.get(key, "") for key in _FIELDS}
        if any(not isinstance(value, str) for value in details.values()):
            raise ValueError("VOC 咨询事件业务字段无效。")
        # Keep a display label and matching text from verified source spans.
        # Generated episode titles can change when the same conversation is rerun.
        title = " ".join(quote.split())[:160]
        issues.append({"id": identity, "kind": kind, "title": title, **details, "question": {"quote": quote}})
    return sorted(issues, key=lambda issue: issue["id"])


def _fingerprint(issue: matching._Issue) -> str:
    return assets.digest([issue.kind, issue.module, issue.platform, issue.goal, issue.symptom, issue.quote])


def _active(state: _Snapshot, topic: dict) -> bool:
    if not topic["legacy"]:
        return True
    legacy = state.legacy.get(topic["id"])
    return bool(legacy and legacy["status"] != "ignored" and legacy["primary_intent"] == topic["primary_intent"])


def _legacy_choice(state: _Snapshot, issue: matching._Issue, candidates: list[dict]) -> tuple[str | None, bool]:
    primary = _PRIMARY[issue.kind]
    matches = set()
    for candidate in candidates:
        identity = candidate.get("id")
        quote = candidate.get("quote")
        if not isinstance(identity, str) or not isinstance(quote, str) or matching._key(quote) != issue.quote:
            continue
        actual = state.legacy.get(identity)
        if not actual or actual["primary_intent"] != primary or candidate.get("primary_intent") != primary:
            continue
        if actual["status"] == "ignored" or actual["revision"] != candidate.get("revision"):
            return None, True
        matches.add(identity)
    return (next(iter(matches)), False) if len(matches) == 1 else (None, len(matches) > 1)


def _topic(domain: str, identity: str, issue: matching._Issue, *, legacy: bool = False) -> dict:
    source = issue.episode
    boundary = " · ".join(dict.fromkeys(source[key] for key in ("goal", "symptom") if source[key]))
    return {"domain": domain, "id": identity, "question": source["title"],
            "theme": source["module"] if issue.module else source["title"],
            "boundary": boundary, "primary_intent": _PRIMARY[issue.kind],
            "revision": 1, "legacy": int(legacy), "representative": assets.dump(source)}


def _discoverable(issue: matching._Issue) -> bool:
    if not issue.module:
        return False
    if issue.kind == "configuration":
        return bool(issue.goal and issue.goal not in matching._GENERIC_GOALS)
    return bool(issue.symptom and (issue.distinctions or issue.codes or issue.goal))


def _matching_topic(index: int, guards: dict[str, list[int]], scores: dict[tuple[int, int], float]) -> tuple[str | None, bool]:
    qualified, related = [], False
    for identity, positions in guards.items():
        values = [scores.get(matching._pair_key(index, pos), 0.) for pos in positions if pos != index]
        if any(values):
            related = True
        if values and all(values):
            qualified.append((min(values), identity))
    qualified.sort(key=lambda row: (-row[0], row[1]))
    if not qualified:
        return None, related
    if len(qualified) > 1 and qualified[0][0] - qualified[1][0] <= matching._AMBIGUITY_MARGIN:
        return None, True
    return qualified[0][1], False


def _plan(domain: str, state: _Snapshot, incoming: list[dict], legacy_candidates: dict[str, list[dict]],
          embedder: Embedder | None, rerank: matching.Reranker | None) -> tuple[dict[str, str], list[dict]]:
    guards, fingerprints, issues = defaultdict(list), defaultdict(set), []
    retired = []

    def add_guard(topic_id: str, issue: matching._Issue, index: int) -> None:
        fingerprint = _fingerprint(issue)
        if fingerprint not in fingerprints[topic_id]:
            fingerprints[topic_id].add(fingerprint)
            guards[topic_id].append(index)

    for topic in state.topics.values():
        issue = matching._issue(json.loads(topic["representative"]))
        if _active(state, topic):
            add_guard(topic["id"], issue, len(issues))
        else:
            retired.append(len(issues))
        issues.append(issue)
    for assignment in state.assignments.values():
        topic_id = assignment["topic_id"]
        if topic_id in guards and assignment["fingerprint"] not in fingerprints[topic_id]:
            issue = matching._issue(json.loads(assignment["issue"]))
            add_guard(topic_id, issue, len(issues))
            issues.append(issue)

    first = len(issues)
    issues.extend(matching._issue(episode) for episode in incoming)
    indices = range(first, len(issues))
    assignments, created, blocked = {}, {}, set()
    for index in indices:
        issue = issues[index]
        episode_id = issue.episode["id"]
        previous = state.assignments.get(episode_id)
        if previous and previous["fingerprint"] == _fingerprint(issue):
            if previous["topic_id"] in guards:
                assignments[episode_id] = previous["topic_id"]
            else:
                blocked.add(index)
            continue
        identity, ambiguous = _legacy_choice(state, issue, legacy_candidates.get(episode_id, []))
        if ambiguous:
            blocked.add(index)
            continue
        # Preserve a previously established identity when a later episode also
        # has a legacy link to an independently generated topic for this issue.
        known = [topic_id for topic_id, positions in guards.items() if positions and all(
            (candidate := matching._candidate(issue, issues[pos], 0.)) is not None and candidate[0]
            for pos in positions)]
        if len(known) > 1:
            blocked.add(index)
            continue
        if known:
            identity = known[0]
        if identity:
            if any(matching._candidate(issue, issues[pos], 1.) is None for pos in guards[identity]):
                blocked.add(index)
                continue
            if identity not in state.topics and identity not in created:
                created[identity] = _topic(domain, identity, issue, legacy=True)
            assignments[episode_id] = identity
            add_guard(identity, issue, index)

    unresolved = [index for index in indices if index not in blocked and issues[index].episode["id"] not in assignments]
    if not unresolved:
        return assignments, list(created.values())
    unresolved_set = set(unresolved)
    pairs = ((i, j) for j in indices for i in range(j) if i in unresolved_set or j in unresolved_set)
    scores = matching._scores(issues, embedder, rerank, pairs=pairs)
    discovery_edges = {pair: score for pair, score in scores.items() if all(pos in unresolved_set for pos in pair)}
    ambiguous_discoveries = matching._ambiguous(discovery_edges)
    for index in unresolved:
        issue = issues[index]
        episode_id = issue.episode["id"]
        identity, uncertain = _matching_topic(index, guards, scores)
        held = any(scores.get(matching._pair_key(index, pos), 0.) for pos in retired)
        if identity is None and not (uncertain or held or index in ambiguous_discoveries) and _discoverable(issue):
            identity = "t_" + assets.digest(["episode-topic-v1", domain, episode_id, _fingerprint(issue)])[:24]
            if identity in state.topics or identity in created:
                continue
            created[identity] = _topic(domain, identity, issue)
        if identity is not None:
            assignments[episode_id] = identity
            add_guard(identity, issue, index)
    return assignments, list(created.values())


def assign(cfg, episodes: list[dict], *, embedder: Embedder | None = None, rerank: matching.Reranker | None = None,
           legacy_candidates: dict[str, list[dict]] | None = None,
           publish: Callable[[sqlite3.Connection, dict[str, str]], bool] | None = None) -> dict[str, str]:
    """Find or create persistent topic IDs from verified episode descriptors.

    Exact legacy anchors are optional. Ambiguous matches are left unassigned.
    Model work runs before the write transaction and decisions are retried if the
    catalog or the legacy topics changed while the comparison was running.
    A publish callback may commit the owning segment in the same transaction.
    """
    incoming = _source_issues(episodes)
    if not incoming and publish is None:
        return {}
    domain, store = variants.mode(cfg), assets.store_path(cfg)
    with assets.connection(store, write=True) as conn:
        conn.executescript(_SCHEMA)
    for _ in range(3):
        with assets.connection(store) as conn:
            conn.execute("BEGIN")
            state = _snapshot(conn, domain)
        assignments, created = _plan(domain, state, incoming, legacy_candidates or {}, embedder, rerank)
        with assets.connection(store, write=True) as conn, assets.transaction(conn):
            if _snapshot(conn, domain) != state:
                continue
            conn.executemany("""INSERT INTO voc_episode_topic
                (domain,id,question,theme,boundary,primary_intent,revision,legacy,representative)
                VALUES (:domain,:id,:question,:theme,:boundary,:primary_intent,:revision,:legacy,:representative)""", created)
            for episode in incoming:
                episode_id = episode["id"]
                identity = assignments.get(episode_id)
                if identity is None:
                    # Preserve ignored-topic associations so reruns of the same
                    # source cannot resurrect a manually excluded legacy topic.
                    continue
                conn.execute("""INSERT INTO voc_episode_assignment VALUES (?,?,?,?,?)
                    ON CONFLICT(domain,episode_id) DO UPDATE SET
                    fingerprint=excluded.fingerprint,topic_id=excluded.topic_id,issue=excluded.issue
                    WHERE fingerprint<>excluded.fingerprint OR topic_id<>excluded.topic_id""",
                             (domain, episode_id, _fingerprint(matching._issue(episode)), identity, assets.dump(episode)))
            if publish is not None and not publish(conn, assignments):
                raise CatalogPublishRejected("会话来源已变化，请重新整理。")
            return assignments
    raise RuntimeError("VOC 主题目录同时更新，请重试。")


def topics(cfg) -> list[dict]:
    """Read published catalog definitions; callers join only current episodes."""
    store = assets.store_path(cfg)
    if not store.is_file():
        return []
    with assets.connection(store) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='voc_episode_topic'").fetchone():
            return []
        return [dict(row) for row in conn.execute("""SELECT id,question,theme,boundary,revision,primary_intent
                FROM voc_episode_topic WHERE domain=? AND legacy=0 ORDER BY id""", (variants.mode(cfg),))]
