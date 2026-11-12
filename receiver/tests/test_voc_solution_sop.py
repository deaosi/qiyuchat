"""Exercise the strict bridge with model-style refs and current official turns."""

from copy import deepcopy
import unittest

from src import voc_playbooks, voc_solution_sop


QUESTION = "抖音子账号的退款场景开关已开启，但机器人没有触发。"
CHECK = "请核对退款场景开关是否开启。"
ACTION = "请调整退款场景的触发条件并保存。"
RETEST = "请重新测试退款场景是否触发。"
SUCCESS = "我调整退款场景的触发条件并保存了，用同一个买家问法重新测试退款场景，已经触发了。"
FAILURE = "我调整退款场景的触发条件并保存了，用同一个买家问法重新测试退款场景，仍然没有触发。"


def fixture(qa_id=42, *, feedback=SUCCESS, with_retest=False, before_action=False, action=ACTION, later=""):
    turns = [("q", "user", QUESTION), ("d", "staff", CHECK), ("a", "staff", action)]
    if with_retest:
        turns.append(("r", "staff", RETEST))
    if feedback:
        turns.insert(2 if before_action else len(turns), ("f", "user", feedback))
    if later:
        turns.append(("x", "user", later))
    messages, originals, refs = [], {}, {}
    for index, (identity, role, text) in enumerate(turns, 1):
        ts = index * 1000
        mid = f"source:{qa_id}:{identity}"
        messages.append({"id": identity, "role": role, "kind": "text", "ts": ts, "text": text})
        originals[identity] = {"source_message_id": mid, "role": role, "ts": ts}
        refs[identity] = {"qa_id": qa_id, "message_id": mid, "quote": text, "ts": ts}
    sections = [{"kind": "problem", "evidence": [refs["q"]]}, {"kind": "diagnosis", "evidence": [refs["d"]]},
                {"kind": "action", "evidence": [refs["a"]]}]
    if with_retest:
        sections.append({"kind": "verification", "evidence": [refs["r"]]})
    if feedback:
        sections.append({"kind": "verification", "evidence": [refs["a"], refs["f"]]})
    model = {"id": f"model-{qa_id}", "qa_id": qa_id, "problem": refs["q"], "solution": {"sections": sections}}
    candidate = {"messages": messages, "anchors": {"q"}, "original": originals}
    return model, candidate, {**refs["q"], "anchored": True}


def issue(*members):
    return {"id": "issue-1", "family": "no_reply", "title": "退款场景不触发", "platform": "抖音", "module": "退款场景",
            "condition": "已开启", "cases": [], "impact": {}, "_members": list(members)}


class TestSolutionSop(unittest.TestCase):
    def test_recompiles_exact_action_and_customer_retest_without_a_preexisting_episode(self):
        model, proof, member = fixture()
        member = {**member, "quote": member["quote"].removesuffix("。")}
        card = issue(member)
        [sop] = voc_solution_sop.compile_supported([card], [model], {42: proof}, [])
        self.assertEqual(sop["status"], "verified")
        self.assertEqual(sop["problem"]["message_id"], member["message_id"])
        self.assertEqual(sop["problem"]["quote"], member["quote"])
        self.assertEqual(sop["result"]["quote"], SUCCESS)
        [playbook] = voc_playbooks.build([card], [sop])["items"]
        [branch] = playbook["branches"]
        self.assertEqual(branch["status"], "verified")
        self.assertEqual([step["text"] for step in branch["steps"]], [CHECK, ACTION])
        self.assertEqual(voc_solution_sop.compile_supported([card], [model], {42: proof}, [sop]), [])

    def test_missing_result_can_only_contribute_existing_documented_guidance(self):
        model, proof, member = fixture(feedback="")
        self.assertEqual(voc_solution_sop.compile_supported([issue(member)], [model], {42: proof}, []), [])
        model, proof, member = fixture(feedback="谢谢。", with_retest=True)
        card = issue(member)
        [sop] = voc_solution_sop.compile_supported([card], [model], {42: proof}, [])
        [playbook] = voc_playbooks.build([card], [sop])["items"]
        self.assertEqual(sop["status"], "guidance")
        self.assertIsNone(sop["result"])
        self.assertEqual(playbook["branches"][0]["result_evidence"], [])

    def test_recommended_step_and_expected_acceptance_cannot_be_used_as_retest_proof(self):
        model, proof, member = fixture()
        sections = model["solution"]["sections"]
        sections.append({"kind": "next_step", "evidence": [member]})
        sections[-2]["mode"] = "observed"
        [sop] = voc_solution_sop.compile_supported([issue(member)], [model], {42: proof}, [])
        self.assertEqual(sop["status"], "verified")
        sections[-2]["mode"] = "expected"
        self.assertEqual(voc_solution_sop.compile_supported([issue(member)], [model], {42: proof}, []), [])

    def test_wrong_issue_actor_chronology_and_source_refs_do_not_promote(self):
        model, proof, member = fixture()
        wrong_platform = {**issue(member), "platform": "淘宝"}
        wrong_problem = issue({**member, "quote": member["quote"].replace("抖音", "淘宝")})
        duplicate = {**issue(member), "id": "issue-2"}
        invalid_actor = deepcopy(model)
        invalid_actor["solution"]["sections"][2]["evidence"] = [member]
        invalid_quote = deepcopy(model)
        invalid_quote["solution"]["sections"][2]["evidence"][0]["quote"] = "调整退款场景的触发条件"
        invalid_time = deepcopy(model)
        invalid_time["solution"]["sections"][-1]["evidence"][-1]["ts"] += 1
        early, early_proof, early_member = fixture(before_action=True)
        staff_action, staff_proof, staff_member = fixture(action="我这边已经调整了退款场景的触发条件。")
        for cards, path, candidate in (([wrong_platform], model, proof), ([wrong_problem], model, proof),
                                       ([issue(member), duplicate], model, proof), ([issue(member)], invalid_actor, proof),
                                       ([issue(member)], invalid_quote, proof), ([issue(member)], invalid_time, proof),
                                       ([issue(early_member)], early, early_proof), ([issue(staff_member)], staff_action, staff_proof)):
            with self.subTest(path=path, cards=cards):
                self.assertEqual(voc_solution_sop.compile_supported(cards, [path], {42: candidate}, []), [])

    def test_wrong_result_or_unquoted_later_contradiction_cannot_verify(self):
        for feedback, later in ((SUCCESS.replace("重新测试退款场景", "重新测试发货场景"), ""), (SUCCESS, FAILURE)):
            model, proof, member = fixture(feedback=feedback, later=later)
            self.assertEqual(voc_solution_sop.compile_supported([issue(member)], [model], {42: proof}, []), [])

    def test_new_unreferenced_request_and_conflicting_account_scope_are_excluded(self):
        model, proof, member = fixture()
        other = {"id": "other", "role": "user", "kind": "text", "ts": 2500, "text": "另一家店怎么绑定？"}
        proof["messages"].insert(2, other)
        proof["original"]["other"] = {"source_message_id": "source:42:other", "role": "user", "ts": 2500}
        self.assertEqual(voc_solution_sop.compile_supported([issue(member)], [model], {42: proof}, []), [])
        model, proof, member = fixture(action="请在主账号调整退款场景的触发条件并保存。", with_retest=True, feedback="")
        self.assertEqual(voc_solution_sop.compile_supported([issue(member)], [model], {42: proof}, []), [])

    def test_exact_compatible_failure_blocks_the_same_action_without_publishing(self):
        success, first, member1 = fixture(42)
        failure, second, member2 = fixture(43, feedback=FAILURE)
        card = issue(member1, member2)
        first_sop, failed = voc_solution_sop.compile_supported([card], [success, failure], {42: first, 43: second}, [])
        self.assertEqual(failed["status"], "incomplete")
        self.assertEqual(failed["steps"][-1]["feedback"]["quote"], FAILURE)
        [playbook] = voc_playbooks.build([card], [first_sop, failed])["items"]
        self.assertEqual(playbook["branches"], [])
        self.assertIn("conflicting_case", playbook["missing"])
