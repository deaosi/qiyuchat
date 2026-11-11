"""Coverage-aware issue views using isolated business and VOC stores."""
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import voc_assets as assets, voc_engine as engine, voc_topic_insights as insights, voc_variants as variants
from src.config import Settings


class TestTrendScope(unittest.TestCase):
    def test_equal_shanghai_periods_and_half_open_midnight(self):
        current, previous = insights.periods({"start": "2026-11-08", "end": "2026-11-14"})
        self.assertEqual(previous.start.isoformat(), "2026-11-01")
        self.assertEqual(previous.end.isoformat(), "2026-11-07")
        start, stop = current.bounds()
        self.assertEqual(start, int(datetime.fromisoformat("2026-11-07T16:00:00+00:00").timestamp() * 1000))
        self.assertEqual(current.rows({1: {"asked_at": start - 1}, 2: {"asked_at": start},
                                       3: {"asked_at": stop - 1}, 4: {"asked_at": stop}, 5: {"asked_at": 0}}).keys(), {2, 3})
        self.assertIsNone(insights.periods({"start": "2026-11-08"})[1])

    def test_partial_coverage_uses_processed_rate_and_does_not_relabel_recurring_topic(self):
        current = {"source_pairs": 12, "processed_pairs": 8}
        previous = {"source_pairs": 8, "processed_pairs": 5}
        scope = insights.comparison(current, previous, "")
        self.assertTrue(scope["comparable"])
        self.assertIn("已整理样本，非全量", scope["note"])
        self.assertEqual(insights.trend(1, 0, False, scope)["status"], "new")
        self.assertEqual(insights.trend(1, 0, True, scope)["status"], "stable")
        self.assertEqual(insights.trend(3, 1, True, scope)["status"], "rising")
        self.assertEqual(insights.trend(5, 3, True, scope)["status"], "rising")
        self.assertEqual(insights.trend(6, 4, True, scope)["status"], "persistent")

    def test_imbalanced_and_missing_coverage_only_show_observed_counts(self):
        current = {"source_pairs": 16, "processed_pairs": 8}
        previous = {"source_pairs": 8, "processed_pairs": 8}
        scope = insights.comparison(current, previous, "")
        self.assertFalse(scope["comparable"])
        self.assertEqual(insights.trend(4, 2, True, scope), {"status": "unknown", "previous_consultation_count": 2, "delta": 2})
        scope = insights.comparison(current, {"source_pairs": 0, "processed_pairs": 0}, "")
        self.assertEqual(insights.trend(1, 0, False, scope), {"status": "unknown", "previous_consultation_count": None, "delta": None})


class TestWorkbenchProjection(unittest.TestCase):
    def test_only_canonical_validation_can_allow_direct_use(self):
        certificate = {"auto_reusable": True, "status": "accepted", "reason_codes": []}
        for acceptance, effect, level, expected in (
            ("resolved", "customer_reported_success", "verified", "direct"),
            ("unknown", "not_established", "guidance", "direct"),
            ("resolved", "customer_reported_success", None, "history"),
            ("understood", "not_established", "reference", "reference"),
            ("weak_closure", "not_established", "reference", "reference"),
            ("continuing", "not_established", "reference", "blocked"),
        ):
            with self.subTest(acceptance=acceptance, effect=effect, level=level):
                self.assertEqual(insights.reuse_level({"knowledge_reuse": {"level": level}, "conversation_verification": {
                    **certificate, "acceptance": acceptance, "effect": effect}}), expected)
        self.assertEqual(insights.reuse_level({"kind": "source_conflict"}), "blocked")

    def test_case_checks_require_anchored_verified_positive_evidence(self):
        question = "抖音商品后台已经设置，但机器人还是不触发。"
        checks = ["已核对商品后台设置无误。", "已核对知汇鸽场景条件无误。", "现场复测仍不触发。"]

        def ref(side, text, quote):
            start = text.index(quote)
            return {"source": side, "quote": quote, "start": start, "end": start + len(quote), "alignment": "exact"}

        answer = "\n".join(checks)
        row = {"source": {"question": question, "answer": answer, "_staff_verified": True,
                           "_question_parts": [{"message_id": "q", "start": 0, "end": len(question)}],
                           "_answer_parts": [{"message_id": "a", "start": 0, "end": len(answer)}]},
               "data": {"question_span": ref("question", question, question), "domain": {
                   "primary_intent": "functional_anomaly", "diagnosis": {"status": "pending"},
                   "actual": ref("question", question, "还是不触发"),
                   "reproduction_context": [ref("answer", answer, check) for check in checks]}}}
        result = insights.case_details(row)
        self.assertTrue(result["suspected_report_ready"])
        self.assertEqual(result["question_message_id"], "q")
        self.assertEqual([value["quote"] for value in result["commerce_settings"]], checks[:1])
        self.assertEqual([value["quote"] for value in result["scenario_conditions"]], checks[1:2])
        self.assertEqual([value["quote"] for value in result["actual"]], ["还是不触发", checks[2]])
        row["source"]["_answer_parts"] = []
        self.assertFalse(insights.case_details(row)["suspected_report_ready"])
        row["source"]["_question_parts"] = []
        self.assertNotIn("question_message_id", insights.case_details(row))


class TestWorkbenchReads(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = Settings()
        self.cfg = replace(cfg, runtime=replace(cfg.runtime, db_path=str(root / "q.db"), state_dir=str(root)))
        self.filters = {"start": "2026-11-08", "end": "2026-11-14"}
        self.rising = "t_" + "a" * 24
        self.new = "t_" + "b" * 24
        self.returning = "t_" + "c" * 24
        with assets.connection(Path(self.cfg.runtime.db_path), write=True) as conn:
            conn.execute("""CREATE TABLE qa_pair(qa_id INTEGER PRIMARY KEY,session_id TEXT,customer_id TEXT,
                customer_name TEXT,group_name TEXT,staff_name TEXT,asked_at INTEGER,question TEXT,answer TEXT,cluster_id INTEGER)""")
            events = [("11-02", self.rising), ("11-08", self.rising), ("11-09", self.rising),
                      ("11-10", self.rising), ("11-11", self.new), ("11-13", self.returning), ("10-20", self.returning)]
            rows = events + [("11-05", "")] * 4
            for qa_id, (day, _) in enumerate(rows, 1):
                asked_at = int(datetime.fromisoformat(f"2026-{day}T00:00:00+08:00").timestamp() * 1000)
                conn.execute("INSERT INTO qa_pair VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (qa_id, f"session-{qa_id}", "qiyu-user:8", "合成客户", "合成群", "", asked_at, "机器人没触发", "", 0))
        sources = engine._sources(self.cfg, {})
        store = engine.path(self.cfg)
        assets.initialize(store)
        with assets.connection(store, write=True) as conn:
            for topic in {topic for _, topic in events}:
                conn.execute("INSERT INTO v2_topic(id,question,theme,boundary,domain,scope_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                             (topic, f"合成问题 {topic[-1]}", "功能问题", "", variants.mode(self.cfg), "[]", 1, 1))
            for qa_id, (_, topic) in enumerate(rows, 1):
                source = sources[qa_id]
                conn.execute("INSERT INTO v2_source VALUES(?,?,?,?,?,?)", (qa_id, source["_fingerprint"], variants.pipeline(self.cfg),
                             assets.dump({"asked_at": source["asked_at"]}), "done" if topic else "excluded", 1))
                if topic:
                    data = {"question_span": {"start": 0, "end": len(source["question"]), "quote": source["question"]},
                            "claims": [], "scope": [], "answer_kind": "none"}
                    conn.execute("INSERT INTO v2_intent(id,qa_id,fingerprint,topic_id,quote,start_pos,end_pos,payload,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (f"i-{qa_id}", qa_id, source["_fingerprint"], topic, source["question"], 0, len(source["question"]), assets.dump(data), 1))

    def test_filtered_ranking_uses_equal_period_and_returns_only_matching_topics(self):
        result = engine.topics(self.cfg, self.filters, workbench=True)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["comparison"]["current"]["processed_pairs"], 5)
        self.assertEqual(result["comparison"]["previous"]["processed_pairs"], 5)
        by_topic = {item["id"]: item for item in result["items"]}
        self.assertEqual(by_topic[self.rising]["trend"]["status"], "rising")
        self.assertEqual(by_topic[self.new]["trend"]["status"], "new")
        self.assertEqual(by_topic[self.returning]["trend"]["status"], "stable")
        self.assertEqual(by_topic[self.rising]["merchant_count"], 0)  # ID prefix alone is insufficient.
        self.assertEqual(by_topic[self.rising]["unknown_merchant_sessions"], 3)
        filtered = engine.topics(self.cfg, self.filters, workbench=True, trend="new")
        self.assertEqual((filtered["total"], [item["id"] for item in filtered["items"]]), (1, [self.new]))
        self.assertNotIn("comparison", engine.topics(self.cfg, self.filters))

    def test_details_focus_evidence_and_leave_both_databases_unchanged(self):
        before = [Path(self.cfg.runtime.db_path).read_bytes(), engine.path(self.cfg).read_bytes()]
        filters = {**self.filters, "focus_topic": self.rising}
        with patch("src.voc_engine._knowledge_plans", return_value=[]), patch("src.voc_engine._latest_assets", return_value=[]):
            detail = engine.topic_detail(self.cfg, filters, self.rising, workbench=True)
        self.assertEqual(detail["total"], 3)
        self.assertEqual(detail["workbench"]["gaps"], [{"code": "no_staff_reply", "count": 3}])
        self.assertNotIn("question_message_id", detail["events"][0]["workbench"])
        self.assertEqual(engine.topics(self.cfg, filters)["total"], 1)
        for fetch, identity in ((engine.topic_detail, self.new), (engine.event_detail, "i-5")):
            with self.assertRaises(engine.EngineError) as error:
                fetch(self.cfg, filters, identity)
            self.assertEqual(error.exception.status, 404)
        self.assertEqual(before, [Path(self.cfg.runtime.db_path).read_bytes(), engine.path(self.cfg).read_bytes()])
