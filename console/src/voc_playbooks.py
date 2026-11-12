"""Build actionable handling playbooks from exact, compatible case evidence."""

from __future__ import annotations

from collections import defaultdict
import re
import unicodedata

from .voc_assets import digest
from .voc_issue_pilot import _GENERIC_MODULES, _MODULES
from .voc_sops import _post_retest
from .voc_topic_insights import _PLATFORM_NAMES


_TRIAGE = {
    "no_reply": (
        ("在同一次复现里，确认该消息有没有进入预期的机器人接待或场景触发链路。",
         "记录触发节点和时间，继续核对规则或知识检索。",
         "记录店铺、会话、入口和触发条件，先定位接待入口或触发边界。"),
        ("核对本次命中的规则或检索到的知识是否与客户问法、商品和场景条件相符。",
         "记录命中对象和预期回复，继续对比生成、发送与送达结果。",
         "记录预期和实际命中、当前条件及测试问法，沿对应规则或检索路径排查。"),
        ("核对本次是否生成了回复，以及平台侧是否收到或展示。",
         "保留平台显示与客户反馈的差异，确认是否在同一会话和时间点观察。",
         "按未生成、生成未发送、发送未送达分开记录；带会话时间、测试问法和可见日志升级。"),
    ),
    "scenario_config": (
        ("确认商家想在哪个平台、店铺和场景条件下，实现什么具体效果。",
         "固定一条应触发的测试输入和一条不应触发的对照输入。",
         "先补齐店铺、适用商品或入口、触发条件和预期效果，再决定是否调整。"),
        ("核对现有配置及生效范围是否覆盖这条正例，并排除对照样本。",
         "保持本次复测条件一致，检查配置结果和实际接待结果。",
         "记录缺少或冲突的条件，按可核验的操作分支处理。"),
        ("复测正例与对照，核对它们是否分别表现为触发与不触发。",
         "记录测试输入、配置范围和结果，保留为后续复用依据。",
         "保留配置前后差异、测试输入和实际结果，补证据后交接对应模块。"),
    ),
    "configuration": (
        ("确认目标操作、当前配置和预期生效范围。",
         "记录操作前的设置和一条可重复验证的输入。",
         "补齐操作账号、适用范围、期望值和实际值，先明确需要调整哪项。"),
        ("在相同范围用同一输入复测，实际结果是否符合预期？",
         "保留配置前后差异、测试输入和结果。",
         "记录哪个条件未生效，带上配置差异和复测结果交接。"),
    ),
    "anomaly": (
        ("确认同一条件下的预期、实际结果和最短复现步骤。",
         "记录首个偏离预期的步骤和时间，继续核对该步骤前的状态。",
         "补齐触发操作、影响范围、预期结果和实际表现，复现后再归因。"),
        ("能否在同一范围找到一个表现正常的对照样本？",
         "比较正常与异常样本，保留第一处差异和可见错误信息。",
         "保留异常样本、条件及时间，说明尚未获得对照，并提交相应团队核实。"),
    ),
    "platform_limit": (
        ("核对适用平台、账号权限和当前版本，明确限制出现时的操作和提示。",
         "保留原始提示和复现步骤，核对同范围的平台规则。",
         "补齐具体账号角色、版本和可复现操作，避免把其他范围的限制套用到本案。"),
        ("能否在适用范围内用可信规则或测试确认限制？",
         "记录规则版本、适用范围和可核验的替代处理。",
         "保留操作、提示与对照结果，请对应平台或支持团队确认。"),
    ),
    "other": (
        ("确认商家具体要完成的操作、期望结果和当前观察。",
         "固定对象、适用范围和最短检查步骤，留一条原始证据。",
         "先补齐原问题、操作对象和期望结果，再选择处理方向。"),
        ("能否在相同条件下核对预期与实际结果？",
         "记录观察结果和下一步；有复测依据后再提炼处理方法。",
         "保留会话时间、适用条件、已尝试动作与缺少的证据，交接相应团队。"),
    ),
}

_KINDS = {"no_reply": {"anomaly"}, "scenario_config": {"configuration"}, "configuration": {"configuration"},
          "anomaly": {"anomaly"}, "platform_limit": {"configuration", "anomaly"}, "other": {"configuration", "anomaly"}}
_PLATFORM_ALIASES = {"抖音": ("抖店",), "淘宝": ("千牛",)}
_UNBOUND_SCOPE = {"", "未知", "未确认", "待确认", "未识别", "未明确", "未提及", "不详", "全部", "其他"}
_BLOCKERS = {"applicability", "unbound_condition", "unsafe_step", "later_counterevidence"}
_PHASES = {"diagnosis", "action", "retest", "escalation"}
_REF_LIMIT = 8


def _key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold()).strip("。！？?!；;")


def _platforms(value: str) -> set[str]:
    normalized = _key(value)
    return {name for name, aliases in _PLATFORM_NAMES
            if any(alias in normalized for alias in (*aliases, *_PLATFORM_ALIASES.get(name, ())))}


def _investigation(issue: dict) -> list[dict]:
    family = issue["family"]
    title, module = issue["title"], issue["module"] or "相关功能"
    platform, condition = issue["platform"], issue["condition"]
    scope = "、".join(value for value in (platform, module, condition) if value)
    fallback = _TRIAGE[family]
    if family == "no_reply":
        if "回复错误" in title:
            rows = (
                (f"在「{scope}」复现一条答错的原问题，保留预期答复和实际发出的答复。",
                 "把预期与实际答复逐项对比，记录跑偏的字段和当时条件。",
                 "先向商家确认正确口径，并补齐原问、实际回复和对应时间。"),
                ("能否确认本次采用的是哪条规则、哪条知识或哪个回复模板？",
                 "对照该规则的条件、内容及优先级，检查是否命中了错误来源。",
                 "保留店铺、会话时间、测试问题和两版答复，请技术确认回复依据。"),
            )
        elif "未触发" in title:
            rows = (
                (f"按「{scope}」固定测试输入、店铺、入口和时间，核对是否产生预期的触发记录。",
                 "记录触发对象和时间，若最终没有答复，转查命中和发送链路。",
                 "停在触发环节，逐项比对本次输入与生效条件，保留缺失或不匹配项。"),
                ("能否通过同一店铺的一条正例，确认当前条件下应当触发？",
                 "留存正例与失败样本，并标记第一次出现差异的触发条件。",
                 "提交测试输入、预期场景、实际状态和可见日志，确认触发边界。"),
            )
        else:
            rows = ((f"按「{scope}」固定一次测试，确认消息进入接待链路且能定位到时间。",
                     fallback[0][1], fallback[0][2]), *fallback[1:])
        sample_text = "\n".join([title, *(member.get("quote", "") for member in issue.get("_members", []))])
        if re.search(r"偶发|偶尔|时有时无|不稳定|有时候.{0,10}(?:没有|不|没)|(?:有时|偶尔).{0,6}(?:不回复|不触发)", sample_text):
            rows = ((f"若「{scope}」出现间歇现象，能否在同一店铺、问法和生效条件下各取一条成功与失败样本？",
                     "比较两个样本的入口、时间、命中对象和回复结果，定位第一个差异。",
                     "保留失败样本与已知条件，在同一范围复现，等待可比的成功样本。"), *rows)
    elif "调整回复内容" in title:
        rows = (
            (f"明确「{scope}」要修改的回复内容、适用问法和适用范围。",
             "保留修改前的答复，选择一条应命中和一条不应命中的问法。",
             "先让商家确认期望口径与适用范围，补齐之前的实际答复。"),
            ("用同一测试问法核对实际回复内容是否符合新口径，对照问法是否不受影响。",
             "记录新旧答复和测试结果，确认改动只影响目标范围。",
             "记录实际命中的规则或知识来源，检查是否有同范围的旧内容竞争。"),
        )
    elif "查找配置入口" in title or "绑定与授权" in title:
        rows = (
            (f"核对「{scope}」所用账号角色、目标店铺和当前授权状态。",
             "按该平台已确认的操作文档核对入口或授权状态。",
             "先确定目标店铺及操作权限，避免把其他店铺的配置路径套用进来。"),
            ("目标账号能否在同一店铺进入相应功能或读取已授权状态？",
             "记录账号角色、入口和核对结果，作为当前适用范围。",
             "保留权限提示和时间点，向有权限的管理员或负责模块确认。"),
        )
    elif family == "scenario_config":
        rows = ((f"明确「{scope}」的目标操作、预期生效条件和不应生效的边界。",
                 fallback[0][1], fallback[0][2]), *fallback[1:])
    else:
        rows = ((f"针对「{title}」记录适用范围「{scope}」。{fallback[0][0]}",
                 fallback[0][1], fallback[0][2]), *fallback[1:])
    return [{"check": check, "when_yes": yes, "when_no": no} for check, yes, no in rows]


def _ref(raw: object, qa_id: int) -> dict | None:
    if not isinstance(raw, dict):
        return None
    mid, quote, stamp = raw.get("message_id"), raw.get("quote"), raw.get("ts")
    if not isinstance(mid, str) or not mid or not isinstance(quote, str) or not quote.strip() or type(stamp) is not int or stamp <= 0:
        return None
    return {"qa_id": qa_id, "message_id": mid, "quote": quote, "ts": stamp}


def _refs(items: list[dict]) -> list[dict]:
    deduped = {}
    for ref in items:
        deduped.setdefault((ref["qa_id"], ref["message_id"], ref["quote"]), ref)
    return list(deduped.values())[:_REF_LIMIT]


def _scope(issue: dict, case: dict, problem: dict) -> tuple[tuple, str, list[dict]] | None:
    conditions = case.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return None
    refs = [_ref(raw, problem["qa_id"]) for raw in conditions]
    if any(ref is None for ref in refs):
        return None
    quotations = [problem["quote"], *(ref["quote"] for ref in refs)]
    platform = _key(issue.get("platform"))
    module = _key(issue.get("module"))
    if platform in _UNBOUND_SCOPE or module in _UNBOUND_SCOPE:
        return None
    platforms = _platforms("\n".join(quotations))
    expected = _platforms(platform)
    if (expected and (len(expected) != 1 or platforms != expected)
            or not expected and (platforms or not any(platform in _key(text) for text in quotations))):
        return None
    module_name = issue["module"]
    module_pattern = _MODULES.get(module_name) or _GENERIC_MODULES.get(module_name)
    actual_modules = {name for name, pattern in _MODULES.items() if any(re.search(pattern, text, re.I) for text in quotations)}
    if (len(actual_modules) > 1 or module_name in _MODULES and actual_modules != {module_name}
            or not any(re.search(module_pattern, text, re.I) if module_pattern else module in _key(text) for text in quotations)):
        return None
    # Retain each exact prerequisite. Differently worded or absent constraints
    # remain separate until they are confirmed to be interchangeable.
    scope_parts = tuple(sorted({_key(ref["quote"]) for ref in refs}))
    label = "；".join(dict.fromkeys(ref["quote"].strip() for ref in refs))
    return (tuple(sorted(expected)) or (platform,), module, _key(issue.get("condition")), case["kind"], scope_parts), label, refs


def _steps(case: dict, problem: dict) -> list[dict] | None:
    steps = case.get("steps")
    if not isinstance(steps, list):
        return None
    result = []
    previous = problem["ts"]
    for step in steps:
        if not isinstance(step, dict) or step.get("phase") not in _PHASES or step.get("outcome") not in ("failed", "verified", "unconfirmed"):
            return None
        ref = _ref(step.get("evidence"), problem["qa_id"])
        if ref is None or ref["ts"] < previous or ref["ts"] <= problem["ts"] or ref["message_id"] == problem["message_id"]:
            return None
        previous = ref["ts"]
        feedback = _ref(step.get("feedback"), problem["qa_id"])
        if feedback and (feedback["ts"] <= ref["ts"] or feedback["message_id"] == ref["message_id"]):
            return None
        result.append({"phase": step["phase"], "evidence": ref, "outcome": step["outcome"], "feedback": feedback})
    return result


def _action_key(steps: list[dict]) -> tuple:
    return tuple(_key(step["evidence"]["quote"]) for step in steps if step["phase"] == "action")


def _failures(steps: list[dict], missing: set) -> list[tuple]:
    contradicted = []
    attempted = []
    for step in steps:
        if step["phase"] != "action":
            continue
        if step["outcome"] != "failed":
            if attempted:
                contradicted.append((_action_key(attempted), None))
            attempted = []
            continue
        attempted.append(step)
        if step["feedback"]:
            contradicted.append((_action_key(attempted), step["feedback"]))
            attempted = []
    if attempted:
        contradicted.append((_action_key(attempted), None))
    if "later_counterevidence" in missing:
        key = _action_key([step for step in steps if step["outcome"] != "failed"])
        if key:
            contradicted.append((key, None))
    return contradicted


def _branch(case: dict, steps: list[dict], label: str, scope_refs: list[dict], problem: dict, missing: set) -> tuple[dict | None, str]:
    if missing.intersection(_BLOCKERS) or case.get("status") not in ("guidance", "verified"):
        return None, "complete_procedure"
    failed_through = max((step["feedback"]["ts"] if step["feedback"] else step["evidence"]["ts"]
                          for step in steps if step["outcome"] == "failed"), default=0)
    path = [step for step in steps if step["outcome"] != "failed" and step["evidence"]["ts"] > failed_through]
    actions = [step for step in path if step["phase"] == "action"]
    if not actions:
        return None, "complete_procedure"
    retest = _post_retest(path, require_action=True)
    if retest is None:
        return None, "post_change_retest"
    retest_step, retest_text = retest
    procedure = [step for step in path[:retest_step + 1] if step["phase"] in ("diagnosis", "action")]
    # An escalation is optional and kept as an exact sourced fallback.
    escalation = next((step for step in path[retest_step + 1:] if step["phase"] == "escalation"), None)
    result = _ref(case.get("result"), problem["qa_id"])
    verified = (case["status"] == "verified" and result is not None
                and result["ts"] > path[retest_step]["evidence"]["ts"]
                and all(step["outcome"] == "verified" and step["feedback"] == result for step in actions))
    return {"condition": label, "condition_evidence": _refs(scope_refs),
            "status": "verified" if verified else "guidance", "case_count": 1,
            "_actions": _action_key(procedure),
            "steps": [{"text": step["evidence"]["quote"], "evidence": [step["evidence"]]} for step in procedure],
            "retest": retest_text, "retest_evidence": [path[retest_step]["evidence"]],
            "escalation": escalation["evidence"]["quote"] if escalation else "",
            "escalation_evidence": [escalation["evidence"]] if escalation else [],
            "result_evidence": [result] if verified else []}, ""


def _merge_branch(branch: dict, new: dict) -> None:
    branch["case_count"] += 1
    if new["status"] == "verified":
        branch["status"] = "verified"
    for first, second in zip(branch["steps"], new["steps"]):
        first["evidence"] = _refs(first["evidence"] + second["evidence"])
    for key in ("condition_evidence", "retest_evidence", "escalation_evidence", "result_evidence"):
        branch[key] = _refs(branch[key] + new[key])


def build(issues: list[dict], sops: list[dict]) -> dict:
    """Use case transcripts as evidence. An investigation alone has no reusable branch."""
    cases_by_problem = defaultdict(list)
    for case in sops:
        qa_id = case.get("qa_id")
        if type(qa_id) is not int or qa_id <= 0:
            continue
        problem = _ref(case.get("problem"), qa_id)
        if problem is not None:
            cases_by_problem[(qa_id, problem["message_id"], problem["quote"])].append((case, problem))
    items = []
    for issue in issues:
        family = issue.get("family")
        if family not in _TRIAGE:
            continue
        issue_id = issue["id"]
        members, checked = [], set()
        branch_candidates = []
        conflicts = defaultdict(list)
        missing = set()
        for member in issue.get("_members", []):
            qa_id = member.get("qa_id")
            if member.get("anchored") is not True or type(qa_id) is not int or qa_id <= 0:
                continue
            anchor = _ref(member, qa_id)
            if anchor is None:
                continue
            match = (qa_id, anchor["message_id"], anchor["quote"])
            if match in checked:
                continue
            linked = cases_by_problem.get(match, [])
            if len(linked) != 1:
                members.append(anchor)
                continue
            case, problem = linked[0]
            members.append(problem)
            if case.get("kind") not in _KINDS[family]:
                continue
            checked.add(match)
            scope = _scope(issue, case, problem)
            if scope is None:
                missing.add("applicability")
                continue
            scope_key, label, scope_refs = scope
            steps = _steps(case, problem)
            if steps is None:
                missing.add("complete_procedure")
                continue
            case_missing = set(case.get("missing") or [])
            for actions, ref in _failures(steps, case_missing):
                conflicts[(scope_key, actions)].append(ref)
            branch, gap = _branch(case, steps, label, scope_refs, problem, case_missing)
            if branch is None:
                missing.add(gap)
                continue
            branch_candidates.append((scope_key, branch.pop("_actions"), branch))

        published = {}
        counterevidence = []
        for scope, actions, branch in branch_candidates:
            if (scope, actions) in conflicts:
                missing.add("conflicting_case")
                counterevidence.extend(ref for ref in conflicts[scope, actions] if ref is not None)
                continue
            key = (scope, tuple(_key(step["text"]) for step in branch["steps"]), _key(branch["retest"]), _key(branch["escalation"]))
            if key in published:
                _merge_branch(published[key], branch)
            else:
                branch["id"] = "branch_" + digest([issue_id, key])[:24]
                published[key] = branch
        branches = list(published.values())
        if not checked:
            missing.add("case_evidence")
        if not branches:
            missing.add("complete_procedure")
        if any(branch["status"] == "guidance" for branch in branches):
            missing.add("verified_outcome")
        status = ("verified" if branches and all(branch["status"] == "verified" for branch in branches)
                  else "guidance" if branches else "investigation")
        items.append({"id": "playbook_" + digest([issue_id])[:24], "issue_id": issue_id, "title": issue["title"],
                      "family": family, "platform": issue.get("platform", ""), "module": issue.get("module", ""),
                      "condition": issue.get("condition", ""), "status": status, "checked_cases": len(checked),
                      "triage": _investigation(issue),
                      "branches": branches, "missing": sorted(missing), "evidence": _refs(counterevidence + members)})
    return {"items": items, "total": len(items)}
