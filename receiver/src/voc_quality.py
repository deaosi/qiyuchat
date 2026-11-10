"""Deterministic quality gates over grounded claims, never model confidence.

An automatic answer is extractive: its words come from staff evidence, not an
unverifiable model paraphrase. Semantic agreement is used only for low-risk
alternate wording; policies/promises require repeated exact support.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import voc_assets as assets
from .voc_extract import canonical, claim_context_checks, negation_signature, numeric_signature, source_knowledge_blocker
from .voc_semantics import Embedder, similarity, vectors

VERSION = "quality-v4"
_RISK = re.compile(r"退款|退货|赔偿|赔付|保修|保证|承诺|免费|收费|价格|费用|发货|到货|工作日|自然日|[￥¥%]")
_INFO_OBJECT = r"截图|截屏|报错(?:信息|页面|内容)?|错误(?:信息|页面|内容)|日志|账号|帐号|订单号|店铺(?:名称|名|链接)|联系方式|电话号码|手机号码|诊断信息|复现(?:步骤|视频)"
_SEND = r"提供|发(?:送|给|来|一下|下|个|我|您|一张)?|给我|给您|告知|告诉|反馈|上传|再发"
_COLLECT = re.compile(r"(?:" + _SEND + r").{0,24}(?:" + _INFO_OBJECT + r")|(?:" + _INFO_OBJECT + r").{0,24}(?:" + _SEND + r")")
_CAPTURE = re.compile(r"截图|截屏|报错页面|错误页面|日志页面|(?:复制|导出|保存).{0,12}(?:账号|订单号|报错|错误信息|日志)")
_REMEDY = re.compile(r"保存|重启|刷新|更新(?!页面|界面|入口|向导)|安装(?!页面|界面|入口|向导)|卸载|关闭|开启|启用|禁用|删除|修复|更换|重置|提交|重新登[录陆]|(?:核对|确认|检查|查看).{0,12}权限|配置.{0,4}(?:参数|文件|项|为|成)|(?:同步|复制).{0,8}(?:知识|商品|配置|话术)|(?:知识|商品).{0,8}(?:同步|复制)")
_LOOKUP_QUESTION = re.compile(r"在哪|哪里|入口|如何查看|怎么查看|如何查询|怎么查询|如何申请|怎么申请")
_NAVIGATE = re.compile(r"打开|进入|点击|选择|查看|查询")
_LOCATION = re.compile(r"(?:打开|进入|查看|切换到|点击|选择)[^，,。！？!?\n]{0,24}?(?:页面|界面|窗口|菜单|选项卡|入口|向导|对话框)")
_NAV_PREFIX = re.compile(r"^(?:(?:请您?|麻烦您?|劳烦您?|先|您先|建议您)\s*)*(?:打开|进入|查看|切换到|选择)")
_CONDITION = re.compile(r"如果|若|仅|只限|除外|除非|前提|必须|否则|不能|不支持|不可以|无法|禁止")
_ASK_TIME = re.compile(r"什么时候|何时|多久|几天|时间|日期|时限")
_TIME_UNKNOWN = re.compile(r"(?:时间|日期|时限|安排|计划)[^，,。；;]{0,20}(?:未确定|未确认|未明确|不清楚|不知道|尚未|还未|没有|未定)|(?:尚未|暂未|还未|没有|没)[^，,。；;]{0,10}(?:收到|得到|接到|公布|确定|明确)[^，,。；;]{0,10}(?:通知|时间|日期|安排)|尚无.{0,8}通知|暂无.{0,8}通知|待通知|时间未定")
_DENIES_UNKNOWN = re.compile(r"并非|并不是|不是|不代表|不表示|不意味着")
_SOFTWARE_PROMISE = re.compile(r"保证.{0,12}(?:触发|修复|恢复|成功)|一定.{0,12}(?:修复|恢复|成功)|承诺.{0,12}(?:修复|恢复|赔偿)")


def scope_key(data: dict) -> tuple:
    return tuple(sorted({(item["kind"], canonical(item["quote"])) for item in data.get("scope", [])}))


def hard_scope_key(data: dict) -> tuple:
    return tuple(item for item in scope_key(data) if item[0] in {"product", "version", "audience"})


def session_key(source: dict) -> str:
    return source.get("_session_key") or assets.digest(["raw-session", source.get("session_id", "")])


def only_collects_information(reply: str, question: str = "") -> bool:
    """Purpose gate: opening/copying evidence is not a remedial operation.

    A real remedial step followed by conditional evidence collection remains a
    solution candidate. This does not rely on the model's answer_kind label.
    """
    if not _COLLECT.search(reply):
        return False
    # The follow-up request can share one written sentence with a genuine fix.
    # Only the prefix before an explicit failure/continuation condition qualifies.
    prefix = re.split(r"如果|若|仍(?:然)?(?:失败|不行|报错|有问题)|还是(?:不行|失败)|未解决|未成功", reply, maxsplit=1)[0]
    if prefix != reply and _has_remedy(prefix) and not _CAPTURE.search(prefix) and not _COLLECT.search(prefix):
        return False
    for clause in re.split(r"[，,；;。！？!?\n]", reply):
        if not clause.strip():
            continue
        if _CAPTURE.search(clause) or _COLLECT.search(clause):
            continue
        if _has_remedy(clause):
            return False
        if _LOOKUP_QUESTION.search(question) and _NAVIGATE.search(clause) and not re.search(_INFO_OBJECT, clause):
            return False
    return True


def _has_remedy(clause: str) -> bool:
    # Destination names are nouns, even when named “安装/配置/更新”. Navigating
    # there to capture evidence is not executing that named operation. Preserve
    # an actual subsequent action, e.g. “进入设置页面并点击保存”.
    without_locations = _LOCATION.sub("", clause)
    if without_locations == clause and _NAV_PREFIX.match(clause.strip()):
        rest = re.split(r"点击|然后|接着|随后|并|再|后", clause, maxsplit=1)
        if len(rest) == 1:
            return False
        without_locations = rest[1]
    return bool(_REMEDY.search(without_locations))


def coverage(data: dict, answer: str) -> float:
    positions = set()
    for claim in data["claims"]:
        positions.update(range(claim["start"], claim["end"]))
    return len(positions) / max(1, len(answer))


def evidence(row: dict) -> dict:
    source, data = row["source"], row["data"]
    def claim_refs(claim: dict) -> list[dict]:
        return [{"message_id": part["message_id"], "start": max(claim["start"], part["start"]) - part["start"],
                 "end": min(claim["end"], part["end"]) - part["start"]}
                for part in source.get("_answer_parts", []) if part["start"] < claim["end"] and claim["start"] < part["end"]]
    return {"id": row["id"], "event_id": row["id"], "qa_id": source["qa_id"],
            **{key: source.get(key, "") for key in ("session_id", "customer_id", "customer_name", "group_name", "staff_name", "asked_at", "asked_at_text", "question", "answer", "source_kind", "question_message_ids", "answer_message_ids")},
            "question_quote": data["question_span"]["quote"], "question_span": data["question_span"],
            "span_coordinate": "clean_visible_text",
            "answer_kind": data["answer_kind"], "staff_source_verified": source.get("_staff_verified", False),
            "claims": [{"role": "staff", **claim, "source_refs": claim_refs(claim)} for claim in data["claims"]],
            "scope_evidence": data["scope"], "source_fingerprint": source["_fingerprint"],
            **({"domain": data["domain"]} if data.get("domain") else {})}


def evaluate(path: Path, embedder: Embedder, topic: dict, rows: list[dict], *,
             software_support: bool = False, allow_unknown_as_advice: bool = False) -> dict:
    citations = [evidence(row) for row in rows]
    base = {"evidence": citations, "evidence_count": len(citations), "steps": [],
            "reply_patterns": [], "cautions": [], "quality_version": VERSION,
            "source_level": "single_source" if len({session_key(row["source"]) for row in rows}) == 1 else "multiple_sources",
            "reason": "", "kind": "", "answer": "", "scope": "",
            "status": "exception", "gate_checks": []}

    def fail(kind: str, reason: str, *, gap: bool = False) -> dict:
        return {**base, "status": "gap" if gap else "exception", "kind": kind, "reason": reason}

    if not rows:
        return fail("no_current_evidence", "当前来源已更新或移除，需要重新整理。", gap=True)
    answered = [row for row in rows if row["source"]["answer"].strip()]
    if not answered:
        return fail("no_staff_answer", "有客户诉求，但没有可用客服回复，暂不生成知识答案。", gap=True)
    if any(not row["source"].get("_staff_verified", False) for row in answered):
        return fail("unverified_staff_source", "回答尚不能对应到实际客服角色的原消息，暂不作为知识依据。")
    resolved = [row for row in answered if row["data"]["answer_kind"] == "resolved_answer" and row["data"]["claims"]]
    if not resolved:
        kind = "collect_info" if any(row["data"]["answer_kind"] == "collect_info" for row in answered) else "no_resolved_answer"
        return fail(kind, "现有回复用于收集信息或处理个例，尚无可复用的解决答案。", gap=True)
    # Unanswered occurrences can still count towards demand. A contextual or
    # unfinished staff answer is not silently treated as supporting agreement.
    unresolved = [row for row in answered if row not in resolved]
    if unresolved:
        return fail("mixed_answer_types", "同一问题同时出现个例处理或未完成答复，需要核对适用条件。")
    if software_support:
        for row in resolved:
            blocker = source_knowledge_blocker(row["source"],row["data"]["question_span"])
            if blocker:
                return fail(blocker,"原始客服回复仍明确缺少当前请求所需的结果或必要参数，不能把导航/排查动作当作完整答案。",gap=True)
    if any(row["data"]["conflicts"] for row in resolved):
        return fail("source_conflict", "客服原句中存在明确冲突，未自动选用其中一种口径。")
    if any(any(not isinstance(gap, dict) or gap.get("support") != "source-anchored"
               for gap in row["data"].get("missing_info", [])) for row in resolved):
        return fail("missing_conditions", "阻塞缺口合同不完整，需要重新提取或核对。")
    def blocks_method(gap: dict) -> bool:
        question = gap.get("request", {}).get("quote", "")
        return not (software_support and allow_unknown_as_advice and gap.get("kind") == "explicit_unknown"
                    and re.search(r"原因|为什么|为何", question))
    if any(any(blocks_method(gap) for gap in row["data"].get("missing_info", [])) for row in resolved):
        return fail("missing_conditions", "回答缺少关键条件或后续步骤，暂不作为完整知识。")
    scopes = {tuple(row["data"]["_software_scope_key"]) if software_support and "_software_scope_key" in row["data"] else
              tuple(sorted((name, canonical(ref["quote"])) for name in ("platforms", "modules", "prerequisites")
                           for ref in row["data"].get("domain", {}).get(name, [])))
              if software_support else scope_key(row["data"]) for row in resolved}
    if len(scopes) != 1:
        return fail("scope_conflict", "产品、版本或适用条件不一致，需要区分范围。")
    replies = ["\n".join(claim["quote"] for claim in row["data"]["claims"]) for row in resolved]
    for row, reply in zip(resolved, replies):
        answer = row["source"]["answer"]
        # An exact oral yes/no answer can be complete when its current software
        # question supplies the target. Only the deterministic domain certificate
        # enables this; generic short/policy replies keep the original threshold.
        minimum = 2 if software_support and row["data"].get("capability_context_validated") is True else 8
        if len(canonical(reply)) < minimum:
            return fail("incomplete_answer", "客服原句过短，无法形成完整可复用答案。", gap=True)
        if only_collects_information(reply, row["data"]["question_span"]["quote"]):
            return fail("collect_info", "回复主要用于索要信息，不能当作已解决答案。", gap=True)
        if not allow_unknown_as_advice and _ASK_TIME.search(row["data"]["question_span"]["quote"]):
            for clause in re.split(r"[，,。；;！？!?\n]", reply):
                if _TIME_UNKNOWN.search(clause) and not _DENIES_UNKNOWN.search(clause):
                    return fail("pending_information", "当前请求的时间或安排在客服原句中仍明确未知，不能当作已给出答案。", gap=True)
        multi = row["data"].get("source_intent_count", 1) > 1
        local = claim_context_checks(answer, [row["data"]])[0]
        context = row["data"].get("claim_context")
        subset = software_support and row["data"].get("software_context_validated") is True
        if (not local["full_sentences"] or context is None and multi
                or not subset and not (context or local)["context_complete"]
                or subset and multi and (not context.get("intent_context_bound") or context.get("shared_context"))):
            return fail("incomplete_claim_coverage", "本意图引文未独立保留完整句子的主体、条件或否定，或与其他意图共享了无法独立绑定的答案。")
        if not subset and (not row["data"].get("source_complete", True) or not multi and (coverage(row["data"], answer) < 0.60
                or numeric_signature(answer) != numeric_signature(reply)
                or negation_signature(answer) != negation_signature(reply))):
            return fail("incomplete_claim_coverage", "抽取未完整保留客服答案的数字、否定或上下文，需核对。")
        for sentence in re.split(r"[。！？\n]", "" if multi or subset else answer):
            if _CONDITION.search(sentence) and canonical(sentence) not in canonical(reply):
                return fail("condition_omitted", "客服原句的限定条件未被完整保留。")
    comparison = ["\n".join(action["instruction"]["quote"] for action in row["data"].get("domain", {}).get("actions", []))
                  if software_support else reply for row, reply in zip(resolved, replies)]
    if not software_support and len({numeric_signature(reply) for reply in comparison}) > 1:
        return fail("number_conflict", "相同问题的价格、时长或数量口径不同，需要核对。")
    if not software_support and len({negation_signature(reply) for reply in comparison}) > 1:
        return fail("negation_conflict", "相同问题的支持/不支持或排除条件不同，需要核对。")
    high_risk = any(_SOFTWARE_PROMISE.search(reply) if software_support else
                    _RISK.search(reply) or numeric_signature(reply) or negation_signature(reply) for reply in replies)
    sessions = {session_key(row["source"]) for row in resolved}
    exact_agreement = len({canonical(reply) for reply in replies}) == 1
    if high_risk and (len(sessions) < 2 or not exact_agreement):
        return fail("policy_needs_support", "涉及政策、承诺、数字或否定口径，尚缺跨会话一致依据。")
    if not exact_agreement and not software_support:
        encoded = vectors(path, embedder, replies)
        if any(similarity(encoded[0], other) < 0.94 for other in encoded[1:]):
            return fail("answer_variation", "同一问题的客服解决口径尚未达到一致标准。")
    chosen = max(range(len(replies)), key=lambda index: len(replies[index]))
    selected = resolved[chosen]["data"]
    base.update(status="ready", answer=replies[chosen],
                scope="；".join(dict.fromkeys(item["quote"] for item in selected["scope"])),
                steps=list(dict.fromkeys(claim["quote"] for claim in selected["claims"] if claim["type"] == "step")),
                reply_patterns=list(dict.fromkeys(replies))[:5],
                cautions=list(dict.fromkeys(claim["quote"] for claim in selected["claims"] if claim["type"] == "caution")),
                reason="精确出处、回答类型、完整性、条件、数字和否定检查通过。",
                gate_checks=["exact_staff_source", "resolved_answer", "complete_claims", "scope_consistent",
                             "number_preserved", "negation_preserved", "same_software_method_branch" if software_support else "exact_agreement" if exact_agreement else "semantic_low_risk_agreement"])
    return base
