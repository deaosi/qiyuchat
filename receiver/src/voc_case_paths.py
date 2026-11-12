"""Surface case-specific staff actions from current, exact SevenFish transcripts."""

from __future__ import annotations

import json
from pathlib import Path
import re

from . import voc_assets, voc_outcome
from .config import Settings
from .knowledge_model import _staff_answer
from .voc_issue_pilot import _safe_label
from .voc_knowledge_reuse import _RISK, _RISK_WITHOUT_EFFECT
from .voc_topic_insights import _PLATFORM_NAMES


_VISITOR_PREFIXES = ("qiyu-external:", "qiyu-external-sha256:", "qiyu-user:")
_TARGETS = tuple(re.compile(pattern, re.I) for pattern in (
    r"参考聊天记录", r"(?:改|修改|更改)(?:收货)?地址", r"待办", r"接待调优|前台接待",
    r"问答知识(?:库)?", r"自定义知识(?:库)?", r"商品知识(?:库)?", r"表格知识(?:库)?", r"自定义\s*agent",
    r"商品(?:同步|列表|规格)|同步商品", r"店铺授权|绑定店铺|店铺绑定|授权店铺|(?:怎么|如何)授权",
    r"售后场景|退款场景|退货场景|发货场景|签收场景|物流场景|欢迎语|催付|催单|跟单",
    r"场景(?:设置|配置|规则|开关)|触发(?:条件|规则)|限定条件|关键词|自动回复|快捷回复|回复话术",
    r"转人工|转接客服", r"消息(?:接入|对接)|接入消息",
))
_OPERATION = re.compile(
    r"重新(?:同步|配置|授权|登录)|配置(?:为|成)|设置(?:为|成)|调整|修改|更改|配置|设置|"
    r"新建|创建|添加|加入|取消|开启|启用|保存|发布|绑定|授权|同步|更新|刷新|切换|选择|点击|打开|进入"
)
_STAFF_PREFIX = re.compile(
    r"(?:我(?:们)?(?:这边)?|这边|技术同事)\s*(?P<mode>(?:已经|刚刚|刚才|已|先|帮您|帮你|给您|给你|"
    r"为您|马上|稍后|准备|打算|计划|会|将|可以|来|\s){0,8})$"
)
_MERCHANT_PREFIX = re.compile(
    r"^(?:(?:老师|您好|然后|接着|建议|麻烦|需要|您|你|请|先|再|可以|直接)\s*)*"
    r"(?:在[^，。！？；,!?;\n]{1,30})?$"
)
_PLANNED = re.compile(r"先|稍后|准备|打算|计划|会|将|可以|马上")
_COMPLETED = re.compile(r"已经|刚刚|刚才|已")
_ACTION_NEGATION = re.compile(r"(?:无法|不能|无需|不用|不必|不要|不建议|没(?:有)?|未|还没|尚未)\s*$")
_STATUS = re.compile(r"^(?:成功|完成|正常|恢复|生效|触发|显示|出现|好了|了)(?:了|老师|呀|啦|哦|哈|\s|$)*$")
_NARRATION = re.compile(r"(?:成功了?|完成了?|失败了?|好了|了)(?:老师|呀|啦|哦|哈|\s)*$")
_ATTRIBUTED = re.compile(r"(?:问|让|叫|请|希望|建议|说要|说让|要我|需要我)$")
_UNSAFE = re.compile(r"解绑|关闭.{0,8}(?:订单|售后|退款)|取消.{0,8}(?:订单|售后|退款)|\[(?:链接|图片|文件)\]")
_VAGUE = re.compile(r"^(?:一下|下|这个|那个|它|好|好了|了|就行|老师|即可|一下就好|\s)*$")
_CONDITIONAL = re.compile(r"如果|假如|假设|倘若|除非|仅限|只限|只有|适用于|前提是|否则|若是|若为|(?:^|[，。；\n])若")
_ACTION_END = re.compile(r"辛苦(?:观察|留意|关注)|麻烦(?:观察|留意|关注)|(?:就行|即可)(?:了|老师|$)")
_OBJECT = re.compile(r"^[“「『\"]?([\u4e00-\u9fffA-Za-z0-9]{4,24})")
_GENERIC_OBJECT = re.compile(r"^(?:这(?:个|些|项|种)|该|那个|对应|相关|目前|当前|一下|试试|看看|设置|操作|一遍|成功)")
_SAVE = re.compile(r"^(?:(?:然后|接着|再|并|请)\s*)*((?:点击|点)?(?:保存(?:设置|配置)?|确定))$")
_REQUEST = re.compile(r"怎么|如何|未|没|不|无法|失败|报错|没有|能否|能不能|[？?]")
_CRITERION = re.compile(r"(?:一定要|必须|需要|希望|要能|应当|预期)(?:能|有|触发|生成|加入|进入|回复|显示|同步)")
_CLAUSES = re.compile(r"[，。；！？,;!?\n]+")


def _targets(text: str) -> set[int]:
    return {index for index, pattern in enumerate(_TARGETS) if pattern.search(text)}


def _actions(text: str) -> list[tuple[str, str, str, set[int], str]]:
    # Conditional directions are preserved in the source chat. Selecting a
    # branch for this customer would require a separate applicability check.
    if (_RISK.search(text) or _RISK_WITHOUT_EFFECT.search(text) or _UNSAFE.search(text)
            or _CONDITIONAL.search(text) or re.search(r"[？?]", text)):
        return []
    actions = []
    for clause in _CLAUSES.split(text):
        save = _SAVE.fullmatch(clause.strip())
        if save and actions and actions[-1][0] == "merchant":
            actions.append(("merchant", "instructed", save[1], *actions[-1][3:]))
            continue
        for operation in _OPERATION.finditer(clause):
            before = clause[:operation.start()].strip()
            action = _ACTION_END.split(clause[operation.start():], maxsplit=1)[0].strip()
            after = action[len(operation[0]):].strip()
            if _ACTION_NEGATION.search(before) or _VAGUE.fullmatch(after) or _STATUS.fullmatch(after):
                continue
            # The action needs a target, rather than a stand-alone direction
            # such as “再刷新一下” or a report such as “同步成功了”。
            targets = _targets(action)
            novel = _OBJECT.match(after)
            object_text = novel[1] if novel and not _GENERIC_OBJECT.search(novel[1]) else ""
            if not targets and not object_text:
                continue
            staff = _STAFF_PREFIX.search(before)
            if staff and not _ATTRIBUTED.search(before[:staff.start()].rstrip()):
                mode = staff["mode"]
                completed = _COMPLETED.search(mode) and not _PLANNED.search(mode)
                actor, state = "staff", "reported" if completed else "proposed"
            elif _MERCHANT_PREFIX.fullmatch(before) and not _NARRATION.search(after):
                actor, state = "merchant", "instructed"
            else:
                continue
            actions.append((actor, state, action, targets, object_text))
            break
    return actions


def _source_ids(source: dict, side: str) -> list[str]:
    value = source.get(f"{side}_message_ids")
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value) or len(set(value)) != len(value):
        return []
    return value


def _bound(source: dict, side: str, ids: list[str], positions: dict[str, tuple[int, dict]], owner: str) -> list[tuple[int, dict, str]]:
    text, parts = source.get(side), source.get(f"_{side}_parts")
    if not isinstance(text, str) or not isinstance(parts, list) or len(parts) != len(ids):
        return []
    result, offset = [], 0
    role = "user" if side == "question" else "staff"
    for identity, part in zip(ids, parts):
        position = positions.get(identity)
        if position is None or not isinstance(part, dict):
            return []
        index, message = position
        if (message["role"] != role or message["_owner"] != owner or not message["_is_text"] or message["_automated"]
                or type(message["ts"]) is not int or message["ts"] <= 0 or (result and index <= result[-1][0])):
            return []
        quote = (voc_outcome._text if role == "user" else _staff_answer)(message["content"])
        end = offset + len(quote)
        if (not quote or text[offset:end] != quote or part.get("message_id") != identity
                or part.get("start") != offset or part.get("end") != end or part.get("ts") != message["ts"]):
            return []
        result.append((index, message, quote))
        offset = end + 1
    return result if text == "\n".join(quote for _, _, quote in result) else []


def _case(qa_id: int, source: dict, questions: list[tuple], answers: list[tuple]) -> dict | None:
    question_text = [(message, _safe_label(quote, source)) for _, message, quote in questions]
    answer_text = [(message, _safe_label(quote, source)) for _, message, quote in answers]
    instructions = [(message, visible, action) for message, visible in answer_text for action in _actions(visible)]
    if not instructions:
        return None

    # Bind each operation to the same specific customer utterance. A generic
    # request or a tie across separate turns is insufficient to choose a target.
    ranked = []
    for message, visible in question_text:
        signals = _targets(visible)
        related = [len(signals & targets) + bool(object_text and object_text in visible)
                   for _, _, (_, _, _, targets, object_text) in instructions]
        if all(related):
            context = sum(len(signals & _targets(reply)) for _, reply, _ in instructions)
            ranked.append(((sum(related), context, bool(_REQUEST.search(visible))), message, visible))
    if not ranked:
        return None
    ranked.sort(key=lambda value: value[0], reverse=True)
    score, problem, quote = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == score:
        return None

    platforms = [name for name, aliases in _PLATFORM_NAMES if any(
        alias in text for _, _, text in questions + answers for alias in aliases)]
    if len(platforms) > 1:
        return None

    def ref(message: dict, text: str) -> dict:
        return {"qa_id": qa_id, "message_id": message["source_message_id"], "quote": text, "ts": message["ts"]}

    steps = [{"actor": actor, "state": state, "action": action, "evidence": ref(message, visible)}
             for message, visible, (actor, state, action, _, _) in instructions]
    problem_ref = ref(problem, quote)
    identity = [qa_id, problem_ref["message_id"], [step["evidence"]["message_id"] for step in steps]]
    case = {"id": "case_path_" + voc_assets.digest(identity)[:24], "qa_id": qa_id, "problem": problem_ref,
            "steps": steps, "platform": platforms[0] if platforms else "", "outcome": "not_recorded"}
    criteria = {clause: ref(message, clause) for message, visible in question_text for fragment in _CLAUSES.split(visible)
                if (clause := fragment.strip()) and _CRITERION.search(clause)
                and any((_targets(clause) & targets) or (object_text and object_text in clause)
                        for _, _, (_, _, _, targets, object_text) in instructions)}
    if len(criteria) == 1:
        case["criterion"] = next(iter(criteria.values()))
    return case


def build(cfg: Settings, sources: dict[int, dict]) -> list[dict]:
    path = Path(cfg.runtime.db_path)
    eligible = {qa_id: source for qa_id, source in sources.items()
                if source.get("_staff_verified") is True and _source_ids(source, "question") and _source_ids(source, "answer")}
    if not eligible or not path.is_file():
        return []

    cases, snapshots = [], {}
    with voc_assets.connection(path) as conn:
        conn.execute("BEGIN")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"qa_pair", "qa_origin", "raw_message", "push_sync_state"} <= tables:
            return []
        owners = conn.execute("""SELECT pair.qa_id, pair.session_id, pair.customer_id,
                    origin.question_message_ids, origin.answer_message_ids, origin.fingerprint, origin.revision
                FROM qa_pair AS pair
                JOIN qa_origin AS origin ON origin.qa_id = pair.qa_id AND origin.session_id = pair.session_id
                WHERE origin.active = 1 AND pair.qa_id IN (SELECT value FROM json_each(?))
                ORDER BY pair.qa_id""", (voc_assets.dump(list(eligible)),))
        for row in owners:
            qa_id, session, owner = row["qa_id"], row["session_id"], row["customer_id"] or ""
            if not owner.startswith(_VISITOR_PREFIXES):
                continue
            source = eligible[qa_id]
            question_ids, answer_ids = _source_ids(source, "question"), _source_ids(source, "answer")
            try:
                if (json.loads(row["question_message_ids"]) != question_ids or json.loads(row["answer_message_ids"]) != answer_ids
                        or source.get("_origin_fingerprint") != [row["fingerprint"], row["revision"]]):
                    continue
            except (ValueError, TypeError):
                continue
            if session not in snapshots:
                try:
                    messages = voc_outcome._snapshot(conn, session)["messages"]
                    positions = {message["source_message_id"]: (index, message) for index, message in enumerate(messages)
                                 if message.get("source_message_id")}
                    snapshots[session] = (messages, positions)
                except (ValueError, TypeError, KeyError, OverflowError):
                    snapshots[session] = None
            snapshot = snapshots[session]
            if snapshot is None:
                continue
            messages, positions = snapshot
            questions = _bound(source, "question", question_ids, positions, owner)
            answers = _bound(source, "answer", answer_ids, positions, owner)
            if not questions or not answers or questions[-1][0] >= answers[0][0] or questions[-1][1]["ts"] >= answers[0][1]["ts"]:
                continue
            span = messages[questions[0][0]:answers[-1][0] + 1]
            segment = questions[0][1]["_segment"]
            anchors = set(question_ids + answer_ids)
            if any(message["_segment"] != segment or message["_owner"] != owner or message["role"] in ("bot", "boundary")
                   or message["_automated"] or (message["_is_text"] and message["source_message_id"] not in anchors)
                   for message in span):
                continue
            case = _case(qa_id, source, questions, answers)
            if case is not None:
                cases.append(case)
    return cases
