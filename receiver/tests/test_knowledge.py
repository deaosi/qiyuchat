"""Knowledge versions and evidence snapshots; temp databases and mocked gateway."""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import replace
from unittest.mock import patch

from src import knowledge, voc
from src.voc_model import VocModelError
from tests.test_local import LocalFixture


class TestKnowledge(LocalFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, dry_run=False,
                           api_key="test-not-a-real-key", base_url="https://not-called.invalid/v1",
                           model_main="test-voc", batch_size=10))
        self.voc_job()
        self.topic_id = voc.topics(self.cfg, {})["items"][0]["id"]

    def sql(self, sql, args=()):
        conn = sqlite3.connect(self.cfg.runtime.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in conn.execute(sql, args)]
            conn.commit()
            return rows
        finally:
            conn.close()

    def voc_job(self):
        job = voc.start_job(self.cfg, {}, {}, launch=False)["job"]
        def analyze(cfg, pairs, topics):
            topic_id = topics[0]["id"] if topics else None
            return {"results": [{"qa_id": p["qa_id"], "excluded": False, "reason": "有效咨询", "intents": [{"topic_id": topic_id, "question": "如何了解商品业务？", "theme": "业务咨询", "boundary": "仅业务咨询", "reason": "有效咨询", "confidence": 0.8}]} for p in pairs], "usage_tokens": 10}
        with patch("src.voc.analyze_batch", side_effect=analyze):
            voc._worker(self.cfg, job["id"])

    @staticmethod
    def proposal(cfg, topic, pairs):
        answered = next((p for p in pairs if p["answer"].strip()), None)
        quotes = [{"qa_id": answered["qa_id"], "quote": answered["answer"]}] if answered else []
        return {"knowledge": {"question": topic["question"], "answer": answered["answer"] if answered else "", "scope": "仅限来源体现的业务条件", "cautions": ["需结合订单实际情况核对"], "reply_patterns": [{"label": "日常答复", "text": answered["answer"], "qa_ids": [answered["qa_id"]]}] if answered else [], "conflicts": [], "missing_info": [] if answered else ["来源没有有效客服答复"], "source_quotes": quotes}, "usage_tokens": 25}

    def run_job(self, side_effect=None, filters=None):
        job = knowledge.start_job(self.cfg, filters or {}, {"topic_id": self.topic_id}, launch=False)["job"]
        if job["status"] != "completed":
            with patch("src.knowledge.analyze_knowledge", side_effect=side_effect or self.proposal):
                knowledge._worker(self.cfg, job["id"])
            job = knowledge.jobs(self.cfg)["items"][0]
        return job

    def save_body(self, record, **changes):
        return {**{key: record[key] for key in ("id", "question", "answer", "scope", "cautions", "status", "revision")}, **changes}

    def test_model_generation_is_draft_with_real_sources_and_qa_unchanged(self):
        before = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        job = self.run_job()
        self.assertEqual((job["status"], job["processed"], job["usage_tokens"]), ("completed", 5, 25))
        detail = knowledge.detail(self.cfg, job["knowledge_id"])
        record = detail["knowledge"]
        self.assertEqual(record["status"], "draft")
        self.assertEqual(set(record["evidence_ids"]), {p["qa_id"] for p in detail["evidence"]})
        for quote in record["source_quotes"]:
            source = next(p for p in detail["evidence"] if p["qa_id"] == quote["qa_id"])
            self.assertIn(quote["quote"], source["answer"])
        self.assertEqual(self.sql("SELECT * FROM qa_pair ORDER BY qa_id"), before)

    def test_same_input_does_not_overwrite_manual_confirmation(self):
        job = self.run_job()
        record = knowledge.detail(self.cfg, job["knowledge_id"])["knowledge"]
        saved = knowledge.save(self.cfg, self.save_body(record, answer="人工复核后的明确答复", status="confirmed"))["knowledge"]
        with patch("src.knowledge.analyze_knowledge", side_effect=AssertionError("must be cached")):
            repeated = self.run_job()
        self.assertEqual((repeated["id"], repeated["knowledge_id"]), (job["id"], saved["id"]))
        self.assertEqual(knowledge.detail(self.cfg, saved["id"])["knowledge"]["answer"], "人工复核后的明确答复")
        self.assertEqual(self.sql("SELECT COUNT(*) n FROM knowledge_revision")[0]["n"], 1)

    def test_new_input_creates_new_draft_and_keeps_confirmed_record_snapshot(self):
        job = self.run_job()
        old_detail = knowledge.detail(self.cfg, job["knowledge_id"])
        old = knowledge.save(self.cfg, self.save_body(old_detail["knowledge"], status="confirmed"))["knowledge"]
        source_id = old["source_quotes"][0]["qa_id"]
        self.sql("UPDATE qa_pair SET answer='更新后的实际客服回复' WHERE qa_id=?", (source_id,))
        self.voc_job()
        new_job = self.run_job()
        self.assertNotEqual(new_job["knowledge_id"], old["id"])
        self.assertEqual(knowledge.detail(self.cfg, new_job["knowledge_id"])["knowledge"]["status"], "draft")
        self.assertEqual(knowledge.detail(self.cfg, old["id"])["evidence"], old_detail["evidence"])
        self.assertEqual(knowledge.detail(self.cfg, old["id"])["knowledge"]["status"], "confirmed")

    def test_no_staff_answer_stays_draft_and_cannot_be_confirmed(self):
        job = self.run_job(filters={"q": "滤芯多久"})
        record = knowledge.detail(self.cfg, job["knowledge_id"])["knowledge"]
        self.assertEqual((record["answer"], record["status"], record["reply_patterns"]), ("", "draft", []))
        with self.assertRaises(voc.VocError):
            knowledge.save(self.cfg, self.save_body(record, answer="没有来源的新政策", status="confirmed"))
        self.assertEqual(knowledge.save(self.cfg, self.save_body(record, status="draft"))["knowledge"]["status"], "draft")

    def test_empty_answer_and_stale_revision_cannot_be_confirmed(self):
        job = self.run_job()
        record = knowledge.detail(self.cfg, job["knowledge_id"])["knowledge"]
        with self.assertRaises(voc.VocError):
            knowledge.save(self.cfg, self.save_body(record, answer="", status="confirmed"))
        with self.assertRaises(voc.VocError) as error:
            knowledge.save(self.cfg, self.save_body(record, revision=0))
        self.assertEqual(error.exception.status, 409)

    def test_failed_call_retries_and_records_consumed_tokens(self):
        def fail(*args):
            raise VocModelError("secret-response", usage_tokens=19)
        failed = self.run_job(fail)
        self.assertEqual((failed["status"], failed["failed"], failed["usage_tokens"]), ("failed", 5, 19))
        self.assertNotIn("secret", failed["error"])
        self.assertEqual(knowledge.list_knowledge(self.cfg)["total"], 0)
        self.assertEqual(self.run_job()["status"], "completed")

    def test_network_holds_no_sqlite_transaction(self):
        def inspect(cfg, topic, pairs):
            conn = sqlite3.connect(self.cfg.runtime.db_path, timeout=0.2)
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            conn.close()
            return self.proposal(cfg, topic, pairs)
        self.assertEqual(self.run_job(inspect)["status"], "completed")

    def test_input_change_during_generation_does_not_store_stale_draft(self):
        def change(cfg, topic, pairs):
            self.sql("UPDATE qa_pair SET answer='并发修改客服答复' WHERE qa_id=?", (pairs[0]["qa_id"],))
            return self.proposal(cfg, topic, pairs)
        self.assertEqual(self.run_job(change)["status"], "failed")
        self.assertEqual(knowledge.list_knowledge(self.cfg)["total"], 0)

    def test_manual_exclusion_during_generation_invalidates_source_snapshot(self):
        def exclude(cfg, topic, pairs):
            current = voc.detail(self.cfg, {}, topic["id"])["topic"]
            voc.evidence_action(self.cfg, {}, {"topic_id": topic["id"], "revision": current["revision"], "qa_ids": [pairs[0]["qa_id"]], "action": "exclude"})
            return self.proposal(cfg, topic, pairs)
        failed = self.run_job(exclude)
        self.assertEqual((failed["status"], failed["usage_tokens"]), ("failed", 25))
        self.assertEqual(knowledge.list_knowledge(self.cfg)["total"], 0)

    def test_voc_and_knowledge_jobs_are_mutually_exclusive(self):
        knowledge.start_job(self.cfg, {}, {"topic_id": self.topic_id}, launch=False)
        with self.assertRaises(voc.VocError) as error:
            voc.start_job(self.cfg, {}, {}, launch=False)
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(voc.VocError):
            knowledge.start_job(self.cfg, {}, {"topic_id": self.topic_id}, launch=False)

    def test_dead_knowledge_job_recovers_without_touching_completed_versions(self):
        self.run_job()
        self.sql("UPDATE qa_pair SET answer='新来源' WHERE qa_id=(SELECT MIN(qa_id) FROM qa_pair)")
        self.voc_job()
        job = knowledge.start_job(self.cfg, {}, {"topic_id": self.topic_id}, launch=False)["job"]
        self.sql("UPDATE knowledge_job SET owner='999999999:dead' WHERE id=?", (job["id"],))
        knowledge._INITIALIZED.discard(voc._path(self.cfg))
        knowledge.startup(self.cfg)
        self.assertEqual(knowledge.jobs(self.cfg)["items"][0]["status"], "failed")
        self.assertEqual(knowledge.list_knowledge(self.cfg)["total"], 1)

    def test_export_defaults_to_confirmed_and_escapes_formulas(self):
        job = self.run_job()
        rows = list(csv.reader(io.StringIO(knowledge.export_csv(self.cfg).decode('utf-8-sig'))))
        self.assertEqual(len(rows), 1)
        record = knowledge.detail(self.cfg, job["knowledge_id"])["knowledge"]
        knowledge.save(self.cfg, self.save_body(record, question="=公式注入防护问题", status="confirmed"))
        rows = list(csv.reader(io.StringIO(knowledge.export_csv(self.cfg).decode('utf-8-sig'))))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1][2].startswith("'="))

    def test_demo_never_calls_model(self):
        cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, mock=True))
        with patch("src.knowledge.analyze_knowledge", side_effect=AssertionError("no network")):
            with self.assertRaises(voc.VocError):
                knowledge.start_job(cfg, {}, {"topic_id": self.topic_id})
