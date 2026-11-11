"""Grade exact, case-scoped evidence from a verified visitor segment."""
from __future__ import annotations

from collections import Counter
import re

from .voc_outcome import _COURTESY, _PROSPECTIVE, _QUESTION, _STAFF_CLOSING, _UNDERSTOOD
from .voc_quality import _CONDITION, _has_remedy, only_collects_information

_ACTION_VERBS = (
    r"开启|打开|启用", r"关闭|禁用", r"保存|提交", r"修改|更改|调整|重设|设置|配置|改",
    r"重启|重新启动", r"刷新|重新加载", r"重登|重新登录|重新登陆", r"更新|升级",
    r"绑定|解绑", r"授权", r"安装|卸载", r"同步", r"导出|导入", r"切换|选择", r"移除|删除",
)
_APPLIED = tuple(re.compile(rf"(?:{verbs})(?:了|完|好|成功|完成|过|[^，,。！？\n]{{1,18}}(?:后|以后|之后|了))")
                 for verbs in _ACTION_VERBS)
_PRESCRIBED = tuple(re.compile(rf"(?:{verbs})") for verbs in _ACTION_VERBS)
_APPLIED_AS_DIRECTED = re.compile(r"(?:按|照)(?:照|着)?(?:您|你)(?:说的|给的步骤|的建议)[^，,。！？\n]{0,14}(?:了|完|好|做过)")
_OBSERVED = re.compile(
    r"(?:成功|正常|已经|已|顺利)[^，,。！？\n]{0,12}"
    r"(?:触发|命中|回复|同步|生效|显示|恢复|解决|登录|登陆|导入|导出)"
    r"|(?:触发|命中|回复|同步|生效|显示|恢复|解决|登录|登陆|导入|导出)(?:了|成功|正常|通过)"
    r"|(?:复测|测试)(?:已|已经)?(?:通过|成功)"
)
_HYPOTHETICAL = re.compile(r"若|只要|要不|是不是|就(?:会|能|可以)|能不能|可以吗|(?:没有|没|还没|未)(?:去|再)?(?:修改|更改|调整|改|保存|设置|开启|关闭|操作|重启|刷新|测试|移除|删除)")
_FAILURE = re.compile(
    r"(?:还是|仍然|依然|仍|又).{0,8}(?:不行|报错|失败|没反应)"
    r"|(?:没有|没|尚未|未|不能|无法|不再|不)(?:成功|触发|命中|回复|同步|生效|显示|恢复|解决|登录|登陆|正常|通过|行)"
    r"|(?:操作|复测|测试|试了).{0,8}(?:失败|不行)|失败了|报错了"
)
_DIRECTIVE = re.compile(r"^(?:请您?|麻烦您?|建议您?|您先|先|再|然后|需要)?\s*(?:进入|打开|点击|选择|开启|启用|关闭|禁用|保存|设置|调整|修改|改|重启|刷新|更新|升级|安装|卸载|绑定|解绑|授权|同步|提交|切换|重新登录|重新登陆|导入|导出|移除|删除)")
_PROHIBITION = re.compile(r"(?:请勿|勿|千万别|不要|禁止|不能|无需|不需要|不用)\s*(?:关闭|开启|修改|设置|改|保存|提交|移除|删除|重启|刷新|更新|重置|登录|切换)")
_INTERVENTION = re.compile(r"(?:我|这边|技术|同事|工程师).{0,8}(?:已经|已|刚刚|帮您|帮你).{0,8}(?:处理|修复|修改|改|调整|切换|重启|恢复)")
_SENTENCE = re.compile(r"[。！？!?；;\n]+")
_SCOPE_CONDITION = re.compile(r"只有|只能|适用|限定|前置|支持.{0,8}版本")


def _result(status: str, reason_codes: set[str], verified_actions: list[dict] | None = None) -> dict:
    return {"status": status, "verified_actions": verified_actions or [], "reason_codes": sorted(reason_codes)}


def _bound_ref(value: object, scope: dict, role: str) -> tuple[int, dict, dict] | None:
    if not isinstance(value, dict):
        return None
    message_id, quote = value.get("message_id"), value.get("quote")
    if not isinstance(message_id, str) or not isinstance(quote, str) or not quote.strip() or message_id not in scope:
        return None
    index, source = scope[message_id]
    if source["role"] != role or source.get("kind", "text") != "text" or source.get("automated"):
        return None
    start = source["text"].find(quote)
    if start < 0 or start != source["text"].rfind(quote):
        return None
    return index, source, {"message_id": message_id, "quote": quote}


def _after(later: tuple[int, dict, dict], earlier: tuple[int, dict, dict]) -> bool:
    return later[0] > earlier[0] and later[1]["ts"] > earlier[1]["ts"] > 0


def _step(quote: str, whole_text: str, question: str) -> bool:
    # A clipped phrase can omit a prerequisite, a negation or the purpose of a step.
    # Keep a complete support message for a case claim or an intact sentence when
    # the other sentences do not change its purpose.
    sentence = quote.strip(" \t\r\n。！？!?；;")
    preceding, _, following = whole_text.partition(quote)
    outside = preceding + "\n" + following
    return (bool(sentence)
            and (whole_text.strip() == quote.strip()
                 or sentence in [part.strip() for part in _SENTENCE.split(whole_text)])
            and not _PROHIBITION.search(whole_text)
            and not (_CONDITION.search(outside) or _SCOPE_CONDITION.search(outside))
            and not only_collects_information(whole_text, question)
            and (_has_remedy(sentence) or _DIRECTIVE.match(sentence) is not None))


def _confirmed(instruction: str, feedback: str) -> bool:
    if (_FAILURE.search(feedback) or _QUESTION.search(feedback) or _PROSPECTIVE.search(feedback)
            or _HYPOTHETICAL.search(feedback) or _INTERVENTION.search(feedback) or not _OBSERVED.search(feedback)):
        return False
    # A report must describe completing the advised action as well as a result.
    return (bool(_APPLIED_AS_DIRECTED.search(feedback))
            or any(prescribed.search(instruction) and applied.search(feedback)
                   for prescribed, applied in zip(_PRESCRIBED, _APPLIED)))


def assess(episode: dict, messages: list[dict]) -> dict:
    """Use verified text, timing and issue membership to grade an action's result.

    ``case_verified`` means that this visitor reported an effect after applying
    the sourced step. It is evidence for the case and is never a publish decision.
    ``guidance_supported`` means a staff instruction is anchored and the effect
    is unconfirmed. ``failed`` and ``unverified`` withhold case success evidence.
    """
    if episode.get("kind") not in ("configuration", "anomaly"):
        return _result("unverified", {"unclear_episode"})
    indexed = {}
    for index, message in enumerate(messages):
        if (not isinstance(message, dict) or not isinstance(message.get("id"), str)
                or not message["id"] or message["id"] in indexed or not isinstance(message.get("text"), str)
                or not isinstance(message.get("role"), str) or type(message.get("ts")) is not int):
            return _result("unverified", {"invalid_source_segment"})
        indexed[message["id"]] = index, message
    ids = episode.get("message_ids")
    if (not isinstance(ids, list) or not ids or any(not isinstance(mid, str) or mid not in indexed for mid in ids)
            or len(set(ids)) != len(ids)):
        return _result("unverified", {"invalid_episode_membership"})
    scope = {mid: indexed[mid] for mid in ids}
    question = _bound_ref(episode.get("question"), scope, "user")
    if question is None or question[1]["ts"] <= 0:
        return _result("unverified", {"invalid_question_evidence"})
    last_index = max(index for index, _ in scope.values())
    if any(message["role"] == "boundary" for message in messages[question[0] + 1:last_index + 1]):
        return _result("unverified", {"source_boundary_crossed"})

    scoped = sorted(scope.values())
    failures = [index for index, message in scoped if index > question[0]
                and message["role"] == "user" and message.get("kind", "text") == "text"
                and _FAILURE.search(message["text"])]
    actions = episode.get("actions")
    if not isinstance(actions, list) or any(not isinstance(action, dict) for action in actions):
        return _result("unverified", {"invalid_actions"})
    proposed_feedback = Counter(ref["message_id"] for action in actions
                                if isinstance(ref := action.get("feedback"), dict) and isinstance(ref.get("message_id"), str))
    reasons: set[str] = set()
    guidance = False
    verified = []
    for action in actions:
        instruction = _bound_ref(action.get("instruction"), scope, "staff")
        if (instruction is None or not _after(instruction, question)
                or not _step(instruction[2]["quote"], instruction[1]["text"], question[2]["quote"])):
            reasons.add("unbound_or_incomplete_staff_step")
            continue
        guidance = True
        feedback = _bound_ref(action.get("feedback"), scope, "user")
        if feedback is None or not _after(feedback, instruction):
            reasons.add("unbound_or_unordered_feedback")
            continue
        if action.get("outcome") != "success" or not _confirmed(instruction[2]["quote"], feedback[2]["quote"]):
            if _COURTESY.fullmatch(feedback[1]["text"]) or _UNDERSTOOD.fullmatch(feedback[1]["text"]):
                reasons.add("acknowledgement_without_effect")
            else:
                reasons.add("explicit_applied_step_and_effect_missing")
            continue
        # Check the containing message for incompatible language outside the
        # model's chosen quote. A quote may not drop a conditional or negation.
        if not _confirmed(instruction[2]["quote"], feedback[1]["text"]):
            reasons.add("feedback_context_does_not_confirm_effect")
            continue
        if proposed_feedback[feedback[2]["message_id"]] != 1:
            reasons.add("feedback_shared_between_actions")
            continue
        if any(instruction[0] < index < feedback[0] and message["role"] == "staff"
               and message.get("kind", "text") == "text" and not _STAFF_CLOSING.fullmatch(message["text"])
               and (_has_remedy(message["text"]) or _INTERVENTION.search(message["text"])) for index, message in scoped):
            reasons.add("intervening_staff_action")
            continue
        if any(index > feedback[0] for index in failures):
            reasons.add("later_failure_in_same_episode")
            continue
        verified.append({"instruction": instruction[2], "feedback": feedback[2]})

    if verified:
        return _result("case_verified", reasons, verified)
    if failures:
        return _result("failed", reasons | {"customer_reports_failure"})
    if guidance:
        return _result("guidance_supported", reasons)
    return _result("unverified", reasons | {"no_anchored_staff_step"})
