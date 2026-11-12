"""Build explainable pilot issue cards from the current VOC source rows."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import re
from typing import Iterable

from .clean.rules import mask_pii
from .voc_assets import digest
from .voc_domain import _CONFIG_NEGATED, _CONFIRM, _DEFECT_NEGATED, _TENTATIVE, _message_refs
from .voc_quality import session_key
from .voc_topic_insights import _PLATFORM_NAMES, _exact_ref


_SYMPTOMS = {
    "未触发": r"(?:没有|并未|未|没|不|无法|不能)(?:被|自动|正常|成功)?(?:触发|命中)|(?:触发|命中)(?:失败|不了|不上|不到|不成功)",
    "没有回复": r"(?:没有|并未|未|没|不|无|无法|不能)(?:自动|正常|任何)?(?:回复|响应|应答)|(?:回复|响应)(?:失败|不了)|不说话|(?:没有|未|没)(?:自动)?(?:发送|发出)(?:催付|欢迎语|话术|消息|回复)",
    "回复错误": r"答非所问|答错|回错|(?:回复|回答)(?:内容)?(?:出错|错误|错了|不对|不符|不匹配)|(?:回复|回答)(?:了)?(?:错误|旧的|其他场景)",
}
_MODULES = {
    "自定义 Agent": r"自定义\s*agent",
    "跟单催付": r"催付|催拍|(?:未付款|未支付)(?:订单)?(?:提醒|催促)",
    "表格知识库": r"表格知识(?:库)?",
    "商品知识库": r"商品知识(?:库)?",
    "自定义知识库": r"自定义知识(?:库)?",
    "问答知识库": r"问答知识(?:库)?",
    "问答设置": r"问答(?!知识)(?:设置|配置|规则|怎么(?:设置|配)|如何(?:设置|配))|(?:设了|配了|设置了|配置了|设置|配置|设好|配好)(?:一个|这条|该)?问答(?!知识)",
    "前台接待": r"前台接待|接待前台",
    "快捷回复": r"快捷回复|快捷话术",
    "转人工": r"转人工|转接客服|人工转接",
    "店铺授权": r"店铺授权|店铺绑定|绑定店铺|店铺接入|接入店铺",
    "自动回复": r"自动回复(?:场景|规则|功能|模块|设置|配置)|关键词回复|欢迎语",
    "退款场景": r"退款(?:场景|回复|规则)",
    "退货场景": r"退货(?:场景|回复|规则)",
    "退换货场景": r"退换货场景",
    "发货场景": r"发货场景",
    "签收场景": r"签收场景|收货场景",
}
_GENERIC_MODULES = {
    "跟单": r"跟单|催单",
    "售后场景": r"售后场景",
    "物流场景": r"物流场景",
    "知识库": r"知识库",
    "场景规则": r"场景|触发(?:规则|条件)",
    "机器人接待": r"机器人|自动接待|自动回复|(?<![a-z])ai(?![a-z])",
}
_OTHER_MODULES = {
    "话术管理": r"话术(?:管理|库|导出|导入)|(?:导出|导入|下载)(?:常用|快捷)?话术",
    "会话导出": r"(?:导出|下载)(?:客户|客服|历史|全部)?(?:聊天|会话)(?:记录)?|(?:聊天|会话)(?:记录)?导出",
    "数据报表": r"报表|数据统计|统计数据",
    "商品同步": r"商品同步|同步商品",
    "知识同步": r"知识同步|同步知识",
    "店铺权限": r"店铺(?:的)?权限|店铺(?:子账号|主账号)(?:的)?权限",
    "账号权限": r"(?:账号|帐号|子账号|主账号|角色)(?:的)?权限|权限(?:设置|管理)",
    "登录认证": r"登录|登陆|登录验证|身份验证",
    "消息接入": r"消息(?:接入|对接)|(?:接入|对接)(?:消息|聊天|会话)",
}
_TASKS = {
    "新建场景": r"(?:新建|新增|创建|添加)(?:一个|新的)?(?:场景|规则)|(?:场景|规则)(?:怎么|如何)(?:新建|新增|创建|添加)",
    "设置触发条件": r"触发条件|限定条件|触发规则|匹配条件|命中条件|适用范围",
    "启停场景": r"(?:启用|开启|停用|禁用|关闭)(?:该|这个|此)?(?:场景|机器人|规则)|(?:场景|规则|机器人)(?:怎么|如何)(?:启用|开启|停用|禁用|关闭)|场景开关",
    "保存和发布": r"保存|发布|生效",
    "测试场景": r"测试|复测|预览|验证效果",
    "绑定与授权": r"绑定|接入|授权",
    "查找配置入口": r"(?:在哪|哪里)(?:设置|配置|修改)|(?:设置|配置)(?:入口|页面)(?:在哪|在哪里)|找不到(?:设置|配置)(?:入口|页面)",
    "调整回复内容": r"(?:回复|回答)(?:内容|话术)|(?:设置|配置|编辑|修改)(?:自动)?(?:回复|话术|欢迎语)",
}
_TRIGGERS = {
    "测试时": r"(?:复测|测试)(?:时|中|的时候|发现|怎么|没有|未|没|不)|(?:在|刚才|刚刚|开始|尝试|进行)(?:复测|测试)",
    "买家发消息时": r"买家(?:来询|咨询|发消息|发送消息|发来消息|进线)|(?:客户|顾客)(?:咨询|发消息)|买家消息",
    "关键词命中时": r"(?:触发|命中|匹配|包含|输入|发送|发了|发出)(?:指定|特定)?关键词|关键词(?:命中|匹配|触发)|按关键词",
    "下单后": r"下单后|拍下后|提交订单后|下单(?:未|没)(?:付|支付)",
    "付款后": r"(?:付款|支付|买单)(?:后|完成后|成功后)",
    "发货后": r"发货后|(?:物流|运单)(?:更新|变更)后",
    "退款时": r"退款(?:申请|后|时)|申请退款",
    "退货时": r"退货(?:申请|后|时)|申请退货",
    "转人工后": r"转人工后|转接人工后|人工接管后",
}
_PRECONDITIONS = {
    "已启用": r"(?:已经|已)(?:打开|开启|启用)|(?:打开|开启|启用)(?:开关)?后|开关(?:是开的|已打开|已开启)",
    "已保存": r"(?:已经|已)保存|保存(?:完成|成功)?后",
    "已发布": r"(?:已经|已)发布|发布(?:完成|成功)?后",
    "已配置": r"(?:已经|已)(?:配置|设置)|(?:配置|设置)(?:完成|好)?后|设置好了|配置好了|(?:设了|配了|设置了|配置了)(?:一个|这条|该)?(?:问答|场景|规则|回复)",
}
_AUDIENCES = ("子账号", "主账号", "管理员")
_VERSION = re.compile(r"\b[vV]\d+(?:\.\d+){1,3}\b|(?:版本\s*)\d+(?:\.\d+){1,3}|\d+(?:\.\d+){1,3}\s*(?:版本|版)")
_REQUEST = re.compile(r"怎么|如何|在哪|哪里|能否|能不能|是否可以|请问|想(?:设置|配置|修改|开启|关闭|添加|新建|调整)|找不到.*(?:入口|页面)")
_CONFIG_HINT = re.compile(r"场景|规则|自定义\s*agent|跟单|催付|回复设置|自动回复|知识库|问答|欢迎语|机器人|快捷回复|前台接待|授权", re.I)
_SUPPORT_CONTEXT = re.compile(r"知汇鸽|机器人|(?<![a-z])ai(?![a-z])|(?:自动|快捷)回复|前台接待|提示词|表格检索|知识库|跟单|催付|转人工|场景(?:配置|规则|触发|没有触发|没触发|不触发|开关)|问答(?:设置|配置|规则)|(?:退货|退款)(?:场景|回复|规则)", re.I)
_GENERIC_CONFIG = re.compile(r"(?:设置|配置|编辑|修改|调整)(?:这个|该|此)?(?:场景|规则|问答|快捷回复|前台接待)|(?:场景|规则|问答|快捷回复|前台接待)(?:怎么|如何)(?:设置|配置|编辑|修改|调整)")
_HYPOTHETICAL = re.compile(r"^(?:请问[，, ]*)?(?:假如|假设|如果|万一|要是)|(?:会不会|是否会)(?:不回复|不触发)|有没有(?:自动)?(?:回复|触发)")
_REPORTED = re.compile(r"已经|现在|目前|发现|出现|今天|刚才|仍然|还是|一直|遇到|复测")
_INTERMITTENT = re.compile(r"有时候|有时|偶尔|间歇(?:性)?|时不时|时有时无|一会儿(?:能|有).{0,12}一会儿(?:不能|没有)")
_GENERAL_FAILURE = re.compile(r"报错|错误码|失败|出错|异常|卡住|闪退|白屏|空白页|没(?:有)?生效|无法|不可用|不能|打不开|进不去|丢失|不同步|匹配不了|匹配不上|重复发送")
_GENERAL_TASK = re.compile(r"导出|导入|同步|复制|绑定|授权|注销|查询|查看|搜索|检索|筛选|统计|设置|配置|调整|重置|下载|上传|登录|验证|对接|分流|接入|迁移|批量|发送|切换|安装|升级|更新|启用|开启|关闭|停用|保存|发布")
_LIMITATION = re.compile(r"(?:是否|能不能|能否|平台|只|仅|不)(?:再|能|可以)?支持|支持不支持|支持.{0,16}(?:吗|么)|(?:限制|上限)(?:是|为|多少|几个)|(?:最多|最少).{0,12}(?:个|条|次)|不兼容")
_CAPABILITY_REQUEST = re.compile(r"(?:能不能|能否|可以|是否可以|是否能)(?:直接|全部|一键|批量|再|统一){0,3}(?:导出|导入|同步|绑定|授权|接入|查询|设置|配置|查看|安装|更新|下载|上传)")
_PRODUCT_OBJECT = re.compile(r"会话|聊天记录|历史消息|工单|账号权限|角色权限|店铺权限|账号|子账号|认证|验证码|报表|统计数据|消息队列|开放接口|回调|API|webhook|插件|浏览器扩展|客户标签|质检|接待|分流|渠道|商品数据|同步记录|话术库|常用话术|配置模板|规则模板|批量操作|配置项|版本管理", re.I)
_FRAGMENT = re.compile(r"^(?:(?:[＠@][^\s，。！？]{1,36}|[，。！？？?\s])+)?(?:好(?:的|了)?|收到|知道了|谢谢|明白(?:了)?|在吗|有人吗|是的|没错|回复(?:内容)?(?:不对|错误|异常)|还是不行|没有了|可以了|看下|看一下|帮忙看(?:下|一下)?|方便(?:远程|接电话|看下|看一下|回复)吗)[，。！？？?\s]*$")
_MANUAL_ONLY = re.compile(r"^(?:(?:请问|麻烦|方便|可以|能否|能不能|帮我|请你|请您|请)(?:再|先)?)+(?:远程|回电|拉群|找(?:人工|客服)|转(?:给|接)(?:客服|同事)|看(?:下|一下)|查(?:下|一下))")
_INSTRUCTION_START = re.compile(r"^(?:(?:请(?:您|你)?|建议(?:您|你)?|麻烦(?:您|你)?|可以先|需要先)\s*)?(?:先|再|然后|接着)\s*(?:把|将)?(?:更新|抓取|重启|刷新|打开|开启|关闭|检查|点击|登录|保存|配置|改|手动)")
_SERVICE_OFFER = re.compile(r"(?:我(?:们)?(?:这边)?|这边)(?:可以|先|来)?帮您(?:看|查|处理|核对)")
_STAFF_CHECK_QUESTION = re.compile(r"^(?:请问[，,\s]*)?(?:您(?:这边)?(?:是否|有没有|有无)|是否有|有没有)\s*(?:尝试|检查|核对|确认|试过)")
_HYPOTHETICAL_CLAUSE = re.compile(r"(?:如果|假如|要是|万一|若)")
_BUYER_POLICY = re.compile(r"定制贴纸|定制(?:商品|颜色|图案)|什么时候发货|多久到账|是否包邮|能不能退货|还能退吗|退货运费|价格优惠|买家问|顾客问")
_HISTORY = re.compile(r"(?:历史消息|聊天记录|历史会话|会话历史).{0,12}(?:看不到|消失|丢失|空白|不见|无法查看|没了)|(?:看不到|丢失|找不到|无法查看|不显示).{0,12}(?:历史消息|聊天记录|历史会话|会话历史)|(?:没有|看不到|找不到|丢失|不显示).{0,8}(?:掉线|离线)(?:之前|前)的?(?:历史)?(?:消息|会话|聊天记录)")
_KNOWLEDGE_MISMATCH = re.compile(r"(?:知识库|知识内容).{0,24}(?:不一致|对不上|回复.{0,10}(?:旧|原来)|还是原来)|(?:回复|回答).{0,18}(?:跟|和|与)知识(?:库|内容).{0,10}(?:不一致|不一样)")
_IMPORTED_KNOWLEDGE_NOT_MATCHED = re.compile(r"(?:导入.{0,8}知识|知识.{0,6}导入).{0,16}(?:无法匹配|匹配不了|匹配不上|不能匹配).{0,8}场景")
_MISROUTED = re.compile(r"售后.{0,20}(?:转|分|流|进|变)(?:到|入|给|为|成|了)?(?:了)?售前")
_FALLBACK_AFTER_STAFF = re.compile(r"人工(?:客服)?(?:已经|已)?(?:回复|接待|介入)(?:完成(?:后|了)?|完(?:后|了)?|后|了).{0,18}(?:仍|还|继续|又).{0,8}(?:回复|发送|发).{0,5}(?:兜底|默认)(?:话术|回复)")
_SPEECH_EXPORT = re.compile(r"(?:导出|下载).{0,12}(?:话术|快捷回复)|(?:话术|快捷回复).{0,12}(?:导出|下载)")
_ERROR_CODE = re.compile(r"(?:报错(?:码)?|错误码|error(?:\s+code)?)[：:：\s]*([a-z0-9-]{2,20})", re.I)
_SCOPE_MARKER = re.compile(r"仅限|只限|适用范围|仅(?:在|当)|(?:当|只有)[^，。！？；\n]{2,40}(?:时|后)|除非|前提")
_URL = re.compile(r"https?://[^\s，；。！？]+")
_AT_MENTION = re.compile(r"[＠@][^\s，；。！？、：:]{1,36}")
_LONG_ID = re.compile(r"(?<!\d)\d{5,}(?!\d)")
_NON_OBJECT = re.compile(r"^(?:这个|那个|这个问题|问题|回复|回复内容|内容|操作|设置|人工客服|客户|商家|账号|功能|软件|平台|场景|客服|账号名称|表格|图片|截图)$")
_INVESTIGATED = re.compile(r"已(?:经)?(?:核对|核实|检查|复测|复现|排查|确认)|(?:核对|核实|检查|复测|排查)过|复测(?:发现|仍|还是)|检查发现")
_NOT_VERIFIED = re.compile(r"(?:未(?:能)?|没有|没|无法|尚未|还未|并未|不能)(?:实际|再次|进一步)?(?:核对|核实|检查|复测|复现|排查|确认)|如果|假如")
_DISABLED = re.compile(r"(?:未|没|没有)(?:打开|开启|启用)|开关(?:是关的|已关闭)")
_CONFIRMED_STATUS = {"config_cause_confirmed", "platform_limit_confirmed", "defect_confirmed"}


@dataclass(frozen=True)
class _Scope:
    family: str
    platform: str
    module: str
    trigger: str
    preconditions: tuple[str, ...]
    variant: str
    fingerprint: str = ""

    @property
    def condition(self) -> str:
        return " · ".join(value for value in (self.trigger, *self.preconditions) if value)


@dataclass(frozen=True)
class _Record:
    key: tuple[str, str]
    case: dict
    status: str
    cause_type: str
    cause: str
    merchant: str
    preview: str
    details: dict[str, str]


def _labels(patterns: dict[str, str], text: str) -> list[str]:
    return [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]


def _refs(row: dict, refs: Iterable[dict | None], side: str | None = None) -> list[dict]:
    result = []
    for ref in refs:
        if (bound := _exact_ref(row, ref)) and (side is None or bound["source"] == side):
            result.append(bound)
    return result


def _question(row: dict) -> dict | None:
    source, span = row["source"], row["data"].get("question_span", {})
    start, end, quote = (span.get(key) for key in ("start", "end", "quote"))
    original = source.get("question", "")
    if (span.get("alignment") != "exact" or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(original) or not isinstance(quote, str) or not quote.strip()
            or original[start:end] != quote):
        return None
    refs = _message_refs(source, "question", start, end)
    message_id = refs[0]["message_id"] if len(refs) == 1 and refs[0]["end"] - refs[0]["start"] == end - start else ""
    stamp = source.get("asked_at", 0)
    if message_id:
        stamp = next((part.get("ts") or 0 for part in source["_question_parts"] if part["message_id"] == message_id), 0)
    return {"qa_id": row["qa_id"], "message_id": message_id, "quote": quote,
            "ts": stamp, "anchored": bool(message_id),
            "has_reply": bool(source.get("_staff_verified") and source.get("answer")),
            "start": start, "end": end}


def _safe_label(text: str, source: dict) -> str:
    text = _AT_MENTION.sub("[名称]", _URL.sub("[链接]", mask_pii(text)))
    text = _LONG_ID.sub("[编号]", text)
    for key in ("customer_name", "group_name", "staff_name"):
        name = source.get(key, "")
        if isinstance(name, str) and len(name) >= 2 and name not in ("匿名客户", "未分组", "未记录客服"):
            text = text.replace(name, "[名称]")
    return " ".join(text.split())


def _case_details(row: dict, case: dict, preview: str, status: str) -> dict[str, str]:
    domain, source = row["data"].get("domain", {}), row["source"]
    details = {}
    for key, name in (("goal", "merchant_goal"), ("observed", "actual")):
        refs = _refs(row, [domain.get(name)], "question")
        if not refs:
            continue
        ref = refs[0]
        if case["start"] <= ref["start"] < ref["end"] <= case["end"]:
            text = _safe_label(ref["quote"], source)
            if text and text != preview:
                details[key] = text
    if status != "reported":
        refs = _refs(row, domain.get("diagnosis", {}).get("evidence", []), "answer")
        checks = (_safe_label(ref["quote"], source) for ref in refs
                  if ref["source_refs"] and _INVESTIGATED.search(ref["quote"])
                  and not _NOT_VERIFIED.search(ref["quote"]) and not _TENTATIVE.search(ref["quote"]))
        if checked := next((text for text in checks if text), ""):
            details["checked"] = checked
    return details


def _general_family(quote: str, request: str, product_object: bool, goal: list[dict], actual: list[dict]) -> str:
    question = bool(_REQUEST.search(request) or re.search(r"[？?]\s*$", quote))
    task = bool(_GENERAL_TASK.search(request))
    if not product_object:
        return ""
    if _LIMITATION.search(request) or _CAPABILITY_REQUEST.search(request):
        return "platform_limit"
    observed = "\n".join([quote, *(ref["quote"] for ref in actual)])
    if _GENERAL_FAILURE.search(observed) and (task or actual or not question):
        if not _HYPOTHETICAL.search(quote) or _REPORTED.search(quote):
            return "anomaly"
    if question and task:
        return "configuration"
    if question and goal and (task or re.search(r"什么|哪些|哪个|区别|规则|条件|范围|显示|变更", request)):
        return "other"
    return ""


def _general_label(family: str, quote: str, goal: list[dict], actual: list[dict], source: dict) -> str:
    def clean(text: str) -> str:
        return _safe_label(text, source).strip("，。！？；,;!?")

    goal_text = clean(goal[0]["quote"]) if goal else ""
    actual_text = clean(actual[0]["quote"]) if actual else ""
    if family == "anomaly" and _GENERAL_FAILURE.search(actual_text):
        if goal_text and _GENERAL_TASK.search(goal_text):
            if goal_text in actual_text:
                return actual_text
            return goal_text if actual_text in goal_text else f"{goal_text} · {actual_text}"
        if len(actual_text) >= 8 and _GENERAL_TASK.search(actual_text):
            return actual_text
    if family in ("configuration", "platform_limit", "other") and len(goal_text) >= 4:
        # A limit question must retain the actual limit/query predicate; a bare
        # operation noun does not distinguish an inquiry from an observed error.
        if family == "platform_limit" and (_LIMITATION.search(goal_text) or _CAPABILITY_REQUEST.search(goal_text)):
            return goal_text
        if family != "platform_limit" and _GENERAL_TASK.search(goal_text):
            return goal_text
    return clean(quote)


def _classify(row: dict, quote: str) -> tuple[bool, _Scope | None]:
    data = row["data"]
    domain = data.get("domain", {})
    actual = _refs(row, [domain.get("actual")], "question")
    goal = _refs(row, [domain.get("merchant_goal")], "question")
    observed = "\n".join([quote, *(ref["quote"] for ref in actual)])
    request = "\n".join([quote, *(ref["quote"] for ref in goal)])
    label = _safe_label(quote, row["source"])
    if _FRAGMENT.fullmatch(quote.strip()) or _MANUAL_ONLY.search(quote) or not re.search(r"[\u4e00-\u9fff]{2}", label.replace("[编号]", "").replace("[名称]", "")):
        return bool(_SUPPORT_CONTEXT.search(quote) or _labels(_SYMPTOMS, quote)), None
    if (_SERVICE_OFFER.search(quote) or _STAFF_CHECK_QUESTION.search(quote)) and not _GENERAL_FAILURE.search(quote):
        return bool(_SUPPORT_CONTEXT.search(quote) or _CONFIG_HINT.search(quote) or _GENERAL_TASK.search(quote)), None
    if _INSTRUCTION_START.search(quote.strip()):
        assertion = _HYPOTHETICAL_CLAUSE.split(quote, maxsplit=1)[0]
        if not _REPORTED.search(assertion) or not (_GENERAL_FAILURE.search(assertion) or _labels(_SYMPTOMS, assertion)):
            return bool(_GENERAL_TASK.search(quote) or _SUPPORT_CONTEXT.search(quote)), None
    module_refs = _refs(row, domain.get("modules", []))
    module_text = "\n".join([request, *(ref["quote"] for ref in module_refs)])
    modules = _labels(_MODULES, module_text)
    if not modules:
        modules = _labels(_OTHER_MODULES, module_text)
    if not modules and _SPEECH_EXPORT.search(module_text):
        modules = ["话术管理"]
    if not modules:
        explicit_modules = {_safe_label(ref["quote"], row["source"]) for ref in module_refs}
        modules = sorted(value for value in explicit_modules if 2 <= len(value) <= 26 and not _REQUEST.search(value)
                         and not _NON_OBJECT.fullmatch(value) and not re.search(r"\[|远程|截图|谢谢|好的|照片|退货运费", value))
    if not modules:
        modules = _labels(_GENERIC_MODULES, module_text)[:1]
    symptoms = _labels(_SYMPTOMS, observed)
    config = bool(_REQUEST.search(request) and _CONFIG_HINT.search(request))
    support_context = bool(_labels(_MODULES, quote) or _labels(_GENERIC_MODULES, quote) or _SUPPORT_CONTEXT.search(module_text))
    if _BUYER_POLICY.search(quote) and not support_context:
        return False, None
    product_object = bool(modules or _PRODUCT_OBJECT.search(request) or support_context)
    canonical = ""
    if _HISTORY.search(observed):
        canonical, family, modules = "历史消息缺失", "anomaly", ["会话记录"]
    elif _IMPORTED_KNOWLEDGE_NOT_MATCHED.search(observed):
        canonical, family, modules = "导入的知识无法匹配场景", "anomaly", ["知识导入"]
    elif _KNOWLEDGE_MISMATCH.search(observed):
        canonical, family = "知识内容不一致", "anomaly"
    elif _MISROUTED.search(observed):
        canonical, family, modules = "售后会话被分到售前", "anomaly", ["会话分流"]
    elif _FALLBACK_AFTER_STAFF.search(observed):
        canonical, family, modules = "人工回复后仍发送兜底话术", "anomaly", ["兜底回复"]
    else:
        family = "no_reply" if symptoms and support_context else "scenario_config" if config and not symptoms else ""
    if not family:
        family = _general_family(quote, request, product_object, goal, actual)
    if not family:
        return bool(product_object and (symptoms or _GENERAL_FAILURE.search(quote) or _REQUEST.search(quote))), None
    if (family == "no_reply" and len(symptoms) > 1 or domain.get("fragment_role") == "reproduction_context"
            or family == "no_reply" and _HYPOTHETICAL.search(quote) and not _REPORTED.search(quote)):
        return True, None

    platforms = _refs(row, domain.get("platforms", []))
    platform_text = "\n".join([quote, *(ref["quote"] for ref in platforms)])
    names = [name for name, aliases in _PLATFORM_NAMES if any(alias in platform_text for alias in aliases)]
    if len(names) > 1:
        return True, None
    platform = names[0] if names else ""
    if family == "scenario_config" and len(modules) != 1:
        return True, None
    module = modules[0] if len(modules) == 1 else ""

    context_refs = _refs(row, [*domain.get("prerequisites", []), *domain.get("reproduction_context", []),
                              *(ref for ref in data.get("scope", []) if ref.get("kind") in ("condition", "version", "audience"))], "question")
    context = "\n".join([quote, *(ref["quote"] for ref in context_refs)])
    triggers = _labels(_TRIGGERS, context)
    audiences = [name for name in _AUDIENCES if name in context]
    versions = sorted({re.search(r"\d+(?:\.\d+)+", value.group())[0] for value in _VERSION.finditer(context)})
    if len(triggers) > 1 or len(audiences) > 1 or len(versions) > 1:
        return True, None
    setup = _labels(_PRECONDITIONS, context)
    if _DISABLED.search(context):
        if "已启用" in setup:
            return True, None
        setup.append("未启用")
    trigger = triggers[0] if triggers else ""
    limits = sorted({_safe_label(ref["quote"], row["source"]) for ref in context_refs
                     if len(ref["quote"]) <= 55 and _SCOPE_MARKER.search(ref["quote"])})
    preconditions = tuple([*setup, *audiences, *(f"版本 {version}" for version in versions), *limits])
    fingerprint = ""
    if family == "no_reply":
        variant = ("间歇性" if _INTERMITTENT.search(observed) else "") + symptoms[0]
    elif family == "scenario_config":
        tasks = _labels(_TASKS, request)
        if not tasks and _GENERIC_CONFIG.search(request) and module != "场景规则":
            tasks = ["配置场景"]
        if len(tasks) == 1:
            variant = tasks[0]
        else:
            family = _general_family(quote, request, product_object, goal, actual)
            if not family:
                return True, None
    if family not in ("no_reply", "scenario_config"):
        if not canonical and _SPEECH_EXPORT.search(request):
            canonical = "话术导出失败" if family == "anomaly" else "话术导出"
        if not canonical and module in ("会话导出", "店铺授权", "商品同步", "知识同步"):
            if family == "anomaly":
                canonical = f"{module}失败"
            elif family in ("configuration", "platform_limit"):
                canonical = module
        label = canonical or _general_label(family, quote, goal, actual, row["source"])
        if len(label) < 4:
            return True, None
        variant = label[:72].rstrip() + ("…" if len(label) > 72 else "")
        fingerprint = digest([label, sorted({value.group(1).lower() for value in _ERROR_CODE.finditer(observed)})])
    return True, _Scope(family, platform, module, trigger, preconditions, variant, fingerprint)


def _evidence_status(row: dict) -> tuple[str, str, str]:
    diagnosis = row["data"].get("domain", {}).get("diagnosis", {})
    source = row["source"]
    if not source.get("_staff_verified"):
        return "reported", "", ""
    domain = row["data"].get("domain", {})
    status = diagnosis.get("status", "")
    refs = _refs(row, [diagnosis.get("cause"), *diagnosis.get("evidence", []),
                       *domain.get("reproduction_context", []), domain.get("actual")], "answer")
    anchored = [ref["quote"] for ref in refs if ref["source_refs"]]
    causes = _refs(row, [diagnosis.get("cause")], "answer")
    evidence = _refs(row, diagnosis.get("evidence", []), "answer")
    def authoritative(ref: dict) -> bool:
        quote = ref["quote"]
        return (bool(ref["source_refs"]) and not _TENTATIVE.search(quote) and not _NOT_VERIFIED.search(quote)
                and not (status == "defect_confirmed" and _DEFECT_NEGATED.search(quote))
                and not (status == "config_cause_confirmed" and _CONFIG_NEGATED.search(quote)))

    if (status in _CONFIRMED_STATUS and len(causes) == 1 and authoritative(causes[0])
            and any(authoritative(ref) and _CONFIRM.search(ref["quote"]) for ref in evidence)):
        return "cause_confirmed", status, causes[0]["quote"]
    if any(_INVESTIGATED.search(quote) and not _NOT_VERIFIED.search(quote) and not _TENTATIVE.search(quote) for quote in anchored):
        return "staff_investigated", "", ""
    return "reported", "", ""


def _consultation(row: dict) -> tuple[str, str]:
    source = row["source"]
    return session_key(source), source.get("_known_merchant") or source.get("customer_id") or ""


def _aggregate_status(records: list[_Record], consultations: set[tuple[str, str]]) -> tuple[str, str]:
    confirmed = {(item.cause_type, item.cause) for item in records if item.status == "cause_confirmed"}
    if len(confirmed) == 1 and {item.key for item in records if item.status == "cause_confirmed"} == consultations:
        return "cause_confirmed", next(iter(confirmed))[0]
    if any(item.status != "reported" for item in records):
        return "staff_investigated", ""
    return "reported", ""


def _missing_context(scope: _Scope, evidence_status: str, cause_type: str, records: list[_Record]) -> list[str]:
    missing = []
    if not scope.platform:
        missing.append("使用的平台")
    if not scope.module:
        missing.append("具体功能模块")
    if scope.family in ("no_reply", "anomaly") and not scope.condition:
        missing.append("问题发生时的条件")
    if not any(item.case["has_reply"] for item in records):
        missing.append("可核对的客服处理记录")
    if scope.family == "platform_limit" and cause_type != "platform_limit_confirmed":
        missing.append("平台限制依据")
    if evidence_status == "staff_investigated" and scope.family in ("no_reply", "anomaly"):
        missing.append("原因或复测结论")
    return missing


def _card(scope: _Scope, records: list[_Record]) -> dict:
    consultations = {item.key for item in records}
    status, cause_type = _aggregate_status(records, consultations)
    known = {item.merchant for item in records if item.merchant}
    known_sessions = {item.key[0] for item in records if item.merchant}
    sessions = {key[0] for key in consultations}
    members = {}
    representatives = {}
    for item in sorted(records, key=lambda item: (not item.case["anchored"], not item.case["has_reply"],
                                                   -item.case["ts"], item.case["qa_id"], item.case["start"])):
        case = item.case
        members.setdefault((case["qa_id"], case["start"], case["end"]), case)
        preview = {name: value for name, value in case.items() if name not in ("start", "end")}
        if item.details:
            preview["details"] = item.details
        representatives.setdefault(item.key, {**preview, "quote": item.preview})
    title = scope.variant
    if scope.family == "no_reply":
        title = f"{scope.module or '回复异常'}：{scope.trigger}{scope.variant}"
    elif scope.family == "scenario_config":
        title = f"{scope.module}：{scope.variant}"
    signature = [scope.family, scope.platform, scope.module, scope.trigger, scope.preconditions, scope.variant, scope.fingerprint]
    return {"id": "issue_" + digest(signature)[:24], "family": scope.family, "title": title,
            "platform": scope.platform, "module": scope.module, "condition": scope.condition,
            "scope_status": "scoped" if scope.platform and scope.module and (scope.condition or scope.family not in ("no_reply", "anomaly")) else "needs_context",
            "evidence_status": status, "impact": {"consultations": len(consultations), "sessions": len(sessions),
                                                  "merchants": len(known), "unidentified_sessions": len(sessions - known_sessions)},
            "next": {"owner": "", "action": "", "missing": _missing_context(scope, status, cause_type, records)},
            "cases": list(representatives.values())[:5], "_members": list(members.values())}


def build(rows: list[dict]) -> dict:
    """Keep pilot cases separate until source wording supports the same issue."""
    groups = defaultdict(list)
    matched, classified = set(), set()
    for row in rows:
        if not (case := _question(row)):
            continue
        in_pilot, scope = _classify(row, case["quote"])
        if not in_pilot:
            continue
        key = _consultation(row)
        matched.add(key)
        if scope is None:
            continue
        classified.add(key)
        source = row["source"]
        status, cause_type, cause = _evidence_status(row)
        preview = _safe_label(case["quote"], source)
        groups[scope].append(_Record(key, case, status, cause_type, cause, source.get("_known_merchant") or "",
                                     preview, _case_details(row, case, preview, status)))
    items = [_card(scope, records) for scope, records in groups.items()]
    items.sort(key=lambda item: (-item["impact"]["consultations"], item["scope_status"] != "scoped",
                                 item["evidence_status"] == "reported", -item["impact"]["merchants"],
                                 -item["impact"]["sessions"], item["id"]))
    return {"items": items, "total": len(items),
            "coverage": {"source_events": len(rows), "matched_consultations": len(matched),
                         "classified_consultations": len(classified), "needs_context": len(matched - classified)}}
