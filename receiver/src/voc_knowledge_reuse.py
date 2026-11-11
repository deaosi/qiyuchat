"""Decide when source-backed VOC knowledge can be reused without manual review."""
from __future__ import annotations

import re

from . import voc_outcome
from .voc_domain import _message_refs
from .voc_episode_evidence import _APPLIED, _APPLIED_AS_DIRECTED, _PRESCRIBED, _confirmed

_REQUIRED_GATES = frozenset({"exact_staff_source", "resolved_answer", "complete_claims",
                             "scope_consistent", "same_software_method_branch"})
_METHOD_KINDS = frozenset({"configuration_guide", "troubleshooting_advice", "verified_workaround"})
_BLOCKED_FEEDBACK = frozenset({"ambiguous_multiple_intents", "new_intervention_without_bound_acceptance",
                              "same_topic_reopened_after_transition"})
_RISK = re.compile(r"密码|口令|密钥|令牌|验证码|绕过|关闭.{0,4}安全|(?:永久|批量)删除|清空|卸载|重置|覆盖|"
                   r"支付|打款|退款.{0,8}到账|赔付|赔偿|保修|保证|承诺|免费|收费|"
                   r"(?:导出|发送|提供).{0,12}(?:客户资料|个人信息|手机号|身份证)|\[链接\]|https?://|"
                   r"(?:一定|必定|百分百).{0,10}(?:成功|触发|生效|修复|解决)", re.I)
_RISK_WITHOUT_EFFECT = re.compile(r"删除|移除|断开|停用|禁用")
_EFFECT_ASSERTION = re.compile(r"(?:已(?:经)?|这边|本次).{0,8}(?:恢复|修复|解决|生效)|"
                               r"(?:就|即可|一定).{0,8}(?:恢复|修复|解决|生效|触发)")
_DIAGNOSTIC = re.compile(r"核对|检查|查看|查阅|复测|测试|排查|看(?:一)?下|"
                         r"确认.{0,16}(?:是否|状态|结果|权限|生效|开关|版本|参数值)")
_MUTATING_DIAGNOSTIC = re.compile(r"(?:^|[，,。；;\n]|请(?:您)?|再|并|然后|建议|点击|先|需要|"
                                  r"后(?:再|就|请)?|(?:把|将)[^，,。；;\n]{0,20})\s*"
                                  r"(?:修改|更改|调整|改|重启|安装|刷新|升级|更新|导入|导出|提交|保存|"
                                  r"开启|启用|关闭|禁用|解绑|绑定|更换|重新配置|配置为|配置成|授权)")


def _decision(level: str, reason: str) -> dict:
    return {"level": level, "reason_codes": [reason]}


def _exact_span(source: dict, name: str, ref: dict) -> list[dict]:
    if not isinstance(ref, dict):
        return []
    text = source.get(name, "")
    start, end = ref.get("start"), ref.get("end")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or ref.get("alignment") != "exact" or text[start:end] != ref.get("quote")):
        return []
    return _message_refs(source, name, start, end)


def _procedure(citation: dict, source: dict) -> tuple[str, list[str]] | None:
    if (source.get("source_kind") != "source-anchored" or source.get("_staff_verified") is not True
            or citation.get("staff_source_verified") is not True or citation.get("answer_kind") != "resolved_answer"
            or not _exact_span(source, "question", citation.get("question_span", {}))):
        return None
    claims = citation.get("claims", [])
    if not isinstance(claims, list):
        return None
    for claim in claims:
        refs = _exact_span(source, "answer", claim)
        if not refs or claim.get("role") != "staff" or claim.get("source_refs") != refs:
            return None
    steps = list(dict.fromkeys(claim["quote"] for claim in claims if claim.get("type") == "step"))
    if not steps:
        return None
    return "\n".join(claim["quote"] for claim in claims), steps


def _scope_is_bound(item: dict, cited: list[dict]) -> bool:
    scope = item.get("scope", "")
    refs = item.get("applicability", [])
    if not scope:
        return False
    if not refs:
        return scope == "仅限来源描述的软件操作场景；平台或模块未明确时不扩大适用范围。"
    for ref in refs:
        if not isinstance(ref, dict):
            return False
        name = ref.get("source")
        if name not in ("question", "answer") or ref.get("role") != ("merchant" if name == "question" else "staff"):
            return False
        if not ref.get("source_refs") or not any(
            _exact_span(row["source"], name, ref) == ref["source_refs"] for row in cited
        ):
            return False
    return scope == "；".join(dict.fromkeys(ref["quote"] for ref in refs))


def _complete_sources(certificate: dict, count: int) -> bool:
    coverage = certificate.get("coverage", {})
    return (count > 0 and coverage.get("sources") == coverage.get("complete_sources") == count
            and coverage.get("incomplete_sources") == 0)


def _confirmed_procedure(steps: list[str], feedback: str) -> bool:
    if not steps or not all(_confirmed(step, feedback) for step in steps):
        return False
    if _APPLIED_AS_DIRECTED.search(feedback):
        return True
    procedure = "\n".join(steps)
    return all(applied.search(feedback) for prescribed, applied in zip(_PRESCRIBED, _APPLIED)
               if prescribed.search(procedure))


def evaluate(cfg, item: dict, cited: list[dict], certificate: dict, events: list[dict]) -> dict:
    """Expose operational verification separately from safe source-backed guidance."""
    kind = item.get("kind")
    if (item.get("status") != "ready" or item.get("stale") or kind not in _METHOD_KINDS
            or not _REQUIRED_GATES <= set(item.get("gate_checks") or [])):
        return _decision("reference", "method_source_gate_not_met")
    if item.get("one_off_dependent") or item.get("source_steps_recovered"):
        return _decision("reference", "one_off_or_recovered_step")
    citations = item.get("evidence", [])
    if (len(cited) != len(citations) or not _complete_sources(certificate, len(citations))
            or not _scope_is_bound(item, cited)):
        return _decision("reference", "incomplete_or_unbound_sources")
    if (certificate.get("status") == "negative"
            or _BLOCKED_FEEDBACK.intersection(certificate.get("reason_codes", []))):
        return _decision("reference", "conflicting_or_unbound_feedback")

    procedures = {}
    selected = set()
    for citation, row in zip(citations, cited):
        if citation.get("event_id") != row["id"] or citation.get("source_fingerprint") != row["fingerprint"]:
            return _decision("reference", "incomplete_or_unbound_sources")
        procedure = _procedure(citation, row["source"])
        if not procedure:
            return _decision("reference", "unbound_staff_procedure")
        answer, steps = procedure
        procedures[row["id"]] = steps
        if item.get("answer") == answer and item.get("steps") == steps:
            selected.add(row["qa_id"])
    if not selected:
        return _decision("reference", "unbound_displayed_procedure")
    if _RISK.search(item["answer"]):
        return _decision("reference", "higher_risk_needs_evidence")

    # The legacy closure recognizer understands generic success acknowledgments.
    # A fuller “I did X and retested Y” reply may need an exact-method recheck.
    if "continuing_or_unbound_reply" in certificate.get("reason_codes", []):
        certificate = voc_outcome.evaluate(cfg, cited, events=events, matches_success=lambda row, text:
                                            _confirmed_procedure(procedures.get(row["id"], []), text))
    if (not _complete_sources(certificate, len(citations)) or certificate.get("status") == "negative"
            or certificate.get("acceptance") == "continuing"
            or _BLOCKED_FEEDBACK.intersection(certificate.get("reason_codes", []))):
        return _decision("reference", "conflicting_or_unbound_feedback")

    verified = [ref for ref in certificate.get("evidence", []) if
        ref.get("kind") == "resolved" and ref.get("role") == "merchant" and ref.get("qa_id") in selected
        and _confirmed_procedure(item["steps"], ref.get("quote", ""))]
    if certificate.get("effect") == "customer_reported_success" and verified:
        return {**_decision("verified", "customer_applied_step_and_confirmed_effect"), "evidence": verified}
    if _RISK_WITHOUT_EFFECT.search(item["answer"]) or _EFFECT_ASSERTION.search(item["answer"]):
        return _decision("reference", "effect_or_higher_risk_needs_evidence")
    if kind == "configuration_guide":
        return _decision("guidance", "complete_source_backed_configuration")
    if kind == "troubleshooting_advice" and all(
        _DIAGNOSTIC.search(step) and not _MUTATING_DIAGNOSTIC.search(step) for step in item["steps"]
    ):
        return _decision("guidance", "complete_source_backed_diagnostic")
    return _decision("reference", "effect_verification_missing")
