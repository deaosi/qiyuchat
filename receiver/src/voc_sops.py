"""Compile a bounded handling procedure from current, verified episode messages."""

from __future__ import annotations

import re

from . import voc_episode_evidence as evidence
from .voc_assets import digest
from .voc_extract import logical_units
from .voc_knowledge_reuse import _RISK, _RISK_WITHOUT_EFFECT
from .voc_outcome import _NEGATIVE, _QUESTION, _STAFF_CLOSING
from .voc_quality import only_collects_information
from .voc_variants import _is_notice_or_service


_SCOPE = re.compile(r"如果|若|前提|仅限|只限|只有|适用于|除非|版本|子账号|主账号|管理员")
_COMMAND = r"(?:^|[，,；;]|(?:请|建议|先|再|然后|接着|并|之后))\s*(?:请您?|麻烦您?|您先|建议您?|先|再|然后|接着)?\s*"
_CHECK = re.compile(_COMMAND + r"(?:检查|核对|查看|确认|比对).{0,22}(?:开关|权限|状态|规则|设置|版本|配置|参数|日志|场景|账号|记录|选项)")
_RETEST = re.compile(_COMMAND + r"(?:复测|重新测试|再测试|测试|重试|再试|试一下|验证是否|看是否)")
_CHANGE = re.compile(_COMMAND + r"(?:开启|启用|关闭|禁用|保存|设置|调整|修改|改成|重启|刷新|更新|升级|安装|卸载|绑定|解绑|授权|同步|提交|切换|重新登录|重新登陆|导入|导出|移除|删除)")
_RETEST_TARGET = re.compile(r"(?:是否|能否|能不能|有没有|可否)[^。！？!?\s]+|触发|命中|回复|生效|同步|恢复|正常|成功|登录|授权|下发|送达")
_INLINE_RETEST = re.compile(r"(?:后|然后|再|接着|并)\s*(?:请您?)?((?:复测|重新测试|再测试|测试|重试|再试|试一下|验证是否|看是否).+)")
_ESCALATE = re.compile(_COMMAND + r"(?:联系(?:技术|客服|管理员)|提交(?:工单|故障单)|转交(?:技术|研发))")
_STAFF_SELF_ACTION = re.compile(r"^(?:我(?:们)?|这边|技术|同事|工程师).{0,18}(?:已经|已|刚刚|帮您|帮你|会|将|正在|先(?:帮|查|看))")
_PERSONAL_OR_MEDIA = re.compile(r"\[(?:链接|名称|图片|文件|手机号|电话|邮箱|身份证|联系方式)\]|https?://")
_PRESCRIBED_ACTIONS = tuple(re.compile(_COMMAND + rf"(?:{verbs})") for verbs in evidence._ACTION_VERBS)


def _indexed(messages: list[dict]) -> dict[str, tuple[int, dict]]:
    result = {}
    last_ts = 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return {}
        identity, timestamp = message.get("id"), message.get("ts")
        if (not isinstance(identity, str) or not identity or identity in result
                or type(timestamp) is not int or timestamp < last_ts or not isinstance(message.get("text"), str)
                or message.get("role") not in ("user", "staff", "bot", "boundary")):
            return {}
        result[identity] = index, message
        last_ts = timestamp
    return result


def _bound(value: object, scope: dict, role: str) -> tuple[int, dict, dict, int, int] | None:
    original = evidence._bound_ref(value, scope, role)
    if original is None:
        return None
    index, source, ref = original
    start, end = value.get("start"), value.get("end")
    text, quote = source["text"], ref["quote"]
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or source["ts"] <= 0 or text[start:end] != quote):
        return None
    return index, source, {**ref, "ts": source["ts"]}, start, end


def _answer_units(answer: tuple) -> list[tuple[int, dict, dict, int, int]]:
    index, source, ref, start, end = answer
    units = []
    for unit in logical_units(source["text"]):
        left, right = unit["start"], unit["end"]
        if not unit["substantive"] or start > left or right > end:
            continue
        while right < end and source["text"][right] in "。！？!?":
            right += 1
        quote = source["text"][left:right]
        if source["text"].find(quote) != source["text"].rfind(quote):
            continue
        units.append((index, source, {**ref, "quote": quote}, left, right))
    return units


def _phase(quote: str, question: str) -> str:
    if (_is_notice_or_service(quote) or _STAFF_SELF_ACTION.search(quote)
            or evidence._INTERVENTION.search(quote) or evidence._PROHIBITION.search(quote)):
        return ""
    if only_collects_information(quote, question):
        return ""
    if _ESCALATE.search(quote) and not re.search(r"(?:保存|修改|设置|重新测试)", quote):
        return "escalation"
    if _CHANGE.search(quote):
        return "action"
    if _CHECK.search(quote):
        return "diagnosis"
    if evidence._DIRECTIVE.match(quote) or any(
        evidence._DIRECTIVE.match(clause.strip()) for clause in re.split(r"[，,；;]", quote)
    ):
        return "action"
    if _RETEST.search(quote):
        return "retest"
    if _ESCALATE.search(quote):
        return "escalation"
    return ""


def _post_retest(steps: list[dict], *, require_action: bool = False) -> tuple[int, str] | None:
    actions = [index for index, step in enumerate(steps) if step["phase"] == "action"]
    prior = actions or ([] if require_action else [index for index, step in enumerate(steps) if step["phase"] == "diagnosis"])
    if not prior:
        return None
    last = prior[-1]
    if actions:
        text = steps[last]["evidence"]["quote"]
        for inline in _INLINE_RETEST.finditer(text):
            if _RETEST_TARGET.search(inline[1]):
                return last, inline[1]
    for index in range(last + 1, len(steps)):
        step = steps[index]
        if step["phase"] == "retest" and _RETEST_TARGET.search(step["evidence"]["quote"]):
            return index, step["evidence"]["quote"]
    return None


def _procedure_confirmed(actions: list[tuple], feedback: str) -> bool:
    instructions = [step["evidence"]["quote"] for _, _, step in actions]
    if not instructions or not all(evidence._confirmed(quote, feedback) for quote in instructions):
        return False
    if len({index for index, _, _ in actions}) == 1 and evidence._APPLIED_AS_DIRECTED.search(feedback):
        return True
    mentioned = False
    for prescribed, applied in zip(_PRESCRIBED_ACTIONS, evidence._APPLIED):
        action_messages = {index for index, _, step in actions if prescribed.search(step["evidence"]["quote"])}
        if action_messages:
            mentioned = True
            if len(list(applied.finditer(feedback))) < len(action_messages):
                return False
    return mentioned


def _conditions(problem: tuple, units: list[tuple], episode: dict) -> list[dict]:
    values = [ref for _, _, ref, _, _ in [problem, *units] if _SCOPE.search(ref["quote"])]
    for name in ("platform", "module"):
        value = episode.get(name)
        if not isinstance(value, str) or len(value) < 2 or _PERSONAL_OR_MEDIA.search(value):
            continue
        for _, source, ref, _, _ in [problem, *units]:
            if value in ref["quote"] and source["text"].count(value) == 1:
                values.append({**ref, "quote": value})
                break
    return list({(ref["message_id"], ref["quote"]): ref for ref in values}.values())


def compile_case(episode: dict, messages: list[dict], qa_id: int) -> dict | None:
    """Read existing transcript evidence; a model verdict alone never promotes a procedure."""
    if not isinstance(episode, dict) or not isinstance(messages, list):
        return None
    identity, kind = episode.get("id"), episode.get("kind")
    if not isinstance(identity, str) or not identity or kind not in ("configuration", "anomaly") or type(qa_id) is not int or qa_id <= 0:
        return None
    indexed = _indexed(messages)
    members = episode.get("message_ids")
    if not isinstance(members, list) or not members or any(not isinstance(mid, str) or mid not in indexed for mid in members):
        return None
    order = [indexed[mid][0] for mid in members]
    if order != sorted(set(order)) or any(message.get("role") == "boundary" for message in messages[order[0]:order[-1] + 1]):
        return None
    scope = {mid: indexed[mid] for mid in members}
    problem = _bound(episode.get("question"), scope, "user")
    if problem is None or _PERSONAL_OR_MEDIA.search(problem[2]["quote"]):
        return None
    answers, raw_answers = [], episode.get("answers")
    if not isinstance(raw_answers, list):
        return None
    for raw in raw_answers:
        bound = _bound(raw, scope, "staff")
        if bound is not None and evidence._after(bound, problem):
            answers.append(bound)
    if not answers:
        return None

    units_by_span = {(item[0], item[3], item[4]): item for answer in answers for item in _answer_units(answer)}
    units = [units_by_span[key] for key in sorted(units_by_span)]
    safe_units = [item for item in units if not (_RISK.search(item[2]["quote"]) or _PERSONAL_OR_MEDIA.search(item[2]["quote"]))]
    missing = []
    blocked_risk = any(_RISK.search(answer[2]["quote"]) or _PERSONAL_OR_MEDIA.search(answer[2]["quote"]) for answer in answers)
    if blocked_risk:
        missing.append("unsafe_step")
    quoted_units = {(index, start) for index, _, _, start, _ in units}
    unbound_condition = bool(_SCOPE.search(problem[1]["text"][:problem[3]] + problem[1]["text"][problem[4]:]))
    for index, source, _, _, _ in answers:
        if any(_SCOPE.search(unit["text"]) and (index, unit["start"]) not in quoted_units
               for unit in logical_units(source["text"])):
            unbound_condition = True
    if unbound_condition:
        missing.append("unbound_condition")
    conditions = _conditions(problem, safe_units, episode)
    if not conditions:
        missing.append("applicability")
    steps = []
    for index, source, ref, _, _ in safe_units:
        phase = _phase(ref["quote"], problem[2]["quote"])
        if phase:
            steps.append((index, source["ts"], {"phase": phase, "evidence": ref, "outcome": "unconfirmed"}))

    last_failure = -1
    for index, message in sorted(scope.values()):
        if index <= problem[0] or message.get("role") != "user" or message.get("kind", "text") != "text" or message.get("automated"):
            continue
        text = message["text"]
        if not (evidence._FAILURE.search(text) or _NEGATIVE.search(text)) or _QUESTION.search(text):
            continue
        attempted = [step for step in steps if step[2]["phase"] == "action" and last_failure < step[0] < index and step[1] < message["ts"]]
        if not attempted:
            continue
        last_failure = index
        for _, _, step in attempted:
            step["outcome"] = "failed"
        if not _PERSONAL_OR_MEDIA.search(text):
            attempted[-1][2]["feedback"] = {"message_id": message["id"], "quote": text, "ts": message["ts"]}

    path = [step for step in steps if step[0] > last_failure and step[2]["phase"] != "escalation"]
    actions = [step for step in path if step[2]["phase"] == "action"]
    concrete = any(step["phase"] in ("action", "diagnosis") for _, _, step in path)
    has_retest = _post_retest([step for _, _, step in path]) is not None
    if not actions:
        missing.append("action")
    if not any(step["phase"] == "diagnosis" for _, _, step in steps):
        missing.append("diagnosis")

    feedback_ids = set()
    model_actions = episode.get("actions")
    if isinstance(model_actions, list):
        for action in model_actions:
            if not isinstance(action, dict):
                continue
            instruction = _bound(action.get("instruction"), scope, "staff")
            feedback = _bound(action.get("feedback"), scope, "user")
            if (instruction and feedback and evidence._after(feedback, instruction)
                    and any(index == instruction[0] and start <= instruction[3] < instruction[4] <= end
                            for index, _, _, start, end in safe_units)):
                feedback_ids.add(feedback[2]["message_id"])

    result = None
    if actions:
        last_step = path[-1]
        for index, message in sorted(scope.values(), reverse=True):
            if (message["id"] not in feedback_ids or index <= last_step[0] or message["ts"] <= last_step[1] or message.get("role") != "user"
                    or message.get("kind", "text") != "text" or message.get("automated")
                    or _PERSONAL_OR_MEDIA.search(message["text"]) or not _procedure_confirmed(actions, message["text"])):
                continue
            between = messages[last_step[0] + 1:index]
            if any(turn.get("role") == "staff" and turn.get("kind", "text") == "text"
                   and not _STAFF_CLOSING.fullmatch(turn["text"].strip()) for turn in between):
                continue
            if any(turn.get("role") == "user" and turn.get("kind", "text") == "text"
                   and (evidence._FAILURE.search(turn["text"]) or _NEGATIVE.search(turn["text"]) or _QUESTION.search(turn["text"]))
                   for turn in messages[index + 1:]):
                missing.append("later_counterevidence")
                break
            result = {"message_id": message["id"], "quote": message["text"], "ts": message["ts"]}
            break
    if path and any(turn["id"] not in scope and turn.get("role") == "user" and turn.get("kind", "text") == "text"
                    and (evidence._FAILURE.search(turn["text"]) or _NEGATIVE.search(turn["text"]))
                    for turn in messages[path[-1][0] + 1:]):
        result = None
        if "later_counterevidence" not in missing:
            missing.append("later_counterevidence")

    if not has_retest and result is None:
        missing.append("retest")
    if result is None:
        missing.append("customer_result")
    unsafe_unverified = any(_RISK_WITHOUT_EFFECT.search(step["evidence"]["quote"]) for _, _, step in path)
    if result is not None and conditions and not blocked_risk and not unbound_condition:
        status = "verified"
        for _, _, step in actions:
            step.update(outcome="verified", feedback=result)
    elif (conditions and concrete and has_retest and not blocked_risk and not unsafe_unverified and not unbound_condition
          and "later_counterevidence" not in missing):
        status = "guidance"
    else:
        status = "incomplete"
    item = {"id": "sop_" + digest([identity])[:24], "topic_id": episode.get("topic_id") or "", "qa_id": qa_id,
            "title": problem[2]["quote"], "kind": kind, "status": status, "problem": problem[2], "conditions": conditions,
            "steps": [step for _, _, step in steps], "result": result, "missing": missing}
    if status == "incomplete" and not steps:
        item["replies"] = [ref for _, _, ref, _, _ in safe_units if not _STAFF_CLOSING.fullmatch(ref["quote"].strip())][:3]
    return item
