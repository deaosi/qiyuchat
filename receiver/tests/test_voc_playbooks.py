from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from src import voc_engine
from src.voc_playbooks import build
from tests import test_voc_sops as sop_fixture


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
            "cases": [], "impact": {"consultations": len(cases), "sessions": len(cases), "merchants": 0, "unidentified_sessions": len(cases)},
            "_members": [{**item["problem"], "qa_id": item["qa_id"], "anchored": True, "has_reply": True} for item in cases]}


class TestPlaybooks(unittest.TestCase):
    def test_knowledge_projection_only_includes_complete_verified_witnesses(self):
        verified, unconfirmed, other_scope = case(1), case(2, result=False), case(3, condition="仅限主账号", result=False)
        raw = build([issue(verified, unconfirmed, other_scope)], [verified, unconfirmed, other_scope])
        raw["case_paths"] = [{"id": "partial"}]
        scopes = {"days": 7}
        with patch("src.voc_engine._build_playbooks", return_value=raw) as playbooks:
            library = voc_engine.complete_sops(None, scopes)
        self.assertEqual([call.args[1] for call in playbooks.call_args_list], [scopes, {}])
        self.assertNotIn("case_paths", library)
        [entry] = library["items"]
        [branch] = entry["branches"]
        self.assertEqual((library["total"], entry["status"], entry["checked_cases"], branch["case_count"]), (1, "verified", 1, 1))
        self.assertEqual({ref["qa_id"] for refs in [branch["condition_evidence"], branch["retest_evidence"],
                                                    branch["result_evidence"], *(step["evidence"] for step in branch["steps"])]
                          for ref in refs}, {1})

        # A retest instruction from another conversation cannot complete this case.
        broken = deepcopy(raw)
        broken_branch = broken["items"][0]["branches"][0]
        broken_branch["retest_evidence"] = [ref for ref in broken_branch["retest_evidence"] if ref["qa_id"] == 2]
        with patch("src.voc_engine._build_playbooks", return_value=broken) as playbooks:
            self.assertEqual(voc_engine.complete_sops(None, scopes), {"items": [], "total": 0})
        playbooks.assert_called_once_with(None, scopes, include_case_paths=False)

    def test_guidance_still_rechecks_playbooks_but_skips_knowledge_counterevidence(self):
        guidance = case(1, result=False)
        raw = build([issue(guidance)], [guidance])
        scopes = {"days": 7}
        with patch("src.voc_engine._build_playbooks", return_value=raw) as playbooks:
            self.assertEqual(voc_engine.complete_sops(None, scopes), {"items": [], "total": 0})
        playbooks.assert_called_once_with(None, scopes, include_case_paths=False)

        with patch("src.voc_engine._build_playbooks", return_value=raw) as playbooks:
            ordinary = voc_engine.playbooks(None, scopes)
        self.assertEqual([call.args[1] for call in playbooks.call_args_list], [scopes, {}])
        self.assertEqual(ordinary["items"][0]["branches"][0]["status"], "guidance")

    def test_complete_local_witness_cannot_publish_against_global_counterevidence(self):
        success, failure = case(1), case(2, result=False)
        failure["status"] = "incomplete"
        failure["steps"][1].update(outcome="failed", feedback=ref("f2", "重新设置并保存后，退款场景还是没有触发。", 30))
        scoped = build([issue(success)], [success])
        global_result = build([issue(success, failure)], [success, failure])
        self.assertEqual(scoped["items"][0]["branches"][0]["status"], "verified")
        self.assertIn("conflicting_case", global_result["items"][0]["missing"])
        scopes = {"days": 7, "group": "测试群"}
        with patch("src.voc_engine._build_playbooks", side_effect=[scoped, global_result]) as playbooks:
            self.assertEqual(voc_engine.complete_sops(None, scopes), {"items": [], "total": 0})
        self.assertEqual([call.args[1] for call in playbooks.call_args_list], [scopes, {}])

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
        self.assertEqual(item["triage"], [])

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
        self.assertEqual([source["message_id"] for source in item["counterevidence"]], ["f2"])

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

    def test_exact_customer_record_recovers_performed_retest_without_staff_prompt(self):
        fixture = sop_fixture.TestVocSops()
        fixture.setUp()
        quote = "我调整退款场景的触发条件并保存了，用同一个买家问法重新测试退款场景，已经触发了。"
        feedback = sop_fixture.turn("s", "user", quote, 7)
        example = fixture.compile([fixture.check, fixture.fix, feedback],
                                  actions=[{"instruction": sop_fixture.ref(fixture.fix), "feedback": sop_fixture.ref(feedback), "outcome": "success"}])
        self.assertEqual(example["status"], "verified")
        self.assertEqual([step["phase"] for step in example["steps"]], ["diagnosis", "action"])
        [item] = build([issue(example)], [example])["items"]
        self.assertEqual(item["status"], "verified")
        [branch] = item["branches"]
        self.assertEqual(branch["retest"], f"客户实测原话：{quote}")
        proof = {**example["result"], "qa_id": example["qa_id"]}
        self.assertEqual(branch["retest_evidence"], [proof])
        self.assertEqual(branch["result_evidence"], [proof])
        self.assertEqual([entry["text"] for entry in branch["steps"]], [fixture.check["text"], fixture.fix["text"]])

    def test_observed_retest_withholds_generic_or_out_of_scope_feedback(self):
        base = case(1)
        base["steps"].pop()
        base["steps"][1]["evidence"]["quote"] = "请调整退款场景的触发条件并保存。"
        samples = (
            "调整退款场景的触发条件并保存了，现在好了。",
            "调整退款场景的触发条件并保存了，重新测试后已经触发了。",
            "调整退款场景的触发条件并保存了，重新测试退款场景，测试成功。",
            "调整退款场景的触发条件并保存了，准备重新测试退款场景，已经触发了。",
            "调整退款场景的触发条件并保存了，不用重新测试退款场景，已经触发了。",
            "调整退款场景的触发条件并保存了，重新测试退款场景，可能已经触发了。",
            "调整退款场景的触发条件并保存了，重新测试退款场景，页面已经显示了。",
            "调整退款场景的触发条件并保存了，重新测试发货场景，已经触发了。",
            "调整退款场景的退款金额并保存了，重新测试退款场景，已经触发了。",
            "调整退款场景的触发条件并保存了，换到主账号重新测试退款场景，已经触发了。",
            "调整退款场景的触发条件并保存了，在淘宝重新测试退款场景，已经触发了。",
            "重新测试退款场景，已经触发了，然后调整退款场景的触发条件并保存了。",
            "我按您说的步骤做完，重新测试退款场景，已经触发了。",
        )
        for quote in samples:
            with self.subTest(quote=quote):
                example = deepcopy(base)
                result = ref("s1", quote, 15)
                example["result"] = example["steps"][1]["feedback"] = result
                [item] = build([issue(example)], [example])["items"]
                self.assertEqual((item["status"], item["branches"]), ("investigation", []))
                self.assertIn("post_change_retest", item["missing"])

    def test_recovered_retest_cannot_override_a_later_same_scope_failure(self):
        success, failure = case(1), case(2, result=False)
        action = "请调整退款场景的触发条件并保存。"
        feedback = ref("s1", "调整退款场景的触发条件并保存了，重新测试退款场景，已经触发了。", 15)
        success["steps"].pop()
        success["steps"][1]["evidence"]["quote"] = action
        success["result"] = success["steps"][1]["feedback"] = feedback
        [recovered] = build([issue(success)], [success])["items"]
        self.assertEqual(recovered["status"], "verified")
        failure["status"] = "incomplete"
        failure["steps"][1]["evidence"]["quote"] = action
        failure["steps"][1].update(outcome="failed", feedback=ref("f2", "重新测试退款场景，仍没有触发。", 30))
        [item] = build([issue(success, failure)], [success, failure])["items"]
        self.assertEqual((item["status"], item["branches"]), ("investigation", []))
        self.assertIn("conflicting_case", item["missing"])

    def test_missing_sop_shows_the_real_consultation_and_missing_platform(self):
        evidence = {**ref("q7", "怎么授权啊", 10), "qa_id": 7, "anchored": True, "has_reply": False}
        card = {**issue(family="scenario_config"), "title": "授权：绑定与授权", "platform": "", "module": "授权", "condition": "",
                "cases": [evidence], "_members": [evidence], "impact": {"consultations": 1, "sessions": 1, "merchants": 0, "unidentified_sessions": 1},
                "next": {"owner": "", "action": "", "missing": ["使用的平台", "可核对的客服处理记录"]}}
        [item] = build([card], [])["items"]
        self.assertEqual((item["status"], item["checked_cases"], item["triage"], item["branches"]), ("investigation", 0, [], []))
        self.assertEqual(item["cases"], [evidence])
        self.assertEqual(item["impact"], card["impact"])
        self.assertEqual(set(item["missing"]), {"case_evidence", "使用的平台"})

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
