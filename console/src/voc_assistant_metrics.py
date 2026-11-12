"""Read-only source statistics for the VOC assistant."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import re

from . import voc, voc_assets, voc_quality
from .clean.rules import mask_pii
from .config import Settings
from .voc_topic_insights import Period, ZONE, periods


_DIMENSIONS = {
    "group_breakdown": ("group_name", "客户群", "未分组"),
    "staff_breakdown": ("staff_name", "客服", "未记录客服"),
    "customer_breakdown": ("customer_id", "客户", "匿名客户"),
}
_METRICS = frozenset(("overview", "daily_trend", *_DIMENSIONS))
_IDENTITY = re.compile(
    r"(?:qiyu-(?:external(?:-sha256)?|user|session):[^\s，。；！？、]+"
    r"|(?<![a-z0-9])(?:"
    r"(?:anonymous|customer)-[0-9a-f]{16,64}|[0-9a-f]{16,64}|(?:[ist]|episode|sop)_[a-f0-9]{24,64}"
    r"|wxid_[a-z0-9_-]+|w[mo][a-z0-9_-]{16,}|source:[a-z0-9_.-]+:[a-z0-9_.-]+)(?![a-z0-9]))",
    re.IGNORECASE,
)
_URL = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_MASKED_PHONE = re.compile(r"(?<!\d)1[3-9]\d\*{4}\d{4}(?!\d)")
_ANONYMOUS_CUSTOMER = {"", "匿名客户", "身份未知访客", "来源访客", "未命名客户", "客户", "用户", "访客"}
_REPLY_NOTE = "未记录可见答复只表示当前来源问答没有保留答复内容。"


def _source_rows(cfg: Settings, filters: dict, bounds: tuple[int, int], engine, *, classified: bool) -> list[dict]:
    if classified:
        sources = engine._sources(cfg, filters, time_bounds=bounds)
        events = engine._topic_view(cfg, filters, sources)
        # An episode belongs to its first question. Multiple intents can point
        # at the same source QA and must not multiply the source statistics.
        return [sources[key] for key in dict.fromkeys(event["qa_id"] for event in events) if key in sources]

    source_path = Path(cfg.runtime.db_path)
    if not source_path.is_file():
        return []
    with voc_assets.connection(source_path) as db:
        db.execute("BEGIN")
        sources = voc._scope(db, filters, include_messages=False, _bounds_override=bounds)
        if not sources:
            return []
        # The display session ID may be masked. Keep exact identity internally
        # for deduplication without using a shared database connection.
        sessions = db.execute("SELECT qa_id,session_id FROM qa_pair WHERE qa_id IN (SELECT value FROM json_each(?))",
                              (voc_assets.dump(list(sources)),))
        for qa_id, session_id in sessions:
            sources[qa_id]["_session_key"] = voc_assets.digest(["raw-session", session_id])
    return list(sources.values())


def _safe_label(value: str) -> str:
    value = _CONTROL.sub(" ", value or "")
    value = _URL.sub("[链接已隐藏]", value)
    value = _IDENTITY.sub("[标识已隐藏]", value)
    return " ".join(_MASKED_PHONE.sub("[号码已隐藏]", mask_pii(value)).split())[:120]


def _customer_alias(identity: str) -> str:
    # Use the same anonymous display number as the customer directory.
    value = 2166136261
    for char in identity:
        value = ((value ^ ord(char)) * 16777619) & 0xffffffff
    return f"匿名客户 {value % 100000:05d}"


def _day(value) -> date | None:
    try:
        return datetime.fromtimestamp(value / 1000, ZONE).date() if value > 0 else None
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def _without_answer(rows: list[dict]) -> int:
    return sum(not row["answer"].strip() for row in rows)


def _overview(rows: list[dict], heading: str) -> str:
    sessions = len({voc_quality.session_key(row) for row in rows})
    customers = len({row["customer_id"] for row in rows})
    groups = {row["group_name"] for row in rows if row["group_name"] != "未分组"}
    staff = {row["staff_name"] for row in rows if row["staff_name"] != "未记录客服"}
    without = _without_answer(rows)
    return (
        f"{heading}共有 {len(rows)} 条来源问答，来自 {sessions} 个会话，涉及 {customers} 个客户标识"
        f"、{len(groups)} 个已记录客户群、{len(staff)} 个已记录接待客服。\n"
        f"有可见答复 {len(rows) - without} 条，未记录可见答复 {without} 条。{_REPLY_NOTE}"
    )


def _daily(rows: list[dict], period: Period, heading: str, limit: int) -> str:
    daily = Counter(_day(row["asked_at"]) for row in rows)
    unknown = daily.pop(None, 0)
    if not daily:
        return f"{heading}没有带有效日期的来源问答。时间未知 {unknown} 条。"
    first = period.start or min(daily)
    last = period.end or max(daily)
    day_count = (last - first).days + 1
    peak = max(daily.values())
    peak_days = [day for day, count in daily.items() if count == peak]
    display_count = min(limit, day_count)
    start = last - timedelta(days=display_count - 1)
    head = (f"{heading}{first.isoformat()} 至 {last.isoformat()}共有 {sum(daily.values())} 条可按日统计的来源问答"
            f"，有数据 {len(daily)} 天，时间未知 {unknown} 条。")
    high = f"全时段单日最高 {peak} 条，最早出现在 {min(peak_days).isoformat()}。"
    if len(peak_days) > 1:
        high += f"共有 {len(peak_days)} 天并列。"
    days = [start + timedelta(days=index) for index in range(display_count)]
    lines = [f"{day.isoformat()}：{daily.get(day, 0)} 条" for day in days]
    return "\n".join((head, high, f"逐日明细展示最近 {display_count} 天（北京时间）。", *lines))


def _breakdown(rows: list[dict], name: str, heading: str, limit: int) -> tuple[str, str]:
    field, dimension, missing = _DIMENSIONS[name]
    buckets = defaultdict(list)
    for row in rows:
        buckets[row[field]].append(row)
    ranked = sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]
    title = (f"{heading}共有 {len(rows)} 条来源问答，按{dimension}分为 {len(buckets)} 项，"
             f"以下按问答量展示前 {len(ranked)} 项。")
    display, model = [title], [title]
    for index, (value, members) in enumerate(ranked, 1):
        if field == "customer_id":
            label = _safe_label(members[0]["customer_name"])
            if label in _ANONYMOUS_CUSTOMER or label in (value, "[标识已隐藏]", "[链接已隐藏]"):
                label = _customer_alias(value)
        else:
            label = _safe_label(value) or missing
        without = _without_answer(members)
        share = 100 * len(members) / len(rows)
        data = f"{len(members)} 条问答，占 {share:.1f}%，未记录可见答复 {without} 条。"
        display.append(f"{index}. {label}：{data}")
        model_label = missing if field != "customer_id" and value == missing else f"{dimension} {index}（已脱敏）"
        model.append(f"{index}. {model_label}：{data}")
    note = _REPLY_NOTE
    if field == "staff_name":
        note += "接待客服以来源字段为准。"
    display.append(note)
    model.append(note)
    return "\n".join(display), "\n".join(model)


def read_metric(cfg: Settings, filters: dict, name: str, limit: int, *, engine) -> tuple[str, list[dict], str]:
    """Return display text, citations, and a separate redacted model input."""
    if name not in _METRICS:
        raise ValueError("未知会话统计指标。")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("会话统计展示条数须为 1–10。")
    has_classification = any(filters.get(key) not in (None, "", "all")
                             for key in ("support_intent", "diagnosis_status"))
    if has_classification and cfg.runtime.voc_domain != "zhihuige":
        note = "当前数据源无法按所选支持类型或诊断状态统计来源问答。"
        return note, [], note
    classified = bool(filters.get("focus_topic")) or has_classification
    period, _ = periods(filters)
    rows = _source_rows(cfg, filters, period.bounds(), engine, classified=classified)
    heading = "当前主题或分类中，" if classified else "当前范围内，"
    if not rows:
        note = f"{heading}暂无匹配的来源问答。"
        return note, [], note
    if name in _DIMENSIONS:
        display, model = _breakdown(rows, name, heading, limit)
        return display, [], model
    text = _overview(rows, heading) if name == "overview" else _daily(rows, period, heading, limit)
    return text, [], text
