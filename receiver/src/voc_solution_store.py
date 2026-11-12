"""Run and publish model-written handling summaries from verified seven-fish chat."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from . import voc, voc_assets as assets, voc_episode_live, voc_solution
from .model_protocol import ModelProtocolError
from .voc_case_paths import _safe_label


_SCHEMA = """
CREATE TABLE IF NOT EXISTS voc_solution_case (
 qa_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 state TEXT NOT NULL, solution TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', draft TEXT NOT NULL DEFAULT '',
 attempts INTEGER NOT NULL, retry_at REAL NOT NULL, owner TEXT NOT NULL, updated_at REAL NOT NULL
);
"""
_VERSION = "official-solution-v1"
_CALLS_PER_CASE = 2
_MAX_MESSAGES = 100
_MAX_INPUT_BYTES = 18_000
_NICKNAME = re.compile(r"(?:顾客|客户|买家|访客|用户)(?:的)?昵称[：:\s]*[^，。！？；,!?;\n]{1,60}")


def _eligible(sources: dict[int, dict]) -> dict[int, dict]:
    return {qa_id: row for qa_id, row in sources.items() if row.get("_staff_verified") is True
            and row.get("question_message_ids") and row.get("answer_message_ids")
            and row.get("_origin_fingerprint") and row.get("_session_key")}


def _candidates(cfg, sources, *, session_ids=None) -> list[dict]:
    eligible = _eligible(sources)
    if not eligible:
        return []
    if session_ids is None:
        # Use the same current, independently verified official snapshots as the episode analyzer.
        path = Path(cfg.runtime.db_path)
        with assets.connection(path) as db:
            session_ids = {row[0] for row in db.execute(
                "SELECT DISTINCT session_id FROM qa_pair WHERE qa_id IN (SELECT value FROM json_each(?))",
                (assets.dump(list(eligible)),))}
    profile = [cfg.llm.base_url, cfg.llm.model_main, cfg.llm.json_mode, cfg.llm.thinking]
    records = []
    for segment in voc_episode_live._gather(cfg, session_ids=session_ids):
        turns = [m for m in segment["messages"] if m["kind"] == "text" and m["role"] in ("user", "staff")]
        if len(turns) > _MAX_MESSAGES:
            continue
        originals = {segment["original"][m["id"]]["source_message_id"]: (index, m)
                     for index, m in enumerate(turns)}
        for qa_id, ids in segment["qa_anchors"].items():
            source = eligible.get(qa_id)
            if source is None or set(source["question_message_ids"]) != ids:
                continue
            reply_ids = source["answer_message_ids"]
            if not all(mid in originals and originals[mid][1]["role"] == "staff" for mid in reply_ids) or not ids <= originals.keys():
                continue
            if max(originals[mid][0] for mid in ids) >= min(originals[mid][0] for mid in reply_ids):
                continue
            if max(originals[mid][1]["ts"] for mid in ids) >= min(originals[mid][1]["ts"] for mid in reply_ids):
                continue
            messages = [{**turn, "text": _safe_label(_NICKNAME.sub("[昵称]", turn["text"]), source)} for turn in turns]
            size = len(assets.dump(messages).encode("utf-8"))
            if size > _MAX_INPUT_BYTES:
                continue
            anchor_ids = {originals[mid][1]["id"] for mid in ids}
            fingerprint_parts = [segment["fingerprint"], source["_fingerprint"], profile, messages]
            records.append({"qa_id": qa_id, "session_id": segment["session_id"], "segment_key": segment["segment_key"],
                            "messages": messages, "anchors": anchor_ids, "original": segment["original"],
                            "fingerprint": assets.digest([_VERSION, voc_solution.VERSION, *fingerprint_parts]),
                            "previous_fingerprint": assets.digest([_VERSION, "voc-solution-v3", *fingerprint_parts]),
                            "size": size, "asked_at": source["asked_at"]})
    return records


def _rows(path: Path, ids=None) -> dict[int, dict]:
    if not path.is_file() or ids == set():
        return {}
    with assets.connection(path) as db:
        if not voc_episode_live._table(db, "voc_solution_case"):
            return {}
        query = "SELECT * FROM voc_solution_case"
        if ids is None:
            return {row["qa_id"]: dict(row) for row in db.execute(query)}
        return {row["qa_id"]: dict(row) for row in db.execute(
            query + " WHERE qa_id IN (SELECT value FROM json_each(?))", (assets.dump(sorted(ids)),))}


def _due(row, candidate, now) -> bool:
    if row is not None and row["state"] == "complete" and row["solution"] and row["fingerprint"] == candidate["previous_fingerprint"]:
        return False
    return (row is None or row["fingerprint"] != candidate["fingerprint"]
            or row["state"] in ("retry", "running") and row["retry_at"] <= now)


def _claim(path, candidate, timeout, observed_at):
    now, owner = time.time(), uuid4().hex
    with assets.connection(path, write=True) as db, assets.transaction(db):
        row = db.execute("SELECT * FROM voc_solution_case WHERE qa_id=?", (candidate["qa_id"],)).fetchone()
        if not _due(row, candidate, now) or row is not None and row["updated_at"] > observed_at:
            return None
        same_source = row is not None and row["fingerprint"] == candidate["fingerprint"]
        attempts = row["attempts"] + 1 if same_source else 1
        draft = row["draft"] if same_source else ""
        db.execute("""INSERT INTO voc_solution_case(qa_id,session_id,fingerprint,state,attempts,retry_at,owner,updated_at,draft)
                    VALUES(?,?,?,'running',?,?,?,?,?) ON CONFLICT(qa_id) DO UPDATE SET
                    session_id=excluded.session_id,fingerprint=excluded.fingerprint,state='running',solution='',error='',
                    attempts=excluded.attempts,retry_at=excluded.retry_at,owner=excluded.owner,updated_at=excluded.updated_at,draft=excluded.draft""",
                   (candidate["qa_id"], candidate["session_id"], candidate["fingerprint"], attempts,
                    now + max(300, timeout * 2 + 90), owner, now, draft))
    return owner, attempts, json.loads(draft) if draft else None


def _checkpoint(path, candidate, owner, draft) -> None:
    with assets.connection(path, write=True) as db, assets.transaction(db):
        saved = db.execute("""UPDATE voc_solution_case SET draft=? WHERE qa_id=? AND fingerprint=? AND owner=? AND state='running'""",
                           (assets.dump(draft), candidate["qa_id"], candidate["fingerprint"], owner)).rowcount
        if saved != 1:
            raise RuntimeError("VOC solution lease changed")


def _finish(path, candidate, claim, solution=None, error="") -> bool:
    owner, attempts, _ = claim
    now = time.time()
    retry_at = now + min(21600, 600 * 2 ** min(attempts - 1, 6)) if error else 0
    with assets.connection(path, write=True) as db, assets.transaction(db):
        return db.execute("""UPDATE voc_solution_case SET state=?,solution=?,error=?,retry_at=?,owner='',updated_at=?,
                    draft=CASE WHEN ? THEN draft ELSE '' END
                    WHERE qa_id=? AND fingerprint=? AND owner=? AND state='running'""",
                   ("retry" if error else "complete", assets.dump(solution) if solution else "", error, retry_at, now,
                    error.startswith("gateway_") or error == "model_validation",
                    candidate["qa_id"], candidate["fingerprint"], owner)).rowcount == 1


def pending(cfg, filters=None) -> bool:
    if cfg.runtime.voc_domain != "zhihuige" or cfg.runtime.mock or not voc.configured(cfg):
        return False
    from .voc_engine import _sources

    candidates = _candidates(cfg, _sources(cfg, filters or {}))
    stored = _rows(assets.store_path(cfg))
    now = time.time()
    return any(_due(stored.get(candidate["qa_id"]), candidate, now) for candidate in candidates)


def run(cfg, filters, *, max_calls, max_tokens, qa_ids=None) -> dict:
    """Budget two model requests per case and publish only while the source still matches."""
    report = {"calls": 0, "reserved_tokens": 0, "usage_tokens": 0, "generated": 0, "errors": {}}
    if (max_calls < 1 or max_tokens < 8192 or cfg.runtime.voc_domain != "zhihuige"
            or cfg.runtime.mock or not voc.configured(cfg)):
        return report
    from . import voc_case_paths, voc_engine

    observed_at = time.time()
    sources = voc_engine._sources(cfg, filters, qa_ids=qa_ids)
    candidates = _candidates(cfg, sources)
    if not candidates:
        return report
    path = assets.store_path(cfg)
    stored = _rows(path)
    candidates = [item for item in candidates if _due(stored.get(item["qa_id"]), item, observed_at)]
    if not candidates:
        return report
    preferred = {item["qa_id"] for item in voc_case_paths.build(cfg, sources)}
    def previous_draft(item):
        prior = stored.get(item["qa_id"], {})
        return bool(prior.get("fingerprint") == item["fingerprint"] and prior.get("draft"))

    candidates.sort(key=lambda item: (not previous_draft(item), item["qa_id"] not in preferred,
                                      -len(item["messages"]), -item["asked_at"], item["qa_id"]))
    with assets.connection(path, write=True) as db:
        db.executescript(_SCHEMA)
        with assets.transaction(db):
            if "draft" not in {row[1] for row in db.execute("PRAGMA table_info(voc_solution_case)")}:
                db.execute("ALTER TABLE voc_solution_case ADD COLUMN draft TEXT NOT NULL DEFAULT ''")

    used_segments = set()
    errors: Counter = Counter()
    for candidate in candidates:
        resume = previous_draft(candidate)
        calls = 1 if resume else _CALLS_PER_CASE
        if report["calls"] + calls > max_calls:
            continue
        reservation = (2 * candidate["size"] + 7_000) if resume else (3 * candidate["size"] + 14_000)
        if candidate["segment_key"] in used_segments or max(report["reserved_tokens"], report["usage_tokens"]) + reservation > max_tokens:
            continue
        claim = _claim(path, candidate, cfg.llm.timeout, observed_at)
        if claim is None:
            continue
        used_segments.add(candidate["segment_key"])
        report["calls"] += calls
        report["reserved_tokens"] += reservation
        try:
            result = voc_solution.run(cfg, candidate["messages"], candidate["anchors"], draft=claim[2],
                                      checkpoint=lambda draft: _checkpoint(path, candidate, claim[0], draft))
        except ModelProtocolError as exc:
            report["usage_tokens"] += getattr(exc, "usage_tokens", 0)
            code = f"gateway_{exc.status}"
            _finish(path, candidate, claim, error=code)
            errors[code] += 1
            # The next hourly job can resume this work without delaying its other model tasks.
            break
        report["usage_tokens"] += result["usage_tokens"]
        current = voc_engine._sources(cfg, filters, only_qa_id=candidate["qa_id"])
        proof = _candidates(cfg, current, session_ids={candidate["session_id"]})
        if not any(item["fingerprint"] == candidate["fingerprint"] for item in proof):
            _finish(path, candidate, claim, error="source_changed")
            errors["source_changed"] += 1
            continue
        solution = result["solution"]
        if solution is None and candidate["qa_id"] in preferred and result.get("partial"):
            solution = {**result["partial"], "partial": True}
        error = "model_validation" if result.get("retry") else ""
        if error:
            errors[error] += 1
        if _finish(path, candidate, claim, solution=solution, error=error) and solution and not error:
            report["generated"] += 1
    report["errors"] = dict(errors)
    return report


def _literal_sections(case, candidate):
    by_raw = {candidate["original"][turn["id"]]["source_message_id"]: turn for turn in candidate["messages"]}
    question = by_raw.get(case["problem"]["message_id"])
    if question is None or question["id"] not in candidate["anchors"]:
        return []

    def source(turn):
        return {"message_id": turn["id"], "ts": turn["ts"], "quote": turn["text"]}

    problem = {"kind": "problem", "text": question["text"], "evidence": [source(question)]}
    actions = []
    for step in case["steps"]:
        turn = by_raw.get(step["evidence"]["message_id"])
        if turn and turn["role"] == "staff" and step["action"] in turn["text"]:
            actions.append({"kind": "action", "text": f"客服原话「{step['action']}」", "evidence": [source(turn)]})
    return [problem, *actions] if actions else []


def enrich(cfg, sources, cases, *, proof_by_qa=None):
    """Serve cached results using the same proof and source IDs as the raw chat drawer."""
    eligible = _eligible(sources)
    if not eligible:
        return cases
    stored = {qa_id: row for qa_id, row in _rows(assets.store_path(cfg), set(eligible)).items()
              if row["state"] == "complete" and row["solution"]}
    if not stored:
        return cases
    proof = _candidates(cfg, {qa_id: eligible[qa_id] for qa_id in stored},
                        session_ids={row["session_id"] for row in stored.values()})
    by_qa = {case["qa_id"]: case for case in cases}
    preferred = set(by_qa)
    published = []
    replaced = set()
    seen_solutions = set()
    proof.sort(key=lambda item: (item["qa_id"] not in preferred, item["asked_at"], item["qa_id"]))
    for candidate in proof:
        qa_id = candidate["qa_id"]
        row = stored[qa_id]
        if row["fingerprint"] not in (candidate["fingerprint"], candidate["previous_fingerprint"]):
            continue
        solution = json.loads(row["solution"])
        partial = solution.get("partial") is True
        if partial:
            base = by_qa.get(qa_id)
            literal = _literal_sections(base, candidate) if base else []
            if not literal:
                continue
            kinds = {section["kind"] for section in solution["sections"]}
            sections = [*solution["sections"], *(section for section in literal if section["kind"] not in kinds)]
            order = {"problem": 0, "diagnosis": 1, "action": 2, "decision": 3, "verification": 4}
            sections.sort(key=lambda section: order[section["kind"]])
            solution = {"title": literal[0]["text"][:120], "sections": sections}
        anchors = candidate["anchors"]
        problems = [ref for section in solution["sections"] if section["kind"] == "problem"
                    for ref in section["evidence"] if ref["message_id"] in anchors]
        if not problems:
            continue
        action_refs = {ref["message_id"] for section in solution["sections"] if section["kind"] == "action"
                       for ref in section["evidence"] if candidate["original"][ref["message_id"]]["role"] == "staff"}
        identity = (tuple(ref["message_id"] for ref in problems), tuple(sorted(action_refs)))
        if not action_refs or identity in seen_solutions:
            continue
        seen_solutions.add(identity)

        def original_ref(ref):
            return {**ref, "qa_id": qa_id, "message_id": candidate["original"][ref["message_id"]]["source_message_id"]}

        sections = [{**section, "evidence": [original_ref(ref) for ref in section["evidence"]]}
                    for section in solution["sections"]]
        case = by_qa.get(qa_id)
        if case and any(original_ref(ref)["message_id"] == case["problem"]["message_id"] for ref in problems):
            replaced.add(case["id"])
        else:
            case = {"id": "case_solution_" + assets.digest([qa_id, candidate["segment_key"]])[:24],
                    "qa_id": qa_id, "problem": original_ref(problems[0]), "steps": [],
                    "platform": "", "outcome": "not_recorded"}
        published.append({**case, "solution": {"title": solution["title"], "sections": sections}})
        if proof_by_qa is not None and not partial:
            proof_by_qa[qa_id] = candidate
    return published + [case for case in cases if case["id"] not in replaced]
