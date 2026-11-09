"""飞书推送层。

两个出口，按需要选：
- bot     群自定义机器人 Webhook —— 零权限，最快见效
- openapi 应用身份（tenant_access_token）—— 建文档 / 写多维表格 / 发消息
"""

from .agent import AgentClient
from .auth import FeishuAuth
from .bitable import PAIR_FIELD_SPEC, BitableWriter, pair_to_fields
from .bot import BotSender
from .check import run_all as check_feishu
from .docx import DocxWriter

__all__ = ["FeishuAuth", "AgentClient", "BotSender", "BitableWriter",
           "DocxWriter", "check_feishu", "PAIR_FIELD_SPEC", "pair_to_fields"]
