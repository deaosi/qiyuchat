"""既有质检结果导出连接器，读取导出表中的「会话内容」。

官方另有单会话消息接口；本模块保留原有路径，不在本地看板中调用。

流程是异步的两步：
  1. POST /openapi/chat/inspect/result/download  → 返回 key
  2. POST /openapi/chat/inspect/result/check     → 返回下载链接，或 14403 Wait...
  3. 下载 xlsx → 解析 → 取「会话内容」列

限制：时间跨度最大 2 天；只导出**被质检任务扫过**的会话，覆盖率取决于租户配置。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from ..config import QiyuConfig
from ..http import request
from ..log import get
from ..xlsx import read_table, to_dicts
from .common import QiyuError, ensure_ok
from .sign import signed_query

log = get("qiyu.inspect")

EP_CREATE = "/openapi/chat/inspect/result/download"
EP_CHECK = "/openapi/chat/inspect/result/check"

CONTENT_KEYS = ("会话内容", "聊天内容", "对话内容", "content", "sessionContent")
SESSION_KEYS = ("会话ID", "会话id", "sessionId", "session_id")


class InspectExporter:
    def __init__(self, cfg: QiyuConfig):
        self.cfg = cfg

    # ---------- 对外 ----------

    def export(self, start_ms: int, end_ms: int, interaction: int = 0,
               **filters: Any) -> list[dict[str, Any]]:
        """一步到位：建任务 → 轮询 → 下载 → 解析成行字典列表。"""
        span_days = (end_ms - start_ms) / 86_400_000
        if span_days > self.cfg.inspect_window_days + 0.01:
            raise ValueError(f"质检导出跨度 {span_days:.2f} 天超过上限 {self.cfg.inspect_window_days} 天")

        key = self.create_task(start_ms, end_ms, interaction, **filters)
        url = self.wait_for(key)
        log.info("质检导出就绪，开始下载")
        data = request("GET", url, timeout=max(self.cfg.timeout, 120), raw=True)
        rows = to_dicts(read_table(data))
        log.info("质检导出解析出 %d 行", len(rows))
        return rows

    def create_task(self, start_ms: int, end_ms: int, interaction: int = 0,
                    **filters: Any) -> str:
        body: dict[str, Any] = {
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "interaction": int(interaction),
        }
        for k in ("staffIdList", "kefuGroupIdList", "fromGroupList",
                  "inspectStartTime", "inspectEndTime", "templateCode", "taskIds"):
            if filters.get(k):
                body[k] = filters[k]

        qs, raw = signed_query(self.cfg.app_key, self.cfg.app_secret, body)
        resp = request("POST", f"{self.cfg.base_url}{EP_CREATE}?{qs}",
                       body=raw, timeout=self.cfg.timeout)
        data = ensure_ok(resp, EP_CREATE)
        key = data.get("key") if isinstance(data, dict) else None
        key = key or (resp.get("message") if isinstance(resp, dict) else None)
        if not key:
            raise QiyuError(-1, f"建任务没拿到 key: {resp}", EP_CREATE)
        log.info("质检导出任务已创建 key=%s", key)
        return str(key)

    def wait_for(self, key: str) -> str:
        """轮询直到拿到下载链接。14403 = 还没跑完。"""
        for i in range(1, self.cfg.poll_max_times + 1):
            body = {"key": key}
            qs, raw = signed_query(self.cfg.app_key, self.cfg.app_secret, body)
            resp = request("POST", f"{self.cfg.base_url}{EP_CHECK}?{qs}",
                           body=raw, timeout=self.cfg.timeout)

            code = int(resp.get("code") or 0) if isinstance(resp, dict) else 0
            msg = str(resp.get("message") or "") if isinstance(resp, dict) else ""

            if code == 200 and msg.startswith("http"):
                return msg
            if code == 14403:
                log.info("任务未完成(%d/%d)，%ds 后重试", i, self.cfg.poll_max_times,
                         self.cfg.poll_interval)
                time.sleep(self.cfg.poll_interval)
                continue
            raise QiyuError(code, msg, EP_CHECK)

        raise QiyuError(14403, f"轮询 {self.cfg.poll_max_times} 次仍未完成，key={key}", EP_CHECK)

    # ---------- 解析 ----------

    @staticmethod
    def pick(row: dict[str, str], keys: tuple[str, ...]) -> str:
        for k in keys:
            if k in row and row[k]:
                return row[k]
        # 宽松匹配：表头可能带空格/括号
        for h, v in row.items():
            hn = h.replace(" ", "")
            for k in keys:
                if k in hn and v:
                    return v
        return ""

    @classmethod
    def split_transcript(cls, raw: str, staff_name: str = "") -> list[dict[str, Any]]:
        """把「会话内容」这一大坨文本拆成 [{role, content}]。

        七鱼导出格式不保证稳定，这里用启发式：
        - 按行切
        - 每行尝试剥掉时间戳前缀
        - 冒号前是说话人，按名字/关键词判定 role
        """
        if not raw:
            return []
        text = raw.replace("<br/>", "\n").replace("<br>", "\n").replace("\\n", "\n")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

        ts_re = re.compile(r"^\[?(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\]?\s*")
        # JSON field separators are not speaker separators. Keep the full payload
        # until extract_pairs projects it to text, preserving the speaker turn.
        speak_re = re.compile(r"^([^:：{}\[\]\"]{1,24})[:：]\s*(.*)$")

        out: list[dict[str, Any]] = []
        for ln in lines:
            stamp = ts_re.match(ln)
            ts = 0
            if stamp:
                try:
                    ts = int(datetime.fromisoformat(stamp[1].replace("/", "-")).timestamp() * 1000)
                except (ValueError, OverflowError, OSError):
                    pass
            ln = ts_re.sub("", ln)
            m = speak_re.match(ln)
            if not m:
                if out:
                    out[-1]["content"] += " " + ln
                else:
                    out.append({"role": "user", "content": ln, "ts": ts})
                continue
            who, said = m.group(1).strip(), m.group(2).strip()
            out.append({"role": _role_of(who, staff_name), "content": said, "ts": ts})
        return out


def _role_of(who: str, staff_name: str) -> str:
    if staff_name and who == staff_name:
        return "staff"
    if any(k in who for k in ("客户", "访客", "用户", "买家")):
        return "user"
    if any(k in who for k in ("机器人", "系统", "bot", "Bot")):
        return "bot"
    if any(k in who for k in ("客服", "坐席")):
        return "staff"
    return "user" if staff_name else "staff"
