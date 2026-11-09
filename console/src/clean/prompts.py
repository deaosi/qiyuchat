"""Prompt 资产。改这里就必须同步 bump PROMPT_VERSION，否则回滚对不上。"""

from __future__ import annotations

# 每次修改 prompt 内容都改这个版本号，清洗结果按 (session_id, prompt_version) 存
PROMPT_VERSION = "v1"

CATEGORIES = {
    "物流": ["发货慢", "配送异常", "丢件", "签收问题", "运费"],
    "退换": ["退货", "换货", "退款", "退款未到账"],
    "质量": ["故障", "破损", "异味", "效果不符", "缺件"],
    "咨询": ["使用方法", "规格参数", "价格", "库存", "适配性"],
    "投诉": ["服务态度", "承诺未兑现", "重复问题", "推诿"],
    "其他": ["无法归类"],
}

CLASSIFY_SYSTEM = """你是客服会话分类器。严格按下面的分类体系判断，只输出 JSON，不要任何解释。

分类体系（一级 → 二级，只能选一个组合）：
- 物流：发货慢 / 配送异常 / 丢件 / 签收问题 / 运费
- 退换：退货 / 换货 / 退款 / 退款未到账
- 质量：故障 / 破损 / 异味 / 效果不符 / 缺件
- 咨询：使用方法 / 规格参数 / 价格 / 库存 / 适配性
- 投诉：服务态度 / 承诺未兑现 / 重复问题 / 推诿
- 其他：无法归类

输出字段：
category     一级分类，必须是上面 6 个之一
sub          二级分类，必须属于该一级
emotion      calm / annoyed / angry
resolved     客服是否解决了问题：true / false
risk         low / mid / high（high = 有投诉升级、监管投诉、公开平台曝光倾向）
confidence   0.0 - 1.0，你对分类的把握

输出格式：一个 JSON 数组，顺序与输入会话顺序一致，不要包对象。
[{"category":"物流","sub":"丢件","emotion":"angry","resolved":false,"risk":"high","confidence":0.9}]"""


SUMMARY_SYSTEM = """把客服会话压缩成结构化摘要。只输出 JSON，不要解释。

规则：
1. 客户诉求必须保留原话里的关键词，不要改写成书面术语
2. 处理结果只写客服**实际承诺**的内容，没承诺就写「未承诺」
3. 禁止推测，禁止补充会话里没有的信息
4. 每项不超过 40 字
5. 涉及手机号、地址、订单号的内容不要复述具体值

输出字段：
demand        客户诉求（40 字内）
result        处理结果（40 字内）
product       涉及的产品/型号，没有就空字符串
followup      是否需要后续跟进：true / false
one_line      一句话总览，不超过 30 字
confidence    0.0 - 1.0

输出格式：{"demand":"...","result":"...","product":"...","followup":false,"one_line":"...","confidence":0.9}"""


def classify_user_prompt(batch: list[str]) -> str:
    """把 N 条会话拼成一次分类请求。带序号便于对齐结果。"""
    parts = []
    for i, text in enumerate(batch, 1):
        parts.append(f"### 会话{i}\n{text}")
    return "\n\n".join(parts)


def summary_user_prompt(text: str) -> str:
    return f"### 会话\n{text}"
