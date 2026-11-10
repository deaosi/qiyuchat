"""Grounded merchant-support metadata; only enabled by the zhihuige domain.

This module does not infer a cause from a symptom, fetch context, or call a
model. Codes are search indexes; quoted evidence and status carry the facts.
Ref spans and message-relative source_refs use clean_visible_text coordinates,
as prepared by the source projection. Missing anchors remain explicit [].
"""
from __future__ import annotations

import re
from typing import Any

from .voc_model import VocModelError

VERSION = "zhg-support-v5"
_FIELDS = {"primary_intent", "fragment_role", "platforms", "modules", "merchant_goal",
           "expected", "actual", "reproduction_context", "prerequisites", "operation_path",
           "diagnosis", "actions", "tags"}
_CONFIRMED = {"config_cause_confirmed", "platform_limit_confirmed", "defect_confirmed"}
_STATUSES = _CONFIRMED | {"reported", "pending", "not_applicable"}

PROMPT_APPENDIX = r'''
领域约束：这是商家使用“知汇鸽”软件的支持会话，七鱼只是采集源。两条主线：configuration_guidance（场景配置/使用指导）与functional_anomaly（功能异常/排障）；无法确定为unclear。不要将商家后台问题改写成面向买家的商品FAQ。
高频主题按软件模块、商家目标与报告现象汇总。版本与诊断是前提或归因属性，不单独建立同症状的新主题；只有不同功能目标或报告现象才分题。软件版本、已确认原因、适用前提和方法必须保留为知识分支范围，不能因主题归并丢失限制。
商品名称、SKU、规格、价格、买家退款/退货话术通常只是复现背景或配置内容。不同SKU出现同一模块同一现象应先归同一问题；不同已确认原因保留诊断差异，不能把相同症状伪装成相同原因。原文明示产品/平台规则确实改变操作范围时才保留该限制。
不得凭缺词补平台/模块；未提到就给空数组。商家说“平台已设置但机器人没触发”只证明商家报告了预期/现象，不证明软件bug。反馈研发、帮忙看看、可能配置不对、等待定位，都不是已确认原因/缺陷。
每个intent增加domain字段，字段严格如下：
{"primary_intent":"configuration_guidance|functional_anomaly|unclear","fragment_role":"request|follow_up|reproduction_context","platforms":[],"modules":[],"merchant_goal":null,"expected":null,"actual":null,"reproduction_context":[],"prerequisites":[],"operation_path":[],"diagnosis":{"status":"reported|pending|config_cause_confirmed|platform_limit_confirmed|defect_confirmed|not_applicable","cause_code":"","cause":null,"evidence":[]},"actions":[],"tags":[]}
Ref是对象，不是字符串或数组；只能有source和quote两个键。source的值只能二选一："question"或"answer"（不能输出含竖线的"question|answer"）。例如{"source":"question","quote":"本条客户连续原文"}。不能输出start/end/role/source_refs或引用候选样例。程序计算精确跨度与商家/客服角色。merchant_goal/expected/actual须属于当前意图；其它SKU/规格/买家话术放reproduction_context，不当作新的软件咨询。
Ref类型表（[]、null和{}不能混用）：
- platforms/modules/reproduction_context/prerequisites/operation_path: Ref[]，均为数组；没有则[]，有则[{"source":"question","quote":"原文"}]，绝不是["平台名"]或单个对象。
- merchant_goal/expected/actual: Ref|null，均为单个Ref对象或null，绝不是数组或字符串。
- diagnosis.cause: Ref|null；diagnosis.evidence: Ref[]。
- actions: 操作对象数组；每项instruction: Ref（非空单对象），verification: Ref|null。suggested必须verification=null，verified必须完整客服验证Ref。
- primary_intent/fragment_role/diagnosis.status/cause_code/method_code为字符串；tags为字符串数组，不是Ref数组。
diagnosis.cause_code与actions.method_code为最多64位小写snake_case索引，不是业务事实。同义原因/操作复用相同索引；已明确场景未启用可用scene_disabled，知识未同步可用knowledge_not_synced，已确认版本触发缺陷可用version_trigger_defect。其它明确原因可用简短索引，不能把索引当出处。reported/pending/not_applicable的cause_code为空、cause=null；仅商家报告、原因未查明时不填写已确认原因。
确认原因/平台限制/产品缺陷时，cause和evidence必须来自当前answer中明确、完整逻辑句的人工客服核查/归因原话；evidence至少一项。推测、待查或“不是bug/未发现缺陷”的否定不能成为defect_confirmed。当前意图不能借另一题的诊断。没有明确确认引文就reported/pending，不能猜。
actions每项严格为{"method_code":"snake_case","instruction":Ref,"effect_status":"suggested|verified","verification":null或Ref}。所有instruction必须来自当前answer中属于本意图的完整人工客服原句；商家愿望、已尝试步骤放复现背景，不当作客服操作知识。suggested的verification=null；verified额外要求verification来自当前answer完整客服原句，且明确已经复测通过/恢复/生效，不能把“请尝试/开启后即可/应该恢复”当已验证。
完整的软件配置方法/排障建议可以作为受限操作知识，不强求客户说“好了”；但不得因此声称已查明根因或已验证有效。未知根因的未触发问题保留为排障事件及建议步骤，不能伪称已确认缺陷/通用解决方案。
只有SKU、规格、买家引述或“还是不行”等缺少独立目标的片段，fragment_role为reproduction_context/follow_up，primary_intent可unclear，不编造软件模块。需求可以保留在tags，不额外强造第三个主分类。
当前Q明确请求导出知识文档等软件使用目标时，不能因没有产品品牌名而标unclear/reproduction_context。口语能力续问如“一个店铺也不行吗”，若当前已验证人工answer完整明确说“目前暂不支持导出数据”，可仅用这个当前答句补全省略的功能对象，标准问法/主题应围绕当前可引用的软件能力；不能凭邻接会话、候选题库补对象或平台模块。单纯店名/SKU/占位、客服稍后告知或未来人工代办不构成这种证据。follow_up已有这类当前答句证据时无需永久留在背景，但操作效果、条件和角色门控保持不变。
咨询也可以是陈述式软件跟进，例如说明授权状态，而不必出现问号或“怎么”。统一依据当前Q与当前人工A判断：若Q已有软件目标，当前A有完整、可归属的条件检查步骤，应保留该诉求及完整step claim，不因actions漏填或没有疑问词永久留背景。Python仅校验这些来源与反例，不根据词表替代语义归类；检查建议不等于确定根因或已验证修复。
以下只是完整合法形状示例，必须根据本次真实输入重写，不能复述示例引文：
示例question：知汇鸽退货场景怎么配置？
示例answer：进入退货场景设置页面并保存。
合法domain示例：
```json
{"primary_intent":"configuration_guidance","fragment_role":"request","platforms":[],"modules":[{"source":"question","quote":"退货场景"}],"merchant_goal":{"source":"question","quote":"知汇鸽退货场景怎么配置？"},"expected":null,"actual":null,"reproduction_context":[],"prerequisites":[],"operation_path":[{"source":"answer","quote":"进入退货场景设置页面并保存。"}],"diagnosis":{"status":"not_applicable","cause_code":"","cause":null,"evidence":[]},"actions":[{"method_code":"save_return_scene","instruction":{"source":"answer","quote":"进入退货场景设置页面并保存。"},"effect_status":"suggested","verification":null}],"tags":["configuration"]}
```
'''.strip()

_TENTATIVE = re.compile(r"可能|也许|大概|疑似|应该|估计|不确定|尚待|待(?:确认|核查|定位|排查)|需要(?:核查|排查|确认)|反馈(?:给)?(?:技术|研发)|(?:帮|先)(?:您|你)?(?:看看|看下|核查|排查)", re.I)
_CONFIRM = re.compile(r"已(?:经)?(?:核查|确认|复现|定位|查明|查到|检查|验证|查清)|经[^。！？\n]{0,20}(?:确认|核实|核查)|确认[^。！？\n]{0,20}(?:是|为|存在)|原因(?:是|为)|(?:因为|由于)[^。！？\n]{1,80}(?:所以|导致)|导致|因此|所以")
_DEFECT = re.compile(r"\bbug\b|缺陷|(?:系统|产品|程序|版本|软件|功能|触发)[^。！？\n]{0,12}故障", re.I)
_DEFECT_NEGATED = re.compile(r"(?:不是|并非|不属于|未发现|没有|不能认定|无法确认|尚未确认|排除|非)[^，,。；;！？\n]{0,18}(?:bug|缺陷|故障)", re.I)
_CONFIG = re.compile(r"配置|开关|场景|启用|开启|绑定|同步|知识|规则|设置|参数|权限|接入")
_CONFIG_NEGATED = re.compile(r"(?:不是|并非|不属于|排除|未发现)[^，,。；;！？\n]{0,12}(?:配置|开关|场景|权限|同步|绑定)")
_PLATFORM = re.compile(r"平台|淘宝|天猫|抖音|抖店|微信小店|微信|拼多多|京东|快手|小红书|视频号|1688|有赞|微盟")
_LIMIT = re.compile(r"限制|不支持|不提供|无法|不允许|禁止|仅支持|上限|规则")
_VERIFIED = re.compile(r"已(?:经)?(?:复测|验证|测试)[^。！？\n]{0,40}(?:通过|正常|成功|生效|恢复|触发|命中)|(?:复测|验证|测试)[^。！？\n]{0,30}(?:通过|正常|成功|生效|恢复)|(?:现已|现在|目前|现)[^。！？\n]{0,12}(?:正常|成功|生效|恢复)|已(?:恢复|生效|修复)|已经(?:恢复|生效|修复)")
_NOT_VERIFIED = re.compile(r"仍(?:未|不|无法|没有)|尚未|未(?:恢复|生效|成功|通过)|没有(?:恢复|生效|成功)|不能|不正常|不生效")
_SOFTWARE_TARGET = re.compile(r"知汇鸽|机器人|场景|配置|知识|触发|同步|命中|回复|开关|绑定|平台|权限|授权|账号|帐号|后台|规则|回调|模块|设置|导出|导入")
_REQUEST_FRAME = re.compile(
    r"(?:\s*(?:您好|你好|请问|请教(?:一下)?|想问(?:一下|下)?|麻烦问(?:一下|下)?|麻烦您|请您?|我想|我们想"
    r"|在[^。！？!?\n；;，,]{1,80}(?:里|中|上|内|后台|前台|控制台|页面|界面|客户端|平台|系统|软件))[，,\s]*)+"
)
_OTHER_REQUEST = re.compile(r"怎么|如何|为何|为什么|能否|是否|并|另外|还有|同时|顺便|以及|还想|也想|再(?:问|说|看|帮)")
_REQUEST_SIGNAL = re.compile(r"怎么|如何|为何|为什么|能不能|能否|是否|可不可以|可以|支持|需要|我(?:们)?要|帮(?:忙|我|下|一下)|请|想|失败|没(?:有)?|不触发|无法|问题")
_REQUEST_SOFTWARE_TARGET = re.compile(r"知汇鸽|机器人|场景|配置|知识|触发|同步|命中|开关|绑定|权限|授权|账号|帐号|后台|规则|回调|模块|设置|导出|导入|报表|接口")
_CAPABILITY_QUERY = re.compile(r"能不能|能否|是否|可不可以|可以吗|支持(?:吗|么|不支持)?|行不行|不行(?:吗|么)|也不可以|不可以吗")
_ANSWER_SOFTWARE_TARGET = re.compile(r"导出|导入|同步|复制|权限|授权|登录|登陆|机器人|场景|配置|知识|回调|接口|报表")
_BUYER_REQUEST = re.compile(r"退货|退款|保修|价格|售价|多少钱|赔付|赔偿|发货|到货|运费")
_BACKGROUND_OBJECT = re.compile(r"\bsku\b|货号|款号|规格|颜色|尺寸|型号|毫升|公斤|厘米|\d\s*(?:ml|cm|kg|斤|码)",re.I)
_SOURCE_CHECK = re.compile(r"(?:核对|确认|检查|查看).{0,12}(?:权限|授权)|(?:请(?:您)?|建议(?:您)?|先|再|然后|需要)(?:先|再|重新)?(?:打开|进入|点击|选择|开启|启用|关闭|禁用|保存|设置|设为|配置|重启|刷新|安装|卸载|绑定|登录|登陆)|^(?:打开|进入|点击|选择|开启|启用|关闭|禁用|保存|设置|设为|配置|重启|刷新|安装|卸载|绑定|重新登录|重新登陆)")


def _error(message: str) -> None:
    raise VocModelError("领域合同：" + message) from None


def _json_type(value: Any) -> str:
    """Controlled JSON type labels; never echo unknown keys or object values."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "unsupported"


def _code(value: Any, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        _error("原因/操作索引类型无效。")
    value = value.strip()
    if optional and value in ("", "unknown", "unconfirmed"):
        return ""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        _error("原因/操作索引须为短snake_case，不能代替来源事实。")
    return value


def _message_refs(source: dict, name: str, start: int, end: int) -> list[dict]:
    parts = source.get("_question_parts" if name == "question" else "_answer_parts", [])
    if not isinstance(parts, list):
        return []
    result = []
    for part in parts:
        if (not isinstance(part, dict) or not isinstance(part.get("message_id"), str) or not part["message_id"]
                or type(part.get("start")) is not int or type(part.get("end")) is not int
                or not 0 <= part["start"] < part["end"] <= len(source.get(name, ""))):
            continue
        if part["start"] < end and start < part["end"]:
            result.append({"message_id": part["message_id"],
                           "start": max(start, part["start"]) - part["start"],
                           "end": min(end, part["end"]) - part["start"]})
    return result


def _request_position(text: str, quote: str, qspan: dict) -> int:
    inside = text.find(quote, qspan["start"], qspan["end"])
    if inside >= 0:
        return inside
    # A shortest intent quote may omit a purely locative/polite prefix. Keep
    # the full, exact request in that narrow case; never grant sentence-wide
    # authority to a second request, a prior sentence or a trailing conjunction.
    cursor = 0
    while True:
        start = text.find(quote, cursor)
        if start < 0:
            return -1
        end = start + len(quote)
        if start <= qspan["start"] < qspan["end"] <= end:
            prefix, suffix = text[start:qspan["start"]], text[qspan["end"]:end]
            if (len(prefix) <= 160 and not suffix.strip(" \t。？！!?.")
                    and (not prefix or _REQUEST_FRAME.fullmatch(prefix))
                    and not _OTHER_REQUEST.search(prefix)):
                return start
        cursor = end


def _ref(value: Any, source: dict, qspan: dict, aligner: Any,
         *, current_request: bool = False, path: str = "domain.ref") -> dict:
    if not isinstance(value, dict) or set(value) != {"source", "quote"}:
        _error(f"{path}: Ref只允许source和quote，实际类型={_json_type(value)}。")
    if value["source"] not in ("question", "answer"):
        _error(f"{path}.source: 只允许question或answer，实际类型={_json_type(value['source'])}。")
    quote = value["quote"]
    if not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 3000:
        _error(f"{path}.quote: 引用长度或类型无效，实际类型={_json_type(quote)}。")
    text = source.get(value["source"], "")
    if not isinstance(text, str):
        _error(f"{path}: 来源正文类型无效。")
    # Repeated question text must resolve inside this intent's selected span.
    if current_request and value["source"] == "question":
        desired = _request_position(text, quote, qspan)
        if desired < 0:
            _error(f"{path}: 请求字段引用了另一意图。")
        occurrence, cursor = 1, 0
        while True:
            found = text.find(quote, cursor)
            if found < 0 or found >= desired:
                break
            occurrence += 1
            cursor = found + len(quote)
        try:
            span = aligner.align(text, quote, occurrence)
        except Exception:
            _error(f"{path}: 引用未通过精确原文校验。")
    else:
        try:
            span = aligner.align(text, quote)
        except Exception:
            _error(f"{path}: 引用未通过精确原文校验。")
    if (not isinstance(span, dict) or type(span.get("start")) is not int or type(span.get("end")) is not int
            or span.get("alignment") != "exact" or text[span["start"]:span["end"]] != quote):
        _error(f"{path}: 引用未通过精确原文校验。")
    role = "merchant" if value["source"] == "question" else "staff" if source.get("_staff_verified") is True else "unknown"
    return {"source": value["source"], "quote": quote, "start": span["start"],
            "end": span["end"], "alignment": "exact", "role": role,
            "source_refs": _message_refs(source, value["source"], span["start"], span["end"])}


def _refs(value: Any, source: dict, qspan: dict, aligner: Any, *, path: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 20:
        _error(f"{path}: 引用列表过长或类型无效，实际类型={_json_type(value)}。")
    refs = [_ref(item, source, qspan, aligner, path=f"{path}[{index}]") for index, item in enumerate(value)]
    identities = [(item["source"], item["start"], item["end"]) for item in refs]
    if len(set(identities)) != len(identities):
        _error(f"{path}: 不能重复引用制造额外证据。")
    return refs


def _staff_fact(ref: dict, source: dict, qspan: dict, *, path: str) -> None:
    """Full human-staff sentences, conservatively bound to this request."""
    if ref["source"] != "answer" or ref["role"] != "staff":
        _error(f"{path}: 确认或验证只能使用当前人工客服原话。")
    # Lazy reuse avoids a module import cycle with the extraction owner.
    from .voc_extract import claim_context_checks, logical_units
    claim = {"start": ref["start"], "end": ref["end"], "quote": ref["quote"]}
    current = {"question_span": qspan, "claims": [claim]}
    others = [{"question_span": {"quote": unit["text"]}, "claims": []}
              for unit in logical_units(source["question"])
              if unit["end"] <= qspan["start"] or unit["start"] >= qspan["end"]]
    result = claim_context_checks(source["answer"], [current, *others])[0]
    if not result["full_sentences"] or not result.get("intent_context_bound", True):
        _error(f"{path}: 确认引文须保留完整句子且属于当前意图，不能截掉否定或借别题诊断。")


def current_software_capability(source: dict, qspan: dict, aligner: Any = None) -> dict:
    """Source-bound demand recovery; no adjacent context or catalog authority.

    A real capability question may omit its object when the *current*, verified
    staff sentence supplies it. This proves only that a software consultation
    exists. It never promotes the statement to a confirmed cause or invents a
    platform/module. Complete sentence and current-intent ownership remain
    mandatory for the answer target.
    """
    from .voc_extract import logical_units
    from .voc_variants import _capability_unit, _software_method
    text=source.get("question", "")
    start,end=qspan.get("start"),qspan.get("end")
    empty={"strong_request":False,"answer_target":False,"answer_refs":[],"step_target":False,"step_refs":[]}
    if (not isinstance(text,str) or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(text)):
        return empty
    question=text[start:end]
    software_target=bool(_REQUEST_SOFTWARE_TARGET.search(question))
    explicit=bool(software_target and _REQUEST_SIGNAL.search(question))
    eligible=explicit or bool(_CAPABILITY_QUERY.search(question) and not _BUYER_REQUEST.search(question)
                              and not _BACKGROUND_OBJECT.search(question))
    refs=[];steps=[]
    answer=source.get("answer", "")
    if (eligible or software_target) and source.get("_staff_verified") is True and isinstance(answer,str):
        for unit in logical_units(answer):
            capability=eligible and bool(_ANSWER_SOFTWARE_TARGET.search(unit["text"])) and _capability_unit(unit["text"])
            step=software_target and bool(_SOURCE_CHECK.search(unit["text"])) and _software_method(unit["text"])
            if not capability and not step:
                continue
            left,right=unit["start"],unit["end"]
            while right<len(answer) and answer[right] in "。！？!?":right+=1
            quote=answer[left:right]
            item={"source":"answer","quote":quote,"start":left,"end":right,"alignment":"exact","role":"staff",
                  "source_refs":_message_refs(source,"answer",left,right)}
            try:
                if aligner is not None:
                    occurrence,cursor=1,0
                    while True:
                        found=answer.find(quote,cursor)
                        if found<0 or found>=left:break
                        occurrence+=1;cursor=found+len(quote)
                    span=aligner.align(answer,quote,occurrence)
                    if span.get("alignment")!="exact" or span.get("start")!=left or span.get("end")!=right:continue
                _staff_fact(item,source,{**qspan,"quote":question},path="domain.primary_intent")
            except Exception:
                continue
            if capability:refs.append(item)
            if step:steps.append(item)
    return {"strong_request":explicit or bool(refs) or bool(steps),"answer_target":bool(refs),"answer_refs":refs,
            "step_target":bool(steps),"step_refs":steps}


def validate_domain(raw: Any, source: dict, qspan: dict, aligner: Any) -> dict:
    """Validate a raw domain object; never silently turn a claim into a fact."""
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        _error("领域字段不符合约定。")
    if raw["primary_intent"] not in ("configuration_guidance", "functional_anomaly", "unclear"):
        _error("主分类无效。")
    if raw["fragment_role"] not in ("request", "follow_up", "reproduction_context"):
        _error("片段角色无效。")
    if (not isinstance(qspan, dict) or type(qspan.get("start")) is not int or type(qspan.get("end")) is not int
            or not isinstance(source.get("question"), str) or not 0 <= qspan["start"] < qspan["end"] <= len(source["question"])):
        _error("当前意图跨度无效。")
    qspan = {**qspan, "quote": source["question"][qspan["start"]:qspan["end"]]}
    result = {"primary_intent": raw["primary_intent"], "fragment_role": raw["fragment_role"]}
    for name in ("platforms", "modules", "reproduction_context", "prerequisites", "operation_path"):
        result[name] = _refs(raw[name], source, qspan, aligner, path="domain."+name)
    for name in ("merchant_goal", "expected", "actual"):
        result[name] = None if raw[name] is None else _ref(raw[name], source, qspan, aligner, current_request=True, path="domain."+name)
    diagnosis = raw["diagnosis"]
    if not isinstance(diagnosis, dict) or set(diagnosis) != {"status", "cause_code", "cause", "evidence"} or not isinstance(diagnosis.get("status"), str) or diagnosis["status"] not in _STATUSES:
        _error("诊断字段无效。")
    status = diagnosis["status"]
    cause = None if diagnosis["cause"] is None else _ref(diagnosis["cause"], source, qspan, aligner, path="domain.diagnosis.cause")
    evidence = _refs(diagnosis["evidence"], source, qspan, aligner, path="domain.diagnosis.evidence")
    cause_code = _code(diagnosis["cause_code"], optional=status not in _CONFIRMED)
    if status not in _CONFIRMED:
        if cause is not None or cause_code:
            _error("待定位/商家报告不能携带已确认原因。")
    else:
        if cause is None or not evidence:
            _error("已确认原因缺少完整人工客服证据。")
        _staff_fact(cause, source, qspan, path="domain.diagnosis.cause")
        for index, ref in enumerate(evidence):
            _staff_fact(ref, source, qspan, path=f"domain.diagnosis.evidence[{index}]")
        text = "\n".join(ref["quote"] for ref in [cause, *evidence])
        if _TENTATIVE.search(text) or not _CONFIRM.search(text):
            _error("推测/反馈/待查不能当作已确认原因。")
        if status == "defect_confirmed" and (not _DEFECT.search(text) or _DEFECT_NEGATED.search(text)):
            _error("没有明确正向缺陷确认，或原文已否定缺陷。")
        if status == "config_cause_confirmed" and (not _CONFIG.search(text) or _CONFIG_NEGATED.search(text)):
            _error("没有明确配置原因确认，或原文已否定配置原因。")
        if status == "platform_limit_confirmed" and (not _PLATFORM.search(text) or not _LIMIT.search(text)):
            _error("没有明确平台限制确认。")
    result["diagnosis"] = {"status": status, "cause_code": cause_code, "cause": cause, "evidence": evidence}
    if not isinstance(raw["actions"], list) or len(raw["actions"]) > 12:
        _error("操作列表无效。")
    actions = []
    for index, action in enumerate(raw["actions"]):
        if not isinstance(action, dict) or set(action) != {"method_code", "instruction", "effect_status", "verification"} or action["effect_status"] not in ("suggested", "verified"):
            _error("操作字段无效。")
        action_path = f"domain.actions[{index}]"
        instruction = _ref(action["instruction"], source, qspan, aligner, current_request=True, path=action_path+".instruction")
        _staff_fact(instruction, source, qspan, path=action_path+".instruction")
        verification = None if action["verification"] is None else _ref(action["verification"], source, qspan, aligner, path=action_path+".verification")
        if action["effect_status"] == "verified":
            if verification is None:
                _error("已验证效果缺少客服复测原话。")
            _staff_fact(verification, source, qspan, path=action_path+".verification")
            if not _VERIFIED.search(verification["quote"]) or _NOT_VERIFIED.search(verification["quote"]) or _TENTATIVE.search(verification["quote"]):
                _error("建议/预期/失败结果不能当作已验证效果。")
        elif verification is not None:
            _error("建议操作不能携带已验证效果。")
        actions.append({"method_code": _code(action["method_code"]), "instruction": instruction,
                        "effect_status": action["effect_status"], "verification": verification})
    result["actions"] = actions
    if not isinstance(raw["tags"], list) or len(raw["tags"]) > 10 or any(not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 80 or any(ord(char) < 32 for char in tag) for tag in raw["tags"]):
        _error("标签类型或长度无效。")
    result["tags"] = list(dict.fromkeys(tag.strip() for tag in raw["tags"]))
    current=current_software_capability(source,qspan,aligner)
    if current["strong_request"] and raw["primary_intent"]=="unclear":
        _error("domain.primary_intent: 当前问答已明确支持软件使用诉求，须重提取标准问题与主分类，不能静默当作背景。")
    if current["strong_request"] and raw["fragment_role"]=="reproduction_context":
        _error("domain.fragment_role: 当前问答已明确支持软件使用诉求，须重提取诉求片段，不能静默当作背景。")
    explicit_target = any(ref and _SOFTWARE_TARGET.search(ref["quote"])
                          for ref in (result["merchant_goal"], result["actual"]))
    result["needs_context"] = (raw["primary_intent"] == "unclear"
                               or raw["fragment_role"] == "reproduction_context"
                               or raw["fragment_role"] == "follow_up" and not (explicit_target or current["answer_target"] or current["step_target"]))
    result["version"] = VERSION
    return result


DOMAIN_CASES = [
    {"question": "知汇鸽的退货场景已配置，SKU A01的买家问退货时机器人没有触发。", "answer": "收到，我先核查触发记录和场景绑定，暂时无法确认原因。", "expected_topic": "scenario_not_triggered", "answer_kind": "contextual", "must_not_be_ready": True, "expected_primary_intent": "functional_anomaly", "expected_diagnosis": "pending", "expected_knowledge_kind": "incident"},
    {"question": "SKU B02也遇到同样的问题：知汇鸽退货场景配置后，买家问退货没有触发。", "answer": "我先检查触发记录和场景绑定，暂时无法确认原因。", "expected_topic": "scenario_not_triggered", "answer_kind": "contextual", "must_not_be_ready": True, "expected_primary_intent": "functional_anomaly", "expected_diagnosis": "pending", "expected_knowledge_kind": "incident"},
    {"question": "在知汇鸽里怎么设置买家问退货时的回复？", "answer": "进入场景配置，选择退货场景，填写本店退货口径，开启场景后保存。", "expected_topic": "configure_return_scene", "answer_kind": "resolved_answer", "must_not_be_ready": False, "expected_primary_intent": "configuration_guidance", "expected_diagnosis": "not_applicable", "expected_knowledge_kind": "configuration_howto"},
    {"question": "知汇鸽的退货场景不触发是什么原因？", "answer": "已核查，该退货场景未启用，导致机器人没有触发。开启场景并保存后即可触发对应回复。", "expected_topic": "scenario_not_triggered", "answer_kind": "resolved_answer", "must_not_be_ready": False, "expected_primary_intent": "functional_anomaly", "expected_diagnosis": "config_cause_confirmed", "expected_cause_code": "scene_disabled", "expected_knowledge_kind": "configuration_resolution"},
    {"question": "平台后台已有退货知识，知汇鸽机器人却没有触发该场景。", "answer": "已核查，平台后台的新知识尚未同步到知汇鸽，导致当前场景无法命中。请先同步知识，再保存并测试场景。", "expected_topic": "scenario_not_triggered", "answer_kind": "resolved_answer", "must_not_be_ready": False, "expected_primary_intent": "functional_anomaly", "expected_diagnosis": "config_cause_confirmed", "expected_cause_code": "knowledge_not_synced", "expected_knowledge_kind": "configuration_resolution"},
    {"question": "商家后台已经设置好了，知汇鸽机器人仍然没触发，是不是系统bug？", "answer": "收到，我帮您看下，目前还需要排查。", "expected_topic": "scenario_not_triggered", "answer_kind": "contextual", "must_not_be_ready": True, "expected_primary_intent": "functional_anomaly", "expected_diagnosis": "pending", "expected_knowledge_kind": "incident"},
    {"question": "知汇鸽2.8版本的机器人场景无法触发，有临时处理办法吗？", "answer": "研发已确认这是知汇鸽2.8版本的触发缺陷。临时关闭再开启场景并保存，已复测机器人可以正常触发。", "expected_topic": "scenario_not_triggered", "answer_kind": "resolved_answer", "must_not_be_ready": False, "expected_primary_intent": "functional_anomaly", "expected_diagnosis": "defect_confirmed", "expected_knowledge_kind": "verified_workaround"},
    {"question": "买家测试话术是‘7天退货’，我要在知汇鸽里配置这个回复，不是问商品能不能退。", "answer": "买家的‘7天退货’只是测试问法。请在知汇鸽的退货场景绑定该商品并开启场景，然后保存测试。", "expected_topic": "configure_return_scene", "answer_kind": "resolved_answer", "must_not_be_ready": False, "expected_primary_intent": "configuration_guidance", "expected_diagnosis": "not_applicable", "expected_knowledge_kind": "configuration_howto", "forbidden_knowledge_kind": "buyer_product_policy"},
    {"question": "SKU A01，500ml，蓝色款。", "answer": "", "expected_topic": "reproduction_context", "answer_kind": "none", "must_not_be_ready": True, "expected_primary_intent": "unclear", "expected_diagnosis": "not_applicable", "expected_knowledge_kind": "context_needed", "expected_fragment_role": "reproduction_context"},
]
