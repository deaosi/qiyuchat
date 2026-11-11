"""会话记录导出：POST /openapi/export/session

官方流程（开发指南 hFBnNYw5ui「获取会话记录」）是**三段式**，不是一个请求：

1. 建任务  POST /openapi/export/session
           body {"start":ms,"end":ms,"type":0}
           → {"code":200,"message":"<key>"}          message 是任务 key，不是数据
2. 查任务  POST /openapi/export/session/check
           body {"key":"<key>"}
           → {"code":200,"message":"<httpUrl>"}      下载链接
           → {"code":14403,"message":"Wait..."}      还没跑完，继续轮询
3. 下数据  GET  <httpUrl>  → zip
           解压密码 = appkey 的前 12 个字符；链接 6 小时有效；解压得两个 txt

官方限制：时间跨度最大 5 天；同一企业同一时间只能有 1 个批量导出任务；
24 小时内最多 1888 个导出任务；相同参数 24 小时内并发 1 次。

注意：这个接口返回的是**会话级元数据**（谁、什么时候、哪个客服、多久、满意度、
分类、关联工单……），不含逐句聊天正文。要正文只能走质检导出。
"""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from typing import Any

from ..config import QiyuConfig
from ..http import request
from ..log import get
from .common import QiyuError, ensure_ok
from .message import source_integer, validate_messages
from .sign import signed_query

log = get("qiyu.session")

ENDPOINT = "/openapi/export/session"
CHECK_ENDPOINT = "/openapi/export/session/check"
ONE_ENDPOINT = "/openapi/export/session/one"
MESSAGE_ENDPOINT = "/openapi/export/session/one/message"

# 判定「哪个 txt 是会话表」用的表头特征
_SESSION_MARKERS = ("starttime", "endtime", "会话开始时间", "会话id", "stype")
_SESSION_ID_MARKERS = ("id", "会话id", "sessionid")


class SessionExporter:
    def __init__(self, cfg: QiyuConfig, poll_interval: float = 3.0, max_wait: float = 600.0, *, retries: int = 3):
        self.cfg = cfg
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.retries = retries
        self.last_files: dict[str, bytes] = {}

    # ---------- 1. 建任务 ----------

    def create_task(self, start_ms: int, end_ms: int, type_: int = 0) -> str:
        span_days = (end_ms - start_ms) / 86_400_000
        if span_days > self.cfg.session_window_days + 0.01:
            raise ValueError(f"时间跨度 {span_days:.2f} 天超过上限 {self.cfg.session_window_days} 天")

        body = {"start": int(start_ms), "end": int(end_ms), "type": int(type_)}
        qs, raw = signed_query(self.cfg.app_key, self.cfg.app_secret, body)
        url = f"{self.cfg.base_url}{ENDPOINT}?{qs}"

        log.info("建导出任务 %s → %s (type=%s)", _fmt(start_ms), _fmt(end_ms), type_)
        data = ensure_ok(request("POST", url, body=raw, timeout=self.cfg.timeout), ENDPOINT)

        key = ""
        if isinstance(data, dict):
            key = str(data.get("message") or data.get("key") or "")
        if not key:
            raise QiyuError(-1, f"建任务响应里没有 key：{data!r}", ENDPOINT)
        log.info("任务已建，key=%s", key)
        return key

    # ---------- 2. 轮询 ----------

    def wait_task(self, key: str) -> str:
        deadline = time.monotonic() + self.max_wait
        attempt = 0
        while True:
            attempt += 1
            body = {"key": key}
            qs, raw = signed_query(self.cfg.app_key, self.cfg.app_secret, body)
            url = f"{self.cfg.base_url}{CHECK_ENDPOINT}?{qs}"
            resp = request("POST", url, body=raw, timeout=self.cfg.timeout)

            code = _code_of(resp)
            message = str((resp or {}).get("message") or "") if isinstance(resp, dict) else ""

            if code == 200 and message.startswith("http"):
                log.info("任务完成（第 %d 次轮询），链接已拿到", attempt)
                return message
            if code == 14403:
                if time.monotonic() > deadline:
                    raise QiyuError(14403, f"等待超过 {self.max_wait:.0f}s 任务仍未完成", CHECK_ENDPOINT)
                log.info("任务未完成（%s），%.1fs 后再查", message or "Wait...", self.poll_interval)
                time.sleep(self.poll_interval)
                continue
            raise QiyuError(code, message or f"未知响应 {resp!r}", CHECK_ENDPOINT)

    # ---------- 3. 下载 + 解压 ----------

    def download(self, url: str) -> bytes:
        blob = request("GET", url, timeout=max(self.cfg.timeout, 120), raw=True)
        if not isinstance(blob, (bytes, bytearray)):
            raise QiyuError(-1, "下载导出文件失败：返回的不是二进制", ENDPOINT)
        log.info("已下载 %.1f KB", len(blob) / 1024)
        return bytes(blob)

    def unzip(self, blob: bytes) -> dict[str, bytes]:
        """解压导出包。密码固定是 appkey 的前 12 个字符。"""
        pwd = (self.cfg.app_key or "")[:12].encode("utf-8")
        files: dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for name in zf.namelist():
                    if name.endswith("/"):
                        continue
                    try:
                        files[name] = zf.read(name, pwd=pwd)
                    except RuntimeError as exc:  # 密码错 / 加密算法不支持
                        raise QiyuError(
                            -1,
                            f"解压 {name} 失败：{exc}（密码应为 appkey 前 12 位 = {pwd.decode()}）",
                            ENDPOINT,
                        ) from exc
                    except NotImplementedError as exc:
                        raise QiyuError(-1, f"{name} 使用了不支持的压缩/加密算法：{exc}", ENDPOINT) from exc
        except zipfile.BadZipFile as exc:
            raise QiyuError(-1, f"下载到的不是 zip：{exc}", ENDPOINT) from exc
        self.last_files = files
        log.info("解压出 %d 个文件：%s", len(files), ", ".join(files))
        return files

    # ---------- 4. 解析 ----------

    @staticmethod
    def parse_txt(blob: bytes) -> list[dict[str, Any]]:
        """把导出的 txt 解析成字典列表。

        实测（2026-11）：七鱼实际下发的是**一行一个 JSON**，文件名固定
        `session.txt` / `message.txt`。早期文档与 SDK 里写的是 tab 分隔，
        所以两种都要认。
        """
        text = _decode(blob)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return []

        if lines[0].lstrip().startswith("{"):
            rows: list[dict[str, Any]] = []
            for ln in lines:
                try:
                    obj = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
            if rows:
                return rows

        header_line = lines[0]
        delimiter = "\t" if header_line.count("\t") >= header_line.count(",") else ","
        reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]

    def pick_session_file(self, parsed: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """两个 txt 里挑出「会话表」。"""
        best: list[dict[str, Any]] = []
        best_score = -1
        for name, rows in parsed.items():
            if not rows:
                continue
            # 文件名是强信号：session.txt 就是会话表
            base = str(name).rsplit("/", 1)[-1].lower()
            if "session" in base and "message" not in base:
                log.info("  %s：按文件名判定为会话表（%d 行）", name, len(rows))
                return rows
            cols = [c.lower() for c in rows[0].keys()]
            score = sum(1 for m in _SESSION_MARKERS if any(m in c for c in cols))
            score += 2 * sum(1 for m in _SESSION_ID_MARKERS if any(m == c for c in cols))
            log.info("  %s：%d 行，表头 %d 列，会话表得分 %d", name, len(rows), len(cols), score)
            if score > best_score:
                best, best_score = rows, score
        return best

    # ---------- 对外 ----------

    def export_bundle(self, start_ms: int, end_ms: int,
                      type_: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """建任务 → 轮询 → 下载 → 解压 → 解析，返回 (会话表, 消息表)。

        这是唯一能拿到「全量会话 + 结构化消息正文」的接口：
        `session.txt` 是 60+ 字段元数据，`message.txt` 是逐条消息（含 `from` 区分
        访客/客服、`mType`、`msg` 正文）。
        """
        key = self.create_task(start_ms, end_ms, type_=type_)
        url = self.wait_task(key)
        blob = self.download(url)
        files = self.unzip(blob)

        sessions: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        others: dict[str, list[dict[str, Any]]] = {}
        for name, content in files.items():
            base = str(name).rsplit("/", 1)[-1].lower()
            rows = self.parse_txt(content)
            if "message" in base:
                messages.extend(rows)
            elif "session" in base:
                sessions.extend(rows)
            else:
                others[name] = rows          # 文件名看不出类型 → 攒起来统一判
        if not sessions and others:
            sessions = self.pick_session_file(others)
        log.info("本次返回 %d 条会话 / %d 条消息", len(sessions), len(messages))
        return sessions, messages

    def export(self, start_ms: int, end_ms: int, type_: int = 0) -> list[dict[str, Any]]:
        """只要会话表的旧接口，保留兼容。"""
        sessions, _ = self.export_bundle(start_ms, end_ms, type_=type_)
        return sessions

    def export_one(self, session_id: int) -> dict[str, Any]:
        """按会话 ID 取单条详情。官方限制 5 次/秒。"""
        body = {"sessionId": int(session_id)}
        qs, raw = signed_query(self.cfg.app_key, self.cfg.app_secret, body)
        url = f"{self.cfg.base_url}{ONE_ENDPOINT}?{qs}"
        data = ensure_ok(request("POST", url, body=raw, timeout=self.cfg.timeout, retries=self.retries), ONE_ENDPOINT)
        return data if isinstance(data, dict) else {}


    def export_messages(self, session_id: int) -> list[dict[str, Any]]:
        """Read source messages; caller limits rate. Only official data:list is accepted."""
        if not source_integer(session_id, positive=True):
            raise ValueError("会话 ID 须为正整数")
        qs, raw = signed_query(self.cfg.app_key, self.cfg.app_secret, {"sessionId": session_id})
        data = ensure_ok(request("POST", f"{self.cfg.base_url}{MESSAGE_ENDPOINT}?{qs}",
                                 body=raw, timeout=self.cfg.timeout, retries=self.retries), MESSAGE_ENDPOINT)
        return validate_messages(data, session_id)


def _code_of(resp: Any) -> int:
    if not isinstance(resp, dict):
        return -1
    try:
        return int(resp.get("code", -1))
    except (TypeError, ValueError):
        return -1


def _decode(blob: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", errors="replace")


def _fmt(ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
