"""飞书 Aily 自定义智能体（agent）客户端。

和 `bot` / `bitable` / `docx` 不同，这条是**双向**的：
我们把七鱼的统计结果作为问题发过去，智能体回一段分析，我们再把这段写回文档或群卡片。

接口（官方文档「服务端 API → 飞书 Aily → 自定义智能体」）：
    POST /aily/v1/agents/{agent_id}/chats                     发起对话（异步，返回 agent_chat_id）
    GET  /aily/v1/agents/{agent_id}/chats/{agent_chat_id}     获取对话结果（轮询）
    POST /aily/v1/agents/{agent_id}/sessions                  创建会话（多轮用）
    GET  /aily/v1/agents/{agent_id}/agent_visibility/check    校验可见性

前提（三条缺一不可）：
    1. 智能体后台 → 右上角「使用渠道」→ 打开 Open API 渠道
    2. 应用有 aily:agent_chat:write / aily:agent_chat:read 权限，且已发版
    3. 调用方与智能体同租户

agent_id 从智能体编辑页地址栏拿：
    https://xxx.feishu.cn/ai/custom_agent/agent_4k6wf15ngw7wu/builder/persona
                                          ^^^^^^^^^^^^^^^^^^
"""

from __future__ import annotations

import time
from typing import Any

from ..config import FeishuConfig
from ..log import get
from .auth import FeishuAuth

log = get("feishu.agent")

# 终态。Cancelled 也算结束，否则会一直轮询。
TERMINAL = {"Completed", "Failed", "Cancelled", "Canceled"}


class AgentClient:
    def __init__(self, cfg: FeishuConfig, auth: FeishuAuth | None = None):
        self.cfg = cfg
        self.auth = auth or FeishuAuth(cfg.app_id, cfg.app_secret)

    def _base(self) -> str:
        return f"/aily/v1/agents/{self.cfg.agent_id}"

    # ---------- 前置校验 ----------

    def visibility(self, channel_type: str = "web_sdk") -> dict[str, Any]:
        """校验「当前身份」在该渠道下能不能用这个智能体。"""
        return self.auth.call(
            "GET", f"{self._base()}/agent_visibility/check?channel_type={channel_type}"
        )

    # ---------- 会话 ----------

    def create_session(self, title: str = "") -> str:
        body = {"title": title} if title else {}
        data = self.auth.call("POST", f"{self._base()}/sessions", body=body)
        return data.get("session_id") or data.get("agent_chat_session_id", "")

    # ---------- 对话 ----------

    def start(self, text: str, session_id: str = "") -> dict[str, Any]:
        """发起一轮对话，立即返回。不阻塞。"""
        body: dict[str, Any] = {
            "user_message": {"content": [{"type": "text", "text": text}]},
        }
        if session_id:
            body["session_id"] = session_id
        return self.auth.call("POST", f"{self._base()}/chats", body=body)

    def result(self, chat_id: str) -> dict[str, Any]:
        data = self.auth.call("GET", f"{self._base()}/chats/{chat_id}")
        return {
            "status": data.get("status", ""),
            "finish_reason": data.get("finish_reason", ""),
            "content": data.get("content") or [],
        }

    def wait(self, chat_id: str, timeout: int = 180, interval: float = 2.0) -> dict[str, Any]:
        """轮询到终态。返回 {status, text, content, elapsed}。"""
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.result(chat_id)
            if last["status"] in TERMINAL or last["finish_reason"]:
                break
            time.sleep(interval)
        else:
            log.warning("智能体对话超时（%ds），返回最后一次结果 status=%s",
                        timeout, last.get("status"))

        texts = [c.get("text", "") for c in last.get("content", [])
                 if c.get("type") == "text" and c.get("text")]
        return {
            "status": last.get("status", "Timeout"),
            "text": "\n".join(texts),
            "content": last.get("content", []),
        }

    def ask(self, text: str, session_id: str = "",
            timeout: int = 180, interval: float = 2.0) -> dict[str, Any]:
        """发起 + 轮询，一步拿到回复。"""
        if not self.cfg.agent_id:
            raise RuntimeError("未配置 FEISHU_AGENT_ID（智能体编辑页地址栏里 agent_xxx 那段）")

        started = self.start(text, session_id=session_id)
        chat_id = started.get("agent_chat_id", "")
        if not chat_id:
            raise RuntimeError(f"发起对话没拿到 agent_chat_id: {started}")
        log.info("智能体对话已发起 agent_chat_id=%s", chat_id)

        out = self.wait(chat_id, timeout=timeout, interval=interval)
        out["agent_chat_id"] = chat_id
        out["session_id"] = started.get("session_id", session_id)
        return out
