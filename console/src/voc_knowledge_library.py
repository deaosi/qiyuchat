"""Project reusable answers and proven SOPs without changing their evidence gates."""

from __future__ import annotations


def _bound_refs(source: dict, qa_id: int, spans: list[dict], side: str) -> list[dict]:
    text = source[side]
    parts = {part["message_id"]: part for part in source.get("_" + side + "_parts", [])}
    result = []
    for span in spans:
        part = parts.get(span.get("message_id"))
        if part is None or type(part.get("ts")) is not int or part["ts"] <= 0:
            return []
        start, end = span.get("start"), span.get("end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= part["end"] - part["start"]:
            return []
        quote = text[part["start"] + start:part["start"] + end]
        if not quote.strip():
            return []
        result.append({"qa_id": qa_id, "message_id": span["message_id"], "quote": quote, "ts": part["ts"]})
    return result


def _unique_refs(refs: list[dict]) -> list[dict]:
    return list({(ref["qa_id"], ref["message_id"], ref["quote"]): ref for ref in refs}.values())


def _answer(item: dict, current: dict[int, dict]) -> dict | None:
    level = item.get("knowledge_reuse", {}).get("level")
    if level not in ("guidance", "verified") or item.get("status") != "ready" or item.get("stale"):
        return None
    evidence, scope_evidence, sources = [], [], {}
    for citation in item.get("evidence", []):
        qa_id = citation["qa_id"]
        source = current.get(qa_id)
        if (source is None or source.get("_fingerprint") != citation.get("source_fingerprint")
                or not source.get("_staff_verified")):
            return None
        claims = citation.get("claims", [])
        if not claims:
            return None
        for claim in claims:
            refs = _bound_refs(source, qa_id, claim.get("source_refs", []), "answer")
            if not refs:
                return None
            evidence.extend(refs)
        for scope in item.get("applicability", []):
            side = scope.get("source")
            if side in ("question", "answer"):
                scope_evidence.extend(_bound_refs(source, qa_id, scope.get("source_refs", []), side))
        sources[qa_id] = {key: source.get(key) for key in (
            "qa_id", "question", "answer", "staff_name", "customer_name", "asked_at",
            "question_message_ids", "answer_message_ids",
        )}
    if not sources or not evidence:
        return None
    return {
        "id": "knowledge_" + str(item["id"]), "type": "knowledge",
        "topic_id": item["topic_id"], "variant_key": item.get("variant_key", ""),
        "title": item["question"], "question": item["question"],
        "answer": item["answer"], "scope": item["scope"], "kind": item["kind"], "status": level,
        "steps": item["steps"], "cautions": item.get("cautions", []),
        "evidence": _unique_refs(evidence), "scope_evidence": _unique_refs(scope_evidence),
        "sources": list(sources.values()), "source_count": len(sources),
        "updated_at": item["updated_at"],
    }


def build(knowledge: list[dict], current: dict[int, dict], sops: list[dict], format_time) -> dict:
    items = [answer for item in knowledge if (answer := _answer(item, current)) is not None]
    for sop in sops:
        refs = _unique_refs([
            ref for branch in sop["branches"]
            for group in (
                branch["condition_evidence"], *(step["evidence"] for step in branch["steps"]),
                branch["retest_evidence"], branch["result_evidence"],
            )
            for ref in group
        ])
        items.append({
            **sop, "type": "sop", "source_count": sop["checked_cases"],
            "updated_at": format_time(max((ref["ts"] for ref in refs), default=0) / 1000),
            "evidence": refs,
        })
    items.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    return {"items": items, "total": len(items)}
