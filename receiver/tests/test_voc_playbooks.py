from __future__ import annotations

from copy import deepcopy
import unittest

from src.voc_playbooks import build


def ref(mid, quote, ts):
    return {"message_id": mid, "quote": quote, "ts": ts}


def case(qa_id, *, platform="抖店", condition="仅限子账号", result=True):
    question = ref(f"q{qa_id}", f"{platform}退款场景开关开了，但是机器人没有触发。", 10)
    scope = ref(f"q{qa_id}", platform, 10)
    precondition = ref(f"c{qa_id}", condition, 11)
    check = ref(f"d{qa_id}", "请核对退款场景是否使用子账号。", 12)
    action = ref(f"a{qa_id}", "请重新设置退款场景触发条件并保存。", 13)
    retest = ref(f"r{qa_id}", "请重新测试退款场景是否触发。", 14)
    feedback = ref(f"s{qa_id}", "重新设置并保存了，重新测试后已经触发了。", 15) if result else None
    steps = [
        {"phase": "diagnosis", "evidence": check, "outcome": "unconfirmed"},
        {"phase": "action", "evidence": action, "outcome": "verified" if result else "unconfirmed"},
        {"phase": "retest", "evidence": retest, "outcome": "unconfirmed"},
    ]
    if feedback:
        steps[1]["feedback"] = feedback
    return {"qa_id": qa_id, "kind": "anomaly", "status": "verified" if result else "guidance",
            "problem": question, "conditions": [scope, precondition], "steps": steps, "result": feedback,
            "missing": [] if result else ["customer_result"]}


def issue(*cases, family="no_reply"):
    return {"id": "issue_1", "family": family, "title": "退款场景不触发", "platform": "抖音", "module": "退款场景", "condition": "已开启",
            "_members": [{**item["problem"], "qa_id": item["qa_id"], "anchored": True, "has_reply": True} for item in cases]}


class TestPlaybooks(unittest.TestCase):
    def test_complete_evidence_keeps_chronology_and_combines_only_identical_scope(self):
        first, second, other_scope = case(1), case(2, result=False), case(3, condition="仅限主账号", result=False)
        items = build([issue(first, second, other_scope)], [first, second, other_scope])["items"]
        [item] = items
        self.assertEqual((item["checked_cases"], item["status"]), (3, "guidance"))
        main, other = item["branches"]
        self.assertEqual((main["status"], main["case_count"]), ("verified", 2))
        self.assertIn("仅限子账号", main["condition"])
        self.assertEqual([source["message_id"] for source in main["condition_evidence"]], ["q1", "c1", "q2", "c2"])
        self.assertEqual([source["qa_id"] for source in main["steps"][1]["evidence"]], [1, 2])
        self.assertEqual([source["message_id"] for source in main["retest_evidence"]], ["r1", "r2"])
        self.assertEqual([source["message_id"] for source in main["result_evidence"]], ["s1"])
        self.assertIn("仅限主账号", other["condition"])
        self.assertEqual((other["status"], other["case_count"]), ("guidance", 1))

    def test_preflight_and_cross_platform_do_not_publish(self):
        first = case(1, result=False)
        first["steps"].pop()
        second = case(2, platform="淘宝", result=False)
        [item] = build([issue(first, second)], [first, second])["items"]
        self.assertEqual((item["status"], item["branches"], item["checked_cases"]), ("investigation", [], 2))
        self.assertIn("post_change_retest", item["missing"])
        self.assertIn("applicability", item["missing"])
        self.assertEqual(len(item["triage"]), 3)

    def test_required_original_anchor_does_not_use_a_fuzzy_quote(self):
        example = case(1)
        not_exact = issue(example)
        not_exact["_members"][0]["quote"] += " "
        [item] = build([not_exact], [example])["items"]
        self.assertEqual((item["status"], item["checked_cases"], item["branches"]), ("investigation", 0, []))
        self.assertIn("case_evidence", item["missing"])

    def test_later_staff_reply_in_exact_episode_can_cover_first_question(self):
        example = case(1)
        card = issue(example)
        card["_members"][0]["has_reply"] = False
        [item] = build([card], [example])["items"]
        self.assertEqual((item["status"], item["checked_cases"]), ("verified", 1))

    def test_confirmed_failure_with_same_scope_withholds_path_and_keeps_proof(self):
        success, failure = case(1), case(2, result=False)
        failure["status"] = "incomplete"
        failure["steps"][1].update(outcome="failed", feedback=ref("f2", "重新设置并保存后，退款场景还是没有触发。", 30))
        [item] = build([issue(success, failure)], [success, failure])["items"]
        self.assertEqual((item["status"], item["branches"]), ("investigation", []))
        self.assertIn("conflicting_case", item["missing"])
        self.assertIn("f2", {source["message_id"] for source in item["evidence"]})

    def test_failure_on_other_account_scope_does_not_poison_established_path(self):
        success, failure = case(1), case(2, condition="仅限主账号", result=False)
        failure["status"] = "incomplete"
        failure["steps"][1].update(outcome="failed", feedback=ref("f2", "还是没有触发。", 30))
        [item] = build([issue(success, failure)], [success, failure])["items"]
        self.assertEqual(item["status"], "verified")
        self.assertNotIn("conflicting_case", item["missing"])

    def test_failure_still_withholds_branch_when_private_feedback_is_hidden(self):
        success, failure = case(1), case(2, result=False)
        failure["status"] = "incomplete"
        failure["steps"][1]["outcome"] = "failed"
        [item] = build([issue(success, failure)], [success, failure])["items"]
        self.assertEqual((item["status"], item["branches"]), ("investigation", []))
        self.assertIn("conflicting_case", item["missing"])

    def test_later_outcome_must_match_every_action_and_retest(self):
        fixture = case(1)
        bad = deepcopy(fixture)
        bad["result"]["ts"] = 14
        [item] = build([issue(bad)], [bad])["items"]
        self.assertEqual(item["branches"][0]["status"], "guidance")
        self.assertEqual(item["branches"][0]["result_evidence"], [])

    def test_investigation_follows_the_actual_problem(self):
        base = issue()
        not_triggered = {**base, "title": "退款场景：未触发"}
        intermittent = {**base, "title": "退款场景：没有回复", "_members": [{"quote": "有时候不回复。"}]}
        wrong_answer = {**base, "title": "退款场景：回复错误"}
        reply_configuration = {**base, "family": "scenario_config", "title": "退款场景：调整回复内容"}
        platform = {**base, "family": "platform_limit", "title": "退款场景：当前版本无法绑定"}
        items = build([not_triggered, intermittent, wrong_answer, reply_configuration, platform], [])["items"]
        self.assertTrue(all(item["status"] == "investigation" and not item["branches"] for item in items))
        self.assertIn("停在触发环节", items[0]["triage"][0]["when_no"])
        self.assertIn("各取一条成功与失败样本", items[1]["triage"][0]["check"])
        self.assertIn("预期答复和实际发出的答复", items[2]["triage"][0]["check"])
        self.assertIn("修改的回复内容", items[3]["triage"][0]["check"])
        self.assertIn("版本无法绑定", items[4]["triage"][0]["check"])

    def test_other_families_are_visible_but_inconsistent_case_kind_cannot_publish(self):
        example = case(1)
        new_config = issue(example, family="configuration")
        anomaly = issue(example, family="anomaly")
        misc = {**issue(family="other"), "platform": "", "module": "", "condition": ""}
        items = build([new_config, anomaly, misc], [example])["items"]
        self.assertEqual([item["status"] for item in items], ["investigation", "verified", "investigation"])
        self.assertEqual([item["family"] for item in items], ["configuration", "anomaly", "other"])


if __name__ == "__main__":
    unittest.main()
