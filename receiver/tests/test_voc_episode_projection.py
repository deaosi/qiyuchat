"""Integration of persisted episodes into scoped topic and case reads."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime
import sqlite3
import unittest
from unittest.mock import patch

from src import voc_assets as assets, voc_engine as engine, voc_episode, voc_episode_live as live
from src import voc_episode_projection as projection, voc_topic_insights as insights, voc_variants as variants
from tests import test_voc_episode_replay as official


class TestEpisodeProjection(unittest.TestCase):
    seed = official.TestEpisodeReplay.seed

    def setUp(self):
        official.TestEpisodeReplay.setUp(self)
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, dry_run=False, base_url="https://synthetic.invalid",
                                                api_key="synthetic", model_main="synthetic"))
        self.old = "t_" + "a" * 24
        self.separate = "t_" + "b" * 24

    def legacy(self, *, same_qa=False):
        turns = [
            (1, "退货场景没有触发。", "merchant-a", {}),
            (0, "先查看退货场景条件。", "merchant-a", {}),
            (1, "我看了条件，退货场景还是没有触发。", "merchant-a", {}),
            (0, "请核对商品后台设置。", "merchant-a", {}),
            (1, "怎么导出商品资料？", "merchant-a", {}),
            (0, "进入商品资料并选择导出。", "merchant-a", {}),
        ]
        if same_qa:
            turns[1], turns[2] = turns[2], turns[1]
        midnight = int(datetime.fromisoformat("2026-11-10T00:00:00+08:00").timestamp() * 1000)
        self.seed([(direction, text, customer, {"time": midnight + (index - 2) * 1000})
                   for index, (direction, text, customer, _) in enumerate(turns)])
        sources = engine._sources(self.cfg, {})
        self.assertEqual(len(sources), 2 if same_qa else 3)
        store = engine.path(self.cfg)
        assets.initialize(store)
        with assets.connection(store, write=True) as conn:
            for topic, question, primary in ((self.old, "退货机器人未触发", "functional_anomaly"),
                                             (self.separate, "导出商品资料", "configuration_guidance")):
                conn.execute("""INSERT INTO v2_topic(id,question,theme,boundary,domain,scope_json,primary_intent,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (topic, question, "使用支持", "", "zhihuige", "[]", primary, 1, 1))
            for qa_id, source in sources.items():
                is_anomaly = qa_id < (2 if same_qa else 3)
                topic, primary = ((self.old, "functional_anomaly") if is_anomaly
                                  else (self.separate, "configuration_guidance"))
                conn.execute("INSERT INTO v2_source VALUES(?,?,?,?,?,?)", (qa_id, source["_fingerprint"], variants.pipeline(self.cfg),
                             assets.dump({"asked_at": source["asked_at"]}), "done", 1))
                for index, part in enumerate(source["_question_parts"]):
                    start = part["start"]
                    quote = source["question"][start:part["end"]].rstrip("。？")
                    end = start + len(quote)
                    data = {"question_span": {"quote": quote, "start": start, "end": end, "alignment": "exact"},
                            "claims": [], "scope": [], "answer_kind": "contextual", "domain": {
                                "primary_intent": primary, "diagnosis": {"status": "reported" if is_anomaly else "not_applicable"}}}
                    event_id = f"i-{qa_id}-followup" if index else f"i-{qa_id}"
                    conn.execute("INSERT INTO v2_intent(id,qa_id,fingerprint,topic_id,quote,start_pos,end_pos,payload,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (event_id, qa_id, source["_fingerprint"], topic, quote, start, end, assets.dump(data), 1))

    def extract(self, cfg, messages):
        question, *context = messages[:4]
        item = {"kind": "anomaly", "title": "退货场景没有触发", "module": "退货场景", "platform": "",
                "goal": "", "symptom": "没有触发", "message_ids": [m["id"] for m in messages[:4]],
                "question": {"message_id": question["id"], "quote": question["text"]},
                "answers": [{"message_id": m["id"], "quote": m["text"]} for m in context if m["role"] == "staff"], "actions": []}
        return {"episodes": voc_episode.validate(messages, {"episodes": [item]}), "usage_tokens": 50}

    def test_new_and_existing_topics_share_rankings_without_breaking_old_evidence(self):
        self.legacy()
        original = engine._events(self.cfg, {})
        result = live.run(self.cfg, max_calls=1, max_tokens=200_000, extract=self.extract)
        self.assertEqual(result["assigned"], 1)
        with patch("src.voc_episode.extract", side_effect=AssertionError("read path cannot infer")):
            ranking = engine.topics(self.cfg, {}, workbench=True)
            self.assertEqual(ranking["comparison"]["episode"], {"consultations": 1, "sessions": 1})
            items = {item["id"]: item for item in ranking["items"]}
            self.assertEqual(len(items), 2)
            self.assertIn(self.separate, items)
            new = next(topic for topic in items if topic != self.separate)
            self.assertNotEqual(new, self.old)
            self.assertEqual(items[new]["event_count"], 1)
            detail = engine.topic_detail(self.cfg, {"focus_topic": new}, new, workbench=True)
            self.assertEqual(detail["total"], 1)
            [evidence] = detail["events"]
            self.assertEqual(evidence["workbench"]["question_message_id"], "source:15429:1")
            self.assertIn("请核对商品后台设置。", evidence["answer"])
            self.assertEqual(evidence["claims"][-1]["source_refs"][0]["message_id"], "source:15429:4")
            self.assertEqual(engine.event_detail(self.cfg, {"focus_topic": new}, evidence["id"])["event"]["topic_id"], new)
            self.assertEqual(engine.topic_detail(self.cfg, {}, self.old, workbench=True)["total"], 2)
            self.assertEqual(engine.event_detail(self.cfg, {"focus_topic": new}, "i-2")["event"]["topic_id"], self.old)
            gaps = insights.gaps(engine._topic_view(self.cfg, {"focus_topic": new}),
                                 [{"reuse_level": "history", "evidence": [{"event_id": "i-1"}, {"event_id": "i-2"}]}], [])
            self.assertIn({"code": "closure_unconfirmed", "count": 1}, gaps)
            self.assertEqual(engine._events(self.cfg, {}), original)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE push_sync_state SET status='archived',source_version='',source_json=''")
        self.assertEqual({item["id"] for item in engine.topics(self.cfg, {})["items"]}, {self.old, self.separate})

    def test_first_question_owns_the_consultation_and_evidence_is_scope_independent(self):
        self.legacy()
        self.assertEqual(live.run(self.cfg, max_calls=1, max_tokens=200_000, extract=self.extract)["assigned"], 1)
        first_day = {"start": "2026-11-09", "end": "2026-11-09"}
        followup_day = {"start": "2026-11-10", "end": "2026-11-10"}
        [topic] = engine.topics(self.cfg, first_day, workbench=True)["items"]
        self.assertNotEqual(topic["id"], self.old)
        [full] = engine.topic_detail(self.cfg, {}, topic["id"], workbench=True)["events"]
        [scoped] = engine.topic_detail(self.cfg, first_day, topic["id"], workbench=True)["events"]
        self.assertEqual(scoped, full)
        self.assertIn("请核对商品后台设置。", scoped["answer"])
        self.assertEqual([item["id"] for item in engine.topics(self.cfg, followup_day, workbench=True)["items"]], [self.separate])
        self.assertEqual(engine.topics(self.cfg, {"q": "还是没有触发"})["items"], [])
        self.assertEqual(engine.topic_detail(self.cfg, {"q": "还是没有触发"}, self.old)["events"][0]["id"], "i-2")

    def test_followup_in_same_qa_can_fold_without_hiding_other_intents(self):
        self.legacy(same_qa=True)
        self.assertEqual(live.run(self.cfg, max_calls=1, max_tokens=200_000, extract=self.extract)["assigned"], 1)
        sources = engine._sources(self.cfg, {})
        legacy = engine._events(self.cfg, {})
        followup = next(row for row in legacy if row["id"] == "i-1-followup")
        separate = {**followup, "id": "i-1-other", "topic_id": self.separate, "data": {
            **followup["data"], "domain": {"primary_intent": "configuration_guidance"}}}
        rows = projection.project(self.cfg, sources, [*legacy, separate], {}, set(engine._topic_definitions(self.cfg)))
        [episode] = [row for row in rows if row.get("episode_id")]
        self.assertEqual(episode["legacy_event_ids"], ("i-1", "i-1-followup"))
        self.assertEqual({row["id"] for row in rows}, {episode["id"], "i-1-other", "i-2"})

    def test_overlapping_other_legacy_intent_cannot_be_hidden_by_episode(self):
        self.legacy()
        sources = engine._sources(self.cfg, {})
        legacy = engine._events(self.cfg, {})
        original = next(row for row in legacy if row["qa_id"] == 1)
        conflicting = {**original, "id": "i-conflict", "data": {**original["data"], "domain": {
            "primary_intent": "configuration_guidance"}}}
        result = live.run(self.cfg, max_calls=1, max_tokens=200_000, extract=self.extract)
        self.assertEqual(result["assigned"], 1)
        rows = projection.project(self.cfg, sources, [*legacy, conflicting], {}, set(engine._topic_definitions(self.cfg)))
        self.assertEqual({row["id"] for row in rows}, {"i-1", "i-2", "i-3", "i-conflict"})
