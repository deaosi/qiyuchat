"""Project a published topic into the shared VOC workbench's read-only shape."""

from __future__ import annotations

import re


_INTENTS = {"configuration_guidance", "functional_anomaly", "unclear"}
_DIAGNOSES = {
    "商家报告": "reported", "待定位": "pending", "配置原因已确认": "config_cause_confirmed",
    "平台约束已确认": "platform_limit_confirmed", "缺陷已确认": "defect_confirmed", "不适用": "not_applicable",
}
_TRENDS = {"new", "rising", "persistent", "stable", "unknown"}
_GAPS = {
    "缺少人工客服回复": "no_staff_reply", "客服身份未核验": "unverified_staff_reply",
    "缺少诊断依据": "insufficient_diagnosis", "缺少处理结果或后续反馈": "closure_unconfirmed",
    "证据冲突或后续仍有疑问": "conflicting_evidence",
}
_FACTS = {
    "commerce_settings": ("电商后台设置", "待补查平台侧商品设置。"),
    "scenario_conditions": ("知汇鸽场景条件", "待补查场景启用及命中条件。"),
    "actual": ("实际触发结果", "待补查同一条件下的实际回复。"),
}
_TOPIC_COUNTS = ("consultation_count", "session_count", "merchant_count", "unknown_merchant_sessions",
                 "event_count", "customer_count", "mention_count")
_REF_LISTS = ("platforms", "modules", "reproduction_context", "prerequisites", "operation_path")
_REF_FIELDS = ("merchant_goal", "expected", "actual")
_TOPIC_ID = re.compile(r"t_[a-f0-9]{24}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?(?:[+-][0-9]{2}:[0-9]{2})?")
_COUNT = re.compile(r"[0-9]{1,12}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value, limit: int = 24) -> list:
    return value[:limit] if isinstance(value, list) else []


def _count(value):
    if isinstance(value, str) and _COUNT.fullmatch(value):
        value = int(value)
    return value if type(value) is int and 0 <= value < 10**12 else None


def _code(value, options, default):
    return value if isinstance(value, str) and value in options else default


def _stamp(value) -> str:
    return value if isinstance(value, str) and _STAMP.fullmatch(value) else ""


def _period(value) -> dict | None:
    value = _mapping(value)
    start, end = value.get("start"), value.get("end")
    source, processed = _count(value.get("source_pairs")), _count(value.get("processed_pairs"))
    if (not isinstance(start, str) or not isinstance(end, str) or not _DATE.fullmatch(start)
            or not _DATE.fullmatch(end) or start > end or source is None or processed is None or processed > source):
        return None
    return {"start": start, "end": end, "source_pairs": source, "processed_pairs": processed}


def workspace(value, clean) -> dict:
    value = _mapping(value)
    topic = _mapping(value.get("topic"))
    identity = value.get("topic_id")
    if not isinstance(identity, str) or not _TOPIC_ID.fullmatch(identity):
        return {}
    comparison = _mapping(value.get("comparison"))
    trend = _mapping(topic.get("trend"))
    episode = _mapping(comparison.get("episode"))
    episode_counts = {key: _count(episode.get(key)) for key in ("consultations", "sessions")}
    result = {
        "topic_id": identity,
        "topic": {
            **{key: number for key in _TOPIC_COUNTS if (number := _count(topic.get(key))) is not None},
            "question": clean(topic.get("question"))[:800],
            "theme": clean(topic.get("theme"))[:500],
            "last_seen": _stamp(topic.get("last_seen")),
            "primary_intent": _code(topic.get("primary_intent"), _INTENTS, "unclear"),
            "platforms": [text for item in _items(topic.get("platforms")) if (text := clean(item)[:50])],
            "trend": {
                "status": _code(trend.get("status"), _TRENDS, "unknown"),
                "previous_consultation_count": _count(trend.get("previous_consultation_count")),
                "delta": trend["delta"] if type(trend.get("delta")) is int and abs(trend["delta"]) < 10**12 else None,
            },
        },
        "comparison": {
            "current": _period(comparison.get("current")),
            "previous": _period(comparison.get("previous")),
            "comparable": comparison.get("comparable") is True,
            "note": clean(comparison.get("note"))[:700],
            "last_synced_at": _stamp(comparison.get("last_synced_at")),
        },
        "gaps": [],
    }
    if all(number is not None for number in episode_counts.values()):
        result["comparison"]["episode"] = episode_counts
    for item in _items(value.get("gaps")):
        item = _mapping(item)
        code, number = _code(item.get("code"), _GAPS.values(), ""), _count(item.get("count"))
        if code and number:
            result["gaps"].append({"code": code, "count": number})
    result["knowledge"] = [item for raw in _items(value.get("knowledge"), 8) if (item := _knowledge(raw, clean))]
    return result


def _ref(value, clean) -> dict | None:
    value = _mapping(value)
    source = _code(value.get("source"), ("question", "answer"), "")
    quote = clean(value.get("quote"))[:3000]
    if not source or not quote or value.get("alignment") != "exact":
        return None
    role = _code(value.get("role"), ("merchant", "staff", "unknown"), "unknown")
    return {"source": source, "quote": quote, "role": role, "alignment": "exact"}


def _refs(values, clean) -> list[dict]:
    return [ref for value in _items(values) if (ref := _ref(value, clean))]


def _knowledge(value, clean) -> dict | None:
    value = _mapping(value)
    keys = _items(value.get("keys"), 20)
    if (not keys or len(keys) != len(value["keys"]) or not all(isinstance(key, (list, tuple)) and len(key) == 2
            and type(key[0]) is int and key[0] > 0 and isinstance(key[1], str)
            and (key[1] == "" or _DIGEST.fullmatch(key[1])) for key in keys)):
        return None
    level = _code(value.get("level"), ("verified", "guidance", "reference", "history", "blocked"), "")
    kind = _code(value.get("kind"), ("configuration_guide", "troubleshooting_advice", "verified_workaround", "capability_fact"), "")
    question, answer = clean(value.get("question"))[:800], clean(value.get("answer"))[:6000]
    if not level or not question or not answer:
        return None
    feedback = []
    for raw in _items(value.get("feedback")):
        raw = _mapping(raw)
        quote, qa = clean(raw.get("quote"))[:3000], raw.get("qa")
        if not quote or type(qa) is not int or not any(qa == key[0] for key in keys):
            return None
        feedback.append({"quote": quote, "qa": qa})
    return {"keys": [list(key) for key in keys], "level": level, "kind": kind,
            "question": question, "answer": answer, "scope": clean(value.get("scope"))[:1600],
            "steps": [text for step in _items(value.get("steps")) if (text := clean(step)[:2000])],
            "applicability": _refs(value.get("applicability"), clean), "feedback": feedback}


def evidence(value, clean) -> dict:
    if not isinstance(value, dict):
        return {}
    domain, facts = _mapping(value.get("domain")), _mapping(value.get("workbench"))
    diagnosis = _mapping(domain.get("diagnosis"))
    verified = value.get("staff_source_verified") is True
    source = {}
    if type(value.get("source_anchored")) is bool:
        source["source_anchored"] = value["source_anchored"]
        if verified and (reply := clean(value.get("source_reply"))[:6000]):
            source["source_reply"] = reply
    actions = []
    for action in _items(domain.get("actions")):
        action = _mapping(action)
        instruction = _ref(action.get("instruction"), clean)
        if instruction:
            actions.append({"method_code": "", "instruction": instruction,
                            "effect_status": "verified" if action.get("effect_status") == "verified" else "suggested",
                            "verification": _ref(action.get("verification"), clean)})
    return {
        "domain": {
            "primary_intent": _code(domain.get("primary_intent"), _INTENTS, "unclear"),
            "fragment_role": _code(domain.get("fragment_role"), ("request", "follow_up", "reproduction_context"), "request"),
            **{key: _refs(domain.get(key), clean) for key in _REF_LISTS},
            **{key: _ref(domain.get(key), clean) for key in _REF_FIELDS},
            "diagnosis": {"status": _code(diagnosis.get("status"), _DIAGNOSES.values(), "pending"),
                          "cause_code": "", "cause": _ref(diagnosis.get("cause"), clean),
                          "evidence": _refs(diagnosis.get("evidence"), clean)},
            "actions": actions,
            "tags": [],
        },
        "workbench": {**{key: _refs(facts.get(key), clean) for key in _FACTS},
                      "suspected_report_ready": facts.get("suspected_report_ready") is True},
        "claims": [text for value in _items(value.get("claims")) if (text := clean(value)[:3000])],
        "staff_source_verified": verified,
        **source,
    }


def _sections(value) -> dict:
    return {item["title"]: item["lines"] for item in value.get("sections", [])}


def _legacy_ref(text: str) -> dict:
    return {"source": "question", "role": "unknown", "quote": text, "alignment": "exact"}


def _legacy_evidence(case: dict, clean) -> dict:
    sections = _sections(case)
    category = case.get("category")
    steps = sections.get("客服给出的处理步骤", [])
    return evidence({
        "domain": {
            "primary_intent": ("configuration_guidance" if category == "场景配置与使用指导"
                               else "functional_anomaly" if category in _DIAGNOSES or category == "疑似异常 · 待技术确认"
                               else "unclear"),
            "diagnosis": {"status": _DIAGNOSES.get(category, "pending"),
                          "evidence": [_legacy_ref(line) for line in sections.get("诊断依据", [])]},
            "actions": [{"instruction": {"source": "answer", "role": "staff", "alignment": "exact", "quote": line}}
                        for line in steps],
        },
        "workbench": {
            **{key: [_legacy_ref(line) for line in sections.get(title, []) if line != missing]
               for key, (title, missing) in _FACTS.items()},
            "suspected_report_ready": category == "疑似异常 · 待技术确认",
        },
        "claims": steps or sections.get("客服原话", sections.get("回复原话 · 来源未核验", [])),
        "staff_source_verified": bool(steps or sections.get("客服原话")) or category == "疑似异常 · 待技术确认",
    }, clean)


def _published_count(entry: dict):
    prefix = re.match(r"^([0-9]{1,12}) 次咨询", entry.get("summary", ""))
    return _count(prefix[1]) if prefix else None


def _legacy_workspace(entry: dict, total: int) -> dict:
    metrics = {metric["label"]: _count(metric["value"]) for metric in entry.get("metrics", [])}
    overview = _sections(entry).get("问题概况", [])
    coverage = next((match.groups() for line in [*overview, entry["detail"]]
                     if (match := re.search(r"已整理 ([0-9]{1,12}) / ([0-9]{1,12}) 条问答", line))), None)
    classification = entry.get("taxonomy", [{}])[0] if entry.get("taxonomy") else {}
    theme = " / ".join(filter(None, (classification.get("primary"), classification.get("secondary"))))
    platforms = next((line.removeprefix("涉及平台：").split("、") for line in overview if line.startswith("涉及平台：")), [])
    gaps = []
    for line in _sections(entry).get("待补证据", []):
        label, separator, amount = line.rpartition(" · ")
        number = _count(amount.removesuffix(" 次咨询")) if separator else None
        if label in _GAPS and number:
            gaps.append({"code": _GAPS[label], "count": number})
    published = _published_count(entry)
    current = {"start": entry["start"], "end": entry["end"]}
    if coverage and (processed := int(coverage[0])) <= (source := int(coverage[1])):
        current.update(source_pairs=source, processed_pairs=processed)
    return {
        "topic": {
            "theme": theme, "question": entry["title"], "platforms": platforms,
            "consultation_count": published if published is not None else metrics.get("咨询次数"),
            "session_count": metrics.get("独立会话"), "merchant_count": metrics.get("可识别商家"),
            "event_count": total,
            "last_seen": next((_stamp(line.removeprefix("最近发生：")) for line in overview if line.startswith("最近发生：")), ""),
        },
        "comparison": {"current": current,
                       "previous": None, "comparable": False, "note": "", "last_synced_at": ""},
        "gaps": gaps,
    }


def _event(case: dict, detail: dict, index: int) -> dict:
    # Use only page-local identifiers. Locate a quote only when its public text
    # appears unambiguously in one visible message from the expected side.
    messages = case["messages"]
    existing = {line for lines in _sections(case).values() for line in lines}
    refs = 0

    def locate(ref: dict) -> dict | None:
        nonlocal refs
        refs += 1
        quote = ref["quote"]
        item = {**ref, "start": refs, "end": refs + len(quote)}
        role = "staff" if ref["source"] == "answer" else "user"
        if ref["role"] == "unknown":
            return item if quote in existing else None
        matches = [message for message in messages if message["role"] == role and quote in message["text"]]
        if len(matches) == 1:
            start = matches[0]["text"].index(quote)
            item["source_refs"] = [{"message_id": matches[0]["id"], "start": start, "end": start + len(quote)}]
        return item if matches or quote in existing else None

    def add_locations(value):
        if isinstance(value, dict):
            if value.get("alignment") == "exact":
                return locate(value)
            return {key: add_locations(child) for key, child in value.items()}
        if isinstance(value, list):
            return [child for item in value if (child := add_locations(item)) is not None]
        return value

    event = add_locations(detail)
    source_anchored = event.pop("source_anchored", None)
    source_reply = event.pop("source_reply", "")
    event["domain"]["actions"] = [action for action in event["domain"]["actions"] if action["instruction"]]
    diagnosis = event["domain"]["diagnosis"]
    if not diagnosis["cause"] and not diagnosis["evidence"] and _DIAGNOSES.get(case["category"]) != diagnosis["status"]:
        diagnosis["status"] = "pending" if diagnosis["status"] not in ("reported", "not_applicable") else diagnosis["status"]
    key = f"guest-case-{index}"
    question = case["title"] or next((item["text"] for item in messages if item.get("anchor")), "")
    anchor = next((item["id"] for item in messages if item.get("anchor")), "")
    if anchor:
        event["workbench"]["question_message_id"] = anchor
    staff_messages = [message["text"] for message in messages if message["role"] == "staff"]
    quotes = [quote for quote in event.pop("claims") if (source_anchored is None and quote in existing)
              or any(quote in message for message in staff_messages)]
    reply = "\n".join(quotes)
    if source_reply and " ".join(source_reply.split()) in " ".join(" ".join(staff_messages).split()):
        reply = source_reply
    if (source_anchored and case.get("scope") in ("context", "limited") and anchor
            and any(message["role"] == "user" and message.get("anchor") and question in message["text"] for message in messages)):
        event["source_kind"] = "source-anchored"
    return {**event, "id": key, "event_id": key, "qa_id": index + 1,
            "question": question, "question_quote": question, "answer": reply,
            "asked_at": case["time"] or next((item["time"] for item in messages if item.get("anchor")), ""),
            "claims": [{"role": "staff", "quote": quote, "start": 0, "end": len(quote), "source_refs": []}
                       for quote in quotes]}


def _public_knowledge(items, events, cases, case_keys) -> list[dict]:
    authorized = {tuple(key): index for index, key in enumerate(case_keys)}
    result = []
    for item in items:
        if any(tuple(key) not in authorized for key in item["keys"]):
            continue
        indices = [authorized[tuple(key)] for key in item["keys"]]

        def matches(quote, role, selected=indices):
            normalized = " ".join(quote.split())
            return [(index, message) for index in selected for message in cases[index]["messages"]
                    if message["role"] == role and normalized in " ".join(message["text"].split())]

        if any(not matches(quote, "staff") for quote in [*item["answer"].splitlines(), *item["steps"]] if quote.strip()):
            continue
        conditions = item["applicability"]
        if any(ref["role"] == "unknown" or not matches(ref["quote"], "staff" if ref["source"] == "answer" else "user")
               for ref in conditions):
            continue
        feedback = []
        for ref in item["feedback"]:
            selected = [index for index in indices if case_keys[index][0] == ref["qa"]]
            located = matches(ref["quote"], "user", selected)
            if len(located) != 1:
                break
            index, message = located[0]
            feedback.append({"quote": ref["quote"], "qa_id": index + 1, "role": "merchant",
                             "message_id": message["id"], "ts": message["time"]})
        if len(feedback) != len(item["feedback"]) or (item["level"] == "verified" and not feedback):
            continue
        scope = item["scope"]
        if scope != "仅限来源描述的软件操作场景；平台或模块未明确时不扩大适用范围。" and scope != "；".join(
                dict.fromkeys(ref["quote"] for ref in conditions)):
            if item["level"] in ("verified", "guidance"):
                continue
            scope = ""
        direct = item["level"] in ("verified", "guidance")
        result.append({
            "id": f"guest-knowledge-{len(result)}", "topic_id": "guest-topic", "status": "ready",
            "reuse_level": "direct" if direct else item["level"],
            **({"kind": item["kind"]} if item["kind"] else {}),
            **({"knowledge_reuse": {"level": item["level"], "reason_codes": [], "evidence": feedback}} if direct else {}),
            "question": item["question"], "answer": item["answer"], "scope": scope,
            "steps": item["steps"], "cautions": [], "reply_patterns": [], "evidence_count": len(indices),
            "version": 1, "updated_at": "", "evidence": [events[index] for index in indices],
            "applicability": [{**ref, "start": 0, "end": len(ref["quote"])} for ref in conditions],
        })
    return result


def resolve(entry: dict, cases: list[dict], total: int, value, case_details: list, case_keys: list, clean, *,
            indices: list[int] | None = None, topic_id: str = "guest-topic") -> dict:
    legacy = _legacy_workspace(entry, total)
    stored = workspace(value, clean)
    topic = {**legacy["topic"], **stored.get("topic", {})}
    published = _published_count(entry)
    if published is not None and topic.get("consultation_count") != published:
        topic["consultation_count"] = published
        topic.pop("trend", None)
    topic = {key: value for key, value in topic.items() if value is not None}
    topic.update(id=topic_id, question=topic["question"] or entry["title"], samples=[])
    comparison = stored.get("comparison") or legacy["comparison"]
    current = comparison.get("current")
    if not current or (current["start"], current["end"]) != (entry["start"], entry["end"]):
        comparison = legacy["comparison"]
    selected = range(len(cases)) if indices is None else indices
    events = [_event(cases[index], evidence(case_details[index], clean) or _legacy_evidence(cases[index], clean), index)
              for index in selected]
    cases = [cases[index] for index in selected]
    case_keys = [case_keys[index] for index in selected]
    knowledge = _public_knowledge(stored.get("knowledge", []), events, cases, case_keys)
    return {"topic": topic, "events": events, "workbench": {"comparison": comparison, "knowledge": knowledge,
                                                              "gaps": stored.get("gaps", legacy["gaps"])}}


def resolve_variant(entry: dict, value: dict, ordinal: int, indices: list[int], cases: list[dict],
                    case_details: list, case_keys: list, clean) -> dict:
    topic = value["topic"]
    scoped = {**{field: entry[field] for field in ("kind", "start", "end")},
              "title": topic["question"], "summary": f'{topic["consultation_count"]} 次咨询', "detail": ""}
    return {
        "topic_detail": resolve(scoped, cases, len(indices), value, case_details, case_keys, clean,
                                indices=indices, topic_id=f"guest-topic-{ordinal}"),
        "case_indices": indices,
    }
