#!/usr/bin/env python3
"""统一入口。

用法：
    python run.py doctor                 自检：配置、DB、权限
    python run.py pull --days 7          拉会话记录
    python run.py inspect --days 2       拉质检导出（正文）
    python run.py extract --limit 0      抽问答对 + 聚类（零 API，纯规则）
    python run.py report --top 20        三层高频报告 → 飞书文档
    python run.py clean --limit 200      规则 + AI 清洗
    python run.py push --to all          推送飞书（bitable / bot / doc）
    python run.py push --source clean   推 LLM 清洗结果（需 LLM_API_KEY）
    python run.py rotate --window 60     滑动窗口裁剪：留最近 60 个自然日
    python run.py prune --message-days 90  按小时精度清理（一般用 rotate 就够）
    python run.py backup --out backups   在线备份数据库（不需要 sqlite3 命令）
    python run.py llm-check              模型网关联通性自检（自有网关先跑这个）
    python run.py feishu-check           飞书联通性自检（真连一次，报错带修法）
    python run.py agent --ask "..."      把问题交给飞书 Aily 智能体，拿回分析
    python run.py all --limit 200        全流程
    python run.py mock                   用内置假数据跑一遍全链路（不碰任何外部接口）
    python run.py mock --real-feishu     假数据，但真推飞书（没接七鱼时先验飞书这一段）
    python run.py demo --to bot          造假数据 → 真实抽取 → 真发群卡片（打通飞书用）
    python run.py serve --open           本地看板（默认 http://127.0.0.1:8848）
    python run.py receiver               接收七鱼「消息与事件接收url」推送（默认 :8860）

环境变量见 .env.example。MOCK=1 时全部走假数据。
部署见 deploy/README.md（先跑 python3 deploy/preflight.py 自检）。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from src.clean.llmcheck import check as llm_check
from src.clean.llmcheck import format_report
from src.app import (
    get_db,
    init_logging,
    stage_all,
    stage_clean,
    stage_doctor,
    stage_extract,
    stage_pull,
    stage_backup,
    stage_pull_inspect,
    stage_prune,
    stage_push,
    stage_report,
    stage_rotate,
    stage_feishu_check,
    stage_agent,
)
from src.config import settings


def _pp(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qiyu-pipeline",
        description="七鱼会话取数 → AI 清洗 → 飞书呈现",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # 每个子命令都能带 --mock，写起来顺手（run.py extract --mock）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mock", action="store_true",
                        help="读 mock 数据库（复查假数据结果）")
    common.add_argument("--real-feishu", action="store_true",
                        help="配合 --mock：数据用假数据，但飞书真发（验证飞书配置用）")

    sub.add_parser("doctor", help="自检配置与数据库", parents=[common])

    sp = sub.add_parser("pull", help="拉会话记录", parents=[common])
    sp.add_argument("--days", type=int, default=0, help="回溯天数，0=从水位续跑")

    si = sub.add_parser("inspect", help="拉质检导出（拿正文）", parents=[common])
    si.add_argument("--days", type=int, default=2, help="回溯天数（上限 2）")

    sc = sub.add_parser("clean", help="规则 + AI 清洗", parents=[common])
    sc.add_argument("--limit", type=int, default=200)

    se = sub.add_parser("extract", help="抽问答记录 + 高频问题聚类（零 API 调用）", parents=[common])
    se.add_argument("--limit", type=int, default=0, help="处理多少个新会话，0=全部")
    se.add_argument("--threshold", type=float, default=0.45, help="相似度阈值，越小簇越大")
    se.add_argument("--rebuild", action="store_true", help="忽略已处理，全量重抽")

    sr = sub.add_parser("report", help="三层聚合报告", parents=[common])
    sr.add_argument("--top", type=int, default=20)
    sr.add_argument("--no-push", action="store_true", help="只算不推飞书")

    su = sub.add_parser("push", help="推送飞书", parents=[common])
    su.add_argument("--limit", type=int, default=500)
    su.add_argument("--to", choices=["all", "bitable", "bot", "doc"], default="all")
    su.add_argument("--source", choices=["pairs", "clean"], default="pairs",
                    help="pairs=问答明细（零 API，默认）；clean=LLM 清洗结果")

    sa = sub.add_parser("all", help="全流程：pull→inspect→extract→report→clean→push→rotate",
                        parents=[common])
    sa.add_argument("--no-rotate", action="store_true",
                    help="跳过末尾的滑动窗口裁剪（先跑几天看看时用）")
    sa.add_argument("--limit", type=int, default=200)
    sa.add_argument("--days", type=int, default=0)

    sub.add_parser("mock", help="内置假数据跑全链路", parents=[common])

    sd = sub.add_parser("demo", help="假数据 + 真推飞书（七鱼没接时先打通飞书这一段）",
                        parents=[common])
    sd.add_argument("--to", choices=["all", "bitable", "bot", "doc"], default="all")
    sub.add_parser("stats", help="看数据库统计", parents=[common])

    sv = sub.add_parser("serve", help="启动本地看板（浏览器里看数据）", parents=[common])
    sv.add_argument("--host", default="127.0.0.1", help="监听地址，默认只本机可访问")
    sv.add_argument("--port", type=int, default=8848)
    sv.add_argument("--open", action="store_true", help="自动打开浏览器")
    sv.add_argument("--strict-port", action="store_true",
                    help="端口被占用时直接报错退出，不自动换端口（服务器部署必须开）")

    svc = sub.add_parser(
        "receiver",
        help="接收七鱼「消息与事件接收url」推送（OpenApi 渠道事件）",
        parents=[common],
    )
    svc.add_argument("--host", default="", help="监听地址，默认用 RECEIVER_HOST（127.0.0.1）")
    svc.add_argument("--port", type=int, default=0, help="端口，默认用 RECEIVER_PORT（8860）")
    svc.add_argument("--path", default="", help="对外路径，默认 /qiyu/event，必须和后台填的一致")
    svc.add_argument("--no-verify", action="store_true",
                     help="关掉 checksum 校验（仅本机联调用，生产别关）")
    svc.add_argument("--no-auto-session", action="store_true",
                     help="MSG 早于 SESSION_START 时不自动建 live- 会话，直接丢弃")

    sp = sub.add_parser("prune", help="清理历史原始数据（控制库体积）", parents=[common])
    sp.add_argument("--message-days", type=int, default=90,
                    help="删掉多少天前的原始消息，0=不删（默认 90）")
    sp.add_argument("--session-days", type=int, default=0,
                    help="删掉多少天前的会话记录，0=不删（默认不删）")
    sp.add_argument("--vacuum", action="store_true",
                    help="清理后回收磁盘（会锁库，耗时随库大小增长）")

    sr = sub.add_parser("rotate", help="滑动窗口裁剪（保留最近 N 个自然日）", parents=[common])
    sr.add_argument("--window", type=int, default=0,
                    help="保留多少个自然日，0=用 RETENTION_DAYS（默认 60）")
    sr.add_argument("--vacuum", action="store_true", help="裁剪后回收磁盘")
    sr.add_argument("--dry-run", action="store_true", help="只统计不删")

    sb = sub.add_parser("backup", help="在线备份数据库（不需要 sqlite3 命令）", parents=[common])
    sb.add_argument("--out", default="backups", help="备份目录，默认 ./backups")
    sb.add_argument("--keep", type=int, default=30, help="保留最近几个备份，默认 30")

    sl = sub.add_parser("llm-check", help="模型网关联通性自检（自有网关必备）",
                        parents=[common])
    sl.add_argument("--model", default="", help="指定要测的模型名，默认用 LLM_MODEL_MAIN")
    sl.add_argument("--json", action="store_true", help="输出原始 JSON，便于脚本判断")

    sf = sub.add_parser("feishu-check", help="飞书联通性自检（真发请求，报错翻译成怎么修）",
                        parents=[common])
    sf.add_argument("--send", action="store_true",
                    help="真往群里发一条测试卡片（不加只验证配置）")

    sg = sub.add_parser("agent", help="把高频问题交给飞书 Aily 智能体，拿回分析结论",
                        parents=[common])
    sg.add_argument("--ask", default="", help="直接问一句话；留空则自动用三层聚合结果组 prompt")
    sg.add_argument("--top", type=int, default=15, help="自动组 prompt 时带几个高频问题")
    sg.add_argument("--timeout", type=int, default=180, help="等智能体回复的秒数")
    sg.add_argument("--doc", action="store_true", help="把回复写成飞书文档")

    return p


def _with_mock_db(cfg, real_feishu: bool = False):
    """把配置指向 mock.db，方便单独跑 extract/report 看假数据。

    real_feishu=True 时不强制飞书 dry run —— 用来在没接七鱼之前先验证飞书这一段。
    """
    from dataclasses import replace
    from pathlib import Path

    mock_db = str(Path(cfg.runtime.db_path).with_name("mock.db"))
    return replace(
        cfg,
        runtime=replace(cfg.runtime, mock=True, db_path=mock_db),
        llm=replace(cfg.llm, dry_run=True),
        feishu=replace(cfg.feishu, dry_run=not real_feishu),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = settings()
    if args.mock:
        cfg = _with_mock_db(cfg, real_feishu=args.real_feishu)
    init_logging(cfg)

    try:
        if args.cmd == "doctor":
            _pp(stage_doctor(cfg))

        elif args.cmd == "pull":
            _pp(stage_pull(cfg, days=args.days))

        elif args.cmd == "inspect":
            _pp(stage_pull_inspect(cfg, days=args.days))

        elif args.cmd == "clean":
            _pp(stage_clean(cfg, limit=args.limit))

        elif args.cmd == "extract":
            _pp(stage_extract(cfg, limit=args.limit, threshold=args.threshold,
                              rebuild=args.rebuild))

        elif args.cmd == "report":
            _pp(stage_report(cfg, top_n=args.top, push=not args.no_push))

        elif args.cmd == "push":
            _pp(stage_push(cfg, limit=args.limit, to=args.to, source=args.source))

        elif args.cmd == "all":
            _pp(stage_all(cfg, limit=args.limit, rotate=not args.no_rotate))

        elif args.cmd == "mock":
            _pp(_run_mock(cfg, real_feishu=args.real_feishu))

        elif args.cmd == "demo":
            from src.app import stage_demo
            # demo 永远写 mock.db 并真发飞书，不碰生产库
            _pp(stage_demo(_with_mock_db(cfg, real_feishu=True), to=args.to))

        elif args.cmd == "stats":
            _pp(get_db(cfg).stats())

        elif args.cmd == "serve":
            from src.web import serve
            serve(cfg, host=args.host, port=args.port, open_browser=args.open,
                  strict_port=args.strict_port)

        elif args.cmd == "receiver":
            from src.receiver import serve_forever
            rc = cfg.receiver
            serve_forever(
                get_db(cfg),
                host=args.host or rc.host,
                port=args.port or rc.port,
                path=args.path or rc.path,
                app_secret=rc.secret or cfg.qiyu.app_secret,
                verify=rc.verify and not args.no_verify,
                auto_session=not args.no_auto_session,
            )

        elif args.cmd == "prune":
            _pp(stage_prune(cfg, message_days=args.message_days,
                            session_days=args.session_days, vacuum=args.vacuum))

        elif args.cmd == "rotate":
            _pp(stage_rotate(cfg, window_days=args.window,
                             vacuum=args.vacuum, dry_run=args.dry_run))

        elif args.cmd == "backup":
            _pp(stage_backup(cfg, out_dir=args.out, keep=args.keep))

        elif args.cmd == "agent":
            _pp(stage_agent(cfg, ask=args.ask, top_n=args.top,
                            timeout=args.timeout, write_doc=args.doc))

        elif args.cmd == "llm-check":
            result = llm_check(cfg, model=args.model)
            if args.json:
                _pp(result)
            else:
                print(format_report(result))
            return 0 if result.get("verdict") in ("可用", "跳过") else 1

        elif args.cmd == "feishu-check":
            out = stage_feishu_check(cfg, send=args.send)
            _pp(out)
            return 0 if out["全部就绪"] else 1

        return 0

    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as e:                              # noqa: BLE001
        print(f"\n[失败] {type(e).__name__}: {e}", file=sys.stderr)
        if cfg.runtime.log_level == "DEBUG":
            traceback.print_exc()
        return 1


def _run_mock(cfg, real_feishu: bool = False) -> dict:
    """强制走假数据，一次跑完全链路。用于本地验证和 CI。

    不修改环境变量、不 reload 模块 —— 直接构造一份 mock 版配置传下去。
    """
    from dataclasses import replace
    from pathlib import Path

    import src.app as A

    mock_db = str(Path(cfg.runtime.db_path).with_name("mock.db"))
    cfg2 = replace(
        cfg,
        runtime=replace(cfg.runtime, mock=True, db_path=mock_db),
        llm=replace(cfg.llm, dry_run=True),
        feishu=replace(cfg.feishu, dry_run=not real_feishu),
    )
    A.reset_db()
    out = A.stage_all(cfg2, limit=100)
    out["stats"] = A.get_db(cfg2).stats()
    return out


if __name__ == "__main__":
    raise SystemExit(main())
