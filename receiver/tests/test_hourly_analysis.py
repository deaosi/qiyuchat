"""Hourly scheduling uses only synthetic DBs and mocked model adapters."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from unittest.mock import patch

from src import hourly_analysis as H, knowledge, voc
from tests.test_local import LocalFixture


class TestHourlyAnalysis(LocalFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, dry_run=False,
                           api_key="synthetic-hourly-key", base_url="https://never-called.invalid/v1",
                           model_main="synthetic-hourly-model", batch_size=10))

    def sql(self, statement, args=()):
        conn = sqlite3.connect(self.cfg.runtime.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in conn.execute(statement, args)]
            conn.commit()
            return rows
        finally:
            conn.close()

    @staticmethod
    def classify(cfg, pairs, topics):
        active = next((t for t in topics if t["status"] != "ignored"), None)
        return {"results": [{"qa_id": p["qa_id"], "excluded": False, "reason": "合成咨询",
                 "intents": [{"topic_id": active["id"] if active else None, "question": "如何查询商品业务？",
                              "theme": "商品服务", "boundary": "仅来源业务", "confidence": .9,
                              "reason": "合成问题"}]} for p in pairs], "usage_tokens": 10}

    @staticmethod
    def proposal(cfg, topic, pairs):
        source = next(p for p in pairs if knowledge._staff_answer(p["answer"]))
        return {"knowledge": {"question": topic["question"], "answer": source["answer"], "scope": "来源范围",
                "cautions": [], "reply_patterns": [], "conflicts": [], "missing_info": [],
                "source_quotes": [{"qa_id": source["qa_id"], "quote": source["answer"]}]}, "usage_tokens": 20}

    def run_hour(self, **kwargs):
        with (patch("src.voc.analyze_batch", side_effect=self.classify) as classify,
              patch("src.knowledge.analyze_knowledge", side_effect=self.proposal) as generate):
            result = H.run(self.cfg, **kwargs)
        return result, classify.call_count, generate.call_count

    def test_check_is_read_only_without_schemas_or_database_and_zero_models(self):
        before = self.sql("SELECT name,sql FROM sqlite_master ORDER BY name")
        with (patch("src.voc.analyze_batch") as model, patch("src.knowledge.analyze_knowledge") as draft,
              patch("src.knowledge.startup", side_effect=AssertionError("no startup"))):
            result = H.check(self.cfg)
        self.assertEqual((result["pending_pairs"], result["knowledge_needed"], result["status"]), (5, 0, "ready"))
        model.assert_not_called()
        draft.assert_not_called()
        self.assertEqual(self.sql("SELECT name,sql FROM sqlite_master ORDER BY name"), before)
        empty_cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, db_path=str(self.temp.name) + "/not-created.db"))
        self.assertFalse(H.check(empty_cfg)["database_exists"])
        self.assertFalse(os.path.exists(empty_cfg.runtime.db_path))

    def test_full_round_finishes_persistent_jobs_inline_and_next_hour_is_noop(self):
        original = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        with patch("src.voc.threading.Thread", side_effect=AssertionError("no daemon worker")):
            result, model_calls, draft_calls = self.run_hour()
        self.assertEqual((result["status"], result["voc_processed"], result["knowledge_created"]), ("completed", 5, 1))
        self.assertEqual((model_calls, draft_calls, result["usage_tokens"]), (1, 1, 30))
        self.assertEqual(self.sql("SELECT status FROM voc_job")[0]["status"], "completed")
        self.assertEqual(self.sql("SELECT status FROM knowledge_job")[0]["status"], "completed")
        self.assertEqual(self.sql("SELECT status FROM knowledge_record")[0]["status"], "draft")
        counts = (len(voc.jobs(self.cfg)["items"]), len(knowledge.jobs(self.cfg)["items"]))
        repeated, model_calls, draft_calls = self.run_hour()
        self.assertEqual((repeated["reason"], model_calls, draft_calls), ("no_changes", 0, 0))
        self.assertEqual((len(voc.jobs(self.cfg)["items"]), len(knowledge.jobs(self.cfg)["items"])), counts)
        self.assertEqual(self.sql("SELECT * FROM qa_pair ORDER BY qa_id"), original)

    def test_live_console_owner_skips_before_startup_without_changing_job(self):
        job = voc.start_job(self.cfg, {}, {}, launch=False)["job"]
        # Parent process is alive and distinct from the CLI's process identity.
        self.sql("UPDATE voc_job SET owner=? WHERE id=?", (f"{os.getppid()}:external-console", job["id"]))
        before = self.sql("SELECT * FROM voc_job WHERE id=?", (job["id"],))
        with (patch("src.knowledge.startup", side_effect=AssertionError("must not recover live console job")),
              patch("src.voc.analyze_batch") as model, patch("src.knowledge.analyze_knowledge") as draft):
            result = H.run(self.cfg)
            check = H.check(self.cfg)
        self.assertEqual((result["status"], result["reason"], check["status"]), ("skipped", "model_busy", "busy"))
        self.assertEqual(self.sql("SELECT * FROM voc_job WHERE id=?", (job["id"],)), before)
        model.assert_not_called()
        draft.assert_not_called()

    def test_dead_cli_job_is_recovered_then_retried(self):
        job = voc.start_job(self.cfg, {}, {}, launch=False)["job"]
        self.sql("UPDATE voc_job SET owner='999999999:dead-cli' WHERE id=?", (job["id"],))
        voc._INITIALIZED.discard(voc._path(self.cfg))
        knowledge._INITIALIZED.discard(voc._path(self.cfg))
        result, _, _ = self.run_hour()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.sql("SELECT status FROM voc_job WHERE id=?", (job["id"],))[0]["status"], "failed")

    def test_ignored_and_confirmed_same_fingerprint_never_regenerate_or_overwrite(self):
        self.run_hour()
        record = knowledge.list_knowledge(self.cfg)["items"][0]
        for status in ("ignored", "confirmed", "draft"):
            self.sql("UPDATE knowledge_record SET status=?,answer='人工审核后的答复',revision=7 WHERE id=?", (status, record["id"]))
            before = self.sql("SELECT * FROM knowledge_record WHERE id=?", (record["id"],))[0]
            result, model_calls, draft_calls = self.run_hour()
            self.assertEqual((model_calls, draft_calls, result["knowledge_created"]), (0, 0, 0))
            self.assertEqual(self.sql("SELECT * FROM knowledge_record WHERE id=?", (record["id"],))[0], before)

    def test_new_evidence_creates_new_draft_and_preserves_confirmed_asset(self):
        self.run_hour()
        old = knowledge.list_knowledge(self.cfg)["items"][0]
        self.sql("UPDATE knowledge_record SET status='confirmed',answer='人工确认知识' WHERE id=?", (old["id"],))
        before = self.sql("SELECT * FROM knowledge_record WHERE id=?", (old["id"],))[0]
        self.sql("UPDATE qa_pair SET answer='变化后的实际客服回复' WHERE qa_id=(SELECT MIN(qa_id) FROM qa_pair)")
        result, model_calls, draft_calls = self.run_hour()
        self.assertEqual((result["voc_total"], model_calls, draft_calls, result["knowledge_created"]), (1, 1, 1, 1))
        self.assertEqual(self.sql("SELECT * FROM knowledge_record WHERE id=?", (old["id"],))[0], before)
        self.assertEqual(knowledge.list_knowledge(self.cfg)["items"][0]["status"], "draft")

    def test_zero_staff_answers_never_queue_knowledge(self):
        self.sql("UPDATE qa_pair SET answer=''")
        result, _, draft_calls = self.run_hour()
        self.assertEqual((result["knowledge_created"], draft_calls), (0, 0))
        self.assertEqual(knowledge.jobs(self.cfg)["items"], [])

    def test_limits_bound_incremental_voc_and_knowledge_jobs(self):
        result, _, _ = self.run_hour(voc_limit=1, knowledge_limit=0)
        self.assertEqual((result["voc_total"], result["voc_processed"], result["knowledge_created"]), (1, 1, 0))
        self.assertEqual(voc.summary(self.cfg, {})["pending_pairs"], 4)
        self.assertEqual(knowledge.jobs(self.cfg)["items"], [])

    def test_knowledge_limit_caps_actual_model_calls_and_leaves_the_rest_for_next_hour(self):
        def separate_topics(cfg, pairs, topics):
            result = self.classify(cfg, pairs, [])
            for item in result["results"]:
                item["intents"][0]["question"] = f"如何处理第{item['qa_id']}类合成业务？"
            return result
        with (patch("src.voc.analyze_batch", side_effect=separate_topics),
              patch("src.knowledge.analyze_knowledge", side_effect=self.proposal) as generate):
            first = H.run(self.cfg, knowledge_limit=1)
        self.assertEqual((first["knowledge_created"], generate.call_count), (1, 1))
        self.assertEqual(H.check(self.cfg)["knowledge_needed"], 1)
        repeated, model_calls, draft_calls = self.run_hour(knowledge_limit=1)
        self.assertEqual((repeated["knowledge_created"], model_calls, draft_calls), (1, 0, 1))

    def test_knowledge_selection_is_atomic_and_stale_ignored_topic_is_skipped(self):
        self.run_hour(knowledge_limit=0)
        topic = voc.topics(self.cfg, {})["items"][0]
        original = knowledge._selected
        def selected(conn, *args):
            self.assertTrue(conn.in_transaction)
            return original(conn, *args)
        with patch("src.knowledge._selected", side_effect=selected):
            queued = H._queue_knowledge(self.cfg, topic["id"])
        self.assertIsNotNone(queued)
        self.sql("UPDATE knowledge_job SET status='failed'")
        self.sql("UPDATE voc_topic SET status='ignored' WHERE id=?", (topic["id"],))
        self.assertIsNone(H._queue_knowledge(self.cfg, topic["id"]))
        self.assertEqual(len(knowledge.jobs(self.cfg)["items"]), 1)

    def test_model_failure_is_retryable_and_untrusted_exception_is_hidden(self):
        def fail(*args):
            raise RuntimeError("secret-api-key customer-original")
        with patch("src.voc.analyze_batch", side_effect=fail):
            result = H.run(self.cfg)
        self.assertEqual((result["status"], result["voc_failed"]), ("partial", 5))
        self.assertNotIn("secret-api-key", json.dumps(result))
        retried, _, _ = self.run_hour()
        self.assertEqual((retried["status"], retried["voc_processed"]), ("completed", 5))

    def test_manual_job_race_is_skipped_and_not_cancelled(self):
        knowledge.startup(self.cfg)
        original = voc.start_job
        other = []
        def manual_wins(cfg, filters, body, launch=True):
            other.append(original(cfg, filters, body, launch=False)["job"])
            return original(cfg, filters, body, launch=False)
        with patch("src.voc.start_job", side_effect=manual_wins):
            result = H.run(self.cfg)
        self.assertEqual((result["status"], result["reason"]), ("skipped", "model_busy"))
        self.assertEqual(voc.jobs(self.cfg)["items"][0]["status"], "queued")

    def test_interruption_marks_only_created_persistent_job_failed(self):
        with patch("src.voc._worker", side_effect=KeyboardInterrupt):
            result = H.run(self.cfg)
        self.assertEqual((result["status"], result["reason"]), ("failed", "interrupted"))
        self.assertEqual(voc.jobs(self.cfg)["items"][0]["status"], "failed")
        self.assertTrue(all(row["status"] == "failed" for row in self.sql("SELECT status FROM voc_job_item")))

    def test_cli_check_is_strictly_read_only_and_safe(self):
        before = self.sql("SELECT name,sql FROM sqlite_master ORDER BY name")
        with (patch("src.hourly_analysis.load_settings", return_value=self.cfg),
              patch("src.hourly_analysis.logging.disable"), patch("builtins.print") as output,
              patch("src.voc.analyze_batch") as model, patch("src.knowledge.analyze_knowledge") as draft):
            self.assertEqual(H.main(["--check"]), 0)
        report = json.loads(output.call_args.args[0])
        self.assertEqual((report["mode"], report["voc_limit"], report["knowledge_limit"]), ("check", 100, 10))
        self.assertNotIn(self.cfg.llm.api_key, output.call_args.args[0])
        model.assert_not_called()
        draft.assert_not_called()
        self.assertEqual(self.sql("SELECT name,sql FROM sqlite_master ORDER BY name"), before)

    def test_mock_or_unconfigured_never_calls_models_or_creates_jobs(self):
        for cfg in (replace(self.cfg, runtime=replace(self.cfg.runtime, mock=True)),
                    replace(self.cfg, llm=replace(self.cfg.llm, dry_run=True))):
            with patch("src.voc.analyze_batch") as model, patch("src.knowledge.analyze_knowledge") as draft:
                self.assertEqual(H.run(cfg)["reason"], "unconfigured")
            model.assert_not_called()
            draft.assert_not_called()
