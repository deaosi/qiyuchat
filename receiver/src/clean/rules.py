"""0 档：规则层。免费、确定性、先跑。

三件事：
1. 无效剔除 —— 能砍掉 30~50% 的噪声，直接省一半 token
2. PII 脱敏 —— 必须在进模型之前做，不能交给模型
3. 规则评分 —— 质量分只用确定性数据算，不让模型打绝对分
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

# ---------- 脱敏 ----------

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # 手机号（中国大陆）
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    # 身份证
    (re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)"), r"\1********\2"),
    # 邮箱
    (re.compile(r"([A-Za-z0-9._%+-]{1,3})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"), r"\1***\2"),
    # 银行卡 / 长数字串
    (re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)"), r"\1********\2"),
    # 只保留省市区前缀，绝不能把完整地址再加星号当作脱敏。
    (re.compile(r"((?:[一-鿿]{2,8}省)?[一-鿿]{2,8}市(?:[一-鿿]{2,8}[区县])?)[^\s，,。;；\n]*(?:路|街|道|镇|村|号|栋|幢|室|楼|层)[^\s，,。;；\n]*"), r"\1[详细地址已脱敏]"),
    (re.compile(r"[一-鿿A-Za-z0-9]{2,}(?:路|街|小区|大厦)\d+[^\s，,。;；\n]*"), "[详细地址已脱敏]"),
]

_ORDER_RE = re.compile(r"(订单号?|运单号?|单号)[:：]?\s*([A-Za-z0-9]{8,})")

# 快路径：只有命中这些字符才可能触发上面任何一条规则。
# 没有它们就直接返回原文，省掉 7 次正则扫描（中文闲聊里占大多数）。
_MAYBE_PII = re.compile(r"[0-9@省市区县路街号栋幢室楼层镇村]|小区|大厦")


@lru_cache(maxsize=200_000)
def _mask_cached(text: str) -> str:
    if not _MAYBE_PII.search(text):
        return text
    # 订单号先处理：否则会被下面的长数字规则先吃掉，导致关键词失效
    out = _ORDER_RE.sub(lambda m: f"{m.group(1)}: [已脱敏]", text)
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def mask_pii(text: str) -> str:
    """本地正则脱敏。不调用任何外部服务。"""
    if not text:
        return ""
    return _mask_cached(text)


# ---------- 噪声判定 ----------

def is_noise(row: Any) -> tuple[bool, str]:
    """返回 (是否噪声, 原因)。row 是 raw_session 的一行（sqlite3.Row 或 dict）。"""
    g = (lambda k, d=0: (row[k] if k in row.keys() else d) if hasattr(row, "keys") else row.get(k, d))

    if int(g("is_valid", 1) or 0) == 0:
        return True, "isValid=0 无效会话"
    if int(g("s_type", 0) or 0) == 3:
        return True, "sType=3 未接入会话"
    if int(g("round_number", 0) or 0) <= 1:
        return True, "roundNumber<=1 单轮无信息"
    return False, ""


# ---------- 规则评分 ----------

def rule_score(row: Any) -> int:
    """0-100 的确定性质量分。只用七鱼返回的客观字段，不碰模型。

    权重：
      首次响应 35  —— 越慢扣越多（30s 内满分）
      平均响应 25  —— 60s 内满分
      会话轮数 15  —— 太少信息不足，太多效率低
      满意度   25  —— 未评价给中位分
    """
    def g(k: str, d: int = 0) -> int:
        try:
            if hasattr(row, "keys"):
                v = row[k] if k in row.keys() else d
            else:
                v = row.get(k, d)
            return int(v or 0)
        except (TypeError, ValueError, KeyError):
            return d

    frc = g("first_reply_cost") / 1000.0   # 秒
    avg = g("avg_resp_duration") / 1000.0
    rounds = g("round_number")
    ev = g("evaluation")

    s1 = 35 if frc <= 30 else max(0, int(35 * (1 - (frc - 30) / 120)))
    s2 = 25 if avg <= 60 else max(0, int(25 * (1 - (avg - 60) / 240)))
    s3 = 15 if 2 <= rounds <= 20 else (8 if rounds > 20 else 5)
    s4 = 25 if ev >= 100 else (15 if ev > 0 else 12)

    return max(0, min(100, s1 + s2 + s3 + s4))


# ---------- 组装送模型的文本 ----------

def build_clean_text(row: Any, transcript: str, max_chars: int = 6000) -> str:
    """把元数据 + 正文拼成给模型的输入。正文为空时只给元数据。"""
    def g(k: str, d: str = "") -> str:
        if hasattr(row, "keys"):
            return str((row[k] if k in row.keys() else d) or d)
        return str(row.get(k, d) or d)

    head = (
        f"[渠道]{g('from_type') or '未知'} "
        f"[分流组]{g('from_group') or '未知'} "
        f"[轮数]{g('round_number')} "
        f"[首次响应]{int(g('first_reply_cost') or 0) // 1000}s "
        f"[满意度]{g('evaluation') or '未评价'}"
    )
    body = mask_pii(transcript).strip()
    if not body:
        return ""
    text = f"{head}\n{body}"
    return text[:max_chars]
