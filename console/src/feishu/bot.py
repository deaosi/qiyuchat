"""群自定义机器人：发交互卡片。

零应用权限，只需要一个 webhook URL。适合日报、异常告警。
签名算法（飞书自定义机器人）：
    key   = f"{timestamp}\n{secret}"
    sign  = base64(HMAC-SHA256(key=key, msg=b""))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from ..config import FeishuConfig
from ..http import request
from ..log import get

log = get("feishu.bot")

# 事件 → 卡片主题色
COLORS = {
    "high": "red", "mid": "orange", "low": "blue",
}


class BotSender:
    def __init__(self, cfg: FeishuConfig):
        self.cfg = cfg

    def _sign(self) -> dict[str, Any]:
        if not self.cfg.webhook_secret:
            return {}
        ts = str(int(time.time()))
        key = f"{ts}\n{self.cfg.webhook_secret}"
        digest = hmac.new(key.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
        return {"timestamp": ts, "sign": base64.b64encode(digest).decode("utf-8")}

    def send_card(self, title: str, lines: list[str], color: str = "blue",
                  buttons: list[tuple[str, str]] | None = None) -> bool:
        elements: list[dict[str, Any]] = [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
        ]
        if buttons:
            elements.append({
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": t},
                     "type": "primary", "url": u}
                    for t, u in buttons
                ],
            })

        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": title},
                           "template": color},
                "elements": elements,
            },
        }
        payload.update(self._sign())

        if self.cfg.dry_run or not self.cfg.webhook:
            log.info("[DRY RUN] 群卡片 → %s | %s", title, " / ".join(lines)[:200])
            return True

        resp = request("POST", self.cfg.webhook, body=payload, timeout=20)
        code = int(resp.get("code") or 0) if isinstance(resp, dict) else 0
        if code != 0:
            log.warning("群卡片发送失败: %s", resp)
            return False
        return True

    def send_pairs(self, ov: dict[str, Any], groups: list[dict[str, Any]] | None = None,
                   top_n: int = 5) -> bool:
        """零 API 主链路的日报：客户高频问题（来自 qa_pair 聚类）。"""
        lines = [
            f"**问答记录** {ov.get('total_questions', 0)} 条",
            f"**涉及客户** {ov.get('customer_count', 0)} 位",
            f"**客户群/渠道** {ov.get('group_count', 0)} 个",
            f"**归并问题簇** {ov.get('cluster_count', 0)} 个",
            "",
            "**全局高频问题**",
        ]
        for t in ov.get("top", [])[:top_n]:
            lines.append(f"· {t['question']}  {t['count']} 次 / {t['variants']} 种说法")
        for g in (groups or [])[:2]:
            if not g.get("top"):
                continue
            lines += ["", f"**{g['group_name']}**（{g['total_questions']} 条 / {g['customer_count']} 人）"]
            for t in g["top"][:3]:
                lines.append(f"· {t['question']}  {t['count']} 次")
        return self.send_card("七鱼客户高频问题日报", lines, color="blue")

    def send_daily(self, stats: dict[str, Any], top_issues: list[tuple[str, int]]) -> bool:
        """日报卡片：一条消息把关键数字和 Top 问题带出来。"""
        lines = [
            f"**候选会话** {stats.get('candidates', 0)} 条",
            f"**噪声剔除** {stats.get('noise', 0)} 条",
            f"**无正文** {stats.get('no_text', 0)} 条",
            f"**已清洗** {stats.get('classified', 0)} 条",
            "",
            "**Top 问题**",
        ]
        for name, cnt in top_issues[:5]:
            lines.append(f"· {name}  {cnt} 条")
        return self.send_card("七鱼会话清洗日报", lines, color="blue")
