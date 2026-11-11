from __future__ import annotations

import unittest

from src.voc_episode_evidence import assess


def message(mid, role, text, ts, **fields):
    return {"id": mid, "role": role, "text": text, "ts": ts, **fields}


def ref(message):
    return {"message_id": message["id"], "quote": message["text"]}


def action(instruction, feedback, outcome="success"):
    return {"instruction": ref(instruction), "feedback": ref(feedback), "outcome": outcome}


class TestEpisodeEvidence(unittest.TestCase):
    def setUp(self):
        self.question = message("q", "user", "抖店退款场景开了，但机器人没有触发。", 1000)
        self.instruction = message("a", "staff", "请修改退款场景的触发条件并保存。", 3000)
        self.success = message("f", "user", "改完这条规则后重新测试，已经触发了。", 5000)

    def evaluate(self, *tail, actions=None, members=None):
        transcript = [self.question, *tail]
        episode = {"id": "incident-1", "kind": "anomaly", "question": ref(self.question),
                   "message_ids": members if members is not None else [turn["id"] for turn in transcript],
                   "actions": actions if actions is not None else [action(self.instruction, self.success)]}
        return assess(episode, transcript)

    def test_applied_step_with_explicit_effect_binds_exact_staff_and_visitor_quotes(self):
        result = self.evaluate(self.instruction, self.success)
        self.assertEqual(result["status"], "case_verified")
        self.assertEqual(result["verified_actions"], [{"instruction": ref(self.instruction), "feedback": ref(self.success)}])

    def test_later_new_action_can_succeed_after_earlier_failure(self):
        first = message("first", "staff", "请重启机器人。", 2000)
        failure = message("failed", "user", "重启后还是不行。", 2500)
        result = self.evaluate(first, failure, self.instruction, self.success,
                               actions=[action(first, failure, "failure"), action(self.instruction, self.success)])
        self.assertEqual(result["status"], "case_verified")
        self.assertEqual([record["instruction"]["message_id"] for record in result["verified_actions"]], ["a"])

    def test_rule_removal_is_matched_across_staff_and_customer_language(self):
        step = message("a", "staff", "请移除冲突规则并保存。", 3000)
        feedback = message("f", "user", "我删除规则后重新测试，已经触发了。", 5000)
        result = self.evaluate(step, feedback, actions=[action(step, feedback)])
        self.assertEqual(result["status"], "case_verified")
        for text in ("我没有删除规则，重新测试已经触发了。", "我准备删除规则，已经触发了。"):
            unverified = {**feedback, "text": text}
            self.assertEqual(self.evaluate(step, unverified, actions=[action(step, unverified)])["status"],
                             "guidance_supported")

    def test_courtesy_and_understanding_never_count_as_applied_effect(self):
        for text in ("收到，谢谢。", "明白了。", "改好了。", "可以了。", "已经触发了。",
                     "希望改完这条规则后可以触发。", "改完这条规则就能触发了。", "没有修改，已经触发了。",
                     "我重启了电脑，重新测试已经触发了。", "保存好了，现在触发规则。"):
            with self.subTest(text=text):
                feedback = message("f", "user", text, 5000)
                result = self.evaluate(self.instruction, feedback, actions=[action(self.instruction, feedback)])
                self.assertEqual(result["status"], "guidance_supported")
                self.assertEqual(result["verified_actions"], [])

    def test_staff_intervention_blocks_old_action_but_unrelated_interleaved_turn_does_not(self):
        new_step = message("a2", "staff", "请重启机器人并重新登录。", 4000)
        result = self.evaluate(self.instruction, new_step, self.success)
        self.assertEqual(result["status"], "guidance_supported")
        self.assertIn("intervening_staff_action", result["reason_codes"])
        result = self.evaluate(self.instruction, new_step, self.success, members=["q", "a", "f"])
        self.assertEqual(result["status"], "case_verified")

    def test_later_same_episode_failure_withholds_prior_effect(self):
        failure = message("later", "user", "再测还是没有触发。", 6000)
        result = self.evaluate(self.instruction, self.success, failure)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verified_actions"], [])
        self.assertIn("later_failure_in_same_episode", result["reason_codes"])
        self.assertEqual(self.evaluate(self.instruction, self.success, failure, members=["q", "a", "f"])["status"],
                         "case_verified")

    def test_source_role_text_and_timestamps_are_required(self):
        for turn in ({**self.success, "role": "staff"}, {**self.success, "kind": "image"},
                     {**self.success, "ts": self.instruction["ts"]}, {**self.success, "automated": True}):
            with self.subTest(turn=turn):
                self.assertEqual(self.evaluate(self.instruction, turn)["status"], "guidance_supported")
        self.assertEqual(self.evaluate({**self.instruction, "role": "bot"}, self.success)["status"], "unverified")

    def test_quote_cannot_drop_condition_or_feedback_negation(self):
        partial = message("a", "staff", "只有企业版支持。请修改退款场景的触发条件并保存。", 3000)
        proposed = action(self.instruction, self.success)
        result = self.evaluate(partial, self.success, actions=[proposed])
        self.assertEqual(result["status"], "unverified")
        tentative = message("f", "user", "如果改完这条规则后重新测试，已经触发了。", 5000)
        result = self.evaluate(self.instruction, tentative, actions=[proposed])
        self.assertEqual(result["status"], "guidance_supported")
        self.assertIn("feedback_context_does_not_confirm_effect", result["reason_codes"])

    def test_collecting_screenshot_is_not_an_action(self):
        step = message("a", "staff", "请进入场景配置页面截图发给我。", 3000)
        result = self.evaluate(step, self.success, actions=[action(step, self.success)])
        self.assertEqual(result["status"], "unverified")

    def test_duplicate_feedback_or_boundary_cannot_certify_effect(self):
        step = message("second", "staff", "请保存修改后的配置。", 4000)
        result = self.evaluate(self.instruction, step, self.success,
                               actions=[action(self.instruction, self.success), action(step, self.success)])
        self.assertEqual(result["status"], "guidance_supported")
        self.assertIn("feedback_shared_between_actions", result["reason_codes"])
        boundary = message("boundary", "boundary", "", 4000)
        result = self.evaluate(self.instruction, boundary, self.success, members=["q", "a", "f"])
        self.assertEqual(result["status"], "unverified")
        self.assertIn("source_boundary_crossed", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
