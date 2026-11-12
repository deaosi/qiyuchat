"""Check case examples against real scoped VOC evidence shapes."""

import unittest
from unittest.mock import Mock

from src.voc_assistant_examples import build_examples


def _span(quote: str, start: int = 0) -> dict:
    return {"quote": quote, "start": start, "end": start + len(quote), "alignment": "exact"}


def _event(identity: str, qa: int, question: str = "我的场景为什么没有触发？",
           reply: str = "先在子账号权限设置中开启商品权限。") -> dict:
    return {
        "id": identity, "event_id": identity, "qa_id": qa, "source_fingerprint": f"fixture-{qa}",
        "question": question, "question_quote": question, "question_span": _span(question),
        "answer": reply, "asked_at": 1794448800000,
        "source_kind": "source-anchored", "staff_source_verified": True,
        "question_message_ids": [f"user-{qa}"], "answer_message_ids": [f"staff-{qa}"],
        "claims": [{**_span(reply), "role": "staff",
                    "source_refs": [{"message_id": f"staff-{qa}", "start": 0, "end": len(reply)}]}],
    }


def _ref(kind: str, identity: str) -> dict:
    return {"kind": kind, "id": identity, "title": "引用"}


def _knowledge(cases: list[dict], quote: str, qa: int) -> dict:
    message = f"feedback-{qa}"
    proof = {"quote": quote, "role": "merchant", "kind": "resolved", "qa_id": qa,
             "message_id": message, "ts": 1794448820000, "span_coordinate": "clean_visible_text",
             "source_refs": [{"message_id": message, "start": 0, "end": len(quote)}]}
    return {"id": "knowledge-1", "status": "ready", "steps": ["先在子账号权限设置中开启商品权限。"],
            "evidence": cases, "knowledge_reuse": {"level": "verified", "evidence": [proof]}}


class TestAssistantExamples(unittest.TestCase):
    def setUp(self):
        self.engine = Mock()
        self.cfg = object()
        self.scoped = {"group": "测试群", "start": "2026-11-12", "end": "2026-11-12", "support_intent": "functional_anomaly"}

    def test_direct_cases_are_exact_redacted_and_deduplicated(self):
        original = _event("event-1", 1, "测试商家张说场景没触发，电话 13812345678。")
        original["customer_name"] = "测试商家张"
        duplicate = _event("event-2", 2)
        other = _event("event-3", 3, reply="请对照平台商品规格，检查场景的商品范围。")
        forged = _event("event-4", 4, reply="无效引文。")
        forged["claims"][0]["quote"] = "伪造文字。"
        wrong_author = {**_event("event-5", 5, reply="不可用的来源。"), "staff_source_verified": False}
        references = [(_ref("event", row["id"]), row) for row in (forged, wrong_author, original, duplicate, other)]
        output = build_examples(self.engine, self.cfg, self.scoped, references)
        self.assertEqual([item["citation"]["id"] for item in output], ["event-1", "event-3"])
        self.assertNotIn("测试商家张", output[0]["question"])
        self.assertNotIn("13812345678", output[0]["question"])
        self.assertEqual(output[0]["reply"], original["answer"])
        self.assertEqual(output[0]["time"], "2026-11-12 10:00:00")
        self.assertNotIn("outcome", output[0])
        self.engine.event_detail.assert_not_called()

    def test_whole_anchored_pair_allowed_but_other_intent_and_unbound_answer_are_not(self):
        whole = {**_event("event-1", 1), "claims": []}
        partial = _event("event-2", 2, "场景未触发？此外，怎么开子账号商品权限？")
        partial.update(question_quote="场景未触发？", question_span=_span("场景未触发？"), claims=[])
        forged = _event("event-3", 3)
        forged["claims"][0]["source_refs"][0]["message_id"] = "not-this-staff-message"
        references = [(_ref("event", row["id"]), row) for row in (partial, forged, whole)]
        output = build_examples(self.engine, self.cfg, self.scoped, references)
        self.assertEqual([item["citation"]["id"] for item in output], ["event-1"])

    def test_topic_expansion_is_narrow_and_nested_case_is_revalidated(self):
        old, fresh = _event("event-1", 1), _event("event-2", 2)
        detail = {"topic": {"id": "topic-1"}, "events": [old, fresh]}
        self.engine.topic_detail.return_value = detail
        self.engine.event_detail.side_effect = [
            {"event": {**old, "source_fingerprint": "changed"}}, {"event": fresh},
        ]
        output = build_examples(self.engine, self.cfg, self.scoped, [(_ref("topic", "topic-1"), detail["topic"])])
        self.assertEqual([item["citation"]["id"] for item in output], ["event-2"])
        self.engine.topic_detail.assert_called_once_with(self.cfg, self.scoped, "topic-1")
        self.assertTrue(all(call.args[1] == self.scoped for call in self.engine.event_detail.call_args_list))
        self.assertEqual([call.args[2] for call in self.engine.event_detail.call_args_list], ["event-1", "event-2"])
        output = build_examples(self.engine, self.cfg, {**self.scoped, "focus_topic": "another-topic"},
                                [(_ref("topic", "topic-1"), detail["topic"])])
        self.assertEqual(output, [])

    def test_only_single_case_explicit_success_can_be_displayed_as_outcome(self):
        original = _event("event-1", 1)
        same_pair_other_intent = _event("event-2", 1, "子账号商品配置在哪里？")
        definite = "按照您说的开启了商品权限，复测场景已经正常触发了。"
        knowledge_ref = _ref("knowledge", "knowledge-1")
        event_ref = _ref("event", "event-1")
        for quote, cases, proof_qa, expected in (
            (definite, [original], 1, definite),
            ("好的，谢谢。", [original], 1, None),
            ("可以了。", [original], 1, None),
            (definite, [original], 2, None),
            (definite, [original, same_pair_other_intent], 1, None),
        ):
            with self.subTest(quote=quote, cases=len(cases), proof_qa=proof_qa):
                output, = build_examples(self.engine, self.cfg, self.scoped,
                                          [(event_ref, original), (knowledge_ref, _knowledge(cases, quote, proof_qa))], limit=1)
                self.assertEqual(output.get("outcome"), expected)
