"""Display current episode classifications through existing VOC topic views.

The episode model may group a question and its follow-ups. This projection
rebinds that decision to current QA anchors before it can affect statistics.
Original events remain available to the knowledge compiler and direct links.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from . import voc_assets as assets, voc_quality as quality, voc_variants as variants
from .voc_domain import _message_refs
from .voc_episode_replay import _alias


_INTENTS = {"configuration": "configuration_guidance", "anomaly": "functional_anomaly"}


def customer_key(identity: str) -> str:
    return _alias("customer", identity)


def source_filters(filters: dict) -> dict:
    return {key: value for key, value in filters.items()
            if key not in ("focus_topic", "support_intent", "diagnosis_status")}


def _in_scope(row: dict, filters: dict) -> bool:
    domain = row["data"].get("domain", {})
    return (filters.get("support_intent", "all") in ("", "all", domain.get("primary_intent"))
            and filters.get("diagnosis_status", "all") in ("", "all", domain.get("diagnosis", {}).get("status")))


def _question_span(source: dict, ref: dict) -> dict | None:
    for part in source.get("_question_parts", []):
        if part["message_id"] != ref.get("message_id"):
            continue
        start, end = ref.get("start"), ref.get("end")
        quote = ref.get("quote")
        if (not isinstance(quote, str) or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= part["end"] - part["start"]):
            return None
        start, end = part["start"] + start, part["start"] + end
        if source["question"][start:end] == quote:
            return {"quote": quote, "start": start, "end": end, "alignment": "exact"}
    return None


def _overlap(first: dict, second: dict) -> bool:
    return first["start"] < second["end"] and second["start"] < first["end"]


def _primary(rows: list[dict], span: dict, intent: str) -> tuple[bool, dict | None]:
    overlapping = [row for row in rows if _overlap(row["data"]["question_span"], span)]
    if not overlapping:
        return True, None
    if len(overlapping) != 1:
        return False, None
    row = overlapping[0]
    prior = row["data"]["question_span"]
    if (row["data"].get("domain", {}).get("primary_intent") != intent
            or not span["start"] <= prior["start"] < prior["end"] <= span["end"]):
        return False, None
    return True, row


def _followups(rows: list[dict], allowed: set[str], topic_ids: set[str], intent: str) -> list[dict]:
    result = []
    for row in rows:
        domain = row["data"].get("domain", {})
        if domain.get("primary_intent") != intent:
            continue
        if row["topic_id"] not in topic_ids and domain.get("fragment_role") != "follow_up":
            continue
        span = row["data"]["question_span"]
        anchors = {part["message_id"] for part in row["source"].get("_question_parts", []) if _overlap(part, span)}
        if anchors and anchors <= allowed:
            result.append(row)
    return result


def _additional_answers(source: dict, data: dict, refs: list[dict], eligible: dict[int, dict]) -> tuple[dict, dict]:
    """Attach exact, complete messages from current anchored staff replies."""
    verified = {}
    for candidate in eligible.values():
        if candidate.get("_staff_verified"):
            for part in candidate["_answer_parts"]:
                verified[part["message_id"]] = candidate["answer"][part["start"]:part["end"]]
    new_answer = source["answer"] if source.get("_staff_verified") else ""
    parts = list(source["_answer_parts"]) if new_answer else []
    claims = list(data["claims"]) if new_answer else []
    seen = {part["message_id"] for part in parts}
    for ref in refs:
        mid, quote = ref.get("message_id"), ref.get("quote")
        if (mid in seen or not isinstance(quote, str) or not quote
                or verified.get(mid) != quote or ref.get("start") != 0 or ref.get("end") != len(quote)):
            continue
        start = len(new_answer) + bool(new_answer)
        new_answer = (new_answer + "\n" if new_answer else "") + quote
        end = len(new_answer)
        parts.append({"message_id": mid, "start": start, "end": end})
        claims.append({"quote": quote, "start": start, "end": end, "type": "fact", "alignment": "exact"})
        seen.add(mid)
    if new_answer == source["answer"] and parts == source["_answer_parts"]:
        return source, data
    return ({**source, "answer": new_answer, "answer_message_ids": [part["message_id"] for part in parts],
             "_answer_parts": parts, "_staff_verified": bool(parts)},
            {**data, "claims": claims, "answer_kind": "contextual" if new_answer else "none"})


def _episode_row(segment: dict, item: dict, current: dict[int, dict], legacy: dict[int, list[dict]],
                 membership: Counter) -> tuple[dict, list[dict]] | None:
    intent = _INTENTS.get(item.get("kind"))
    topic_id = item.get("topic_id")
    if not intent or not topic_id:
        return None
    ref = item["question"]
    candidates = [(qa_id, span) for qa_id, source in current.items()
                  if (span := _question_span(source, ref)) is not None]
    if len(candidates) != 1:
        return None
    qa_id, span = candidates[0]
    compatible, primary = _primary(legacy.get(qa_id, []), span, intent)
    if not compatible:
        return None
    source = current[qa_id]
    if primary:
        data = {**primary["data"], "question_span": span}
    else:
        source = {**source, "answer": "", "answer_message_ids": [], "_answer_parts": [], "_staff_verified": False}
        domain = {"primary_intent": intent, "fragment_role": "request", "diagnosis": {
            "status": "reported" if item["kind"] == "anomaly" else "not_applicable"}}
        for field, name in (("platform", "platforms"), ("module", "modules"), ("goal", "merchant_goal"), ("symptom", "actual")):
            quote = item.get(field)
            if not quote or span["quote"].count(quote) != 1:
                continue
            start = span["start"] + span["quote"].index(quote)
            end = start + len(quote)
            grounded = {"source": "question", "quote": quote, "start": start, "end": end,
                        "alignment": "exact", "role": "merchant",
                        "source_refs": _message_refs(source, "question", start, end)}
            domain[name] = [grounded] if field in ("platform", "module") else grounded
        data = {"question_span": span, "answer_kind": "none", "claims": [], "scope": [], "domain": domain}
    allowed = {mid for mid in item["message_ids"] if membership[mid] == 1 and mid != ref["message_id"]}
    matched = [primary] if primary else []
    eligible_topics = {topic_id, primary["topic_id"]} if primary else {topic_id}
    matched.extend(_followups([row for rows in legacy.values() for row in rows if row is not primary],
                              allowed, eligible_topics, intent))
    eligible = {other_qa: row for other_qa, row in current.items()
                if other_qa == qa_id or set(row.get("question_message_ids", [])) <= allowed}
    source, data = _additional_answers(source, data, item.get("answers", []), eligible)
    legacy_ids = tuple(sorted(row["id"] for row in matched))
    fingerprint = assets.digest([source["_fingerprint"], segment["official_version"], item,
                                 source["answer"], source["answer_message_ids"], data, legacy_ids])
    source = {**source, "_fingerprint": fingerprint}
    row = {"id": item["id"], "episode_id": item["id"], "topic_id": topic_id, "qa_id": qa_id,
           "revision": 1, "fingerprint": fingerprint, "data": data, "source": source,
           "legacy_event_ids": legacy_ids}
    return row, matched


def project(cfg, current: dict[int, dict], legacy_rows: list[dict], filters: dict,
            active_topics: set[str]) -> list[dict]:
    """Merge verified episodes into a topic view and keep original IDs resolvable."""
    focus = filters.get("focus_topic")
    if variants.mode(cfg) != "zhihuige":
        return [row for row in legacy_rows if not focus or row["topic_id"] == focus]

    from . import voc_engine, voc_episode_live

    sessions = defaultdict(dict)
    for qa_id, source in current.items():
        if source.get("_question_parts") and source.get("_episode_customer_key"):
            sessions[source["_session_key"]][qa_id] = source
    if not sessions:
        return focus_rows(legacy_rows, legacy_rows, filters)
    segments = [segment for segment in voc_episode_live.load_current(cfg, set(sessions))
                if any(item.get("topic_id") in active_topics for item in segment["episodes"])]
    missing = {qa_id for segment in segments for qa_id in segment["qa_anchors"]} - current.keys()
    additional_legacy = []
    if missing:
        additional_sources, additional_legacy = voc_engine._episode_anchors(cfg, missing)
        for qa_id, source in additional_sources.items():
            if source["_session_key"] in sessions and source.get("_question_parts") and source.get("_episode_customer_key"):
                sessions[source["_session_key"]][qa_id] = source
    legacy = defaultdict(list)
    for row in (*legacy_rows, *additional_legacy):
        legacy[row["qa_id"]].append(row)
    projected, replaced = [], set()
    for segment in segments:
        eligible = {qa_id: source for qa_id, source in sessions[segment["session_key"]].items()
                    if qa_id in segment["qa_anchors"] and source["_episode_customer_key"] == segment["customer_key"]
                    and set(source["question_message_ids"]) == segment["qa_anchors"][qa_id]}
        legacy_segment = {qa_id: legacy[qa_id] for qa_id in eligible}
        membership = Counter(mid for episode in segment["episodes"] for mid in episode["message_ids"])
        for episode in segment["episodes"]:
            if episode.get("topic_id") not in active_topics:
                continue
            result = _episode_row(segment, episode, eligible, legacy_segment, membership)
            if result is None:
                continue
            row, matched = result
            # The episode belongs to the scope of its first question. Follow-up
            # rows can still be suppressed when that question is outside scope.
            if row["qa_id"] in current:
                projected.append(row)
            replaced.update(item["id"] for item in matched)
    projected.extend(row for row in legacy_rows if row["id"] not in replaced)
    return focus_rows(projected, legacy_rows, filters)


def focus_rows(rows: list[dict], legacy_rows: list[dict], filters: dict) -> list[dict]:
    focus = filters.get("focus_topic")
    scoped = [row for row in rows if _in_scope(row, filters) and (not focus or row["topic_id"] == focus)]
    if focus and not scoped:
        scoped = [row for row in legacy_rows if row["topic_id"] == focus and _in_scope(row, filters)]
    return sorted(scoped, key=lambda row: (-row["source"]["asked_at"], row["id"]))


def coverage(rows: list[dict]) -> dict:
    projected = [row for row in rows if row.get("episode_id")]
    return {"consultations": len({variants.consultation_key(row) for row in projected}),
            "sessions": len({quality.session_key(row["source"]) for row in projected})}


def legacy_event_ids(rows: list[dict]) -> set[str]:
    return {identity for row in rows for identity in row.get("legacy_event_ids", (row["id"],))}
