from __future__ import annotations

import unittest

from src.voc_sops import compile_case


def turn(identity: str, role: str, text: str, seconds: int) -> dict:
    return {"id": identity, "role": role, "text": text, "ts": seconds * 1000, "kind": "text"}


def ref(message: dict, quote: str | None = None) -> dict:
    quote = message["text"] if quote is None else quote
    start = message["text"].index(quote)
    return {"message_id": message["id"], "quote": quote, "start": start, "end": start + len(quote)}


class TestVocSops(unittest.TestCase):
    def setUp(self):
        self.question = turn("q", "user", "抖店退款场景开关已开启，但机器人没有触发。", 1)
        self.check = turn("d", "staff", "请核对退款场景开关是否开启。", 4)
        self.fix = turn("a", "staff", "请调整退款场景的触发条件并保存。", 5)
        self.retest = turn("t", "staff", "请重新测试退款场景是否触发。", 6)
        self.success = turn("s", "user", "我调整触发条件并保存了，重新测试后已经触发了。", 7)

    def compile(self, messages, *, answers=None, actions=None, members=None):
        all_messages = [self.question, *messages]
        if answers is None:
            answers = [ref(m) for m in messages if m["role"] == "staff"]
        return compile_case({"id": "episode_1", "topic_id": "t_test", "kind": "anomaly", "title": "未经验证的推断",
                             "question": ref(self.question), "platform": "抖店", "module": "退款场景",
                             "message_ids": members if members is not None else [m["id"] for m in all_messages],
                             "answers": answers, "actions": actions or []}, all_messages, 22)

    def test_builds_ordered_solution_and_separates_an_earlier_failed_attempt(self):
        first = turn("x", "staff", "请重启机器人。", 2)
        failure = turn("f", "user", "重启后还是不行。", 3)
        sop = self.compile([first, failure, self.check, self.fix, self.retest, self.success],
                           actions=[{"instruction": ref(self.fix), "feedback": ref(self.success), "outcome": "unknown"}])
        self.assertEqual(sop["title"], self.question["text"])
        self.assertEqual((sop["qa_id"], sop["status"], sop["missing"]), (22, "verified", []))
        self.assertEqual([step["phase"] for step in sop["steps"]], ["action", "diagnosis", "action", "retest"])
        self.assertEqual([step["outcome"] for step in sop["steps"]], ["failed", "unconfirmed", "verified", "unconfirmed"])
        self.assertEqual(sop["steps"][0]["feedback"]["message_id"], "f")
        self.assertEqual(sop["result"]["quote"], self.success["text"])
        self.assertEqual(sop["result"]["message_id"], "s")

    def test_copyable_guidance_requires_a_check_criterion_not_a_model_success_label(self):
        courtesy = turn("c", "user", "明白了，谢谢。", 7)
        proposal = {"instruction": ref(self.fix), "feedback": ref(courtesy), "outcome": "success"}
        guidance = self.compile([self.check, self.fix, self.retest, courtesy], actions=[proposal])
        self.assertEqual(guidance["status"], "guidance")
        self.assertEqual(guidance["missing"], ["customer_result"])
        self.assertIsNone(guidance["result"])
        before = turn("b", "staff", "请进入退款场景设置页面。", 4)
        incomplete = self.compile([before, self.fix, courtesy], actions=[proposal])
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertIn("retest", incomplete["missing"])

    def test_preflight_check_is_not_the_post_change_retest(self):
        for turns in ([self.check], [self.check, self.fix], [self.check, self.fix, turn("t", "staff", "请再试一下。", 6)]):
            result = self.compile(turns)
            self.assertEqual(result["status"], "incomplete")
            self.assertIn("retest", result["missing"])
        inline = {**self.fix, "text": "请调整退款场景的触发条件并保存，然后重新测试退款场景是否触发。"}
        result = self.compile([self.check, inline])
        self.assertEqual(result["status"], "guidance")
        self.assertNotIn("retest", result["missing"])

    def test_separate_staff_actions_need_explicit_evidence_for_each_operation(self):
        change = turn("x", "staff", "请开启退款场景开关。", 4)
        save = turn("y", "staff", "请保存退款场景配置。", 5)
        for message, level in (("我按您说的步骤做完后重新测试，已经触发了。", "guidance"),
                               ("我开启退款场景后保存了，重新测试已经触发了。", "verified")):
            with self.subTest(level=level):
                feedback = turn("s", "user", message, 7)
                sop = self.compile([change, save, self.retest, feedback],
                                   actions=[{"instruction": ref(save), "feedback": ref(feedback), "outcome": "success"}])
                self.assertEqual(sop["status"], level)

    def test_source_guards_hold_omitted_counterevidence_and_an_incomplete_condition(self):
        contradicted = turn("later", "user", "刚才复测，还是没有触发。", 8)
        result = self.compile([self.fix, self.retest, self.success, contradicted], members=["q", "a", "t", "s"],
                              actions=[{"instruction": ref(self.fix), "feedback": ref(self.success), "outcome": "success"}])
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("later_counterevidence", result["missing"])
        self.assertIsNone(result["result"])

        scoped = {**self.fix, "text": "只有企业版支持。" + self.fix["text"]}
        result = self.compile([scoped, self.retest], answers=[ref(scoped, self.fix["text"]), ref(self.retest)])
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("unbound_condition", result["missing"])

    def test_keeps_a_mixed_check_and_change_as_an_action_and_withholds_higher_risk_steps(self):
        combined = turn("x", "staff", "请检查退款场景设置，然后开启开关。", 4)
        result = self.compile([combined, self.retest])
        self.assertEqual(result["steps"][0]["phase"], "action")
        self.assertEqual(result["status"], "guidance")

        dangerous = turn("x", "staff", "请批量删除场景规则。", 4)
        result = self.compile([dangerous, self.retest])
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("unsafe_step", result["missing"])
        self.assertNotIn("批量删除", repr(result))

    def test_does_not_turn_intake_promises_or_unverified_roles_into_a_procedure(self):
        triage = turn("x", "staff", "请打开退款场景配置页面截图发给我。", 4)
        promised = turn("y", "staff", "我帮您排查退款场景设置。", 5)
        result = self.compile([triage, promised])
        self.assertEqual((result["status"], result["steps"]), ("incomplete", []))
        self.assertEqual([ref["message_id"] for ref in result["replies"]], ["x", "y"])
        self.assertEqual(self.compile([self.retest])["status"], "incomplete")
        fake = {**self.fix, "automated": True}
        self.assertIsNone(self.compile([fake]))
        self.assertIsNone(self.compile([self.fix], answers=[{**ref(self.fix), "start": None}]))


if __name__ == "__main__":
    unittest.main()
