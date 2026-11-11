"""Regression cases for source-bound multi-turn VOC episode extraction."""
from copy import deepcopy
import unittest

from src.config import Settings
from src import voc_episode as episode


def message(identity, role, timestamp, text, kind="text"):
    return {"id": identity, "role": role, "ts": timestamp, "text": text, "kind": kind}


def ref(identity, quote):
    return {"message_id": identity, "quote": quote}


def issue(origin, question, members, *, kind="anomaly", answers=(), actions=(), **fields):
    return {"kind": kind, "title": question, "platform": "", "module": "", "goal": "", "symptom": "",
            "message_ids": members, "question": ref(origin, question), "answers": list(answers),
            "actions": list(actions), **fields}


def action(staff_id, instruction, feedback=None, outcome="unknown"):
    return {"instruction": ref(staff_id, instruction), "feedback": feedback, "outcome": outcome}


class TestEpisode(unittest.TestCase):
    def setUp(self):
        self.messages = [
            message("m1", "user", 1000, "抖店退款场景已开启，但机器人没有回复。"),
            message("m2", "staff", 2000, "请发一下截图。"),
            message("m3", "user", 3000, "[图片]", "image"),
            message("m4", "staff", 4000, "优先调整规则顺序后再测试。"),
            message("m5", "user", 5000, "改完以后测试还是没有触发。"),
            message("m6", "user", 6000, "另外京东退款场景怎么开启？"),
            message("m7", "staff", 7000, "抖店这里要先关闭冲突规则，再保存。"),
            message("m8", "staff", 8000, "京东店铺里进入售后设置，打开退款场景。"),
            message("m9", "user", 9000, "抖店关闭规则后复测，机器人回复了。"),
        ]
        self.first = issue("m1", self.messages[0]["text"], ["m1", "m2", "m3", "m4", "m5", "m7", "m9"],
                           platform="抖店", module="退款场景", symptom="机器人没有回复",
                           answers=[ref("m2", self.messages[1]["text"]), ref("m4", self.messages[3]["text"]),
                                    ref("m7", self.messages[6]["text"])],
                           actions=[
                               action("m4", self.messages[3]["text"], ref("m5", self.messages[4]["text"]), "failure"),
                               action("m7", self.messages[6]["text"], ref("m9", self.messages[8]["text"]), "success"),
                           ])
        self.second = issue("m6", self.messages[5]["text"], ["m6", "m8"], kind="configuration",
                            platform="京东", module="退款场景", answers=[ref("m8", self.messages[7]["text"])])
        self.payload = {"episodes": [self.second, self.first]}

    def test_interleaved_topics_keep_actions_retries_and_images_with_their_issue(self):
        calls = []

        def complete(cfg, system, data, *, max_tokens):
            calls.append(data)
            self.assertIn("完整会话", system)
            self.assertEqual(max_tokens, 4096)
            return {"data": self.payload, "usage_tokens": 120}

        result = episode.extract(Settings(), self.messages, complete=complete)
        first, second = result["episodes"]
        self.assertEqual(result["usage_tokens"], 120)
        self.assertEqual(calls, [{"messages": self.messages}])
        self.assertEqual(first["message_ids"], ["m1", "m2", "m3", "m4", "m5", "m7", "m9"])
        self.assertEqual([item["outcome"] for item in first["actions"]], ["failure", "success"])
        self.assertEqual(second["message_ids"], ["m6", "m8"])
        self.assertEqual(first["id"], episode.validate(self.messages, {"episodes": [self.first]})[0]["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_single_message_can_supply_distinct_question_and_answer_spans(self):
        messages = [
            message("q", "user", 1000, "抖店规则怎么开启？京东规则为什么不触发？"),
            message("a", "staff", 2000, "抖店可在规则管理启用。京东请发当前规则截图。"),
        ]
        first = issue("q", "抖店规则怎么开启？", ["q", "a"], kind="configuration", platform="抖店",
                      answers=[ref("a", "抖店可在规则管理启用。")])
        second = issue("q", "京东规则为什么不触发？", ["q", "a"], platform="京东",
                       answers=[ref("a", "京东请发当前规则截图。")])
        valid = episode.validate(messages, {"episodes": [second, first]})
        self.assertEqual([item["platform"] for item in valid], ["抖店", "京东"])
        self.assertNotEqual(valid[0]["id"], valid[1]["id"])
        second["answers"] = first["answers"]
        with self.assertRaisesRegex(episode.EpisodeError, "互不重叠"):
            episode.validate(messages, {"episodes": [second, first]})

    def test_foreign_fabricated_and_non_text_evidence_is_rejected(self):
        for field, replacement in (
            ("question", ref("m1", "我编造的提问")),
            ("question", ref("m3", "[图片]")),
            ("question", ref("m8", self.messages[7]["text"])),
            ("answers", [ref("m1", self.messages[0]["text"])]),
            ("answers", [ref("m3", "[图片]")]),
            ("module", "后台商品资料"),
        ):
            with self.subTest(field=field, replacement=replacement):
                sample = deepcopy(self.first)
                sample[field] = replacement
                with self.assertRaises(episode.EpisodeError):
                    episode.validate(self.messages, {"episodes": [sample]})

    def test_late_success_cannot_verify_an_earlier_attempt(self):
        first = deepcopy(self.first)
        first["actions"] = [action("m4", self.messages[3]["text"], ref("m9", self.messages[8]["text"]), "success")]
        result = episode.validate(self.messages, {"episodes": [first]})[0]
        self.assertEqual(result["actions"][0]["outcome"], "unknown")

    def test_omitted_instruction_condition_withholds_the_result(self):
        first = deepcopy(self.first)
        first["actions"][-1]["instruction"] = ref("m7", "关闭冲突规则，再保存")
        result = episode.validate(self.messages, {"episodes": [first]})[0]
        self.assertEqual(result["actions"][-1]["outcome"], "unknown")

    def test_bot_is_context_only_and_required_fields_are_enforced(self):
        messages = self.messages + [message("bot", "bot", 10000, "商品导入")]
        first = deepcopy(self.first)
        first["message_ids"].append("bot")
        self.assertEqual(len(episode.validate(messages, {"episodes": [first]})), 1)
        first["module"] = "商品导入"
        with self.assertRaisesRegex(episode.EpisodeError, "业务字段"):
            episode.validate(messages, {"episodes": [first]})
        del first["module"]
        with self.assertRaisesRegex(episode.EpisodeError, "结构字段"):
            episode.validate(messages, {"episodes": [first]})

    def test_courtesy_and_negated_or_prospective_partial_quotes_are_unknown(self):
        for text, quote in (("谢谢。", "谢谢"), ("明白了。", "明白了"), ("还是没有成功。", "成功"),
                            ("应该成功了。", "成功了"), ("成功了？", "成功了")):
            with self.subTest(text=text):
                messages = deepcopy(self.messages)
                messages[-1]["text"] = text
                first = deepcopy(self.first)
                first["actions"][-1]["feedback"] = ref("m9", quote)
                result = episode.validate(messages, {"episodes": [first]})[0]
                self.assertEqual(result["actions"][-1]["outcome"], "unknown")

    def test_feedback_order_and_chronology(self):
        first = deepcopy(self.first)
        first["actions"][-1]["feedback"] = ref("m5", self.messages[4]["text"])
        with self.assertRaisesRegex(episode.EpisodeError, "在操作之后"):
            episode.validate(self.messages, {"episodes": [first]})
        tied = deepcopy(self.messages)
        tied[-3]["ts"] = tied[-2]["ts"] = tied[-1]["ts"]
        valid = episode.validate(tied, self.payload)[0]
        self.assertEqual(valid["actions"][-1]["outcome"], "unknown")

    def test_same_question_cannot_be_counted_twice_or_cross_boundary(self):
        with self.assertRaisesRegex(episode.EpisodeError, "互不重叠"):
            episode.validate(self.messages, {"episodes": [self.first, deepcopy(self.first)]})
        messages = self.messages[:4] + [message("divider", "boundary", 4500, "")] + self.messages[4:]
        with self.assertRaisesRegex(episode.EpisodeError, "边界"):
            episode.validate(messages, {"episodes": [self.first]})

    def test_oversize_is_rejected_before_inference_and_empty_source_uses_no_model(self):
        def unexpected(*_args, **_kwargs):
            self.fail("provider must not be called")

        messages = deepcopy(self.messages)
        messages[0]["text"] = "配置" * 24_000
        with self.assertRaises(episode.EpisodeError) as error:
            episode.extract(Settings(), messages, complete=unexpected)
        self.assertEqual(error.exception.code, "source_too_long")
        self.assertEqual(episode.extract(Settings(), [self.messages[2]], complete=unexpected),
                         {"episodes": [], "usage_tokens": 0})


if __name__ == "__main__":
    unittest.main()
