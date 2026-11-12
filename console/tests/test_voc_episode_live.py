from contextlib import closing
from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import patch

from src import voc_assets, voc_engine, voc_episode, voc_episode_catalog, voc_episode_live as live, voc_outcome
from src.model_protocol import ModelProtocolError
from src.voc_assets import digest
from tests import test_voc_episode_replay as official


class TestEpisodeLive(unittest.TestCase):
    seed = official.TestEpisodeReplay.seed

    def setUp(self):
        official.TestEpisodeReplay.setUp(self)
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, dry_run=False, base_url="https://synthetic.invalid",
                                                api_key="synthetic", model_main="synthetic"))
        self.seed([
            (1, "退款场景没有触发。", "merchant-a", {}),
            (0, "请开启退款场景并保存。联系电话13800138000。", "merchant-a", {}),
            (1, "我按您说的步骤做完后重新测试，已经触发了。", "merchant-a", {}),
        ])
        self.key = "s_" + digest(["raw-session", "15429"])
        self.calls = []

    def extract(self, _cfg, messages):
        self.calls.append(messages)
        question, answer, feedback = messages
        qref = {"message_id": question["id"], "quote": "退款场景没有触发"}
        aref = {"message_id": answer["id"], "quote": answer["text"]}
        fref = {"message_id": feedback["id"], "quote": feedback["text"]}
        payload = {"episodes": [{"kind": "anomaly", "title": "退款场景没有触发", "module": "退款场景",
                                 "platform": "", "goal": "", "symptom": "没有触发",
                                 "message_ids": [m["id"] for m in messages], "question": qref,
                                 "answers": [aref], "actions": [{"instruction": aref, "feedback": fref,
                                                                  "outcome": "success"}]}]}
        return {"episodes": voc_episode.validate(messages, payload), "usage_tokens": 91}

    def run_episode(self, extractor=None):
        def assign(cfg, episodes, *, publish, **kwargs):
            from src.voc_episode_catalog import CatalogPublishRejected

            assignments = {e["id"]: "t_known" for e in episodes}
            with voc_assets.connection(voc_assets.store_path(cfg), write=True) as conn, voc_assets.transaction(conn):
                if not publish(conn, assignments):
                    raise CatalogPublishRejected()
            return assignments

        with patch("src.voc_episode_catalog.assign", side_effect=assign):
            return live.run(self.cfg, max_calls=1, max_tokens=200_000, extract=extractor or self.extract)

    def test_opaque_gateway_input_current_refs_and_idempotency(self):
        self.assertTrue(live.pending(self.cfg))
        before = self.path.read_bytes()
        result = self.run_episode()
        self.assertEqual((result["calls"], result["usage_tokens"], result["segments_processed"], result["assigned"]), (1, 91, 1, 1))
        self.assertGreater(result["reserved_tokens"], voc_episode.MAX_OUTPUT_TOKENS)
        self.assertEqual(self.path.read_bytes(), before)
        for private in ("13800138000", "merchant-a", "source:15429"):
            self.assertNotIn(private, repr(self.calls))
        [segment] = live.load_current(self.cfg, {self.key})
        [episode] = segment["episodes"]
        self.assertEqual(segment["session_id"], "15429")
        self.assertTrue(any("source:15429:1" in ids for ids in segment["qa_anchors"].values()))
        self.assertEqual(episode["question"], {"message_id": "source:15429:1", "quote": "退款场景没有触发", "start": 0, "end": 8})
        self.assertEqual(episode["actions"][0]["feedback"]["message_id"], "source:15429:3")
        self.assertEqual(episode["topic_id"], "t_known")
        self.assertEqual(live.status(self.cfg)["assigned"], 1)
        self.assertEqual(self.run_episode()["calls"], 0)
        self.assertFalse(live.pending(self.cfg))
        self.assertEqual(len(self.calls), 1)

    def test_gateway_failure_retries_after_backoff(self):
        def fail(*args):
            raise ModelProtocolError("sensitive upstream message", 504)

        with patch("src.voc_episode_live.time.time", return_value=1000):
            result = self.run_episode(fail)
            self.assertEqual(result["errors"], {"gateway_504": 1})
            self.assertFalse(live.pending(self.cfg))
            self.assertEqual(self.run_episode()["calls"], 0)
            self.assertEqual(live.load_current(self.cfg, {self.key}), [])
        with patch("src.voc_episode_live.time.time", return_value=1061):
            self.assertTrue(live.pending(self.cfg))
            self.assertEqual(self.run_episode()["segments_processed"], 1)

    def test_source_update_during_inference_withholds_result(self):
        def update(cfg, messages):
            output = self.extract(cfg, messages)
            with closing(sqlite3.connect(self.path)) as conn, conn:
                conn.execute("UPDATE push_sync_state SET source_json=source_json || ' '")
            return output

        result = self.run_episode(update)
        self.assertEqual(result["errors"], {"source_changed": 1})
        self.assertEqual(live.load_current(self.cfg, {self.key}), [])
        self.assertTrue(live.pending(self.cfg))
        self.assertEqual(self.run_episode()["segments_processed"], 1)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE push_sync_state SET status='archived',source_version='',source_json=''")
        self.assertEqual(live.load_current(self.cfg, {self.key}), [])
        self.assertEqual(live.status(self.cfg)["assigned"], 0)

    def test_lease_prevents_repeat_inference_and_budgets_guard_before_call(self):
        guarded = live.run(self.cfg, max_calls=1, max_tokens=voc_episode.MAX_OUTPUT_TOKENS, extract=self.extract)
        self.assertEqual((guarded["calls"], guarded["reserved_tokens"]), (0, 0))

        def reenter(cfg, messages):
            self.assertEqual(self.run_episode()["calls"], 0)
            return self.extract(cfg, messages)

        self.assertEqual(self.run_episode(reenter)["segments_processed"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_topic_assignment_and_segment_publish_are_atomic(self):
        original_assign = voc_episode_catalog.assign

        def race(cfg, episodes, *, publish, **kwargs):
            def source_changed(conn, assignments):
                self.assertTrue(assignments)
                with closing(sqlite3.connect(self.path)) as source, source:
                    source.execute("UPDATE push_sync_state SET source_json=source_json || ' '")
                return publish(conn, assignments)

            return original_assign(cfg, episodes, publish=source_changed, **kwargs)

        with patch("src.voc_episode_catalog.assign", side_effect=race):
            result = live.run(self.cfg, max_calls=1, max_tokens=200_000, extract=self.extract)
        self.assertEqual(result["errors"], {"source_changed": 1})
        self.assertEqual(voc_episode_catalog.topics(self.cfg), [])
        self.assertEqual(live.load_current(self.cfg, {self.key}), [])

        result = live.run(self.cfg, max_calls=1, max_tokens=200_000, extract=self.extract)
        self.assertEqual(result["assigned"], 1)
        [topic] = voc_episode_catalog.topics(self.cfg)
        [segment] = live.load_current(self.cfg, {self.key})
        self.assertEqual(segment["episodes"][0]["topic_id"], topic["id"])


class TestMixedVisitorEvidence(unittest.TestCase):
    seed = official.TestEpisodeReplay.seed

    def setUp(self):
        official.TestEpisodeReplay.setUp(self)
        self.seed([
            (1, "退款场景为什么没有触发？", "merchant-a", {}),
            (0, "请开启退款场景并保存。", "merchant-a", {}),
            (1, "京东店铺如何授权？", "merchant-b", {"userId": 102}),
            (0, "请调整退款场景的优先级。", "merchant-a", {}),
            (1, "重新测试后退款场景已经触发了。", "merchant-a", {}),
            (1, "商品知识应该如何同步？", "merchant-a", {}),
            (1, "这个要怎么配置？", "", {"userId": 0}),
            (0, "请联系客服。", "", {"userId": 0, "autoReply": 1}),
        ])

    def test_recovers_known_customer_segments_without_any_ambiguous_staff_evidence(self):
        segments = live._gather(self.cfg)
        self.assertEqual([segment["customer_id"] for segment in segments],
                         ["qiyu-external:merchant-a", "qiyu-external:merchant-b", "qiyu-external:merchant-a"])
        self.assertEqual(len({segment["segment_key"] for segment in segments}), 3)
        self.assertEqual([{row["source_message_id"] for row in segment["original"].values()} for segment in segments],
                         [{"source:15429:1"}, {"source:15429:3"}, {"source:15429:5", "source:15429:6"}])
        self.assertTrue(all(message["role"] == "user" for segment in segments for message in segment["messages"]))
        with voc_assets.connection(self.path) as conn:
            snapshot = voc_outcome._snapshot(conn, "15429")
            self.assertTrue(all(row["_owner"] is None for row in snapshot["messages"] if row["role"] in ("staff", "bot")))
            self.assertTrue(any(row[0] for row in conn.execute("SELECT answer FROM qa_pair")))
        sources = voc_engine._sources(self.cfg, {})
        self.assertTrue(sources)
        self.assertTrue(all(not row["_staff_verified"] and not row["answer"] and not row["answer_message_ids"]
                            and all(message["role"] == "merchant" for message in row["_context"]) for row in sources.values()))

    def test_rejects_qa_with_a_different_owner_or_cross_segment_anchors(self):
        segments = live._gather(self.cfg)
        first, second = (next(iter(segment["qa_anchors"])) for segment in segments[:2])
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE qa_pair SET customer_id=? WHERE qa_id=?", ("qiyu-external:merchant-b", first))
            conn.execute("UPDATE qa_origin SET question_message_ids=? WHERE qa_id=?",
                         ('["source:15429:3", "source:15429:5"]', second))
        [remaining] = live._gather(self.cfg)
        self.assertEqual(remaining["segment_key"], segments[-1]["segment_key"])
