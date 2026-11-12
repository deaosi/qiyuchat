"""Compile SOP candidates from re-bound, current model-case source messages."""

from __future__ import annotations

from collections import defaultdict
import re

from . import voc_episode_evidence, voc_issue_pilot, voc_outcome, voc_playbooks, voc_sops
from .voc_assets import digest


def _source(candidate: dict) -> tuple[list[dict], set[str]]:
    messages, anchors, originals = (candidate.get(key) for key in ("messages", "anchors", "original"))
    if not isinstance(messages, list) or not isinstance(anchors, set) or not isinstance(originals, dict):
        return [], set()
    source, source_anchors = [], set()
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("id"), str):
            return [], set()
        raw = originals.get(message["id"])
        if (not isinstance(raw, dict) or raw.get("role", message.get("role")) != message.get("role")
                or raw.get("ts", message.get("ts")) != message.get("ts")):
            return [], set()
        original_id = raw.get("source_message_id")
        if message["id"] in anchors:
            if not isinstance(original_id, str) or message.get("role") != "user":
                return [], set()
            source_anchors.add(original_id)
        source.append({**message, "id": original_id, "automated": bool(message.get("automated") or raw.get("_automated"))})
    return (source, source_anchors) if len(source_anchors) == len(anchors) else ([], set())


def _bind(raw: object, indexed: dict, qa_id: int, *, full_turn: bool = False) -> dict | None:
    if not isinstance(raw, dict) or raw.get("qa_id") != qa_id or not isinstance(raw.get("message_id"), str):
        return None
    entry = indexed.get(raw["message_id"])
    if entry is None:
        return None
    _, message = entry
    quote = raw.get("quote")
    if (message.get("kind") != "text" or message["automated"] or message["role"] not in ("staff", "user")
            or type(raw.get("ts")) is not int or raw["ts"] != message["ts"] or raw["ts"] <= 0
            or not isinstance(quote, str) or not quote.strip()):
        return None
    text = message["text"]
    start = text.find(quote)
    if start < 0 or start != text.rfind(quote) or (full_turn and quote != text):
        return None
    return {"message_id": raw["message_id"], "quote": quote, "start": start, "end": start + len(quote)}


def _sections(case: dict, indexed: dict, qa_id: int) -> dict[str, list[list[dict]]]:
    solution = case.get("solution")
    if not isinstance(solution, dict) or not isinstance(solution.get("sections"), list):
        return {}
    result = defaultdict(list)
    for section in solution["sections"]:
        if not isinstance(section, dict) or not isinstance(section.get("evidence"), list):
            return {}
        kind = section.get("kind")
        if kind not in ("problem", "diagnosis", "action", "decision", "verification"):
            return {}
        refs = [_bind(ref, indexed, qa_id, full_turn=True) for ref in section["evidence"]]
        if not refs or any(ref is None for ref in refs):
            return {}
        result[kind].append(refs)
    return result


def _kind(issue: dict, quote: str) -> str:
    kinds = voc_playbooks._KINDS[issue["family"]]
    if len(kinds) == 1:
        return next(iter(kinds))
    if voc_issue_pilot._CAPABILITY_REQUEST.search(quote) or voc_issue_pilot._LIMITATION.search(quote):
        return "configuration"
    if voc_issue_pilot._REQUEST.search(quote):
        return "configuration" if voc_issue_pilot._GENERAL_TASK.search(quote) else ""
    return "anomaly" if voc_issue_pilot._GENERAL_FAILURE.search(quote) else ""


def _failed_retest(issue: dict, problem: dict, scope_refs: list[dict], actions: list[dict], quote: str) -> bool:
    module = issue["module"]
    pattern = voc_issue_pilot._MODULES.get(module) or voc_issue_pilot._GENERIC_MODULES.get(module) or re.escape(module)
    supported = "\n".join([problem["quote"], *(ref["quote"] for ref in scope_refs)])
    if (voc_outcome._QUESTION.search(quote) or voc_outcome._PROSPECTIVE.search(quote)
            or voc_playbooks._RETEST_SCOPE_CHANGE.search(quote)
            or any(not actual <= expected for actual, expected in zip(voc_playbooks._scope_signals(quote), voc_playbooks._scope_signals(supported)))):
        return False
    for retest in voc_playbooks._PERFORMED_RETEST.finditer(quote):
        before = quote[:retest.start()]
        after = voc_playbooks._RETEST_BOUNDARY.split(quote[retest.end():], maxsplit=1)[0]
        failed = voc_episode_evidence._FAILURE.search(after) or voc_outcome._NEGATIVE.search(after)
        if (failed and re.search(pattern, after, re.I)
                and voc_playbooks._retest_resolves_issue(issue, failed[0], after[failed.start():])
                and voc_playbooks._completed_before_retest(actions, before)):
            return True
    return False


def _compile(issue: dict, case: dict, problem: dict, sections: dict, messages: list[dict], indexed: dict) -> dict | None:
    qa_id = case["qa_id"]
    kind = _kind(issue, problem["quote"])
    if not kind:
        return None

    def staff_refs(groups: list[list[dict]]) -> dict[str, dict]:
        return {ref["message_id"]: ref for refs in groups for ref in refs
                if indexed[ref["message_id"]][1]["role"] == "staff"}

    action_refs = staff_refs(sections.get("action", []))
    if not action_refs:
        return None
    answers = staff_refs([refs for name in ("action", "diagnosis", "decision", "verification") for refs in sections.get(name, [])])
    position, question_turn = indexed[problem["message_id"]]
    if any(indexed[mid][0] <= position or indexed[mid][1]["ts"] <= question_turn["ts"] for mid in answers):
        return None
    cited = {ref["message_id"] for groups in sections.values() for refs in groups for ref in refs}
    last_answer = max(indexed[mid][0] for mid in answers)
    if any(message["role"] == "user" and message["id"] not in cited and voc_outcome._QUESTION.search(message["text"])
           for message in messages[position + 1:last_answer]):
        return None

    actions, feedback_ids = [], set()
    for refs in sections.get("verification", []):
        customers = [ref for ref in refs if indexed[ref["message_id"]][1]["role"] == "user"]
        if len(customers) != 1:
            continue
        feedback = customers[0]
        feedback_position, feedback_turn = indexed[feedback["message_id"]]
        prior = [mid for mid in action_refs if indexed[mid][0] < feedback_position and indexed[mid][1]["ts"] < feedback_turn["ts"]]
        if not prior:
            continue
        latest = max(prior, key=lambda mid: indexed[mid][0])
        if not any(ref["message_id"] == latest for ref in refs):
            continue
        feedback_ids.add(feedback["message_id"])
        actions.append({"instruction": action_refs[latest], "feedback": feedback})

    members = {problem["message_id"], *answers, *feedback_ids}
    episode = {"id": digest([case.get("id"), qa_id, problem, kind]), "kind": kind,
               "platform": issue.get("platform", ""), "module": issue.get("module", ""), "question": problem,
               "message_ids": [message["id"] for message in messages if message["id"] in members],
               "answers": list(answers.values()), "actions": actions}
    sop = voc_sops.compile_case(episode, messages, qa_id)
    if sop is None:
        return None
    problem_ref = {**sop["problem"], "qa_id": qa_id}
    scope = voc_playbooks._scope(issue, sop, problem_ref)
    steps = voc_playbooks._steps(sop, problem_ref)
    if scope is None or not steps:
        return None
    _, label, scope_refs = scope
    operations = [step for step in steps if step["phase"] == "action"]
    if not operations or any(step["evidence"]["message_id"] not in action_refs for step in operations):
        return None

    # An identified action in another scope cannot become a step of this case.
    scope_text = "\n".join([problem_ref["quote"], *(ref["quote"] for ref in scope_refs)])
    action_text = "\n".join(step["evidence"]["quote"] for step in operations)
    scope_signals = voc_playbooks._scope_signals(scope_text)
    if (any(len(values) > 1 for values in scope_signals[:3])
            or any(not actual <= scoped for actual, scoped in zip(voc_playbooks._scope_signals(action_text), scope_signals))
            or any(re.search(pattern, action_text, re.I) and not re.search(pattern, scope_text, re.I)
                   for pattern in voc_issue_pilot._MODULES.values())):
        return None

    failed = [step for step in operations if step["outcome"] == "failed"]
    if failed:
        failures = voc_playbooks._failures(steps, set())
        if (len(failures) != 1 or failures[0][1] is None
                or not _failed_retest(issue, problem_ref, scope_refs, failed, failures[0][1]["quote"])):
            return None
    current_actions = [step for step in operations if step["outcome"] != "failed"]
    if (sop["status"] == "verified" and not voc_playbooks._observed_customer_retest(
            issue, scope_refs, problem_ref, current_actions, sop["result"]["quote"])):
        return None
    branch, _ = voc_playbooks._branch(issue, sop, steps, label, scope_refs, problem_ref, set(sop["missing"]))
    return sop if branch or failed else None


def compile_supported(issues: list[dict], model_cases: list[dict], current_candidates: dict[int, dict],
                      existing_cases: list[dict]) -> list[dict]:
    """Supply existing playbook gates with uniquely matched, source-backed cases."""
    by_anchor = defaultdict(dict)
    for issue in issues:
        if issue.get("family") not in voc_playbooks._KINDS:
            continue
        for member in issue.get("_members", []):
            if member.get("anchored") is True and isinstance(member.get("message_id"), str) and isinstance(member.get("quote"), str):
                key = (issue["id"], member["quote"])
                by_anchor[member.get("qa_id"), member["message_id"]][key] = issue, member
    used = {case_key(case) for case in existing_cases if case["status"] != "incomplete"
            or "later_counterevidence" in case["missing"] or any(step["outcome"] == "failed" for step in case["steps"])}
    result = []
    for case in model_cases:
        qa_id = case.get("qa_id")
        if type(qa_id) is not int or qa_id <= 0 or qa_id not in current_candidates:
            continue
        messages, anchors = _source(current_candidates[qa_id])
        indexed = voc_sops._indexed(messages)
        sections = _sections(case, indexed, qa_id)
        problem_ids = {ref["message_id"] for refs in sections.get("problem", []) for ref in refs} & anchors
        matches = [(issue, exact) for mid in problem_ids for issue, member in by_anchor[qa_id, mid].values()
                   if (exact := _bind(member, indexed, qa_id)) is not None]
        if len(matches) != 1:
            continue
        issue, problem = matches[0]
        key = (qa_id, problem["message_id"], problem["quote"])
        if key not in used and (sop := _compile(issue, case, problem, sections, messages, indexed)) is not None:
            used.add(key)
            result.append(sop)
    return result


def case_key(case: dict) -> tuple:
    return case["qa_id"], case["problem"]["message_id"], case["problem"]["quote"]
