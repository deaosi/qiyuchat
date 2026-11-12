"""Build a Feishu snapshot from the current, read-only VOC projections."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from xml.etree.ElementTree import Element, SubElement, tostring

from . import voc, voc_assets, voc_engine, voc_topic_insights, voc_variants
from .clean.rules import mask_pii
from .config import Settings
from .extract.text import clean_visible_text
from .web import guest_evidence


_LIMIT = 20
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TOPIC_ID = re.compile(r"t_[a-f0-9]{24}")
_SOP_ID = re.compile(r"sop_[a-f0-9]{24}")
_URL = re.compile(r"""(?:https?://|www\.)[^\s，。；！？、<>'"]+""", re.I)
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_SOURCE_ID = re.compile(r"\b(?:qiyu-(?:external(?:-sha256)?|user|session):[^\s，。；！？、]+|(?:sop_|t_|episode_)[a-f0-9]{24})\b", re.I)
_LABELED_ID = re.compile(r"((?:会话|客户|客服|店铺|商家|商品|账号|订单|消息)\s*(?:ID|编号|号)\s*[:：=]?\s*)[a-z0-9_-]{4,}", re.I)
_PHASES = {"diagnosis": "排查", "action": "处理", "retest": "复测", "escalation": "升级处理"}
_TOPIC_LABELS = {"functional_anomaly": "功能异常", "configuration_guidance": "配置与使用", "unclear": "其他咨询"}
_SOP_LABELS = {"anomaly": "异常与排障", "configuration": "配置与使用"}
_PLACEHOLDERS = {"匿名客户", "未分组", "未记录客服"}


def _pinned_scope(filters: dict) -> dict:
    start, end = filters.get("start", ""), filters.get("end", "")
    if (start or end) and not all(isinstance(day, str) and _DAY.fullmatch(day) for day in (start, end)):
        raise voc.VocError("请选择完整的起止日期，格式为 YYYY-MM-DD。")
    try:
        current, _ = voc_topic_insights.periods(filters)
    except ValueError as exc:
        raise voc.VocError(str(exc)) from None
    if not current.start or not current.end:
        raise voc.VocError("请选择近期范围或填写完整的起止日期。")
    return {**filters, "days": 0, "start": current.start.isoformat(), "end": current.end.isoformat()}


def _private_names(cfg: Settings, filters: dict) -> re.Pattern[str] | None:
    values = [filters.get(key) for key in ("group", "staff", "customer")]
    path = Path(cfg.runtime.db_path)
    if path.is_file():
        with voc_assets.connection(path) as conn:
            tables = voc_engine._tables(conn)
            if "qa_pair" in tables:
                for row in conn.execute("SELECT DISTINCT customer_name, group_name, staff_name FROM qa_pair"):
                    values.extend(row)
            if "raw_session" in tables:
                for row in conn.execute("SELECT DISTINCT user_name, from_group, staff_name FROM raw_session"):
                    values.extend(row)
    names = {value.strip() for value in values if isinstance(value, str) and value.strip()} - _PLACEHOLDERS
    if not names:
        return None
    patterns = []
    for name in sorted(names, key=lambda value: (-len(value), value)):
        escaped = re.escape(name)
        if name.isascii() and name.isalnum():
            escaped = r"(?<![a-zA-Z0-9])" + escaped + r"(?![a-zA-Z0-9])"
        patterns.append(escaped)
    return re.compile("|".join(patterns), re.I)


def _public_text(value: object, names: re.Pattern[str] | None) -> str:
    if not isinstance(value, str):
        return ""
    text = _XML_INVALID.sub("", clean_visible_text(value))
    text = _URL.sub("[链接]", text)
    if names is not None:
        text = names.sub("[名称]", text)
    text = _LABELED_ID.sub(r"\1[已脱敏]", _SOURCE_ID.sub("[已脱敏]", text))
    return " ".join(mask_pii(text).split())



_DIAGNOSES = {
    "reported": "商家报告", "pending": "待定位", "config_cause_confirmed": "配置原因已确认",
    "platform_limit_confirmed": "平台约束已确认", "defect_confirmed": "缺陷已确认", "not_applicable": "不适用",
}
_TRENDS = {"new": "新出现", "rising": "明显上升", "persistent": "持续高频", "stable": "平稳"}
_GAPS = {
    "no_staff_reply": "缺少人工客服回复", "unverified_staff_reply": "客服身份未核验",
    "insufficient_diagnosis": "缺少诊断依据", "applicability": "适用条件", "diagnosis": "排查步骤",
    "action": "处理步骤", "retest": "复测步骤", "customer_result": "客户复测结果", "result": "复测结果",
    "unsafe_step": "操作安全依据", "later_counterevidence": "后续异议或失败的处理结果",
    "unbound_condition": "步骤对应的前置条件",
}
_OUTCOMES = {"verified": "效果已验证", "failed": "尝试未奏效", "unconfirmed": "结果待确认"}
_CASE_LIMIT = 20
_THEME_SEPARATOR = re.compile(r"\s*[/／>＞→]\s*")


def _theme_path(value: object, fallback: str, names: re.Pattern[str] | None) -> tuple[str, str]:
    theme = _public_text(value, names)
    if not theme:
        return fallback, ""
    parts = _THEME_SEPARATOR.split(theme, maxsplit=1)
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return fallback or theme, theme if fallback and fallback != theme else ""


def _topic_taxonomy(topic: dict, rows: list[dict], names: re.Pattern[str] | None) -> list[dict]:
    fallback = _TOPIC_LABELS.get(topic.get("primary_intent"), "")
    canonical = _theme_path(topic.get("theme"), fallback, names)
    observations = {canonical: set()} if canonical[0] else {}
    for row in rows:
        observed = row["data"].get("theme")
        path = _theme_path(observed, fallback, names) if isinstance(observed, str) and observed.strip() else canonical
        if path[0]:
            observations.setdefault(path, set()).add(voc_variants.consultation_key(row))
    return [{"primary": primary, "secondary": secondary,
             **({"count": len(keys)} if keys else {})}
            for (primary, secondary), keys in observations.items()]


def _section(title: str, values: list[object], names: re.Pattern[str] | None) -> dict:
    lines = list(dict.fromkeys(text for value in values if (text := _public_text(value, names))))
    return {"title": title, "lines": lines}


def _quotes(refs: list) -> list[object]:
    return [ref.get("quote") for ref in refs if isinstance(ref, dict)]


def _case_projection(row: dict, names: re.Pattern[str] | None) -> dict:
    source, data = row["source"], row["data"]
    domain = data.get("domain", {})
    diagnosis = domain.get("diagnosis", {})
    facts = voc_topic_insights.case_details(row)
    confirmed = diagnosis.get("status") in ("config_cause_confirmed", "platform_limit_confirmed", "defect_confirmed")
    if confirmed:
        label = _DIAGNOSES[diagnosis["status"]]
    elif facts.get("suspected_report_ready") and source.get("_staff_verified"):
        label = "疑似异常 · 待技术确认"
    elif domain.get("primary_intent") == "configuration_guidance":
        label = "场景配置与使用指导"
    else:
        label = _DIAGNOSES.get(diagnosis.get("status"), "待补查或定位")
    sections = []
    for title, field, missing in (
        ("电商后台设置", "commerce_settings", "待补查平台侧商品设置。"),
        ("知汇鸽场景条件", "scenario_conditions", "待补查场景启用及命中条件。"),
        ("实际触发结果", "actual", "待补查同一条件下的实际回复。"),
    ):
        sections.append(_section(title, _quotes(facts[field]) or [missing], names))
    causes = [voc_topic_insights._exact_ref(row, ref)
              for ref in [diagnosis.get("cause"), *diagnosis.get("evidence", [])]]
    if any(causes):
        sections.append(_section("诊断依据", _quotes(causes), names))
    verified = bool(source.get("_staff_verified"))
    steps = []
    if verified:
        for action in domain.get("actions", []):
            ref = voc_topic_insights._exact_ref(row, action.get("instruction"))
            if ref and ref["role"] == "staff":
                steps.append(ref["quote"])
    if steps:
        sections.append(_section("客服给出的处理步骤", steps, names))
    else:
        reply = source.get("answer", "")
        claims = [claim.get("quote") for claim in data.get("claims", [])
                  if type(claim.get("start")) is int and type(claim.get("end")) is int
                  and 0 <= claim["start"] < claim["end"] <= len(reply)
                  and reply[claim["start"]:claim["end"]] == claim.get("quote")]
        if claims:
            sections.append(_section("客服原话" if verified else "回复原话 · 来源未核验", claims, names))
    return {"qa_id": source["qa_id"], "anchor_id": facts.get("question_message_id", ""),
            "title": _public_text(data["question_span"].get("quote"), names) or "会话依据",
            "category": label, "time": _public_text(source.get("asked_at_text"), names),
            "sections": [section for section in sections if section["lines"]]}


def _topic_guest_view(topic: dict, rows: list[dict], comparison: dict, names: re.Pattern[str] | None) -> dict:
    metric_fields = (("咨询次数", "consultation_count"), ("独立会话", "session_count"), ("可识别商家", "merchant_count"))
    metrics = [{"label": label, "value": topic[field]} for label, field in metric_fields if type(topic.get(field)) is int]
    platform_text = "、".join(topic.get("platforms", []))
    classification = _TOPIC_LABELS.get(topic.get("primary_intent"), "")
    overview = [
        f"支持类型：{classification}" if classification else "",
        f"涉及平台：{platform_text}" if platform_text else "",
        f"最近发生：{topic['last_seen']}" if topic.get("last_seen") else "",
        topic.get("boundary", ""),
    ]
    trend = topic.get("trend") or {}
    trend_label = _TRENDS.get(trend.get("status"), "")
    if trend_label:
        overview.append(f"咨询趋势：{trend_label}")
    current = comparison.get("current") or {}
    if type(current.get("processed_pairs")) is int and type(current.get("source_pairs")) is int:
        overview.append(f"当前范围已整理 {current['processed_pairs']} / {current['source_pairs']} 条问答")
    sections = [_section("问题概况", overview, names)]
    diagnostics = defaultdict(set)
    for row in rows:
        status = row["data"].get("domain", {}).get("diagnosis", {}).get("status")
        if status in _DIAGNOSES:
            diagnostics[status].add(voc_variants.consultation_key(row))
    if diagnostics:
        sections.append(_section("诊断状态", [f"{label} · {len(diagnostics[status])} 次咨询"
                                             for status, label in _DIAGNOSES.items() if status in diagnostics], names))
    gaps = voc_topic_insights.gaps(rows, [], []) if rows else []
    if gaps:
        sections.append(_section("待补证据", [f"{_GAPS[item['code']]} · {item['count']} 次咨询"
                                             for item in gaps if item["code"] in _GAPS], names))
    candidates = sorted(
        (row for row in rows if type(row["source"].get("qa_id")) is int and row["source"]["qa_id"] > 0),
        key=lambda row: (not row["source"].get("_staff_verified"), not row["source"].get("answer"),
                         -int(row["source"].get("asked_at") or 0), row["source"]["qa_id"]),
    )
    by_consultation = {}
    for row in candidates:
        by_consultation.setdefault(voc_variants.consultation_key(row), row)
    cases = [_case_projection(row, names) for row in list(by_consultation.values())[:_CASE_LIMIT]]
    return {"taxonomy": _topic_taxonomy(topic, rows, names), "metrics": metrics,
            "sections": [section for section in sections if section["lines"]],
            "cases": cases, "total_cases": len(by_consultation)}


def topic_guest_views(cfg: Settings, pinned_scope: dict, topic_ids: set[str]) -> dict[str, dict]:
    """Rebuild only the already-published topics in their originally published scope."""
    selected = {topic_id for topic_id in topic_ids if isinstance(topic_id, str) and _TOPIC_ID.fullmatch(topic_id)}
    if not selected:
        return {}
    scope = _pinned_scope(pinned_scope)
    names = _private_names(cfg, scope)
    ranked = voc_engine.feishu_topics(cfg, scope, topic_ids=selected)
    return {topic["id"]: _topic_guest_view(ranked["guest_topics"].get(topic["id"], topic),
                                          ranked["guest_rows"].get(topic["id"], []), ranked["comparison"], names)
            for topic in ranked["items"] if topic["id"] in selected}


def _sop_guest_view(item: dict, topic: dict, category: str, names: re.Pattern[str] | None,
                    anchors: list[tuple[int, str]]) -> dict:
    sections = [
        _section("问题现象", [item["problem"].get("quote")], names),
        _section("适用条件", _quotes(item["conditions"]), names),
    ]
    steps = []
    index = 0
    for step in item["steps"]:
        phase, outcome = _PHASES.get(step["phase"]), _OUTCOMES.get(step["outcome"])
        if phase and outcome and (quote := _public_text(step["evidence"].get("quote"), names)):
            index += 1
            steps.append(f"{index}. {phase} · {outcome}\n{quote}")
            feedback = step.get("feedback") or {}
            if feedback.get("message_id") != (item.get("result") or {}).get("message_id"):
                text = _public_text(feedback.get("quote"), names)
                if text:
                    steps.append(f"客户反馈：{text}")
    sections.append({"title": "处理流程", "lines": steps})
    sections.append(_section("人工回复记录", _quotes(item.get("replies") or []), names))
    sections.append(_section("复测结果", [(item.get("result") or {}).get("quote") or "操作效果待验证。"], names))
    sections.append(_section("待补证据", [_GAPS.get(code, "其他待核验信息") for code in item.get("missing", [])], names))
    primary, secondary = _theme_path(topic.get("theme"), category, names)
    cases = [{"qa_id": qa_id, "anchor_id": anchor_id,
              "title": _public_text(item["problem"].get("quote"), names) or "会话依据",
              "category": category, "time": "", "sections": []}
             for qa_id, anchor_id in anchors]
    return {"taxonomy": [{"primary": primary, "secondary": secondary}], "metrics": [],
            "sections": [section for section in sections if section["lines"]],
            "cases": cases, "total_cases": len(cases)}


def select_topic_anchors(cfg: Settings, pinned_scope: dict, topic_ids: set[str]) -> dict[str, list[tuple[int, str]]]:
    """Resolve exact, previously exported topics without selecting a new top list."""
    selected = {topic_id for topic_id in topic_ids if _TOPIC_ID.fullmatch(topic_id)}
    if not selected:
        return {}
    ranked = voc_engine.feishu_topics(cfg, _pinned_scope(pinned_scope), topic_ids=selected)
    return ranked["guest_anchors"]


def _topic_records(cfg: Settings, scope: dict, names: re.Pattern[str] | None) -> tuple[list[dict], str]:
    ranked = voc_engine.feishu_topics(cfg, scope)
    coverage = ranked["comparison"]["current"]
    source, processed = coverage["source_pairs"], coverage["processed_pairs"]
    if type(source) is not int or type(processed) is not int or not 0 <= processed <= source:
        raise voc.VocError("当前范围的整理进度无法核验。")
    coverage_text = f"已整理 {processed} / {source} 条问答。咨询次数按各问题下的咨询统计。"
    records = []
    for topic in ranked["items"]:
        key, count = topic["id"], topic["consultation_count"]
        title = _public_text(topic["question"], names) or _public_text(topic.get("theme"), names)
        if not _TOPIC_ID.fullmatch(key) or not title or type(count) is not int or count <= 0:
            continue
        category = _TOPIC_LABELS.get(topic.get("primary_intent"), "")
        summary = " · ".join(value for value in (f"{count} 次咨询", category) if value)
        detail = f"{summary}。\n统计区间 {scope['start']} 至 {scope['end']}（上海时间）。\n{coverage_text}"
        record = {"key": key, "title": title, "summary": summary, "detail": detail,
                  "url": "", "consultations": count, "_guest_anchors": ranked["guest_anchors"].get(key, [])}
        if len(records) < _LIMIT:
            record["_guest_view"] = _topic_guest_view(ranked.get("guest_topics", {}).get(key, topic),
                                                      ranked.get("guest_rows", {}).get(key, []),
                                                      ranked["comparison"], names)
        records.append(record)
    return records, coverage_text


def _sop_records(cfg: Settings, scope: dict, identity: str, names: re.Pattern[str] | None) -> tuple[list[dict], str]:
    data = voc_engine.sops(cfg, scope, identity)
    cases = [data["item"]] if identity else data["items"]
    topics = voc_engine._topic_definitions(cfg)
    records = []
    for item in cases:
        key, status = item["id"], item["status"]
        category = _SOP_LABELS.get(item["kind"])
        if status not in ("verified", "guidance") or not _SOP_ID.fullmatch(key) or not category:
            continue
        conditions = list(dict.fromkeys(text for ref in item["conditions"]
                                       if (text := _public_text(ref.get("quote"), names))))
        steps = []
        for step in item["steps"]:
            phase = step["phase"]
            if phase not in _PHASES or step["outcome"] not in ("unconfirmed", "verified"):
                continue
            text = _public_text(step["evidence"].get("quote"), names)
            if text:
                steps.append((phase, text))
        if not conditions or not any(phase in ("diagnosis", "action") for phase, _ in steps):
            continue
        if status == "verified" and not (item.get("result") or {}).get("quote"):
            continue
        topic_id = item.get("topic_id")
        topic = topics.get(topic_id, {})
        title = _public_text(topic.get("question"), names) or f"{category}处理流程"
        label = "效果已验证" if status == "verified" else "已核验指引，效果待验证"
        result = "客户反馈按该流程处理后复测成功。" if status == "verified" else "操作效果待验证。"
        detail = "\n".join([
            label,
            f"统计区间 {scope['start']} 至 {scope['end']}（上海时间）。",
            "适用条件",
            *conditions,
            "处理步骤",
            *(f"{index}. {_PHASES[phase]} · {text}" for index, (phase, text) in enumerate(steps, 1)),
            "复测结果",
            result,
        ])
        qa_id, anchor_id = item.get("qa_id"), item["problem"].get("message_id")
        anchors = [(qa_id, anchor_id)] if type(qa_id) is int and qa_id > 0 and isinstance(anchor_id, str) and anchor_id else []
        records.append({"key": key, "title": title, "summary": f"{category} · {label}", "detail": detail,
                        "url": "", "_guest_anchors": anchors,
                        "_guest_view": _sop_guest_view(item, topic, category, names, anchors)})
    return records, f"当前范围有 {len(cases)} 份处理流程，其中 {len(records)} 份可用于本次整理。"


def build_document(cfg: Settings, filters: dict, kind: str, identity: str = "") -> dict:
    """Return exportable records and their equivalent Feishu DocxXML preview."""
    if kind not in ("topics", "sops"):
        raise voc.VocError("未知的飞书成果类型。")
    if identity and (kind != "sops" or not _SOP_ID.fullmatch(identity)):
        raise voc.VocError("处理流程标识无效。")

    scope = _pinned_scope(filters)
    names = _private_names(cfg, scope)
    if kind == "topics":
        heading = "高频问题"
        records, coverage = _topic_records(cfg, scope, names)
    else:
        heading = "可复用处理 SOP"
        records, coverage = _sop_records(cfg, scope, identity, names)

    start, end = scope["start"], scope["end"]
    dates = start if start == end else f"{start} 至 {end}"
    title = f"语海Voc {heading} {dates}"
    total = len(records)
    records = records[:_LIMIT]
    count = len(records)
    document = Element("document")
    SubElement(document, "title").text = title
    SubElement(document, "p").text = f"统计区间 {dates}（上海时间）。{coverage}"
    if scope.get("group") or scope.get("staff"):
        SubElement(document, "p").text = "统计沿用当前客户群或客服筛选。"
    SubElement(document, "p").text = f"符合条件 {total} 项，本次列出 {count} 项。"
    SubElement(document, "h1").text = heading
    if not records:
        SubElement(document, "p").text = "当前范围暂无可导出的条目。"
    for record in records:
        SubElement(document, "h2").text = record["title"]
        for line in record["detail"].splitlines():
            SubElement(document, "p").text = line
    content = "\n".join(tostring(block, encoding="unicode") for block in document)
    return {"title": title, "content": content, "count": count, "total": total,
            "items": [{"title": row["title"], "summary": row["summary"]} for row in records],
            "records": records, "start": start, "end": end}



def attach_guest_links(cfg: Settings, document: dict, kind: str) -> dict:
    """Issue a scoped page at publish time, including entries without shareable context."""
    if kind not in ("topics", "sops"):
        raise voc.VocError("未知的飞书成果类型。")
    records = []
    for record in document["records"]:
        entry = {"kind": kind, "title": record["title"], "summary": record["summary"], "detail": record["detail"],
                 "start": document["start"], "end": document["end"]}
        url = ""
        for qa_id, anchor_id in record.get("_guest_anchors", []):
            url = guest_evidence.issue_url(cfg, qa_id, anchor_id, entry=entry, view=record.get("_guest_view"))
            if url:
                break
        if not url:
            url = guest_evidence.issue_url(cfg, None, "", entry=entry, view=record.get("_guest_view"))
        if not url:
            raise voc.VocError("访客链接暂不可生成，成果尚未发布。", 503)
        records.append({**{key: value for key, value in record.items() if key not in ("_guest_anchors", "_guest_view")}, "url": url})
    return {**document, "records": records}
