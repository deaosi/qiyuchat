"""聚合：单客户 → 客户群 → 全局。

三层看的是同一批 qa_pair，只是分组维度不同：
    客户层  「这个客户老问什么」—— 判断他是不是卡在同一个问题上
    群层    「这个群/这个渠道在反馈什么」—— 定位是哪条业务线的问题
    全局层  「所有客户都在说什么」—— 决定产品/话术先改哪里
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def _top_questions(rows: list[dict[str, Any]], n: int = 5) -> list[dict[str, Any]]:
    """按簇统计，返回 [(代表原话, 次数, 簇号)]。"""
    by_cluster: dict[int, list[str]] = defaultdict(list)
    for r in rows:
        cid = r.get("cluster_id")
        if cid:
            by_cluster[cid].append(r["question"])

    out = []
    for cid, qs in by_cluster.items():
        cnt = Counter(qs)
        cnt_sorted = cnt.most_common()
        cnt_sorted.sort(key=lambda kv: (-kv[1], len(kv[0])))
        rep, times = cnt_sorted[0]
        out.append({"question": rep, "count": len(qs), "cluster_id": cid,
                    "variants": len(cnt)})
    out.sort(key=lambda x: -x["count"])
    return out[:n]


def by_customer(rows: list[dict[str, Any]], top_n: int = 5) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r.get("customer_id") or "未知客户"].append(r)

    out = []
    for cid, rs in groups.items():
        ts = [int(r.get("asked_at") or 0) for r in rs if r.get("asked_at")]
        out.append({
            "customer_id": cid,
            "customer_name": rs[0].get("customer_name") or "",
            "group_name": rs[0].get("group_name") or "",
            "total_questions": len(rs),
            "distinct_clusters": len({r.get("cluster_id") for r in rs if r.get("cluster_id")}),
            "top": _top_questions(rs, top_n),
            "first_seen": min(ts) if ts else 0,
            "last_seen": max(ts) if ts else 0,
        })
    out.sort(key=lambda x: -x["total_questions"])
    return out


def by_group(rows: list[dict[str, Any]], top_n: int = 10) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r.get("group_name") or "未分组"].append(r)

    out = []
    for name, rs in groups.items():
        out.append({
            "group_name": name,
            "total_questions": len(rs),
            "customer_count": len({r.get("customer_id") for r in rs}),
            "top": _top_questions(rs, top_n),
        })
    out.sort(key=lambda x: -x["total_questions"])
    return out


def overall(rows: list[dict[str, Any]], top_n: int = 20) -> dict[str, Any]:
    ts = [int(r.get("asked_at") or 0) for r in rows if r.get("asked_at")]
    return {
        "total_questions": len(rows),
        "customer_count": len({r.get("customer_id") for r in rows}),
        "group_count": len({r.get("group_name") for r in rows}),
        "cluster_count": len({r.get("cluster_id") for r in rows if r.get("cluster_id")}),
        "first_seen": min(ts) if ts else 0,
        "last_seen": max(ts) if ts else 0,
        "top": _top_questions(rows, top_n),
    }
