"""Show the latest verified VOC publication and latest analysis job in Feishu."""

from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlencode, urlsplit, urlunsplit


ZONE = timezone(timedelta(hours=8))
JOB_STATUS = {"queued": "等待整理", "running": "整理中", "completed": "已完成",
              "partial": "部分完成", "failed": "整理失败", "skipped": "已跳过"}
NOTE = "语海 Voc 每半小时同步近 7 天的高频问题。实际同步时间与统计区间见右侧日志。下方表格是高频问题登记表预览。"


def _stamp(timestamp):
    return datetime.fromtimestamp(timestamp, ZONE).strftime("%m-%d %H:%M:%S")


def _text(snapshot, synced_at, job):
    topics = snapshot["documents"]["topics"]
    count = topics["count"]
    period = "–".join(datetime.fromisoformat(topics[key]).strftime("%m-%d") for key in ("start", "end"))
    registered = snapshot.get("register_count", count)
    lines = [f"飞书同步 · **{_stamp(synced_at)}** · 成功 · 登记 {registered} / Top {min(100, count)} · 统计 {period}"]
    if job:
        status = JOB_STATUS.get(job["status"], "状态未知")
        lines.append(f"语海整理 · **{_stamp(job['updated_at'])}** · {status} · "
                     f"已处理 {job['processed']} / {job['total']} · 失败 {job['failed']} 条")
    else:
        lines.append("语海整理 · 暂无可核对的作业记录")
    return "<br/>".join(lines)


def _only(items, predicate, description):
    matches = [item for item in items if predicate(item)]
    if len(matches) != 1:
        raise ValueError(f"无法唯一核对飞书{description}。")
    return matches[0]


def _update(target, cli, dashboard_id, block, text, *, name=None):
    args = ("--dashboard-id", dashboard_id, "--block-id", block["block_id"])
    current = cli(target, "+dashboard-block-get", *args)["block"]
    if current["type"] != "text":
        raise ValueError("飞书仪表盘的日志组件类型已变化。")
    expected_name = name or current["name"]
    if current["data_config"].get("text") == text and current["name"] == expected_name:
        return
    options = ("--name", expected_name) if current["name"] != expected_name else ()
    cli(target, "+dashboard-block-update", *args, *options,
        "--data-config", json.dumps({"text": text}, ensure_ascii=False))
    result = cli(target, "+dashboard-block-get", *args)["block"]
    if result["name"] != expected_name or result["data_config"].get("text") != text:
        raise ValueError("飞书仪表盘日志回读与本次更新不一致。")


def update(target, cli, snapshot, synced_at, job):
    dashboards = cli(target, "+dashboard-list")
    if dashboards.get("has_more"):
        raise ValueError("飞书仪表盘列表未完整返回。")
    dashboard = _only(dashboards["items"], lambda item: item["name"] == "仪表盘", "仪表盘")
    dashboard_id = dashboard["dashboard_id"]
    blocks = cli(target, "+dashboard-block-list", "--dashboard-id", dashboard_id)
    if blocks.get("has_more"):
        raise ValueError("飞书仪表盘组件列表未完整返回。")
    items = blocks["items"]
    log = _only(items, lambda item: item["name"] in ("最近一次数据同步时间", "最近整理同步"), "最近同步组件")
    _update(target, cli, dashboard_id, log, _text(snapshot, synced_at, job), name="最近整理同步")
    note = _only(items, lambda item: item["name"] == "数据同步说明", "数据同步说明组件")
    _update(target, cli, dashboard_id, note, NOTE)
    url = urlsplit(target.destination)
    return urlunsplit(url._replace(query=urlencode({"table": dashboard_id})))
