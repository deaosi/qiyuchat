import unittest

from src.web.guest_legacy_transcript import match_pair


QUESTION = "怎么设置自动回复？"
ANSWER = "在场景设置中启用自动回复。"
STAMP = 1794106186000


def message(seq, role, content, ts=STAMP):
    return {"context_seq": seq, "role": role, "content": content, "ts": ts, "source_message_id": None}


class TestGuestLegacyTranscript(unittest.TestCase):
    pair = {"session_id": "123", "asked_at": STAMP, "question": QUESTION, "answer": ANSWER}

    def test_replays_only_the_exact_question_and_answer_rows(self):
        rows = [
            message(0, "user", "另外一个问题怎么处理？", STAMP - 2000),
            message(1, "staff", "另一个问题的回复。", STAMP - 1000),
            message(2, "user", QUESTION),
            message(4, "bot", "系统提示。", STAMP + 1000),
            message(6, "user", "店铺后台在哪里设置？", STAMP + 2000),
            message(8, "staff", ANSWER, STAMP + 3000),
            message(9, "user", "下一次咨询是什么？", STAMP + 4000),
        ]
        pair = {**self.pair, "question": f"{QUESTION}\n店铺后台在哪里设置？"}
        selected, anchor = match_pair(pair, rows)
        self.assertEqual(selected, [rows[2], rows[4], rows[5]])
        self.assertEqual(anchor, 2)
        self.assertTrue(all(row["source_message_id"] is None for row in rows))

    def test_refuses_ambiguous_or_changed_evidence(self):
        rows = [message(0, "user", QUESTION), message(1, "staff", ANSWER),
                message(2, "user", QUESTION), message(3, "staff", ANSWER)]
        self.assertIsNone(match_pair(self.pair, rows))
        self.assertIsNone(match_pair({**self.pair, "asked_at": STAMP + 1}, rows[:2]))
        self.assertIsNone(match_pair({**self.pair, "answer": "来源答复已变化。"}, rows[:2]))
        self.assertIsNone(match_pair(self.pair, [message(0, "source_identity", ""), *rows[2:]]))

    def test_replays_a_question_without_a_reply(self):
        rows = [message(4, "user", QUESTION), message(5, "bot", "等待接入人工。")]
        self.assertEqual(match_pair({**self.pair, "answer": ""}, rows), ([rows[0]], 4))
