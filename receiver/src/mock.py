"""内置假数据：让整条链路在没有 AppKey 的情况下也能跑通、能被验证。

MOCK=1 时 pull 阶段用这里的数据，字段名与七鱼接口返回保持一致。
"""

from __future__ import annotations

import random
import time
from typing import Any

_SCRIPTS = [
    ("物流", "我的快递三天没动了，单号 SF1234567890123，到底什么时候能到？",
     "您好，我帮您查一下……查到包裹在杭州中转站积压了，预计明天下午派送。",
     "明天要是还不到呢？", "到时候还没到您再找我，我这边给您加急催一下。"),
    ("退换", "买的洗衣机昨天到的，面板是裂的，我要退货",
     "非常抱歉，我给您走换货流程，您把机器留在原地，师傅48小时内上门取件。",
     "那钱什么时候退？", "换货签收后3个工作日内退款到原支付渠道。"),
    ("质量", "这个空气炸锅用了两次就有塑料味，正常吗",
     "新机器首次使用有轻微气味是正常的，建议空烧15分钟通风。",
     "已经空烧过了还是有味道", "那给您安排换新，麻烦拍一张机器底部的序列号照片。"),
    ("咨询", "这个净水器滤芯多久换一次", "建议6-8个月更换一次，水质硬的地区建议6个月。",
     "滤芯多少钱", "官方滤芯199元一支，现在买二送一。"),
    ("投诉", "上次说好给我补发配件，一周了没动静，你们是不是不打算处理了",
     "抱歉让您久等了，我查一下工单……确实漏了，今天给您安排顺丰发出。",
     "每次都这样，我要投诉", "非常抱歉，我这边给您升级处理，2小时内主管会联系您。"),
    ("物流", "地址填错了能改吗，改到公司地址", "还没发货可以改，麻烦提供一下新地址。",
     "北京市朝阳区建国路88号SOHO现代城A座1801", "好的已修改，今天下午发出。"),
    ("退换", "退款申请提交5天了还没到账", "我查一下退款单……银行处理中，最迟明天到账。"),
    ("质量", "收到的锅盖把手是松的", "给您补发一个把手，您自己拧上就行，不需要寄回。"),
    ("咨询", "这个扫地机器人能爬2cm的门槛吗", "最大越障2cm，超过的话需要放个坡道。"),
    ("投诉", "客服态度太差了，问了三次都没人回", "非常抱歉，我马上处理您的问题，请说具体情况。"),
]

_CHANNELS = ["Web", "Wx", "wx_ma", "App", "Ios", "Android"]
_GROUPS = ["售前咨询组", "售后一组", "售后二组", "VIP专线"]
_STAFF = [("1001", "张敏"), ("1002", "李强"), ("1003", "王芳"), ("1004", "机器人")]


def make_sessions(n: int = 20, days_back: int = 3) -> list[dict[str, Any]]:
    random.seed(42)
    now = int(time.time() * 1000)
    out = []
    for i in range(n):
        cat, q, a, *rest = _SCRIPTS[i % len(_SCRIPTS)]
        sid = str(900000 + i)
        start = now - random.randint(0, days_back * 86_400_000)
        dur = random.randint(120, 1800) * 1000
        staff_id, staff_name = _STAFF[i % len(_STAFF)]
        out.append({
            "id": sid,
            "startTime": start,
            "endTime": start + dur,
            "sType": 0,
            "staffId": staff_id,
            "staffName": staff_name,
            "userId": f"u{1000 + i}",
            "userName": f"客户{i}",
            "userPhone": f"1380000{1000 + i}",
            "fromGroup": _GROUPS[i % len(_GROUPS)],
            "fromType": _CHANNELS[i % len(_CHANNELS)],
            "category": cat,
            "evaluation": random.choice([0, 0, 100, 1, 50]),
            "evaluationType": 3,
            "roundNumber": random.randint(1, 12),
            "isValid": 1 if i % 7 else 0,
            "firstReplyCost": random.randint(3000, 180000),
            "avgRespDuration": random.randint(5000, 300000),
            "worksheetIds": "" if i % 3 else f"W{5000 + i}",
        })
    return out


def make_transcript(session_id: str, base_ms: int = 0) -> list[dict[str, Any]]:
    """按会话 ID 生成固定的几轮对话。

    base_ms 是会话开始时间，逐条按 30 秒递进 —— 和真实数据一样带 ts，
    否则消息时间戳为 0，会被滑动窗口裁剪当成「最古老的数据」直接删掉。
    """
    idx = int(session_id) - 900000
    cat, q, a, *rest = _SCRIPTS[idx % len(_SCRIPTS)]
    msgs = [
        {"role": "user", "content": q},
        {"role": "staff", "content": a},
    ]
    for j in range(0, len(rest) - 1, 2):
        msgs.append({"role": "user", "content": rest[j]})
        msgs.append({"role": "staff", "content": rest[j + 1]})
    if base_ms:
        for i, m in enumerate(msgs):
            m["ts"] = base_ms + i * 30_000
    return msgs
