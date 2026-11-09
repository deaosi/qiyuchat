"""VOC persistence/worker boundaries; temporary DB and mocked models only."""
from __future__ import annotations

import json
import http.client
import sqlite3
import threading
from dataclasses import replace
from unittest.mock import patch

from src import voc
from src.voc_model import VocModelError
from src.web.server import DashboardServer
from tests.test_local import LocalFixture


class TestVoc(LocalFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, dry_run=False,
                           api_key="test-not-a-real-key", base_url="https://not-called.invalid/v1",
                           model_main="test-voc", batch_size=2))
        self.filters = {"days": 0}

    def sql(self, statement, args=()):
        conn = sqlite3.connect(self.cfg.runtime.db_path)
        try:
            conn.row_factory = sqlite3.Row
            result = [dict(row) for row in conn.execute(statement, args)]
            conn.commit()
            return result
        finally:
            conn.close()

    @staticmethod
    def result(cfg, pairs, topics):
        existing = next((t for t in topics if t["status"] != "ignored"), None)
        return {"results": [{"qa_id": p["qa_id"], "excluded": False, "reason": "有效咨询",
                 "intents": [{"topic_id": existing["id"] if existing else None,
                 "question": "如何查询商品信息？", "theme": "商品咨询", "boundary": "仅商品信息咨询",
                 "confidence": 0.9, "reason": "问题表达商品咨询意图"}]} for p in pairs], "usage_tokens": 11}

    def run_job(self, side_effect=None, filters=None):
        job = voc.start_job(self.cfg, filters or self.filters, {}, launch=False)["job"]
        if job["total"]:
            with patch("src.voc.analyze_batch", side_effect=side_effect or self.result):
                voc._worker(self.cfg, job["id"])
        return voc.jobs(self.cfg)["items"][0]

    def topic(self):
        return voc.topics(self.cfg, self.filters)["items"][0]

    def test_completed_idempotent_and_original_qa_unchanged(self):
        before = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        job = self.run_job()
        self.assertEqual((job["status"], job["total"], job["processed"], job["failed"]), ("completed", 5, 5, 0))
        self.assertEqual(job["usage_tokens"], 33)
        self.assertEqual(self.topic()["count"], 5)
        self.assertEqual(self.run_job()["total"], 0)
        self.assertEqual(self.sql("SELECT * FROM qa_pair ORDER BY qa_id"), before)
        self.assertEqual(voc.summary(self.cfg, self.filters)["pending_pairs"], 0)

    def test_distinct_customers_use_stable_anonymous_identity(self):
        self.run_job()
        self.assertEqual(self.topic()["customers"], 4)

    def test_failed_model_not_marked_processed_and_retry(self):
        job = self.run_job(lambda *args: (_ for _ in ()).throw(RuntimeError("secret-response customer-original api-key")))
        self.assertEqual((job["status"], job["failed"], job["processed"]), ("failed", 5, 0))
        self.assertNotIn("secret-response", job["error"])
        self.assertNotIn("api-key", str(self.sql("SELECT error FROM voc_job_item")))
        self.assertEqual(voc.summary(self.cfg, self.filters)["pending_pairs"], 5)
        self.assertEqual(self.run_job()["processed"], 5)

    def test_safe_json_and_truncation_errors_visible_and_bounded(self):
        messages = ["模型返回的不是严格 JSON 对象。", "模型输出未完整结束，请缩小批次或增加输出上限。", "安全校验说明" * 100]
        for message in messages:
            with self.subTest(message=message[:20]):
                def fail(*args):
                    raise VocModelError(message)
                job = self.run_job(fail)
                expected = message[:240]
                self.assertEqual(job["error"], expected)
                errors = self.sql("SELECT error FROM voc_job_item WHERE job_id=?", (job["id"],))
                self.assertTrue(errors)
                self.assertTrue(all(item["error"] == expected for item in errors))

    def test_partial_job_retries_only_failed_rows(self):
        calls = 0
        def flaky(cfg, pairs, topics):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise VocModelError("failure")
            return self.result(cfg, pairs, topics)
        job = self.run_job(flaky)
        self.assertEqual((job["status"], job["processed"], job["failed"]), ("partial", 3, 2))
        self.assertEqual(self.run_job()["total"], 2)

    def test_network_call_holds_no_sqlite_write_lock(self):
        def probe(cfg, pairs, topics):
            conn = sqlite3.connect(self.cfg.runtime.db_path, timeout=0.2)
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            conn.close()
            return self.result(cfg, pairs, topics)
        self.assertEqual(self.run_job(probe)["status"], "completed")

    def test_only_one_active_job_per_db(self):
        voc.start_job(self.cfg, self.filters, {}, launch=False)
        with self.assertRaises(voc.VocError) as error:
            voc.start_job(self.cfg, self.filters, {}, launch=False)
        self.assertEqual(error.exception.status, 409)

    def test_restart_recovers_dead_owner_once(self):
        job = voc.start_job(self.cfg, self.filters, {}, launch=False)["job"]
        self.sql("UPDATE voc_job SET owner='999999999:dead',status='running' WHERE id=?", (job["id"],))
        voc._INITIALIZED.discard(voc._path(self.cfg))
        voc.startup(self.cfg)
        recovered = voc.jobs(self.cfg)["items"][0]
        self.assertEqual((recovered["status"], recovered["failed"]), ("failed", 5))
        self.assertEqual(self.run_job()["processed"], 5)

    def test_gets_do_not_cancel_live_process_job(self):
        job = voc.start_job(self.cfg, self.filters, {}, launch=False)["job"]
        voc.summary(self.cfg, self.filters)
        voc.topics(self.cfg, self.filters)
        self.assertEqual(voc.jobs(self.cfg)["items"][0]["status"], job["status"])

    def test_changed_input_becomes_pending_and_stale_evidence_hidden(self):
        self.run_job()
        qa_id = self.sql("SELECT qa_id FROM qa_pair ORDER BY qa_id LIMIT 1")[0]["qa_id"]
        self.sql("UPDATE qa_pair SET answer='更新的实际回复' WHERE qa_id=?", (qa_id,))
        self.assertEqual(voc.summary(self.cfg, self.filters)["pending_pairs"], 1)
        self.assertEqual(self.topic()["count"], 4)
        self.assertEqual(self.run_job()["total"], 1)
        self.assertEqual(self.topic()["count"], 5)

    def test_automatic_a_to_b_to_a_can_return_to_previous_topic(self):
        qa_id = self.sql("SELECT qa_id FROM qa_pair ORDER BY qa_id LIMIT 1")[0]["qa_id"]
        filters = {"q": "VOC切换问题"}
        def classify(cfg, pairs, topics):
            result = self.result(cfg, pairs, [])
            for row, pair in zip(result["results"], pairs):
                label = "A" if " A " in pair["question"] else "B"
                row["intents"][0]["question"] = f"如何处理 {label} 类问题？"
            return result
        for label in ("A", "B", "A"):
            self.sql("UPDATE qa_pair SET question=? WHERE qa_id=?", (f"VOC切换问题 {label} 怎么使用？", qa_id))
            self.assertEqual(self.run_job(classify, filters)["processed"], 1)
        current = voc.topics(self.cfg, filters)["items"]
        self.assertEqual([(t["question"], t["count"]) for t in current], [("如何处理 A 类问题？", 1)])

    def test_failed_validation_usage_is_still_recorded(self):
        def fail(*args):
            raise VocModelError("bad schema", usage_tokens=17)
        job = self.run_job(fail)
        self.assertEqual(job["usage_tokens"], 51)

    def test_manual_topic_cannot_be_empty_after_visible_text_cleanup(self):
        attachment = json.dumps({"ext": "jpg", "size": 2, "name": "photo.jpg", "url": "https://example.test/photo.jpg"})
        with self.assertRaises(voc.VocError):
            voc.save_topic(self.cfg, {}, {"question": attachment, "theme": "主题", "boundary": "", "status": "pending"})

    def test_scope_counts_equal_detail_and_unknown_time_not_in_date_filter(self):
        self.run_job()
        topic = self.topic()
        for filters, count in (({"group": "售前"}, 2), ({"group": "售后"}, 3), ({"start": "2026-11-02", "end": "2026-11-02"}, 1)):
            scoped = voc.topics(self.cfg, filters)["items"][0]
            detail = voc.detail(self.cfg, filters, topic["id"])
            self.assertEqual((scoped["count"], detail["total"], len(detail["evidence"])), (count, count, count))

    def test_evidence_status_filtered_before_pagination(self):
        self.run_job()
        topic = self.topic()
        qa_id = voc.detail(self.cfg, self.filters, topic["id"])["evidence"][0]["qa_id"]
        voc.evidence_action(self.cfg, self.filters, {"topic_id": topic["id"], "revision": topic["revision"], "qa_ids": [qa_id], "action": "confirm"})
        pending = voc.detail(self.cfg, self.filters, topic["id"], limit=1, offset=2, evidence_status="pending")
        self.assertEqual((pending["total"], len(pending["evidence"]), pending["topic"]["count"]), (4, 1, 5))
        confirmed = voc.detail(self.cfg, self.filters, topic["id"], evidence_status="confirmed")
        self.assertEqual((confirmed["total"], confirmed["evidence"][0]["qa_id"]), (1, qa_id))

    def test_manual_name_protected_and_confirmation_limited_to_scope(self):
        self.run_job()
        topic = self.topic()
        body = {"id": topic["id"], "question": "人工确认的商品咨询", "theme": "人工主题",
                "boundary": "人工明确边界", "status": "confirmed", "revision": topic["revision"]}
        saved = voc.save_topic(self.cfg, {"group": "售前"}, body)["topic"]
        self.assertEqual(saved["pending_count"], 0)
        self.assertEqual(self.topic()["pending_count"], 3)
        qa_id = self.sql("SELECT qa_id FROM qa_pair ORDER BY qa_id LIMIT 1")[0]["qa_id"]
        self.sql("UPDATE qa_pair SET answer='更新内容' WHERE qa_id=?", (qa_id,))
        self.run_job()
        self.assertEqual(self.topic()["question"], "人工确认的商品咨询")
        self.assertEqual(self.topic()["theme"], "人工主题")

    def test_stale_revision_and_invalid_evidence_rejected(self):
        self.run_job()
        topic = self.topic()
        with self.assertRaises(voc.VocError) as error:
            voc.evidence_action(self.cfg, self.filters, {"topic_id": topic["id"], "revision": topic["revision"] - 1, "qa_ids": [1], "action": "confirm"})
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(voc.VocError):
            voc.evidence_action(self.cfg, self.filters, {"topic_id": topic["id"], "revision": topic["revision"], "qa_ids": [999999], "action": "exclude"})

    def test_excluded_member_is_idempotent_and_model_cannot_restore(self):
        self.run_job()
        topic = self.topic()
        qa_id = voc.detail(self.cfg, self.filters, topic["id"])["evidence"][0]["qa_id"]
        body = {"topic_id": topic["id"], "revision": topic["revision"], "qa_ids": [qa_id], "action": "exclude"}
        changed = voc.evidence_action(self.cfg, self.filters, body)["topic"]
        self.assertEqual(changed["count"], 4)
        body["revision"] = changed["revision"]
        self.assertEqual(voc.evidence_action(self.cfg, self.filters, body)["topic"]["revision"], changed["revision"])
        self.sql("UPDATE qa_pair SET answer='更新内容' WHERE qa_id=?", (qa_id,))
        self.run_job()
        self.assertEqual(self.topic()["count"], 4)

    def test_split_moves_selected_evidence_and_keeps_audit(self):
        self.run_job()
        source = self.topic()
        qa_id = voc.detail(self.cfg, self.filters, source["id"])["evidence"][0]["qa_id"]
        split = voc.evidence_action(self.cfg, self.filters, {"topic_id": source["id"], "revision": source["revision"], "qa_ids": [qa_id], "action": "split", "new_question": "人工拆分的独立问题", "new_theme": "独立主题"})["topic"]
        self.assertEqual((split["count"], split["pending_count"]), (1, 1))
        self.assertEqual(voc.detail(self.cfg, self.filters, source["id"])["topic"]["count"], 4)
        self.assertTrue(self.sql("SELECT * FROM voc_audit WHERE action='split'"))

    def test_split_then_explicit_merge_back_restores_target_tombstone(self):
        self.run_job()
        original = self.topic()
        qa_id = voc.detail(self.cfg, self.filters, original["id"])["evidence"][0]["qa_id"]
        split = voc.evidence_action(self.cfg, self.filters, {"topic_id": original["id"], "revision": original["revision"], "qa_ids": [qa_id], "action": "split", "new_question": "拆出后再合回的独立问题", "new_theme": "业务咨询"})["topic"]
        target = voc.detail(self.cfg, self.filters, original["id"])["topic"]
        self.assertEqual(target["count"], 4)
        restored = voc.merge_topics(self.cfg, self.filters, {"source_id": split["id"], "target_id": target["id"], "source_revision": split["revision"], "target_revision": target["revision"]})["topic"]
        self.assertEqual((restored["count"], voc.detail(self.cfg, {}, restored["id"])["total"]), (5, 5))
        self.assertIn(qa_id, {row["qa_id"] for row in voc.detail(self.cfg, {}, restored["id"])["evidence"]})
        self.assertTrue(self.sql("SELECT * FROM voc_audit WHERE action='merge_restore' AND topic_id=? AND qa_id=?", (restored["id"], qa_id)))
        with voc._connection(self.cfg) as conn:
            self.assertFalse(voc._manually_excluded(conn, qa_id, restored["id"]))
        # A later explicit exclusion is authoritative again.
        excluded = voc.evidence_action(self.cfg, {}, {"topic_id": restored["id"], "revision": restored["revision"], "qa_ids": [qa_id], "action": "exclude"})["topic"]
        self.assertEqual(excluded["count"], 4)
        with voc._connection(self.cfg) as conn:
            self.assertTrue(voc._manually_excluded(conn, qa_id, restored["id"]))

    def test_merge_back_does_not_revive_excluded_source_member(self):
        self.run_job()
        original = self.topic()
        ids = [member["qa_id"] for member in voc.detail(self.cfg, {}, original["id"])["evidence"][:2]]
        split = voc.evidence_action(self.cfg, {}, {"topic_id": original["id"], "revision": original["revision"], "qa_ids": ids, "action": "split", "new_question": "含两条原句的拆分主题", "new_theme": "业务咨询"})["topic"]
        source = voc.evidence_action(self.cfg, {}, {"topic_id": split["id"], "revision": split["revision"], "qa_ids": [ids[1]], "action": "exclude"})["topic"]
        target = voc.detail(self.cfg, {}, original["id"])["topic"]
        merged = voc.merge_topics(self.cfg, {}, {"source_id": source["id"], "target_id": target["id"], "source_revision": source["revision"], "target_revision": target["revision"]})["topic"]
        actual_ids = {member["qa_id"] for member in voc.detail(self.cfg, {}, merged["id"])["evidence"]}
        self.assertEqual((merged["count"], len(actual_ids)), (4, 4))
        self.assertIn(ids[0], actual_ids)
        self.assertNotIn(ids[1], actual_ids)
        self.assertEqual(voc.detail(self.cfg, {}, source["id"])["total"], 0)

    def test_multi_intent_merge_counts_unique_qa_ids(self):
        def multi(cfg, pairs, topics):
            output = self.result(cfg, pairs, [])
            for result in output["results"]:
                result["intents"].append({**result["intents"][0], "question": "如何了解另一个咨询意图？", "theme": "第二意图"})
            return output
        self.run_job(multi)
        source, target = voc.topics(self.cfg, self.filters)["items"]
        merged = voc.merge_topics(self.cfg, self.filters, {"source_id": source["id"], "target_id": target["id"], "source_revision": source["revision"], "target_revision": target["revision"]})["topic"]
        self.assertEqual(merged["count"], 5)
        self.assertEqual(voc.detail(self.cfg, self.filters, source["id"])["total"], 0)
        self.assertTrue(self.sql("SELECT * FROM voc_audit WHERE action='merge'"))

    def test_merge_restores_model_retired_target(self):
        qa_id = self.sql("SELECT qa_id FROM qa_pair ORDER BY qa_id LIMIT 1")[0]["qa_id"]
        self.sql("UPDATE qa_pair SET question='同一条变更问题怎么处理？' WHERE qa_id=?", (qa_id,))
        filters = {"q": "同一条变更问题"}
        def multi(cfg, pairs, topics):
            result = self.result(cfg, pairs, [])
            result["results"][0]["intents"].append({**result["results"][0]["intents"][0], "question": "另一个业务意图如何处理？"})
            return result
        self.run_job(multi, filters)
        first, second = voc.topics(self.cfg, filters)["items"]
        self.sql("UPDATE qa_pair SET answer='更新后的实际答复' WHERE qa_id=?", (qa_id,))
        self.run_job(self.result, filters)
        source = voc.detail(self.cfg, filters, first["id"])["topic"]
        target = voc.detail(self.cfg, filters, second["id"])["topic"]
        self.assertEqual(target["count"], 0)
        merged = voc.merge_topics(self.cfg, filters, {"source_id": source["id"], "target_id": target["id"], "source_revision": source["revision"], "target_revision": target["revision"]})["topic"]
        self.assertEqual(merged["count"], 1)

    def test_model_exclusion_is_explicit_processed_not_failure(self):
        def exclude(cfg, pairs, topics):
            return {"results": [{"qa_id": p["qa_id"], "excluded": True, "reason": "无可整理业务意图", "intents": []} for p in pairs], "usage_tokens": 2}
        job = self.run_job(exclude)
        self.assertEqual((job["processed"], job["failed"]), (5, 0))
        self.assertEqual(voc.summary(self.cfg, self.filters)["pending_pairs"], 0)
        self.assertEqual(voc.topics(self.cfg, self.filters)["total"], 0)

    def test_changed_during_model_call_is_failed_not_committed(self):
        changed = False
        def change(cfg, pairs, topics):
            nonlocal changed
            if not changed:
                self.sql("UPDATE qa_pair SET answer='并发更新' WHERE qa_id=?", (pairs[0]["qa_id"],))
                changed = True
            return self.result(cfg, pairs, topics)
        job = self.run_job(change)
        self.assertEqual((job["status"], job["processed"], job["failed"]), ("partial", 4, 1))
        self.assertEqual(voc.summary(self.cfg, self.filters)["pending_pairs"], 1)

    def test_demo_and_dry_run_never_call_real_model(self):
        for cfg in (replace(self.cfg, runtime=replace(self.cfg.runtime, mock=True)), replace(self.cfg, llm=replace(self.cfg.llm, dry_run=True))):
            with patch("src.voc.analyze_batch", side_effect=AssertionError("no network")):
                with self.assertRaises(voc.VocError):
                    voc.start_job(cfg, {}, {})

    def test_manual_empty_topic_visible_and_duplicate_name_conflicts(self):
        body = {"question": "没有证据的人工主题", "theme": "手工", "boundary": "待补充", "status": "pending"}
        topic = voc.save_topic(self.cfg, {}, body)["topic"]
        self.assertEqual(topic["count"], 0)
        self.assertEqual(voc.topics(self.cfg, {})["total"], 1)
        with self.assertRaises(voc.VocError) as error:
            voc.save_topic(self.cfg, {}, body)
        self.assertEqual(error.exception.status, 409)

    def test_transport_metadata_and_pii_not_sent_to_model(self):
        qa_id = self.sql("SELECT qa_id FROM qa_pair ORDER BY qa_id LIMIT 1")[0]["qa_id"]
        metadata = json.dumps({"ext": "jpg", "name": "photo.jpg", "size": 2, "url": "https://example.test/private.jpg"})
        self.sql("UPDATE qa_pair SET question=? WHERE qa_id=?", ("电话13812345678怎么改？" + metadata, qa_id))
        def inspect(cfg, pairs, topics):
            data = json.dumps([{k: v for k, v in p.items() if not k.startswith('_')} for p in pairs], ensure_ascii=False)
            self.assertNotIn("13812345678", data)
            self.assertNotIn("private.jpg", data)
            return self.result(cfg, pairs, topics)
        self.assertEqual(self.run_job(inspect)["status"], "completed")


class TestVocHttp(LocalFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.server = DashboardServer(("127.0.0.1", 0), self.cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def call(self, name, method="GET", payload=None, same_origin=True):
        port = self.server.server_port
        headers = {"Content-Type": "application/json"}
        if same_origin:
            headers.update({"Origin": f"http://127.0.0.1:{port}", "X-Local-Request": "1"})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, "/api/" + name, body=json.dumps(payload).encode() if payload is not None else b"", headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_voc_uses_original_same_origin_gate_and_safe_errors(self):
        self.assertEqual(self.call("voc/summary")[1]["total_pairs"], 5)
        body = {"question": "人工创建的标准问题", "theme": "业务", "boundary": "", "status": "pending"}
        self.assertEqual(self.call("voc/topic", "POST", body, same_origin=False)[0], 403)
        code, saved = self.call("voc/topic", "POST", body)
        self.assertEqual(code, 200)
        self.assertEqual(saved["topic"]["count"], 0)
        update = {**body, "id": saved["topic"]["id"], "revision": 0}
        self.assertEqual(self.call("voc/topic", "POST", update)[0], 409)
        self.assertEqual(self.call("voc/detail?id=999999")[0], 404)
        self.assertEqual(self.call("voc/run", "POST", {"limit": 5})[0], 400)
        self.assertEqual(self.call("voc/topic", "POST", [body])[0], 400)
