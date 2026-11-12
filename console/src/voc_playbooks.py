"""Build actionable handling playbooks from exact, compatible case evidence."""

from __future__ import annotations

from collections import defaultdict
import re
import unicodedata

from .voc_assets import digest
from .voc_episode_evidence import _APPLIED, _confirmed
from .voc_issue_pilot import _AUDIENCES, _GENERIC_MODULES, _MODULES, _TRIGGERS, _VERSION
from .voc_sops import _PRESCRIBED_ACTIONS, _post_retest
from .voc_topic_insights import _PLATFORM_NAMES


_KINDS = {"no_reply": {"anomaly"}, "scenario_config": {"configuration"}, "configuration": {"configuration"},
          "anomaly": {"anomaly"}, "platform_limit": {"configuration", "anomaly"}, "other": {"configuration", "anomaly"}}
_PLATFORM_ALIASES = {"抖音": ("抖店",), "淘宝": ("千牛",)}
_UNBOUND_SCOPE = {"", "未知", "未确认", "待确认", "未识别", "未明确", "未提及", "不详", "全部", "其他"}
_BLOCKERS = {"applicability", "unbound_condition", "unsafe_step", "later_counterevidence"}
_PHASES = {"diagnosis", "action", "retest", "escalation"}
_REF_LIMIT = 8
_PERFORMED_RETEST = re.compile(r"重新测试|复测|实测|再(?:测|试)了(?:一次|一遍)?")
_PROPOSED_RETEST = re.compile(r"如果|只要|尚未|未|没|不用|无需|不必|不要|不是|并非|应该|准备|计划|打算|将要|接下来|需要|还要|(?:^|\s)要|建议|请|可以|得先|等会|稍后")
_UNCERTAIN_RESULT = re.compile(r"可能|大概|似乎|怀疑|听说|看起来|预计|应该|好像|估计|不确定|不清楚|尚待确认")
_RETEST_SCOPE_CHANGE = re.compile(r"(?:换(?:了|到|成|用)?|改用|切换(?:到|成|为)?)[^，,。；;\n]{0,8}(?:店|平台|账号|版本)|(?:别的|另一(?:个|家)|不同的?)(?:店铺|平台|账号|版本)")
_RETEST_OUTCOME = re.compile(
    r"(?:已经|已|成功|正常|确实|顺利)[^，,。！？\n]{0,12}(?:触发|命中|回复|同步|生效|显示|恢复|登录|登陆|导入|导出|下发|送达)"
    r"|(?:触发|命中|回复|同步|生效|显示|恢复|登录|登陆|导入|导出|下发|送达)(?:了|成功|正常|通过)"
)
_RETEST_BOUNDARY = re.compile(r"[。！？!?；;\n]")
_OPERATION_BOUNDARY = re.compile(r"[，,。！？!?；;\n]|(?:之后|后再|后重新|再复测|然后|接着)")


def _key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold()).strip("。！？?!；;")


def _platforms(value: str) -> set[str]:
    normalized = _key(value)
    return {name for name, aliases in _PLATFORM_NAMES
            if any(alias in normalized for alias in (*aliases, *_PLATFORM_ALIASES.get(name, ())))}


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


def _scope_signals(text: str) -> tuple[set[str], ...]:
    return (_platforms(text), {role for role in _AUDIENCES if role in text},
            {re.search(r"\d+(?:\.\d+)+", match[0])[0] for match in _VERSION.finditer(text)},
            {name for name, pattern in _TRIGGERS.items() if re.search(pattern, text, re.I)})


def _completed_before_retest(actions: list[dict], before: str) -> bool:
    count = [0] * len(_PRESCRIBED_ACTIONS)
    reported = _key(before).replace("的", "")
    for step in actions:
        instruction = step["evidence"]["quote"]
        occurrences = sorted((match.start(), match.end(), index) for index, pattern in enumerate(_PRESCRIBED_ACTIONS)
                             for match in pattern.finditer(instruction))
        if not occurrences:
            return False
        for i, (_, end, index) in enumerate(occurrences):
            limit = occurrences[i + 1][0] if i + 1 < len(occurrences) else len(instruction)
            target = _key(_OPERATION_BOUNDARY.split(instruction[end:limit], maxsplit=1)[0]).replace("的", "")
            target = re.sub(r"^(?:这个|该|这条|此)", "", target)
            if len(target) >= 2 and target not in reported:
                return False
            count[index] += 1
    return all(len(list(pattern.finditer(before))) >= required for pattern, required in zip(_APPLIED, count) if required)


def _retest_resolves_issue(issue: dict, observed: str, full_result: str) -> bool:
    if issue["family"] != "no_reply":
        return True
    title = issue["title"]
    if re.search(r"未触发|不触发|未命中|不命中|触发失败", title):
        return bool(re.search(r"触发|命中", observed))
    if re.search(r"回复错误|答非所问|答错|回错", title):
        return "回复" in observed and bool(re.search(r"正确|符合预期|一致", full_result))
    return bool(re.search(r"回复|下发|送达", observed))


def _observed_customer_retest(issue: dict, scope_refs: list[dict], problem: dict, actions: list[dict], feedback: str) -> bool:
    """Recover a performed, same-target retest without writing an instruction."""
    module = issue["module"]
    pattern = _MODULES.get(module) or _GENERIC_MODULES.get(module) or re.escape(module)
    supported = "\n".join([problem["quote"], *(ref["quote"] for ref in scope_refs)])
    if (_RETEST_SCOPE_CHANGE.search(feedback)
            or any(not reported <= anchored for reported, anchored in zip(_scope_signals(feedback), _scope_signals(supported)))
            or any(re.search(alias, feedback, re.I) and not re.search(alias, supported, re.I) for alias in _MODULES.values())
            or not all(_confirmed(step["evidence"]["quote"], feedback) for step in actions)):
        return False

    for retest in _PERFORMED_RETEST.finditer(feedback):
        before = feedback[:retest.start()]
        leading_clause = re.split(r"[，,。！？!?；;\n]", before)[-1]
        after = _RETEST_BOUNDARY.split(feedback[retest.end():], maxsplit=1)[0]
        outcome = _RETEST_OUTCOME.search(after)
        if (_PROPOSED_RETEST.search(leading_clause) or outcome is None
                or _UNCERTAIN_RESULT.search(after) or not _retest_resolves_issue(issue, outcome[0], after[outcome.start():])
                or not re.search(pattern, before, re.I) or not re.search(pattern, after[:outcome.end()], re.I)):
            continue
        # Each action type in every separate staff turn must be reported as
        # completed before the test. Generic “按步骤做完” cannot satisfy it.
        if _completed_before_retest(actions, before):
            return True
    return False


def _branch(issue: dict, case: dict, steps: list[dict], label: str, scope_refs: list[dict], problem: dict, missing: set) -> tuple[dict | None, str]:
    if missing.intersection(_BLOCKERS) or case.get("status") not in ("guidance", "verified"):
        return None, "complete_procedure"
    failed_through = max((step["feedback"]["ts"] if step["feedback"] else step["evidence"]["ts"]
                          for step in steps if step["outcome"] == "failed"), default=0)
    path = [step for step in steps if step["outcome"] != "failed" and step["evidence"]["ts"] > failed_through]
    actions = [step for step in path if step["phase"] == "action"]
    if not actions:
        return None, "complete_procedure"
    result = _ref(case.get("result"), problem["qa_id"])
    verified = (case["status"] == "verified" and result is not None
                and all(step["evidence"]["ts"] < result["ts"] and step["outcome"] == "verified" and step["feedback"] == result
                        for step in actions))
    retest = _post_retest(path, require_action=True)
    if retest is None:
        if (not verified or any(step["evidence"]["ts"] >= result["ts"] for step in path if step["phase"] != "escalation")
                or not _observed_customer_retest(issue, scope_refs, problem, actions, result["quote"])):
            return None, "post_change_retest"
        retest_text = f"客户实测原话：{result['quote']}"
        retest_ref = result
        before_retest = path
        after_retest = [step for step in path if step["evidence"]["ts"] > result["ts"]]
    else:
        retest_step, retest_text = retest
        retest_ref = path[retest_step]["evidence"]
        before_retest, after_retest = path[:retest_step + 1], path[retest_step + 1:]
        verified = verified and result["ts"] > retest_ref["ts"]
    procedure = [step for step in before_retest if step["phase"] in ("diagnosis", "action")]
    # An escalation is optional and kept as an exact sourced fallback.
    escalation = next((step for step in after_retest if step["phase"] == "escalation"), None)
    return {"condition": label, "condition_evidence": _refs(scope_refs),
            "status": "verified" if verified else "guidance", "case_count": 1,
            "_actions": _action_key(procedure),
            "steps": [{"text": step["evidence"]["quote"], "evidence": [step["evidence"]]} for step in procedure],
            "retest": retest_text, "retest_evidence": [retest_ref],
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
        if family not in _KINDS:
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
            branch, gap = _branch(issue, case, steps, label, scope_refs, problem, case_missing)
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
        elif not branches:
            missing.add("complete_procedure")
        missing.update(label for label in issue.get("next", {}).get("missing", [])
                       if label != "可核对的客服处理记录")
        if any(branch["status"] == "guidance" for branch in branches):
            missing.add("verified_outcome")
        status = ("verified" if branches and all(branch["status"] == "verified" for branch in branches)
                  else "guidance" if branches else "investigation")
        items.append({"id": "playbook_" + digest([issue_id])[:24], "issue_id": issue_id, "title": issue["title"],
                      "family": family, "platform": issue.get("platform", ""), "module": issue.get("module", ""),
                      "condition": issue.get("condition", ""), "status": status, "checked_cases": len(checked),
                      "triage": [], "cases": issue["cases"], "impact": issue["impact"],
                      "branches": branches, "missing": sorted(missing), "evidence": _refs(counterevidence + members),
                      "counterevidence": _refs(counterevidence)})
    return {"items": items, "total": len(items)}
