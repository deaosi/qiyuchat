"""编排层：把「取数 → 清洗 → 推送」串起来。

每个 stage 都是独立可重入的，方便单步调试：
    python run.py doctor
    python run.py pull
    python run.py clean
    python run.py push
    python run.py all
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from . import extract as E
from . import mock as M
from .clean import CleanPipeline
from .clean import prompts as P
from .config import Settings, settings
from .db import DB
from .feishu import (AgentClient, BitableWriter, BotSender, DocxWriter,
                     PAIR_FIELD_SPEC, check_feishu, pair_to_fields)
from .log import get, setup
from .qiyu import InspectExporter, SessionExporter

log = get("app")

DEFAULT_LOOKBACK_DAYS = 7


# ---------- 基础设施 ----------

_db: DB | None = None
_db_path: str = ""


def get_db(cfg: Settings | None = None) -> DB:
    global _db, _db_path
    cfg = cfg or settings()
    if _db is None or _db_path != cfg.runtime.db_path:
        if _db is not None:
            _db.close()
        _db = DB(cfg.runtime.db_path)
        _db_path = cfg.runtime.db_path
    return _db


def reset_db() -> None:
    """切库用（mock 场景）。"""
    global _db, _db_path
    if _db is not None:
        _db.close()
    _db = None
    _db_path = ""


def init_logging(cfg: Settings) -> None:
    setup(cfg.runtime.log_dir, cfg.runtime.log_level)


# ---------- 取数 ----------

def stage_pull(cfg: Settings | None = None, days: int = 0) -> dict[str, int]:
    """拉会话记录。按窗口切片，推进水位。"""
    cfg = cfg or settings()
    db = get_db(cfg)
    now_ms = int(time.time() * 1000)

    if cfg.runtime.mock:
        rows = M.make_sessions(24)
        n = db.upsert_sessions(rows)
        for r in rows:
            db.upsert_messages(str(r["id"]),
                               M.make_transcript(str(r["id"]), int(r.get("startTime") or 0)))
        log.info("[MOCK] 写入会话 %d 条（含正文）", n)
        return {"sessions": n}

    if not cfg.qiyu.app_key or not cfg.qiyu.app_secret:
        raise RuntimeError("缺少 QIYU_APP_KEY / QIYU_APP_SECRET，先用 MOCK=1 跑通链路")

    exporter = SessionExporter(cfg.qiyu)
    wm_name = "session_last_end_ms"
    last = int(db.get_watermark(wm_name, str(now_ms - DEFAULT_LOOKBACK_DAYS * 86_400_000)))

    if days:
        start = now_ms - days * 86_400_000
    else:
        start = last

    window = cfg.qiyu.session_window_days * 86_400_000
    total = 0
    cur = start
    while cur < now_ms:
        end = min(cur + window, now_ms)
        rows = exporter.export(cur, end, type_=0)
        total += db.upsert_sessions(rows)
        db.set_watermark(wm_name, str(end))
        cur = end

    log.info("会话记录拉取完成，共 %d 条，水位推进到 %d", total, cur)
    return {"sessions": total}


def stage_pull_inspect(cfg: Settings | None = None, days: int = 2) -> dict[str, int]:
    """拉质检导出，取会话正文，回填 raw_message。"""
    cfg = cfg or settings()
    db = get_db(cfg)

    if cfg.runtime.mock:
        log.info("[MOCK] 正文已由 pull 阶段写入，跳过")
        return {"messages": 0}

    if not cfg.qiyu.app_key or not cfg.qiyu.app_secret:
        raise RuntimeError("缺少七鱼凭证")

    exporter = InspectExporter(cfg.qiyu)
    now_ms = int(time.time() * 1000)
    start = now_ms - days * 86_400_000

    rows = exporter.export(start, now_ms, interaction=0)
    written = 0
    hit = 0
    for row in rows:
        sid = InspectExporter.pick(row, ("会话ID", "会话id", "sessionId"))
        content = InspectExporter.pick(row, ("会话内容", "聊天内容", "对话内容"))
        if not sid or not content:
            continue
        hit += 1
        staff = InspectExporter.pick(row, ("接待客服", "客服"))
        msgs = InspectExporter.split_transcript(content, staff)
        written += db.upsert_messages(sid, msgs)

    coverage = (hit / len(rows) * 100) if rows else 0
    log.info("质检导出 %d 行，其中 %d 行有正文（覆盖率 %.1f%%），写入消息 %d 条",
             len(rows), hit, coverage, written)
    return {"rows": len(rows), "with_content": hit, "messages": written}


# ---------- 抽取（不调模型） ----------

def stage_extract(cfg: Settings | None = None, limit: int = 0,
                  threshold: float = 0.45, rebuild: bool = False) -> dict[str, int]:
    """抽 Q&A 对 + 聚类。全程零 API 调用。"""
    cfg = cfg or settings()
    db = get_db(cfg)

    sessions = (db.iter_sessions() if rebuild else db.sessions_without_pairs(limit))
    if limit and not rebuild:
        sessions = sessions[:limit]

    total_pairs = 0
    for s in sessions:
        sd = dict(s)
        sd["session_id"] = sd.get("session_id") or sd.get("id")
        sid = str(sd["session_id"])
        db.restore_verified_source(sid)
        msgs = db.get_messages(sid)
        input_data = {k: sd.get(k) for k in
                      ("session_id", "user_id", "user_name", "from_group", "staff_name", "start_ts")}
        fingerprint = hashlib.sha256(json.dumps(
            [input_data, msgs], ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest()
        # 原始正文可能已由保留策略归档；不能把历史问答资产重洗成空。
        pairs = E.extract_pairs(msgs, sd) if msgs else [dict(p) for p in db.iter_qa_pairs([sid])]
        status = ("extracted" if msgs else "archived") if pairs else ("no_question" if msgs else "no_content")
        total_pairs += db.replace_session_pairs(sid, pairs, fingerprint, status)

    log.info("抽取出 %d 条问答对（覆盖 %d 个会话）", total_pairs, len(sessions))

    # 聚类：全量重算，保证簇号稳定可比
    rows = db.iter_qa_pairs()
    items = [(int(r["qa_id"]), r["q_norm"] or "") for r in rows]
    clusters = E.cluster_questions(items, threshold=threshold)

    by_id = {int(r["qa_id"]): dict(r) for r in rows}
    mapping: dict[int, int] = {}
    cluster_rows = []
    for cl in clusters:
        members = [by_id[i] for i in cl["ids"] if i in by_id]
        if not members:
            continue
        custs = {m.get("customer_id") for m in members if m.get("customer_id")}
        grps = {m.get("group_name") for m in members if m.get("group_name")}
        times = [int(m.get("asked_at") or 0) for m in members if m.get("asked_at")]
        # 代表原话：簇内出现最多的原始问句
        from collections import Counter
        cnt = Counter(m["question"] for m in members).most_common()
        cnt.sort(key=lambda kv: (-kv[1], len(kv[0])))
        for i in cl["ids"]:
            mapping[i] = cl["cluster_id"]
        cluster_rows.append({
            "cluster_id": cl["cluster_id"],
            "rep_question": cnt[0][0] if cnt else cl["rep_norm"],
            "rep_norm": cl["rep_norm"],
            "size": len(members),
            "customer_count": len(custs),
            "group_count": len(grps),
            "first_seen": min(times) if times else 0,
            "last_seen": max(times) if times else 0,
        })

    db.set_cluster_ids(mapping)
    run_id = time.strftime("%Y%m%d%H%M%S")
    db.save_clusters(run_id, cluster_rows)

    log.info("聚类完成：%d 条问答 → %d 个问题簇", len(rows), len(cluster_rows))
    return {
        "sessions_scanned": len(sessions),
        "qa_pairs": total_pairs,
        "clusters": len(cluster_rows),
        "largest_cluster": max((c["size"] for c in cluster_rows), default=0),
    }


def stage_report(cfg: Settings | None = None, top_n: int = 20,
                 push: bool = True) -> dict[str, Any]:
    """三层聚合 + 可选推送飞书。"""
    cfg = cfg or settings()
    db = get_db(cfg)
    rows = [dict(r) for r in db.iter_qa_pairs()]
    if not rows:
        log.info("还没有问答记录，先跑 extract")
        return {}

    ov = E.overall(rows, top_n=top_n)
    groups = E.by_group(rows, top_n=top_n)
    customers = E.by_customer(rows, top_n=5)

    result = {"overall": ov, "groups": groups, "top_customers": customers[:10]}

    if push:
        sections = [
            ("总览", [
                f"问答记录：{ov['total_questions']} 条",
                f"涉及客户：{ov['customer_count']} 位",
                f"客户群/渠道：{ov['group_count']} 个",
                f"问题簇：{ov['cluster_count']} 个",
            ]),
            ("全局高频问题", [
                f"- {t['question']}（{t['count']} 次，{t['variants']} 种说法）"
                for t in ov["top"]
            ] or ["暂无"]),
        ]
        for g in groups[:5]:
            sections.append((f"客户群：{g['group_name']}（{g['total_questions']} 条 / {g['customer_count']} 人）", [
                f"- {t['question']}（{t['count']} 次）" for t in g["top"][:8]
            ] or ["暂无"]))
        sections.append(("问得最多的客户", [
            f"- {c['customer_name'] or c['customer_id']}：{c['total_questions']} 问 / {c['distinct_clusters']} 类，最高频「{c['top'][0]['question']}」"
            for c in customers[:10] if c["top"]
        ] or ["暂无"]))

        title = f"客户高频问题分析 {time.strftime('%Y-%m-%d')}"
        result["doc"] = DocxWriter(cfg.feishu).write_report(title, sections)

    return result


# ---------- 清洗 ----------

def stage_clean(cfg: Settings | None = None, limit: int = 200) -> dict[str, int]:
    cfg = cfg or settings()
    db = get_db(cfg)
    pipe = CleanPipeline(db, cfg.llm)
    return pipe.run(limit=limit, prompt_version=P.PROMPT_VERSION)


# ---------- 推送 ----------

def _require_feishu(cfg: Settings, to: str) -> None:
    """真发模式下先检查配置，别让「什么都没配」变成「静默成功」。"""
    f = cfg.feishu
    if f.dry_run:
        return
    missing: list[str] = []
    if to in ("all", "bot") and not f.webhook:
        missing.append("FEISHU_WEBHOOK（群机器人 webhook）")
    if to in ("all", "bitable") and not (f.app_id and f.app_secret):
        missing.append("FEISHU_APP_ID / FEISHU_APP_SECRET（应用凭证）")
    if to in ("all", "bitable") and not (f.bitable_app_token and f.bitable_table_id):
        missing.append("FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_TABLE_ID（目标多维表格）")
    if to in ("all", "doc") and not (f.app_id and f.app_secret):
        missing.append("FEISHU_APP_ID / FEISHU_APP_SECRET（建文档需要应用凭证）")
    if missing:
        raise RuntimeError(
            "飞书真发模式缺少配置：" + "；".join(missing)
            + "。先跑 `python run.py feishu-check` 看逐项状态，或把 FEISHU_DRY_RUN 设回 1。"
        )


def stage_push(cfg: Settings | None = None, limit: int = 500, to: str = "all",
               source: str = "pairs") -> dict[str, Any]:
    """推飞书。

    source="pairs"：零 API 主链路，推 qa_pair 明细（谁/何时/问了什么/原话）
    source="clean"：推 LLM 清洗结果（需要配 LLM_API_KEY）
    """
    _require_feishu(cfg or settings(), to)
    if source == "pairs":
        return _push_pairs(cfg, limit=limit, to=to)
    return _push_clean(cfg, limit=limit, to=to)


def _push_pairs(cfg: Settings | None = None, limit: int = 500,
                to: str = "all") -> dict[str, Any]:
    cfg = cfg or settings()
    db = get_db(cfg)
    rows = [dict(r) for r in db.iter_qa_pairs(limit=limit)]
    if not rows:
        log.info("还没有问答记录，先跑 extract")
        return {}

    out: dict[str, Any] = {}

    if to in ("all", "bitable"):
        out["bitable"] = BitableWriter(
            cfg.feishu, field_spec=PAIR_FIELD_SPEC,
            key_field="问答ID", mapper=pair_to_fields,
        ).upsert(rows)

    if to in ("all", "bot"):
        ov = E.overall(rows, top_n=5)
        groups = E.by_group(rows, top_n=10)
        out["bot"] = BotSender(cfg.feishu).send_pairs(ov, groups)

    if to in ("all", "doc"):
        out["doc"] = stage_report(cfg, top_n=20, push=True).get("doc", "")

    return out


def _push_clean(cfg: Settings | None = None, limit: int = 500,
                to: str = "all") -> dict[str, Any]:
    cfg = cfg or settings()
    db = get_db(cfg)
    rows = [dict(r) for r in db.iter_clean(P.PROMPT_VERSION, limit=limit)]
    if not rows:
        log.info("没有可推送的清洗结果")
        return {}

    # 补齐 raw_session 里的展示字段
    raw = {str(r["session_id"]): dict(r) for r in db.iter_sessions()}
    for r in rows:
        r.update({k: raw.get(r["session_id"], {}).get(k) for k in
                  ("start_ts", "from_type", "staff_name", "from_group")})

    out: dict[str, Any] = {}

    if to in ("all", "bitable"):
        out["bitable"] = BitableWriter(cfg.feishu).upsert(rows)

    if to in ("all", "bot"):
        top = _top_issues(rows)
        stats = {
            "candidates": len(rows),
            "noise": sum(1 for r in rows if r["category"] == "噪声"),
            "no_text": sum(1 for r in rows if r["category"] == "无正文"),
            "classified": sum(1 for r in rows if r["category"] not in ("噪声", "无正文")),
        }
        out["bot"] = BotSender(cfg.feishu).send_daily(stats, top)

    if to in ("all", "doc"):
        out["doc"] = _push_doc(cfg, rows)

    return out


def stage_demo(cfg: Settings | None = None, to: str = "all",
               limit: int = 500) -> dict[str, Any]:
    """造一批假会话 → 跑真实抽取聚类 → 按指定出口真推飞书。

    七鱼还没接的时候，用这个把「清洗整理 → 飞书」这一段先打通。
    数据是假的，但走的代码路径和真实数据完全一样。
    """
    cfg = cfg or settings()
    reset_db()
    r1 = stage_pull(cfg, days=30)
    r2 = stage_extract(cfg)
    r3 = stage_push(cfg, limit=limit, to=to, source="pairs")
    return {"pull": r1, "extract": r2, "push": r3, "stats": get_db(cfg).stats()}


def _top_issues(rows: list[dict[str, Any]], n: int = 5) -> list[tuple[str, int]]:
    counter: dict[str, int] = {}
    for r in rows:
        c = r.get("category") or "其他"
        if c in ("噪声", "无正文"):
            continue
        counter[c] = counter.get(c, 0) + 1
    return sorted(counter.items(), key=lambda x: -x[1])[:n]


def _push_doc(cfg: Settings, rows: list[dict[str, Any]]) -> str:
    valid = [r for r in rows if r.get("category") not in ("噪声", "无正文")]
    top = _top_issues(rows)
    avg = sum(int(r.get("quality_score") or 0) for r in valid) / max(1, len(valid))

    sections = [
        ("概览", [
            f"清洗会话总数：{len(rows)}",
            f"有效会话：{len(valid)}",
            f"平均质量分：{avg:.1f}",
        ]),
        ("Top 问题", [f"- {name}：{cnt} 条" for name, cnt in top] or ["暂无"]),
        ("高风险会话", [
            f"- {r['session_id']}｜{r.get('one_line') or r.get('demand') or '无摘要'}"
            for r in valid if r.get("risk") == "high"
        ][:20] or ["暂无"]),
    ]
    title = f"七鱼会话分析报告 {time.strftime('%Y-%m-%d')}"
    return DocxWriter(cfg.feishu).write_report(title, sections)


# ---------- 自检 ----------

def stage_doctor(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or settings()
    db = get_db(cfg)

    def mark(ok: bool) -> str:
        return "OK" if ok else "--"

    report = {
        "运行模式": "MOCK" if cfg.runtime.mock else "真实接口",
        "七鱼凭证": mark(bool(cfg.qiyu.app_key and cfg.qiyu.app_secret)),
        "LLM 凭证": mark(bool(cfg.llm.api_key)),
        "LLM DRY RUN": "是" if cfg.llm.dry_run else "否",
        "飞书群机器人": mark(bool(cfg.feishu.webhook)),
        "飞书应用": mark(bool(cfg.feishu.app_id and cfg.feishu.app_secret)),
        "多维表格": mark(bool(cfg.feishu.bitable_app_token and cfg.feishu.bitable_table_id)),
        "飞书 DRY RUN": "是" if cfg.feishu.dry_run else "否",
        "接收端": (f"{cfg.receiver.host}:{cfg.receiver.port}{cfg.receiver.path}"
                   f"（验签={'开' if cfg.receiver.verify else '关'}）"),
        "数据库": cfg.runtime.db_path,
        "数据库大小": f"{db.db_size_mb()} MB",
        "数据统计": db.stats(),
    }

    # ---- 保留窗口与水位（裁剪相关的运维视图）----
    days = cfg.runtime.retention_days
    report["保留窗口"] = f"{days} 天（自然日）" if days else "不裁剪"
    try:
        dry = db.rotate(window_days=days, dry_run=True) if days else {}
        if dry:
            report["下次裁剪"] = (f"将删 {dry['raw_session']} 会话 / "
                                f"{dry['raw_message']} 消息")
            if dry["orphan_session"]:
                report["裁剪警告"] = f"{dry['orphan_session']} 个会话未抽取即过期，检查 extract"
    except Exception as exc:  # 自检不该因为这一项挂掉
        report["下次裁剪"] = f"计算失败: {exc}"

    wm = db.get_watermark("session_last_end_ms")
    if wm:
        stale_days = (int(time.time() * 1000) - int(wm)) / 86_400_000
        report["取数水位"] = f"{stale_days:.1f} 天前"
        if days and stale_days > max(3, days // 3):
            report["水位警告"] = "取数已停滞，先别让裁剪继续跑"
    else:
        report["取数水位"] = "未初始化"

    return report


def stage_prune(cfg: Settings | None = None, message_days: int = 90,
                session_days: int = 0, vacuum: bool = False) -> dict[str, Any]:
    """清理历史原始数据，控制库体积。qa_pair / q_cluster 不动。"""
    cfg = cfg or settings()
    db = get_db(cfg)
    before = db.db_size_mb()
    removed = db.prune(message_days=message_days, session_days=session_days,
                       vacuum=vacuum)
    after = db.db_size_mb()
    log.info("清理完成：消息 %d 条 / 会话 %d 条，库 %.1f MB → %.1f MB",
             removed["raw_message"], removed["raw_session"], before, after)
    return {"removed": removed, "size_mb_before": before, "size_mb_after": after}


def stage_rotate(cfg: Settings | None = None, window_days: int = 0,
                 vacuum: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """滑动窗口裁剪：保留最近 N 个自然日，删掉更早的原始明细。

    window_days=0 时取配置里的 RETENTION_DAYS（默认 60）。
    顺序很重要：必须在 extract 之后跑，否则窗口内还没抽的会话会被误删。
    """
    cfg = cfg or settings()
    days = window_days or cfg.runtime.retention_days
    db = get_db(cfg)
    before = db.db_size_mb()
    r = db.rotate(window_days=days, dry_run=dry_run)

    if dry_run:
        log.info("[演练] 窗口 %d 天，将删消息 %d 条 / 会话 %d 条（当前 %.1f MB）",
                 days, r["raw_message"], r["raw_session"], before)
    else:
        if r["orphan_session"]:
            log.warning("窗口外有 %d 个会话没抽过问答就被裁掉了 —— "
                        "检查 extract 是否漏跑", r["orphan_session"])
        log.info("裁剪完成：窗口 %d 天，删消息 %d 条 / 会话 %d 条，库 %.1f MB → %.1f MB",
                 days, r["raw_message"], r["raw_session"], before, db.db_size_mb())

    if vacuum and not dry_run:
        db.prune(message_days=0, session_days=0, vacuum=True)

    return {**r, "size_mb_before": before, "size_mb_after": db.db_size_mb()}


def stage_backup(cfg: Settings | None = None, out_dir: str = "",
                 keep: int = 30) -> dict[str, Any]:
    """在线备份数据库，并轮转旧备份。不依赖 sqlite3 命令行工具。"""
    import time as _t
    from pathlib import Path as _P

    cfg = cfg or settings()
    db = get_db(cfg)
    out = _P(out_dir or "backups")
    stamp = _t.strftime("%Y%m%dT%H%M%SZ", _t.gmtime())
    dest = out / f"qiyu-{stamp}.db"
    info = db.backup(str(dest))

    removed = 0
    if keep > 0:
        olds = sorted(out.glob("qiyu-*.db"), reverse=True)[keep:]
        for f in olds:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    log.info("备份完成：%s，轮转删除 %d 个旧备份", info, removed)
    return {"file": info, "rotated_out": removed}


def stage_feishu_check(cfg: Settings | None = None, send: bool = False) -> dict[str, Any]:
    """飞书联通性自检：真发请求，把错误码翻译成「该去点哪里」。

    和 doctor 的分工：
      doctor        看配置填没填、库健不健康
      feishu-check  真的连一次，验证权限和授权关系

    群机器人会往群里发消息，所以只有 send=True 才真发；
    token / 多维表格 / 文档 都是只读调用，永远真跑。
    """
    cfg = cfg or settings()
    results = check_feishu(cfg.feishu, send=send)

    ok_count = sum(1 for r in results.values() if r["ok"])
    for name, r in results.items():
        flag = "OK  " if r["ok"] else "FAIL"
        log.info("[%s] %s — %s", flag, name, r["detail"])
        if not r["ok"] and r["fix"]:
            log.info("       → %s", r["fix"])

    return {
        "通过": f"{ok_count}/{len(results)}",
        "明细": results,
        "全部就绪": ok_count == len(results),
    }



def _agent_prompt(cfg: Settings, top_n: int = 15) -> str:
    """把三层聚合结果压成一段问题，交给智能体。"""
    db = get_db(cfg)
    rows = [dict(r) for r in db.iter_qa_pairs()]
    if not rows:
        return ""
    ov = E.overall(rows, top_n=top_n)
    groups = E.by_group(rows, top_n=top_n)

    lines = [
        f"这是我们客服系统最近 {cfg.runtime.retention_days} 天的会话分析结果，请据此给结论：",
        "",
        f"问答记录 {ov['total_questions']} 条，涉及 {ov['customer_count']} 位客户、"
        f"{ov['group_count']} 个客户群，归并为 {ov['cluster_count']} 类问题。",
        "",
        "全局高频问题：",
    ]
    for i, t in enumerate(ov["top"], 1):
        lines.append(f"{i}. {t['question']}（{t['count']} 次，{t['variants']} 种说法）")

    for g in groups[:3]:
        lines.append("")
        lines.append(f"客户群「{g['group_name']}」（{g['total_questions']} 条）：")
        for t in g["top"][:5]:
            lines.append(f"- {t['question']}（{t['count']} 次）")

    lines += [
        "",
        "请输出：",
        "1. 最该先解决的三个问题，按影响面排序，说明判断依据；",
        "2. 每个问题对应的产品/流程/话术动作，要具体可执行；",
        "3. 有没有被高频数字掩盖、但值得单独看的长尾问题。",
        "不要复述我给的数字，直接给结论。",
    ]
    return "\n".join(lines)


def stage_agent(cfg: Settings | None = None, ask: str = "", top_n: int = 15,
                timeout: int = 180, write_doc: bool = False) -> dict[str, Any]:
    """把七鱼的统计结果交给飞书 Aily 自定义智能体，拿回分析结论。

    这条是双向链路，和 push（单向写飞书）不同：
        本地聚合 → 智能体 → 回复文本 → 可选写回飞书文档

    前提：智能体「使用渠道」里开了 Open API 渠道，应用有 aily:agent_chat:write/read。
    """
    cfg = cfg or settings()
    if cfg.feishu.dry_run:
        prompt = ask or _agent_prompt(cfg, top_n)
        log.info("[DRY RUN] 不调用智能体。prompt 预览：%s", prompt[:500])
        return {"dry_run": True, "prompt": prompt}
    if not cfg.feishu.agent_id:
        raise RuntimeError("未配置 FEISHU_AGENT_ID —— 智能体编辑页地址栏里 agent_xxxx 那段")

    prompt = ask or _agent_prompt(cfg, top_n)
    if not prompt:
        return {"error": "没有问答记录，先跑 extract"}

    client = AgentClient(cfg.feishu)
    out = client.ask(prompt, timeout=timeout)
    log.info("智能体回复 %d 字，status=%s", len(out.get("text", "")), out.get("status"))

    if write_doc and out.get("text"):
        title = f"智能体分析 {time.strftime('%Y-%m-%d')}"
        body = [ln for ln in out["text"].splitlines() if ln.strip()]
        out["doc"] = DocxWriter(cfg.feishu).write_report(title, [("结论", body)])
    return out


def stage_all(cfg: Settings | None = None, limit: int = 200,
              rotate: bool = True) -> dict[str, Any]:
    """全流程。顺序是契约，不能调：

        pull → inspect → extract → report → clean → push → rotate

    裁剪必须是最后一步，而且必须带闸门 —— 它删的是原始明细，删错了补不回来。
    闸门有两条：
      1) 本轮一条会话都没拉到 → 跳过裁剪（凭证失效 / IP 白名单 / 接口异常时，
         数据其实没进来，此时裁剪等于主动丢数据）；
      2) 本轮抽取产出 0 条问答对，但库里还有会话没抽过 → 跳过裁剪
         （extract 出问题时先别删，等人看一眼）。
    """
    cfg = cfg or settings()
    r1 = stage_pull(cfg)
    r2 = stage_pull_inspect(cfg)
    r3 = stage_extract(cfg)
    r4 = stage_report(cfg, push=True)
    r5 = stage_clean(cfg, limit=limit)
    r6 = stage_push(cfg, limit=limit, source="pairs")

    out = {
        "pull": r1,
        "inspect": r2,
        "extract": r3,
        "report": {k: v for k, v in r4.items() if k != "groups"},
        "clean": r5,
        "push": r6,
    }

    if not rotate:
        log.info("已指定 --no-rotate，跳过裁剪")
        out["rotate"] = {"skipped": "manual"}
        return out

    if not r1.get("sessions"):
        log.warning("本轮没拉到任何会话，跳过裁剪 —— "
                    "先确认七鱼凭证和 IP 白名单，别在数据没进来的时候删数据")
        out["rotate"] = {"skipped": "no_new_sessions"}
        return out

    db = get_db(cfg)
    pending = len(db.sessions_without_pairs(1))
    if pending and not r3.get("qa_pairs"):
        log.warning("本轮抽取 0 条问答对，但库里还有未抽取的会话，跳过裁剪")
        out["rotate"] = {"skipped": "extract_produced_nothing"}
        return out

    r7 = stage_rotate(cfg)
    out["rotate"] = {k: v for k, v in r7.items() if k != "cutoff_ms"}
    return out
