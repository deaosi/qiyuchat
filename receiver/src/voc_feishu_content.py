"""Build a Feishu snapshot from the current, read-only VOC projections."""

from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, SubElement, tostring

from . import voc, voc_assets, voc_engine, voc_topic_insights
from .clean.rules import mask_pii
from .config import Settings
from .extract.text import clean_visible_text


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


def _topic_url(identity: object, scope: dict) -> str:
    if not isinstance(identity, str) or not _TOPIC_ID.fullmatch(identity):
        return ""
    query = urlencode({"topic": identity, "start": scope["start"], "end": scope["end"]})
    return "https://qiyu.fanwulink.cn/#questions?" + query


def _topic_records(cfg: Settings, scope: dict, names: re.Pattern[str] | None) -> tuple[list[dict], str]:
    ranked = voc_engine.topics(cfg, scope, workbench=True)
    coverage = ranked["comparison"]["current"]
    source, processed = coverage["source_pairs"], coverage["processed_pairs"]
    if type(source) is not int or type(processed) is not int or not 0 <= processed <= source:
        raise voc.VocError("当前范围的整理进度无法核验。")
    coverage_text = f"已整理 {processed} / {source} 条问答。咨询次数按各问题下的咨询统计。"
    records = []
    for topic in ranked["items"]:
        key, count = topic["id"], topic["consultation_count"]
        url = _topic_url(key, scope)
        title = _public_text(topic["question"], names) or _public_text(topic.get("theme"), names)
        if not url or not title or type(count) is not int or count <= 0:
            continue
        category = _TOPIC_LABELS.get(topic.get("primary_intent"), "")
        summary = " · ".join(value for value in (f"{count} 次咨询", category) if value)
        detail = f"{summary}。\n统计区间 {scope['start']} 至 {scope['end']}（上海时间）。\n{coverage_text}"
        records.append({"key": key, "title": title, "summary": summary, "detail": detail,
                        "url": url, "consultations": count})
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
        records.append({"key": key, "title": title, "summary": f"{category} · {label}", "detail": detail,
                        "url": _topic_url(topic_id, scope)})
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
        if record["url"]:
            link = SubElement(SubElement(document, "p"), "a", {"type": "url-preview", "href": record["url"]})
            link.text = "查看语海Voc来源"
    content = "\n".join(tostring(block, encoding="unicode") for block in document)
    return {"title": title, "content": content, "count": count, "total": total,
            "items": [{"title": row["title"], "summary": row["summary"]} for row in records],
            "records": records, "start": start, "end": end}
