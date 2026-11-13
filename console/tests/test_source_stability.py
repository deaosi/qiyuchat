"""Verified source retention, stable evidence references and bounded legacy repair.

All records and API results are synthetic; no external requests are made.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from src import app, push_sync as P
from src.extract.qa import extract_pairs
from tests.test_local import LocalFixture
from tests import test_push_sync as fixtures
from tests.test_push_sync import FakeExporter, NOW, SID, message, metadata


class TestSourceStability(LocalFixture):
    sql = fixtures.TestPushSync.sql
    event = fixtures.TestPushSync.event
    sync = fixtures.TestPushSync.sync
    state = fixtures.TestPushSync.state

    def setUp(self):
        from dataclasses import replace
        super().setUp()
        self.seed()
        self.cfg = replace(self.cfg, qiyu=replace(self.cfg.qiyu,
                           app_key="synthetic-key", app_secret="synthetic-secret"))
        self.api = FakeExporter()
        self.event()

    def pairs(self):
        return self.sql("SELECT * FROM qa_pair WHERE session_id=? ORDER BY idx", (str(SID),))

    def overwritten(self):
        """Reproduce the pre-fix DELETE trigger and lossy projection, not retention."""
        self.assertEqual(self.sync()["completed"], 1)
        db = app.get_db(self.cfg)
        with db.tx() as conn:
            conn.execute("INSERT INTO source_write_guard VALUES (?)", (str(SID),))
            conn.execute("DELETE FROM raw_message WHERE session_id=?", (str(SID),))
            conn.execute("DELETE FROM source_write_guard WHERE session_id=?", (str(SID),))
            conn.executemany("INSERT INTO raw_message(session_id,seq,role,content,ts) VALUES (?,?,?,?,?)",
                             [(str(SID), n, "boundary" if r["from"] == 1 else "staff", r["msg"], r["time"])
                              for n, r in enumerate(self.api.rows)])
            conn.execute("DELETE FROM qa_pair WHERE session_id=?", (str(SID),))
            conn.execute("DELETE FROM qa_origin WHERE session_id=?", (str(SID),))
            conn.execute("UPDATE raw_session SET fetched_at=? WHERE session_id=?", (int((NOW+1)*1000),str(SID)))
            conn.execute("UPDATE extract_state SET processed_at=?,status='no_question',pair_count=0 WHERE session_id=?", (int((NOW+1)*1000),str(SID)))
            conn.execute("UPDATE push_sync_state SET status='archived',source_json='',source_version='',archive_reason='',archived_at=0,updated_at=? WHERE session_id=?", (NOW,str(SID)))

    def test_daily_lower_completeness_cannot_replace_verified_metadata_or_messages(self):
        self.sync()
        db = app.get_db(self.cfg)
        before = db.get_messages(str(SID))
        source = self.state()
        session = self.sql("SELECT * FROM raw_session WHERE session_id=?", (str(SID),))
        state = self.sql("SELECT * FROM extract_state WHERE session_id=?", (str(SID),))
        self.assertEqual(db.upsert_sessions([metadata(staffMessageCount=0,userName="低完整度导出")]), 0)
        self.assertEqual(db.upsert_messages(str(SID), [{"role":"user","content":"低完整度快照","ts":1}]), 0)
        self.assertEqual(db.get_messages(str(SID)), before)
        self.assertEqual(self.state(), source)
        self.assertEqual(self.sql("SELECT * FROM raw_session WHERE session_id=?", (str(SID),)), session)
        self.assertEqual(self.sql("SELECT * FROM extract_state WHERE session_id=?", (str(SID),)), state)
        self.assertEqual(len(self.sql("SELECT * FROM source_write_audit WHERE session_id=?", (str(SID),))), 2)

    def test_ordinary_verified_replacement_is_not_retention_and_guard_rolls_back(self):
        self.sync()
        db = app.get_db(self.cfg)
        original = db.get_messages(str(SID))
        replacement = original + [{"role":"boundary","content":"未知系统类型","ts":int(NOW*1000),"source_message_id":"source:1001:88"}]
        db.upsert_messages(str(SID), replacement, verified_source=True)
        self.assertEqual(self.state()["status"], "completed")
        self.assertTrue(self.state()["source_json"])
        self.assertEqual(self.sql("SELECT * FROM source_write_guard"), [])
        with self.assertRaises(Exception):
            db.upsert_messages(str(SID), [{"role":"user","content":{},"ts":1}], verified_source=True)
        self.assertEqual(db.get_messages(str(SID)), replacement)
        self.assertEqual(self.sql("SELECT * FROM source_write_guard"), [])
        self.assertTrue(self.state()["source_json"])

    def test_reextract_restores_persisted_sender_roles_and_stable_qa_ids(self):
        self.api.rows[2]["externalUserId"] = "another-synthetic-visitor"
        self.sync()
        db = app.get_db(self.cfg)
        original = self.pairs()
        origins = [db.get_qa_origin(p["qa_id"]) for p in original]
        self.assertNotEqual(original[0]["customer_id"], original[1]["customer_id"])
        # Simulate a legacy writer that bypassed the protected high-level API.
        with db.tx() as c:
            c.execute("INSERT INTO source_write_guard VALUES (?)", (str(SID),))
            c.execute("DELETE FROM raw_message WHERE session_id=?", (str(SID),))
            c.execute("DELETE FROM source_write_guard")
            c.execute("INSERT INTO raw_message VALUES (?,0,'boundary','损坏的普通投影',1,NULL)", (str(SID),))
        app.stage_extract(self.cfg, rebuild=True)
        self.assertEqual([p["qa_id"] for p in self.pairs()], [p["qa_id"] for p in original])
        for old, new in zip(original, self.pairs()):
            for name in ("question","answer","customer_id","asked_at"):
                self.assertEqual(old[name], new[name])
        self.assertEqual([db.get_qa_origin(p["qa_id"]) for p in original], origins)
        self.assertTrue(any(m["role"] == "source_identity" for m in db.get_messages(str(SID))))

    def test_prepending_messages_reorders_idx_without_reassigning_existing_ids(self):
        self.sync()
        before = {p["question"]:p for p in self.pairs()}
        first = message(9, 1, "这个配件是否单独出售？", time=message()["time"]-5000)
        second = message(10, 0, "请在商品详情中核对。", time=first["time"]+1000)
        self.api.rows = [first,second,*self.api.rows]
        self.api.metadata.update(userMessageCount=3,staffMessageCount=3)
        self.event("end-2",self.api.metadata,received=int((NOW+1)*1000))
        self.assertEqual(self.sync(now=NOW+1)["completed"], 1)
        db = app.get_db(self.cfg)
        for pair in self.pairs()[1:]:
            self.assertEqual(pair["qa_id"],before[pair["question"]]["qa_id"])
            origin = db.get_qa_origin(pair["qa_id"])
            self.assertEqual(origin["revision"],1)
            self.assertTrue(origin["evidence_key"].startswith("issue:"))
        self.assertEqual([p["idx"] for p in self.pairs()], [1,2,3])

    def test_new_answer_updates_revision_not_evidence_key_or_qa_id(self):
        self.sync()
        db = app.get_db(self.cfg)
        before = self.pairs()[-1]
        origin = db.get_qa_origin(before["qa_id"])
        self.api.rows.append(message(5,0,"也可联系原订单客服核对。"))
        self.api.metadata["staffMessageCount"] = 3
        self.event("end-2",self.api.metadata,received=int((NOW+1)*1000))
        self.assertEqual(self.sync(now=NOW+1)["completed"],1)
        after = self.pairs()[-1]
        self.assertEqual(after["qa_id"],before["qa_id"])
        changed = db.get_qa_origin(after["qa_id"])
        self.assertEqual(changed["evidence_key"],origin["evidence_key"])
        self.assertEqual(changed["question_message_ids"],[f"source:{SID}:3"])
        self.assertEqual(changed["answer_message_ids"],[f"source:{SID}:4",f"source:{SID}:5"])
        self.assertEqual(changed["revision"],2)

    def test_retired_origin_reuses_its_reference_if_same_source_returns(self):
        self.sync()
        db = app.get_db(self.cfg)
        rows = self.pairs()
        session = dict(db._conn.execute("SELECT * FROM raw_session WHERE session_id=?",(str(SID),)).fetchone())
        pairs = extract_pairs(db.get_messages(str(SID)),session)
        db.replace_session_pairs(str(SID),[],"changed","no_question")
        self.assertEqual(db.get_qa_origin(rows[0]["qa_id"])["active"],0)
        db.replace_session_pairs(str(SID),pairs,"restored","extracted")
        self.assertEqual([p["qa_id"] for p in self.pairs()],[p["qa_id"] for p in rows])
        self.assertEqual(db.get_qa_origin(rows[0]["qa_id"])["active"],1)
        stripped = [{k:v for k,v in p.items() if k not in ("evidence_key","question_message_ids","answer_message_ids")} for p in pairs]
        with self.assertRaisesRegex(ValueError,"拒绝降级"):
            db.replace_session_pairs(str(SID),stripped,"bad","extracted")

    def test_legacy_repair_check_is_read_only_and_requires_explicit_confirmation(self):
        self.overwritten()
        before_schema = self.sql("SELECT sql FROM sqlite_master ORDER BY name")
        before = self.state()
        with patch("src.push_sync.SessionExporter",side_effect=AssertionError("no network")):
            result = P.check_repairs(self.cfg,session_id=SID,now=NOW+2)
        self.assertEqual(result["eligible"],1)
        self.assertTrue(result["read_only"])
        self.assertEqual(self.state(),before)
        self.assertEqual(self.sql("SELECT sql FROM sqlite_master ORDER BY name"),before_schema)
        self.api.calls.clear()
        with self.assertRaisesRegex(P.PushSyncError,"confirm-overwrite"):
            self.sync(repair_overwritten=True)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.sync()["claimed"],0)

    def test_confirmed_legacy_repair_is_scoped_atomic_and_idempotent(self):
        self.overwritten()
        other = self.sql("SELECT * FROM qa_pair WHERE session_id<>? ORDER BY qa_id",(str(SID),))
        self.api.calls.clear()
        result = self.sync(now=NOW+2,repair_overwritten=True,confirm_overwrite=True,session_id=SID)
        self.assertEqual((result["completed"],result["repaired"],result["qa_pairs"]),(1,1,2))
        self.assertEqual(len(self.api.calls),2)
        self.assertTrue(all(p["answer"] for p in self.pairs()))
        self.assertEqual(self.sql("SELECT * FROM qa_pair WHERE session_id<>? ORDER BY qa_id",(str(SID),)),other)
        self.assertEqual(self.state()["archive_reason"],"")
        self.assertEqual(self.sql("SELECT reason FROM source_write_audit WHERE operation='repair'"),[{"reason":"operator_confirmed_legacy_overwrite"}])
        self.api.calls.clear()
        self.assertEqual(self.sync(now=NOW+3,repair_overwritten=True,confirm_overwrite=True)["claimed"],0)
        self.assertEqual(self.api.calls,[])

    def test_failed_legacy_repair_stays_archived_and_explicit_retry_can_succeed(self):
        self.overwritten()
        before = app.get_db(self.cfg).get_messages(str(SID))
        self.api.on_messages = lambda: (_ for _ in ()).throw(RuntimeError("synthetic-sensitive-url"))
        result = self.sync(now=NOW+2,repair_overwritten=True,confirm_overwrite=True)
        self.assertEqual(result["failed"],1)
        self.assertEqual(self.state()["status"],"archived")
        self.assertNotIn("sensitive",self.state()["safe_error"])
        self.assertEqual(app.get_db(self.cfg).get_messages(str(SID)),before)
        self.assertEqual(self.sync(now=NOW+100)["claimed"],0)
        self.api.on_messages = None
        self.assertEqual(self.sync(now=NOW+100,repair_overwritten=True,confirm_overwrite=True)["repaired"],1)

    def test_repair_requires_old_text_coverage_and_never_overwrites_existing_qa(self):
        self.overwritten()
        self.sql("UPDATE raw_message SET content='这段旧原文官方响应没有' WHERE session_id=? AND seq=0",(str(SID),))
        original = app.get_db(self.cfg).get_messages(str(SID))
        self.assertEqual(self.sync(now=NOW+2,repair_overwritten=True,confirm_overwrite=True)["failed"],1)
        self.assertEqual(app.get_db(self.cfg).get_messages(str(SID)),original)
        self.sql("INSERT INTO qa_pair(session_id,idx,question) VALUES (?,1,'人工已有问题')",(str(SID),))
        self.assertEqual(P.check_repairs(self.cfg,now=NOW+3)["reasons"]["existing_question_assets"],1)

    def test_true_retention_and_expired_end_never_become_repair_candidates(self):
        self.sync()
        db = app.get_db(self.cfg)
        old = int((NOW-100*86400)*1000)
        self.sql("UPDATE raw_session SET end_ts=? WHERE session_id=?",(old,str(SID)))
        db.rotate(window_days=60,now_ms=int(NOW*1000),grace_days=0)
        self.assertEqual(self.state()["archive_reason"],"retention")
        self.assertEqual(self.state()["source_json"],"")
        self.api.calls.clear()
        self.assertEqual(self.sync(repair_overwritten=True,confirm_overwrite=True)["claimed"],0)
        self.assertEqual(self.api.calls,[])
        self.assertEqual(P.check_repairs(self.cfg,now=NOW)["eligible"],0)

    def test_unknown_delete_reason_is_not_operator_repairable(self):
        self.sync()
        self.sql("DELETE FROM raw_message WHERE session_id=?",(str(SID),))
        self.assertEqual(self.state()["archive_reason"],"raw_message_deleted")
        self.assertEqual(P.check_repairs(self.cfg,now=NOW)["eligible"],0)

    def test_interrupted_operator_repair_requires_explicit_reclaim_after_lease(self):
        self.overwritten()
        self.sql("UPDATE push_sync_state SET status='running',archive_reason='legacy_overwrite_candidate',archived_at=?,owner='dead-process',claim_until=? WHERE session_id=?",(NOW,NOW+10,str(SID)))
        self.assertEqual(P.check_repairs(self.cfg,now=NOW+2)["eligible"],0)
        self.assertEqual(self.sync(now=NOW+20)["claimed"],0)
        self.assertEqual(P.check_repairs(self.cfg,now=NOW+20)["eligible"],1)
        self.assertEqual(self.sync(now=NOW+20,repair_overwritten=True,confirm_overwrite=True)["repaired"],1)

    def test_retention_tombstone_prevents_ordinary_pull_resurrecting_originals(self):
        self.sync()
        db = app.get_db(self.cfg)
        self.sql("UPDATE raw_session SET end_ts=? WHERE session_id=?",(int((NOW-100*86400)*1000),str(SID)))
        self.sql("UPDATE raw_message SET ts=? WHERE session_id=?",(int((NOW-100*86400)*1000),str(SID)))
        db.rotate(window_days=60,now_ms=int(NOW*1000),grace_days=0)
        self.assertEqual(db.upsert_sessions([self.api.metadata]),0)
        self.assertEqual(db.upsert_messages(str(SID),[{"role":"user","content":"不可复活过期正文","ts":1}]),0)
        self.assertEqual(db.get_messages(str(SID)),[])
        self.assertEqual(self.sql("SELECT * FROM raw_session WHERE session_id=?",(str(SID),)),[])
        self.assertEqual(self.state()["archive_reason"],"retention")

    def test_legacy_qa_binding_is_exact_and_ambiguous_binding_is_rejected_atomically(self):
        db = app.get_db(self.cfg)
        projected = P._project(self.api.metadata,self.api.rows,metadata(),SID)
        pairs = extract_pairs(projected,self.api.metadata)
        unanchored = [{k:v for k,v in p.items() if k not in ("evidence_key","question_message_ids","answer_message_ids")} for p in pairs]
        db.replace_session_pairs(str(SID),unanchored,"legacy","extracted")
        before = self.pairs()
        db.replace_session_pairs(str(SID),pairs,"anchored","extracted")
        self.assertEqual([p["qa_id"] for p in self.pairs()],[p["qa_id"] for p in before])
        self.assertTrue(db.get_qa_origin(before[0]["qa_id"])["active"])
        duplicate = dict(pairs[0],idx=3,evidence_key="another-synthetic-source")
        self.sql("DELETE FROM qa_origin WHERE session_id=?",(str(SID),))
        current = self.pairs()
        with self.assertRaisesRegex(ValueError,"唯一匹配"):
            db.replace_session_pairs(str(SID),[*pairs,duplicate],"ambiguous","extracted")
        self.assertEqual(self.pairs(),current)
