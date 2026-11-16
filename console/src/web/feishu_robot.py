"""Display read-only health from the scheduled Feishu CLI workers' receipts."""

from datetime import datetime, timezone
import json
from pathlib import Path
from time import time


STATE_DIR = Path("/var/lib/qiyu-feishu-sync")
_HISTORY_LIMIT = 24
_MAX_RECEIPT_BYTES = 32 * 1024
_LANES = {
    "tickets": ("工单双向同步", 30, 180, "增量拉取 · 双向回传"),
    "voc": ("高频问题与知识 SOP", 1800, 2100, "定时整理 · 结果发布"),
}
_COUNTERS = {
    "tickets": {
        "created": "新增工单",
        "updated": "更新工单",
        "imported": "技术反馈变动",
        "feedback_failed": "回传失败",
        "feedback_pending": "待回传",
        "record_pending": "待更新飞书",
        "detail_pending": "待补全详情",
        "review_pending": "待核验回传",
    },
    "voc": {"topics": "登记高频问题", "top": "榜单高频问题", "sops": "知识 SOP"},
}
_RESULTS = {
    "failed": ("error", "同步失败，任务将在后续调度中重试。"),
    "permission": ("error", "同步权限校验失败，请核对应用身份和数据表权限。"),
    "timeout": ("error", "同步请求超时，任务将在后续调度中重试。"),
    "conflict": ("warning", "本轮同步暂无法完成核对，下次调度继续处理。"),
    "maintenance": ("info", "系统维护期间暂停工单同步。"),
    "partial": ("warning", "工单同步部分完成，请查看待处理任务和回传状态。"),
    "ticket_checked": ("success", "已核对七鱼工单和飞书技术回复。"),
    "voc_published": ("success", "高频问题和知识 SOP 已同步并回读确认。"),
    "voc_checked": ("success", "已检查本期发布状态并核对飞书仪表盘。"),
}


def _count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _filtered_counters(lane, values):
    return {key: count for key in _COUNTERS[lane] if (count := _count(values.get(key))) is not None}


def _epoch(value, now):
    return value if type(value) is int and 0 < value <= now + 60 else None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _read(path, lane, now):
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_RECEIPT_BYTES:
            return {}
        with path.open("rb") as handle:
            data = handle.read(_MAX_RECEIPT_BYTES + 1)
        if len(data) > _MAX_RECEIPT_BYTES:
            return {}
        value = json.loads(data)
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("events"), list):
        return {}

    events = []
    for row in value["events"][-_HISTORY_LIMIT:]:
        if not isinstance(row, dict) or (timestamp := _epoch(row.get("timestamp"), now)) is None:
            continue
        code, counters = row.get("code"), row.get("counters")
        if isinstance(code, str) and code in _RESULTS and isinstance(counters, dict):
            events.append({"timestamp": timestamp, "code": code, "counters": _filtered_counters(lane, counters)})
    return {"events": sorted(events, key=lambda event: event["timestamp"]),
            "last_success_at": _epoch(value.get("last_success_at"), now),
            "last_published_at": _epoch(value.get("last_published_at"), now)}


def receipt_path(state_dir, lane):
    if lane not in _LANES:
        raise ValueError("Unknown Feishu sync lane")
    return state_dir / f"robot-health-{lane}.json"


def _result(lane, result):
    state = result.get("status")
    if state == "failed":
        return {403: "permission", 409: "conflict", 504: "timeout"}.get(result.get("http_status"), "failed"), {}
    if lane == "tickets":
        if state == "maintenance":
            return "maintenance", {}
        if state in ("published", "partial"):
            feedback = result.get("feedback")
            queue = result.get("queued")
            counters = {name: result.get(name) for name in ("created", "updated")}
            if isinstance(feedback, dict):
                counters["imported"] = feedback.get("imported")
                counters["feedback_failed"] = feedback.get("failed")
            if isinstance(queue, dict):
                counters.update(feedback_pending=queue.get("feedback"), record_pending=queue.get("records"),
                                detail_pending=queue.get("details"), review_pending=queue.get("verification"))
            return "ticket_checked" if state == "published" else "partial", counters
    elif state == "published":
        register, ranked = result.get("all"), result.get("top")
        return "voc_published", {
            "topics": register.get("topics") if isinstance(register, dict) else None,
            "top": ranked.get("topics") if isinstance(ranked, dict) else None,
            "sops": register.get("sops") if isinstance(register, dict) else None,
        }
    elif state == "already_checked":
        return "voc_checked", {"topics": result.get("all_topics"), "top": result.get("top_topics"),
                              "sops": result.get("sops")}
    return "failed", {}


def receipt(path, lane, result, *, timestamp=None):
    """Convert one worker result to bounded health state without copying response data."""
    now = int(time())
    timestamp = now if timestamp is None else timestamp
    if _epoch(timestamp, now) is None:
        raise ValueError("Invalid Feishu sync receipt timestamp")
    previous = _read(path, lane, now)
    events = previous.get("events", [])
    code, counters = _result(lane, result)
    counters = _filtered_counters(lane, counters)
    event = {"timestamp": timestamp, "code": code, "counters": counters}
    if not events or timestamp >= events[-1]["timestamp"]:
        # Frequent identical checks share one history row and retain the latest result time.
        if events and code == events[-1]["code"] and counters == events[-1]["counters"]:
            events.pop()
        events.append(event)
    else:
        events = sorted([*events, event], key=lambda item: item["timestamp"])

    successful = _RESULTS[code][0] == "success"
    published = code == "voc_published" or (code in ("ticket_checked", "partial") and any(
        counters.get(key) for key in ("created", "updated")))
    return {"version": 1, "events": events[-_HISTORY_LIMIT:],
            "last_success_at": max(previous.get("last_success_at") or 0, timestamp if successful else 0) or None,
            "last_published_at": max(previous.get("last_published_at") or 0, timestamp if published else 0) or None}


def _message(event):
    code = event["code"]
    counters = event["counters"]
    if code == "ticket_checked" and any(counters.get(key) for key in ("created", "updated", "imported")):
        return (f"新增 {counters.get('created', 0)} 条工单，更新 {counters.get('updated', 0)} 条工单，"
                f"处理 {counters.get('imported', 0)} 条技术反馈变动。")
    return _RESULTS[code][1]


def status(cfg, *, include_settings):
    now = int(time())
    states, logs, settings = [], [], []
    for lane, (label, interval, stale_after, mode) in _LANES.items():
        state = {} if cfg.runtime.mock else _read(receipt_path(STATE_DIR, lane), lane, now)
        events = state.get("events", [])
        latest = events[-1] if events else None
        current = "unverified"
        message = "暂未收到同步结果。"
        if latest is not None:
            current = "healthy" if _RESULTS[latest["code"]][0] == "success" else "warning"
            message = _message(latest)
            if now - latest["timestamp"] > stale_after:
                current = "stale"
                message = "已超过预期更新间隔，请检查同步任务。"
        counters = latest["counters"] if latest else {}
        states.append({"id": lane, "label": label, "status": current,
                       "last_run_at": _iso(latest["timestamp"]) if latest else None,
                       "last_success_at": _iso(state.get("last_success_at")),
                       "last_published_at": _iso(state.get("last_published_at")), "interval_seconds": interval,
                       "counters": [{"label": name, "value": counters[key]} for key, name in _COUNTERS[lane].items()
                                    if key in counters], "message": message})
        logs.extend({"timestamp": _iso(event["timestamp"]), "lane": lane, "label": label,
                     "level": _RESULTS[event["code"]][0], "message": _message(event)} for event in events)
        if include_settings:
            target_configured = not cfg.runtime.mock and (STATE_DIR / "target-config.json").is_file()
            if lane == "tickets":
                target_configured = target_configured and (STATE_DIR / "ticket-table-setup.json").is_file()
            settings.append({"id": lane, "label": label, "interval_seconds": interval,
                             "target_configured": target_configured, "mode": mode})

    lane_states = {lane["status"] for lane in states}
    connection = "unverified" if lane_states == {"unverified"} else "degraded"
    if lane_states == {"healthy"}:
        connection = "connected"
    logs.sort(key=lambda row: row["timestamp"], reverse=True)
    response = {"status": connection, "reported_at": logs[0]["timestamp"] if logs else None,
                "lanes": states, "logs": logs}
    if include_settings:
        response["settings"] = {"lanes": settings}
    return response
