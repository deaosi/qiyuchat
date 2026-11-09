#!/usr/bin/env python3
"""服务器部署前自检。不联网、不写文件、不依赖第三方库。

用法：
    python3 deploy/preflight.py

退出码 0 = 可以部署；1 = 有硬性阻塞项。
"""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import sys


OK = "  [OK]  "
WARN = "  [警告] "
FAIL = "  [阻塞] "

problems: list[str] = []
warnings: list[str] = []


def line(tag: str, text: str) -> None:
    print(tag + text)


def need(cond: bool, msg: str, hint: str = "") -> None:
    if cond:
        line(OK, msg)
    else:
        line(FAIL, msg + (f"  → {hint}" if hint else ""))
        problems.append(msg)


def warn(cond: bool, msg: str, hint: str = "") -> None:
    if cond:
        line(OK, msg)
    else:
        line(WARN, msg + (f"  → {hint}" if hint else ""))
        warnings.append(msg)


def mem_mb() -> float:
    """总内存（MB）。Linux 读 /proc/meminfo，其他平台返回 0。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("MemTotal:"):
                    return int(ln.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def disk_free_gb(path: str) -> float:
    try:
        return shutil.disk_usage(path).free / 1024 ** 3
    except OSError:
        return 0.0


def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 62)
    print(" 七鱼会话分析 · 服务器部署自检")
    print("=" * 62)

    print("\n[1] 运行环境")
    line(OK, f"系统      {platform.system()} {platform.release()}  {platform.machine()}")
    py = sys.version_info
    line(OK, f"Python    {py.major}.{py.minor}.{py.micro}")
    need(py >= (3, 7), "Python >= 3.7",
         "代码用了 from __future__ import annotations 和 dataclass")

    sq = sqlite3.sqlite_version_info
    line(OK, f"SQLite    {sqlite3.sqlite_version}")
    need(sq >= (3, 24), "SQLite >= 3.24（upsert 语法）",
         "CentOS 7 自带 3.7.17 不满足，需升级或换发行版")

    print("\n[2] 资源")
    total = mem_mb()
    if total:
        need(total >= 512, f"内存      {total:.0f} MB",
             "纯规则版常驻 < 200MB，512MB 是最低线")
        warn(total >= 1024, "内存 >= 1GB（留给后续 embedding 升级）", "不影响当前功能")
    else:
        line(WARN, "内存      非 Linux，跳过检测")

    free = disk_free_gb(here)
    if free:
        need(free >= 1, f"磁盘剩余  {free:.1f} GB（{here}）",
             "日志 + SQLite 增长，建议留 2GB 以上")

    print("\n[3] 依赖")
    need(True, "第三方依赖 无（纯标准库）")
    for mod, label in (("text2vec", "可选：语义向量"),
                       ("sklearn", "可选：层次聚类"),
                       ("jieba", "可选：中文分词")):
        try:
            __import__(mod)
            line(OK, f"{label} 已安装（{mod}）")
        except ImportError:
            line(WARN, f"{label} 未安装（{mod}）—— 当前功能不需要")

    # sqlite3 命令行工具：只影响手工 .backup，不影响程序运行（程序走 Python API）
    if shutil.which("sqlite3"):
        line(OK, "sqlite3 命令行  可用")
    else:
        line(WARN, "sqlite3 命令行  未安装 —— 用 python3 run.py backup 代替")

    # pip：零依赖不需要，但后续上 B 档（语义向量）时需要
    try:
        __import__("pip")
        line(OK, "pip  可用（后续装可选依赖时需要）")
    except ImportError:
        line(WARN, "pip  未安装 —— 当前不需要，后续上语义向量再装")

    print("\n[4] 时区与时间")
    tz = os.environ.get("TZ") or "未设置（跟随系统）"
    line(OK, f"TZ        {tz}")
    warn(True, "七鱼签名要求时间偏差 < 5 分钟，确认已开启 NTP", "chronyd / systemd-timesyncd")

    print("\n[5] 权限")
    need(os.access(here, os.W_OK), f"工作目录可写  {here}", "需要创建 data/ 和 logs/")
    data_dir = os.path.join(here, "data")
    need(not os.path.exists(data_dir) or os.access(data_dir, os.W_OK),
         "data/ 可写" if os.path.exists(data_dir) else "data/ 待创建")

    print("\n" + "=" * 62)
    if problems:
        print(f" 结论：{len(problems)} 项阻塞，先解决再部署")
        for p in problems:
            print(f"   - {p}")
        return 1
    if warnings:
        print(f" 结论：可以部署（{len(warnings)} 项提示不影响运行）")
    else:
        print(" 结论：可以部署")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
