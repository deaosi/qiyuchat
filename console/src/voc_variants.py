"""Knowledge branches for merchant software support, separate from demand topics.

Topic counts include unresolved reports. Knowledge branches include only cited
software methods and keep diagnosis/effect status separate from reusability.
"""
from __future__ import annotations

import copy
import re

from . import voc_assets as assets, voc_quality as quality
from .voc_extract import VERSION, canonical, claim_context_checks, logical_units, numeric_signature

VARIANT_VERSION = "software-branches-v5"


def mode(cfg) -> str:
    value = str(getattr(cfg.runtime, "voc_domain", "") or "")
    if value not in ("", "zhihuige"):
        raise ValueError("当前项目领域未配置适配器。")
    return value


def pipeline(cfg) -> str:
    if not mode(cfg):
        return VERSION
    from . import voc_domain
    return VERSION + "/" + voc_domain.VERSION


def quality_version(cfg) -> str:
    if not mode(cfg):
        return quality.VERSION
    from . import voc_domain
    return quality.VERSION + "/" + voc_domain.VERSION + "/" + VARIANT_VERSION


def consultation_key(row: dict) -> tuple:
    return quality.session_key(row["source"]), row["source"]["customer_id"], row["topic_id"]


def refs_key(domain: dict) -> tuple:
    return tuple(sorted({(name, canonical(ref["quote"])) for name in ("platforms", "modules", "prerequisites")
                         for ref in domain.get(name, [])}))


_OTHER_REQUEST = re.compile(r"怎么|如何|为何|为什么|能否|是否|另外|还有|同时|顺便|以及|还想|也想|再问")
_VERSION_REF = re.compile(r"\d+(?:\.\d+){0,3}\s*(?:版本|版)|(?:当前|目前|现有|这个|该|此)版本")
_APPLICABILITY_PREMISE = re.compile(r"如果|若|只有|仅限|只限|除非|前提|适用于|仅(?:管理员|主账号|子账号|[^，,。；;]{1,10}(?:用户|账号|帐号|版))")


def _ref_bound(row: dict, name: str, start: int, end: int) -> bool:
    source, data = row["source"], row["data"]
    qspan = data["question_span"]
    if name == "question":
        if qspan["start"] <= start < end <= qspan["end"]:
            return True
        # A shortest request quote may omit its preceding version/permission
        # frame. A second request, sentence or intent never supplies that frame.
        if data.get("source_intent_count", 1) != 1:
            return False
        for unit in logical_units(source["question"]):
            if unit["start"] <= min(start,qspan["start"]) and max(end,qspan["end"]) <= unit["end"] + 1:
                outside = source["question"][unit["start"]:qspan["start"]] + source["question"][qspan["end"]:unit["end"]]
                return not _OTHER_REQUEST.search(outside)
        return False
    if not source.get("_staff_verified"):
        return False
    units = logical_units(source["answer"])
    touched = {i for i,unit in enumerate(units) if start < unit["end"] and unit["start"] < end}
    if not touched:
        return False
    context = data.get("claim_context", {})
    if data.get("source_intent_count", 1) > 1 and (not context.get("intent_context_bound") or context.get("shared_context")
                                                 or not touched <= set(context.get("units", []))):
        return False
    current = {"question_span":qspan,"claims":[{"start":units[i]["start"],"end":units[i]["end"],"quote":units[i]["text"]} for i in sorted(touched)]}
    others = [{"question_span":{"quote":unit["text"]},"claims":[]} for unit in logical_units(source["question"])
              if unit["end"] <= qspan["start"] or unit["start"] >= qspan["end"]]
    return claim_context_checks(source["answer"],[current,*others])[0].get("intent_context_bound",True)


def constraint_projection(row: dict) -> dict:
    """Unify all current source-backed applicability, never infer a new scope.

    Existing extraction spans are rechecked before projection. Roles and message
    offsets are program-derived. A bad/foreign constraint withholds the branch;
    it is never dropped while leaving a broader ready answer behind.
    """
    source, data = row["source"], row["data"]
    from .voc_domain import _message_refs
    candidates = [(ref, ref.get("quote", "")) for ref in data.get("scope", [])]
    domain = data.get("domain", {})
    candidates += [(ref,ref.get("quote", "")) for name in ("platforms","modules","prerequisites") for ref in domain.get(name, [])]
    subset = _usable_subset(row)
    if subset:
        for claim in subset["data"]["claims"]:
            # Recover concrete version frames even if both model metadata fields
            # omitted them. Do not mistake a required setting's numeric value
            # for a version or a distinct applicability condition.
            for match in _VERSION_REF.finditer(claim["quote"]):
                candidates.append(({"source":"answer","quote":match.group(),"start":claim["start"]+match.start(),
                                    "end":claim["start"]+match.end(),"alignment":"exact"},match.group()))
            premise = _APPLICABILITY_PREMISE.search(claim["quote"])
            if premise:
                # Keep the complete certified sentence for display/audit, but
                # index its actual condition before a separate consequence. A
                # 7/30-second obligation must still meet the same-condition rule.
                condition = re.split(r"[，,；;。！？!?]|(?:那么|则|就请|然后)",claim["quote"][premise.start():],maxsplit=1)[0]
                candidates.append(({"source":"answer",**claim},condition))
    refs, keys, seen, invalid = [], set(), set(), False
    for ref,key_quote in candidates:
        name,start,end,quote = ref.get("source"),ref.get("start"),ref.get("end"),ref.get("quote")
        text=source.get(name, "") if name in ("question","answer") else ""
        if (name not in ("question","answer") or not isinstance(quote,str) or not quote
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or ref.get("alignment") != "exact" or text[start:end] != quote or not _ref_bound(row,name,start,end)):
            invalid = True
            continue
        identity=(name,start,end)
        if identity not in seen:
            refs.append({"source":name,"quote":quote,"start":start,"end":end,"alignment":"exact",
                         "role":"merchant" if name=="question" else "staff" if source.get("_staff_verified") is True else "unknown",
                         "source_refs":_message_refs(source,name,start,end)})
            seen.add(identity)
        keys.add((canonical(key_quote),numeric_signature(key_quote)))
    return {"refs":refs,"key":tuple(sorted(keys)),"invalid":invalid}


def applicability_refs(row: dict) -> list[dict]:
    return constraint_projection(row)["refs"]


# These are predicates over current source text, never authority from model labels.
_SOFTWARE_OBJECT = re.compile(r"知汇鸽|机器人|场景|配置|知识|触发|同步|命中|开关|绑定|平台|权限|规则|回调|模块|设置|页面|菜单|控制台|报表|参数|插件|账号|帐号|授权|登录|登陆|导出|导入")
_SOFTWARE_ACTION = re.compile(r"打开|进入|点击|选择|开启|启用|关闭|禁用|绑定|保存|设置|设为|配置|同步|导入|导出|更新|重启|刷新|安装|卸载|(?:核对|确认|检查|查看).{0,12}权限|登录|登陆|授权")
_BUYER_POLICY = re.compile(r"(?:本店|你店|贵店|我们店|店铺|商品|产品).{0,20}(?:支持|可以|允许|提供|享受).{0,15}(?:退货|退款|保修|赔偿|无理由)|退款.{0,20}(?:到账|退回)|(?:发货|到货).{0,10}(?:天|小时)")
_REPRODUCTION = re.compile(r"(?:只是|仅(?:是|作|作为)).{0,12}(?:测试|示例|问法|话术)|买家.{0,20}(?:测试问法|测试话术|示例话术)")
_PREMISE = re.compile(r"如果|若|仅|只有|只限|除非|前提|必须|需要|不需要|不能|不支持|无法|权限|管理员|版本|期间|适用|具备|允许|才可")
_EXCLUSIVE = re.compile(r"必须|只(?:能|允许|支持)|仅(?:能|允许|支持)|上限|最大|最少|至少|不得超过|不(?:能|得)低于|其他.{0,10}(?:无法|不能|不支持)")
_NUMBER_VALUE = re.compile(r"\d+(?:\.\d+)?|[一二三四五六七八九十百千万两半]+(?=秒|分钟|小时|天|次|条|个|%)")
_NOTICE = re.compile(r"同步给(?:您|你)|(?:结果|消息|进展|安排|通知).{0,16}(?:告知|通知|同步)|(?:有|出|等).{0,12}(?:结果|消息).{0,20}(?:同步|通知|告知)")
_HUMAN_SERVICE = re.compile(r"(?:我(?:们)?|这边|人工|客服).{0,20}(?:帮|给|为)(?:您|你).{0,20}(?:导出|导入|同步|操作|处理|修改)|(?:帮|给|为)(?:您|你).{0,12}(?:导出|导入)|(?:导出|导入).{0,12}(?:给|发给)(?:您|你)")
_HUMAN_FOLLOWUP = re.compile(r"(?:我(?:们)?|这边|人工|客服).{0,20}(?:核查|检查|核实|排查|看下|看看|上报|反馈|转交)|(?:反馈|转交|上报|转给|提交给).{0,12}(?:技术|研发|同事)|(?:帮|给|为)(?:您|你).{0,10}(?:核查|检查|核实|排查|看下|看看)")
_ONE_OFF_CHANGE = re.compile(r"^(?:这(?:次|边)|本次|目前|现在|已经|已|帮您|帮你|给您|给你|这次已|本次已).{0,16}(?:修改|调整|处理|更改|改好)(?:了|好|完成|过|的)?[哈呀呢哦\s。！!]*$")
_ONE_OFF_CONTEXT = re.compile(r"(?:本次|这次|已|已经|这边已).{0,12}(?:修改|处理|调整|更改|改好)(?:了|好|完成)?")
_CAPABILITY = re.compile(r"支持|不提供|不允许|不可以|可以|允许|具备|提供.{0,8}功能|已(?:上线|开放)|尚未(?:上线|开放)|暂未(?:上线|开放)")
_CAPABILITY_UNCERTAIN = re.compile(r"可能|也许|大概|应该|不确定|(?:尚未|还未|无法|不能|暂未)(?:确认|确定)|未确定|待(?:确认|核查|定位|排查)|预计|计划|希望|期望|想要|争取|据说|听说|(?:您|你|商家|客户).{0,6}(?:说|反馈|表示)")
_OPERATIONS = re.compile(r"导出|导入|同步|复制|触发|命中|绑定|授权|权限|登录|登陆|自动回复|批量回复")
_TARGETS = re.compile(r"商品|知识|问答|日志|订单|报表|场景|消息|账号|帐号")
_DIRECTED_ACTION = re.compile(r"请|建议|试(?:试|下|一下)|打开|进入|点击|选择|开启|启用|关闭|禁用|保存|设置|设为|配置|重启|刷新|安装|卸载|核对|确认|检查|查看|登录|登陆")


def _is_notice_or_service(text: str) -> bool:
    return bool(_NOTICE.search(text) or _HUMAN_SERVICE.search(text) or _HUMAN_FOLLOWUP.search(text))


def _software_method(text: str) -> bool:
    return bool(_SOFTWARE_OBJECT.search(text) and _SOFTWARE_ACTION.search(text)
                and not _is_notice_or_service(text))


def _buyer_only(text: str) -> bool:
    return bool(_BUYER_POLICY.search(text) and not _SOFTWARE_OBJECT.search(text))


def _effective_actions(row: dict) -> list[dict]:
    """Recover missing redundant domain actions only from certified step claims.

    This does not extract a new step from arbitrary transcript text. The generic
    model claim must already be exact, complete, current-staff and independently
    bound to this intent. Recovery is always a suggestion, never a cause/effect
    confirmation, and keeps the original condition in its complete sentence.
    """
    source,data=row["source"],row["data"]
    domain=data.get("domain",{})
    if domain.get("actions"):
        return domain["actions"]
    if (source.get("_staff_verified") is not True or domain.get("needs_context")
            or quality.only_collects_information(source["answer"],data["question_span"]["quote"])):
        return []
    from .voc_domain import _SOURCE_CHECK, _message_refs, _staff_fact, current_software_capability
    if not current_software_capability(source,data["question_span"])["step_target"]:
        return []
    context=data.get("claim_context",{})
    if data.get("source_intent_count",1)>1 and (not context.get("intent_context_bound") or context.get("shared_context")):
        return []
    result=[]
    for claim in data.get("claims",[]):
        start,end,quote=claim.get("start"),claim.get("end"),claim.get("quote")
        if (claim.get("type")!="step" or claim.get("alignment")!="exact" or type(start) is not int or type(end) is not int
                or not isinstance(quote,str) or not 0<=start<end<=len(source["answer"]) or source["answer"][start:end]!=quote
                or not _SOURCE_CHECK.search(quote) or not _software_method(quote)):
            continue
        instruction={"source":"answer","quote":quote,"start":start,"end":end,"alignment":"exact","role":"staff",
                     "source_refs":_message_refs(source,"answer",start,end)}
        try:_staff_fact(instruction,source,data["question_span"],path="domain.actions")
        except Exception:continue
        method="source_permission_check" if re.search(r"(?:核对|确认|检查|查看).{0,12}(?:权限|授权)",quote) else "source_verified_step"
        result.append({"method_code":method,"instruction":instruction,"effect_status":"suggested","verification":None})
    return result


def software_subset(row: dict) -> dict | None:
    """Select complete current-staff software sentences without changing source.

    A subset certificate is local and deterministic. It cannot be provided by a
    model. Independent buyer-policy sentences are omitted; ambiguous mixed
    sentences and omitted premises are withheld, never repaired by paraphrase.
    """
    source, data = row["source"], row["data"]
    domain = data.get("domain", {})
    actions = _effective_actions(row)
    if not source.get("_staff_verified") or not actions or domain.get("needs_context"):
        return None
    if quality.only_collects_information(source["answer"], data["question_span"]["quote"]):
        return None
    original_context = data.get("claim_context", {})
    if data.get("source_intent_count", 1) > 1 and (not original_context.get("intent_context_bound") or original_context.get("shared_context")):
        return None
    units = logical_units(source["answer"])
    refs = [action["instruction"] for action in actions]
    refs += [ref for action in actions for ref in [action.get("verification")] if ref]
    refs += domain.get("prerequisites", [])
    refs += data.get("scope", [])
    diagnosis = domain.get("diagnosis", {})
    refs += [ref for ref in [diagnosis.get("cause"), *diagnosis.get("evidence", [])] if ref]
    staff_refs = [ref for ref in refs if ref.get("source") == "answer" and ref.get("role", "staff") == "staff"]
    action_units = {i for i, unit in enumerate(units) for action in actions
                    if action["instruction"].get("role") == "staff" and action["instruction"]["start"] < unit["end"]
                    and unit["start"] < action["instruction"]["end"] and _software_method(unit["text"])}
    if not action_units:
        return None
    selected = set(action_units)
    own_units = set(original_context.get("units", []))
    for i, unit in enumerate(units):
        text = unit["text"]
        touched = any(ref["start"] < unit["end"] and unit["start"] < ref["end"] for ref in staff_refs)
        # A full sentence containing both an unrelated buyer promise and a
        # software action cannot be safely split at a comma.
        if (i in selected or touched) and _BUYER_POLICY.search(text) and _SOFTWARE_OBJECT.search(text):
            return None
        if _buyer_only(text) or _REPRODUCTION.search(text) and not _software_method(text) or not unit["substantive"]:
            continue
        if touched:
            selected.add(i)
        elif data.get("source_intent_count", 1) == 1:
            # Preserve unlabelled software context/prerequisites as well. Do not
            # approve an isolated action with an unexplained preceding sentence.
            if _SOFTWARE_OBJECT.search(text) or _PREMISE.search(text) or _ONE_OFF_CHANGE.fullmatch(text):
                selected.add(i)
            else:
                return None
        elif i in own_units:
            selected.add(i)
        elif _PREMISE.search(text):
            return None
    claims = []
    for i in sorted(selected):
        unit = units[i]
        if _buyer_only(unit["text"]):
            continue
        kind = "step" if i in action_units else "condition" if _PREMISE.search(unit["text"]) else "fact"
        start, end = unit["start"], unit["end"]
        # Keep punctuation when present; original offsets remain authoritative.
        while end < len(source["answer"]) and source["answer"][end] in "。！？!?":
            end += 1
        claims.append({"type": kind, "quote": source["answer"][start:end], "start": start, "end": end, "alignment": "exact"})
    if not claims:
        return None
    result = copy.deepcopy(row)
    if not domain.get("actions"):
        result["data"]["domain"]["actions"]=copy.deepcopy(actions)
        result["data"]["source_steps_recovered"]=True
    result["data"].update(claims=claims, answer_kind="resolved_answer", software_context_validated=True,
                          one_off_dependent=any(_ONE_OFF_CONTEXT.search(unit["text"]) for i, unit in enumerate(units) if i in selected))
    return result


def method_is_usable(row: dict) -> bool:
    return software_subset(row) is not None


def _capability_unit(text: str) -> bool:
    """An asserted capability, not a wish, a question or a human service offer."""
    return bool(_CAPABILITY.search(text) and not _CAPABILITY_UNCERTAIN.search(text)
                and not _is_notice_or_service(text) and not _BUYER_POLICY.search(text)
                and not re.search(r"请|建议|试试|试下|尝试|不妨|可以先|您可以|你可以|重新登[录陆]", text)
                and not re.search(r"吗|么|能否|是否|[?？]", text))


def capability_subset(row: dict) -> dict | None:
    """Certify source-complete software capability facts, even oral short replies.

    The software target may be in the current question (e.g. an export request
    followed by '目前不支持的呢'). It is never borrowed from another intent,
    adjacent context, model codes or the catalog. Complete staff sentences and
    their premises remain extractive; an independent later manual offer is not
    transformed into a self-service capability.
    """
    source, data = row["source"], row["data"]
    domain = data.get("domain", {})
    question = data["question_span"]["quote"]
    from .voc_domain import current_software_capability
    answer_target = current_software_capability(source,data["question_span"])["answer_target"]
    if (not source.get("_staff_verified") or domain.get("needs_context")
            or domain.get("primary_intent") == "unclear" or not (_SOFTWARE_OBJECT.search(question) or answer_target)
            or _buyer_only(question)):
        return None
    context = data.get("claim_context", {})
    multi = data.get("source_intent_count", 1) > 1
    if multi and (not context.get("intent_context_bound") or context.get("shared_context")):
        return None
    units = logical_units(source["answer"])
    own = set(context.get("units", []))
    selected = {i for i, unit in enumerate(units) if _capability_unit(unit["text"])
                and (not multi or i in own)}
    if not selected:
        return None
    for i, unit in enumerate(units):
        text = unit["text"]
        if not unit["substantive"] or i in selected:
            continue
        if multi and i not in own:
            # A prerequisite with no safe per-intent binding must not disappear.
            if _PREMISE.search(text):
                return None
            continue
        if _is_notice_or_service(text) or _buyer_only(text) or _REPRODUCTION.search(text):
            continue
        scoped = any(ref.get("source") == "answer" and ref["start"] < unit["end"] and unit["start"] < ref["end"]
                     for ref in data.get("scope", []))
        if _PREMISE.search(text) or scoped:
            # Keep complete current/temporary/version/permission restrictions.
            # Uncertain or unrelated premises cannot be silently omitted.
            if _CAPABILITY_UNCERTAIN.search(text):
                return None
            selected.add(i)
        elif _SOFTWARE_OBJECT.search(text) and not _software_method(text):
            selected.add(i)
        elif not _software_method(text):
            return None
    claims = []
    for i in sorted(selected):
        unit = units[i]; start, end = unit["start"], unit["end"]
        while end < len(source["answer"]) and source["answer"][end] in "。！？!?":
            end += 1
        claims.append({"type":"condition" if not _capability_unit(unit["text"]) else "fact",
                       "quote":source["answer"][start:end], "start":start, "end":end, "alignment":"exact"})
    result = copy.deepcopy(row)
    result["data"].update(claims=claims, answer_kind="resolved_answer", software_context_validated=True,
                          capability_context_validated=True)
    return result


def _usable_subset(row: dict) -> dict | None:
    return software_subset(row) or capability_subset(row)


def _is_capability(row: dict) -> bool:
    capability = capability_subset(row)
    if not capability:
        return False
    method = software_subset(row)
    return not method or not any(_DIRECTED_ACTION.search(action["instruction"]["quote"])
                                for action in _effective_actions(row))


def _operation_targets(row: dict, text: str) -> set[tuple[str, str]]:
    operations = set(_OPERATIONS.findall(text))
    question = row["data"]["question_span"]["quote"]
    if not operations:
        operations = set(_OPERATIONS.findall(question))
    objects = set(_TARGETS.findall(text)) or set(_TARGETS.findall(question))
    return {(op.replace("登陆", "登录"), obj) for op in operations for obj in (objects or {""})}


def _capability_assertions(row: dict) -> set[tuple[str, str, bool]]:
    # A real operational method may mention a temporary symptom or its verified
    # result. Neither is a separate assertion about permanent product capacity.
    if not _is_capability(row):
        return set()
    subset = capability_subset(row)
    if not subset:
        return set()
    values = set()
    for claim in subset["data"]["claims"]:
        for clause in re.split(r"[，,；;。！？!?\n]|但是|但|不过|而", claim["quote"]):
            if not _capability_unit(clause) or re.search(r"不是不|并非不|不能说不|不代表不", clause):
                continue
            negative = bool(re.search(r"不支持|不提供|不允许|不可以|无法|不能|尚未|暂未", clause))
            values.update((op, obj, not negative) for op, obj in _operation_targets(row, clause))
    return values


def _contradicts(first: dict, second: dict) -> bool:
    a, b = _capability_assertions(first), _capability_assertions(second)
    def same_target(x, y):
        return x[0] == y[0] and (x[1] == y[1] or not x[1] or not y[1])
    if any(same_target(x,y) and x[2] != y[2] for x in a for y in b):
        return True
    for capability, method in ((a,second),(b,first)):
        usable = software_subset(method)
        if usable:
            actions = _effective_actions(method)
            targets = set().union(*(_operation_targets(method, action["instruction"]["quote"]) for action in actions))
            if any(not fact[2] and any(same_target(fact, target) for target in targets) for fact in capability):
                return True
    return False


def _strong_assertions(row: dict) -> set[tuple]:
    subset = _usable_subset(row)
    if subset is None:
        return set()
    assertions = set()
    for claim in subset["data"]["claims"]:
        text = claim["quote"]
        if _EXCLUSIVE.search(text) and numeric_signature(text):
            # Keep object, modality and units, normalize values only. A different
            # method index cannot hide the same exclusive configuration rule.
            assertions.add((canonical(_NUMBER_VALUE.sub("VALUE", text)), numeric_signature(text)))
    return assertions


def plans(cfg, topic: dict, rows: list[dict]) -> list[dict]:
    if not mode(cfg):
        return [{"key": "", "rows": rows, "metadata": {}, "actionable": True}]
    groups: dict[str, dict] = {}
    for row in rows:
        domain = row["data"].get("domain", {})
        diagnosis = domain.get("diagnosis", {})
        capability = _is_capability(row)
        actionable = capability or method_is_usable(row)
        primary = domain.get("primary_intent", "unclear")
        projection = constraint_projection(row)
        constraints = projection["key"]
        if projection["invalid"]:
            constraints = (*constraints,("invalid-source-constraint:"+row["id"],()))
        if capability:
            key = "capability_" + assets.digest([constraints])[:24]
        elif actionable:
            confirmed = diagnosis.get("status") in ("config_cause_confirmed", "platform_limit_confirmed", "defect_confirmed")
            cause = [diagnosis.get("status"), diagnosis.get("cause_code")] if confirmed else ["unconfirmed", ""]
            methods = sorted({action["method_code"] for action in _effective_actions(row)})
            # Suggested numeric settings are bounded strategies, not a common
            # platform rule. Keep their original values in separate variants.
            parameters = sorted({numeric_signature(action["instruction"]["quote"]) for action in _effective_actions(row)})
            key = "method_" + assets.digest([primary, cause, methods, constraints, parameters])[:24]
        else:
            key = "record_" + assets.digest([consultation_key(row), diagnosis.get("status", "pending")])[:24]
        group = groups.setdefault(key, {"key": key, "rows": [], "metadata": {}, "actionable": actionable,
                                        "capability": capability,"constraint_key":constraints,
                                        "constraint_invalid":projection["invalid"]})
        group["rows"].append(row)
    for key, group in groups.items():
        domain = group["rows"][0]["data"].get("domain", {})
        diagnosis = domain.get("diagnosis", {})
        verified = not group["capability"] and any(actions and all(action.get("effect_status") == "verified" for action in actions)
                       for row in group["rows"] if (actions := row["data"].get("domain", {}).get("actions", [])))
        kind = ("capability_fact" if group["capability"] else "configuration_guide" if domain.get("primary_intent") == "configuration_guidance"
                else "verified_workaround" if diagnosis.get("status") == "defect_confirmed" and verified
                else "troubleshooting_advice")
        applicability = applicability_refs(group["rows"][0])
        group["metadata"] = {"variant_id": "v_" + assets.digest([topic["id"], key])[:24],
            "kind": kind, "diagnosis_status": diagnosis.get("status", "pending"),
            "effect_status": "verified" if verified else "suggested", "applicability": applicability,
            "confirmed_cause": diagnosis.get("cause"), "primary_intent": domain.get("primary_intent", "unclear")}
        if not group["capability"]:
            group["metadata"]["one_off_dependent"] = any((software_subset(row) or {}).get("data", {}).get("one_off_dependent")
                                                         for row in group["rows"])
            group["metadata"]["source_steps_recovered"] = any((software_subset(row) or {}).get("data",{}).get("source_steps_recovered")
                                                              for row in group["rows"])
    values = list(groups.values())
    for group in values:
        group["conflict_links"] = []
        group["capability_conflict"] = False
        if not group["actionable"]:
            continue
        first = group["rows"][0]["data"]["domain"]
        # A single capability branch can contain conflicting staff assertions.
        if group["capability"] and any(_contradicts(a,b) for i,a in enumerate(group["rows"]) for b in group["rows"][i+1:]):
            group["conflict_links"].extend([row["id"],row["fingerprint"],row["revision"]] for row in group["rows"])
            group["capability_conflict"] = True
        if group["capability"]:
            assertions = set().union(*(_strong_assertions(row) for row in group["rows"]))
            if any(template == other_template and numbers != other_numbers
                   for template,numbers in assertions for other_template,other_numbers in assertions):
                group["conflict_links"].extend([row["id"],row["fingerprint"],row["revision"]] for row in group["rows"])
        for other in values:
            if other is group or not other["actionable"]:
                continue
            second = other["rows"][0]["data"]["domain"]
            if group["constraint_key"] != other["constraint_key"]:
                continue
            if (group["capability"] or other["capability"]) and any(_contradicts(a,b) for a in group["rows"] for b in other["rows"]):
                group["conflict_links"].extend([row["id"],row["fingerprint"],row["revision"]] for row in other["rows"])
                group["capability_conflict"] = True
                continue
            # Distinct confirmed causes are distinct bounded branches. Codes
            # alone do not establish a cause; compare grounded cause text too.
            ca, cb = first["diagnosis"], second["diagnosis"]
            if ca.get("status") != cb.get("status") or ca.get("cause") and cb.get("cause") and canonical(ca["cause"]["quote"]) != canonical(cb["cause"]["quote"]):
                continue
            a = set().union(*(_strong_assertions(row) for row in group["rows"]))
            b = set().union(*(_strong_assertions(row) for row in other["rows"]))
            if any(template == other_template and numbers != other_numbers
                   for template, numbers in a for other_template, other_numbers in b):
                group["conflict_links"].extend([row["id"], row["fingerprint"], row["revision"]] for row in other["rows"])
        group["conflict_links"].sort()
    return values


def method_caution(metadata: dict) -> str:
    """Diagnosis and operation verification are independent source-backed axes."""
    if metadata.get("kind") == "capability_fact":
        return "这是来源当时的软件能力或限制说明；保留当前、暂时、版本和平台前提，不代表未来承诺或人工代办已变为自助功能。"
    if metadata.get("one_off_dependent"):
        return "仅限来源本次修改或处理完成后的操作建议，不能推广为同类问题均可通过这些步骤解决；操作效果尚无复测依据。"
    if metadata.get("source_steps_recovered"):
        return "这是当前人工原句支持的有条件操作或检查建议，尚未确认故障根因或验证修复效果；不能当作已证实的最终解决答案。"
    if metadata["diagnosis_status"] == "not_applicable":
        return ""
    if metadata["effect_status"] == "verified":
        return "来源记录了已验证的操作效果，适用范围以所引原句为准；不能自动推定所有同症状都由此原因导致。"
    if metadata["diagnosis_status"] in ("config_cause_confirmed", "platform_limit_confirmed", "defect_confirmed"):
        return "来源已确认当前诊断；该操作结果尚无复测依据，不能据此声称故障已恢复。"
    if metadata["diagnosis_status"] in ("reported", "pending"):
        return "当前原因尚未确认；来源提供的是排查建议，该操作结果尚无复测依据。"
    return ""


def evaluate(cfg, store, embedder, topic: dict, plan: dict) -> dict:
    if not mode(cfg):
        return quality.evaluate(store, embedder, topic, plan["rows"])
    rows = plan["rows"]
    if not plan["actionable"]:
        result = quality.evaluate(store, embedder, topic, rows)
        statuses = {row["data"].get("domain", {}).get("diagnosis", {}).get("status") for row in rows}
        if "defect_confirmed" in statuses:
            kind, reason = "confirmed_defect", "来源已确认缺陷；当前没有可用的受限处理方法，不作为已解决知识。"
        elif "platform_limit_confirmed" in statuses:
            kind, reason = "platform_constraint", "来源确认了平台限制，保留限制记录及适用范围，不编造处理方法。"
        elif any(not row["source"]["answer"] for row in rows):
            kind, reason = "no_staff_answer", "当前诉求尚无可用人工客服回答。"
        elif any(quality.only_collects_information(row["source"]["answer"], row["data"]["question_span"]["quote"]) or row["data"]["answer_kind"] == "collect_info" for row in rows):
            kind, reason = "collect_info", "当前片段用于收集资料，尚无可复用软件操作方法。"
        else:
            kind, reason = "pending_diagnosis", "保留商家报告与当前诊断状态，尚无可复用的软件操作方法。"
        result.update(status="gap", kind=kind, reason=reason, answer="", steps=[], reply_patterns=[])
    else:
        # Source and fingerprints stay unchanged. Only independently certified
        # software sentences become extractive knowledge facts.
        methods = [(capability_subset(row) if plan.get("capability") else software_subset(row)) for row in rows]
        assert all(methods)
        for method,row in zip(methods,rows):
            method["data"]["_software_scope_key"] = constraint_projection(row)["key"]
        result = quality.evaluate(store, embedder, topic, methods, software_support=True,
                                  allow_unknown_as_advice=plan["metadata"]["kind"] not in ("configuration_guide","capability_fact"))
        if result["status"] == "ready":
            result["scope"] = "；".join(dict.fromkeys(ref["quote"] for ref in plan["metadata"]["applicability"])) or "仅限来源描述的软件操作场景；平台或模块未明确时不扩大适用范围。"
            if plan["metadata"].get("one_off_dependent"):
                result["scope"] = "仅限来源本次修改或处理完成后的场景。" + result["scope"]
            caution = method_caution(plan["metadata"])
            if caution:
                result["cautions"] = list(dict.fromkeys(result["cautions"] + [caution]))
    if plan.get("conflict_links"):
        capability_conflict = plan.get("capability_conflict",False)
        result.update(status="exception", kind="capability_conflict" if capability_conflict else "number_conflict",
                      reason="同一适用条件的软件能力或必要参数限制互相矛盾，不能分别作为可用规则。", answer="", steps=[], reply_patterns=[])
    if plan.get("constraint_invalid"):
        result.update(status="exception",kind="scope_unbound",reason="已锚定的适用条件无法安全绑定当前咨询，未自动放宽范围。",
                      answer="",steps=[],reply_patterns=[])
    result.update({key: value for key, value in plan["metadata"].items() if key != "kind"})
    result["knowledge_kind"] = plan["metadata"]["kind"]
    if result["status"] == "ready":
        result["kind"] = plan["metadata"]["kind"]
    result["quality_version"] = quality_version(cfg)
    return result
