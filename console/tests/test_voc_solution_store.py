from contextlib import closing
from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import patch

from src import voc_engine, voc_solution_store as store
from src.model_protocol import ModelProtocolError
from tests import test_voc_episode_replay as official


class TestSolutionStore(unittest.TestCase):
    seed = official.TestEpisodeReplay.seed

    def setUp(self):
        official.TestEpisodeReplay.setUp(self)
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, dry_run=False, base_url="https://synthetic.invalid",
                                                api_key="synthetic", model_main="synthetic"))
        self.seed([
            (1, "拼多多店铺机器人回复稍等后为什么没有待办？", "merchant-a", {}),
            (0, "它参考的是聊天记录，这个情况我这边配置为知识加入待办。", "merchant-a", {}),
            (1, "顾客昵称：临时昵称。我需要让 AI 继续回复，同时生成待办。", "merchant-a", {}),
            (0, "我已经配置为通用知识了，手机号13800138000。", "merchant-a", {}),
        ])
        self.sources = voc_engine._sources(self.cfg, {})
        self.qa_id = next(qa_id for qa_id, row in self.sources.items() if "为什么没有待办" in row["question"])

    def synthesize(self, _cfg, messages, anchors, **kwargs):
        self.assertEqual(len(messages), 4)
        self.assertIn(messages[0]["id"], anchors)
        self.assertNotIn("merchant-a", repr(messages))
        self.assertNotIn("13800138000", repr(messages))
        self.assertNotIn("临时昵称", repr(messages))
        self.assertNotIn("source:15429", repr(messages))

        def evidence(turn):
            return {"message_id": turn["id"], "quote": turn["text"], "ts": turn["ts"]}

        return {"solution": {"title": "保留稍等回复并生成待办", "sections": [
            {"kind": "problem", "text": "回复稍等后没有待办", "evidence": [evidence(messages[0])]},
            {"kind": "action", "text": "客服称已将这个情况配置为通用知识", "evidence": [evidence(messages[1]), evidence(messages[3])]},
        ]}, "usage_tokens": 124}

    def test_processing_uses_later_followup_and_current_cache_without_gateway_on_read(self):
        self.assertTrue(store.pending(self.cfg))
        with patch("src.voc_solution.run", side_effect=self.synthesize) as model:
            result = store.run(self.cfg, {}, max_calls=2, max_tokens=100_000, qa_ids={self.qa_id})
            self.assertEqual((result["generated"], result["calls"], result["usage_tokens"]), (1, 2, 124))
            self.assertEqual(store.run(self.cfg, {}, max_calls=2, max_tokens=100_000,
                                       qa_ids={self.qa_id})["calls"], 0)
            [case] = voc_engine.playbooks(self.cfg, {})["case_paths"]
            self.assertEqual(case["solution"]["sections"][1]["evidence"][-1]["message_id"], "source:15429:4")
            self.assertEqual(case["outcome"], "not_recorded")
            model.assert_called_once()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE push_sync_state SET source_json=source_json || ' '")
        self.assertEqual(store.enrich(self.cfg, self.sources, []), [])

    def test_gateway_timeout_keeps_literal_path_and_waits_before_retry(self):
        with patch("src.voc_solution.run", side_effect=ModelProtocolError("safe", 504)):
            result = store.run(self.cfg, {}, max_calls=2, max_tokens=100_000, qa_ids={self.qa_id})
        self.assertEqual((result["generated"], result["errors"]), (0, {"gateway_504": 1}))
        fallback = [{"qa_id": self.qa_id, "steps": ["literal"]}]
        self.assertEqual(store.enrich(self.cfg, self.sources, fallback), fallback)
        self.assertEqual(store.run(self.cfg, {}, max_calls=2, max_tokens=100_000,
                                   qa_ids={self.qa_id})["calls"], 0)

    def test_retry_reuses_the_current_draft_and_reserves_only_the_review_call(self):
        saved_draft = {"title": "暂存初稿", "sections": []}

        def interrupted(_cfg, _messages, _anchors, *, draft, checkpoint):
            self.assertIsNone(draft)
            checkpoint(saved_draft)
            raise ModelProtocolError("safe", 504)

        with patch("src.voc_solution.run", side_effect=interrupted), patch("src.voc_solution_store.time.time", return_value=1000):
            store.run(self.cfg, {}, max_calls=2, max_tokens=100_000, qa_ids={self.qa_id})

        def reviewed(cfg, messages, anchors, *, draft, checkpoint):
            self.assertEqual(draft, saved_draft)
            return self.synthesize(cfg, messages, anchors)

        with patch("src.voc_solution.run", side_effect=reviewed), patch("src.voc_solution_store.time.time", return_value=1601):
            result = store.run(self.cfg, {}, max_calls=1, max_tokens=100_000, qa_ids={self.qa_id})
        self.assertEqual((result["generated"], result["calls"]), (1, 1))

    def test_partial_review_can_enrich_a_literal_case_but_never_supplies_a_sop_candidate(self):
        def source(number, quote, stamp):
            return {"qa_id": self.qa_id, "message_id": f"source:15429:{number}", "quote": quote, "ts": stamp}

        [candidate] = store._candidates(self.cfg, {self.qa_id: self.sources[self.qa_id]})
        question, reply = candidate["messages"][:2]
        literal = {"id": "literal", "qa_id": self.qa_id, "problem": source(1, question["text"], question["ts"]),
                   "steps": [{"action": "配置为知识加入待办", "evidence": source(2, reply["text"], reply["ts"])}]}
        diagnostic = {"kind": "diagnosis", "text": "客服指出机器人参考了聊天记录。",
                      "evidence": [{"message_id": reply["id"], "quote": reply["text"], "ts": reply["ts"]}]}
        with (patch("src.voc_case_paths.build", return_value=[literal]),
              patch("src.voc_solution.run", return_value={"solution": None, "partial": {"sections": [diagnostic]}, "usage_tokens": 88})):
            store.run(self.cfg, {}, max_calls=2, max_tokens=100_000, qa_ids={self.qa_id})
        proof = {}
        [case] = store.enrich(self.cfg, self.sources, [literal], proof_by_qa=proof)
        sections = case["solution"]["sections"]
        self.assertEqual([s["kind"] for s in sections], ["problem", "diagnosis", "action"])
        self.assertEqual(sections[-1]["text"], "客服原话「配置为知识加入待办」")
        self.assertEqual(sections[1]["evidence"][0]["message_id"], "source:15429:2")
        self.assertEqual(proof, {})
        self.assertEqual(store.enrich(self.cfg, self.sources, []), [])
