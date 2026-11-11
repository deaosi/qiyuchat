"""Signed queue consumption and source coverage; synthetic data/mocked APIs only."""
from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlsplit

from src import app, push_sync as P
from src.config import QiyuConfig
from src.db import DB
from src.extract.identity import SOURCE_IDENTITY_ROLE, decode_identity, encode_identity
from src.extract.qa import extract_pairs
from src.web import api as web_api
from src import voc
from src.qiyu.message import validate_messages
from src.qiyu.session import MESSAGE_ENDPOINT, SessionExporter
from tests.test_local import LocalFixture

NOW = datetime(2026, 11, 9, 18).timestamp()
SID = 1001


def message(mid=1, direction=1, text="商品发票应该怎么申请？", **changes):
    return {"id": mid, "sessionId": SID, "from": direction,
            "staffId": 9 if direction == 0 else 0, "userId": 101,
            "externalUserId": "external-test-customer", "staffName": "合成客服",
            "mType": 1, "status": 1, "time": int(NOW * 1000) - 10_000 + mid,
            "msg": text, **changes}


def sources():
    return [message(), message(2, 0, "可在订单页面申请电子发票。"),
            message(3, 1, "到账大概需要多久？"), message(4, 0, "以订单页面提示为准。")]


def metadata(**changes):
    return {"id": SID, "startTime": int(NOW * 1000) - 20_000,
            "endTime": int(NOW * 1000) - 1, "staffId": 9, "staffName": "合成客服",
            "userId": 101, "userName": "合成客户", "fromGroup": "售后",
            "fromType": "web", "staffMessageCount": 2, "userMessageCount": 2,
            "roundNumber": 2, "isValid": 1, **changes}


class FakeExporter:
    def __init__(self):
        self.metadata = metadata()
        self.rows = sources()
        self.calls = []
        self.on_one = None
        self.on_messages = None

    def export_one(self, sid):
        self.calls.append(("one", sid))
        if self.on_one:
            self.on_one()
        return copy.deepcopy(self.metadata)

    def export_messages(self, sid):
        self.calls.append(("messages", sid))
        if self.on_messages:
            self.on_messages()
        return copy.deepcopy(self.rows)


class TestSourceMessageApi(unittest.TestCase):
    def setUp(self):
        self.api = SessionExporter(QiyuConfig(app_key="synthetic-key", app_secret="synthetic-secret"))

    def test_exact_official_endpoint_body_and_envelope_keep_original_fields(self):
        attachment = '{"ext":"jpg","name":"a.jpg","url":"https://example.test/a.jpg"}'
        rows = [message(text=attachment, mType=2)]
        with patch("src.qiyu.session.request", return_value={"code": 200, "data": rows}) as transport:
            self.assertEqual(self.api.export_messages(SID), rows)
        args, kwargs = transport.call_args
        self.assertEqual((args[0], urlsplit(args[1]).path), ("POST", MESSAGE_ENDPOINT))
        self.assertEqual(json.loads(kwargs["body"]), {"sessionId": SID})
        self.assertEqual(rows[0]["msg"], attachment)

    def test_direct_list_is_accepted_but_unverified_wrappers_are_not(self):
        with patch("src.qiyu.session.request", return_value=sources()):
            self.assertEqual(len(self.api.export_messages(SID)), 4)
        for bad in ({"messages": sources()}, {"list": sources()}, "[]", ["bad"]):
            with self.subTest(response=type(bad).__name__), patch("src.qiyu.session.request", return_value={"code": 200, "data": bad}):
                with self.assertRaises(ValueError):
                    self.api.export_messages(SID)

    def test_invalid_ids_sids_fields_and_duplicate_messages_are_rejected(self):
        for changes in ({"id": True}, {"id": 0}, {"sessionId": SID + 1},
                        {"from": "1"}, {"time": 0}, {"msg": {}}, {"staffId": None},
                        {"mType": True}, {"status": "1"}, {"userId": "101"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_messages([message(**changes)], SID)
        with self.assertRaises(ValueError):
            validate_messages([message(), message()], SID)
        for sid in (True, "1001", -1, 0, 2**63):
            with patch("src.qiyu.session.request") as transport:
                with self.assertRaises(ValueError):
                    self.api.export_messages(sid)
                transport.assert_not_called()


class TestPushSync(LocalFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.cfg = replace(self.cfg, qiyu=replace(self.cfg.qiyu,
                           app_key="synthetic-key", app_secret="synthetic-secret"))
        self.api = FakeExporter()
        self.event()

    def sql(self, statement, args=()):
        conn = sqlite3.connect(self.cfg.runtime.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(row) for row in conn.execute(statement, args)]
            conn.commit()
            return rows
        finally:
            conn.close()

    def event(self, key="end-1", payload=None, received=None, event_type=P.EVENT_TYPE):
        payload = metadata() if payload is None else payload
        self.sql("INSERT INTO push_event(event_key,event_type,payload,received_at) VALUES (?,?,?,?)",
                 (key, event_type, json.dumps(payload), int(NOW * 1000) if received is None else received))

    def sync(self, now=NOW, **kwargs):
        with patch("src.push_sync.time.sleep"):
            return P.sync_pending(self.cfg, exporter=self.api, now=now, **kwargs)

    def state(self):
        return self.sql("SELECT * FROM push_sync_state WHERE session_id=?", (str(SID),))[0]

    def legacy_snapshot(self):
        self.api.metadata["userId"] = 999  # Session-level ID is a different namespace.
        db = app.get_db(self.cfg)
        db.upsert_sessions([self.api.metadata])
        projected = P._project(self.api.metadata, self.api.rows, metadata(), SID, legacy=True)
        db.upsert_messages(str(SID), projected)
        db._conn.executescript(P.SCHEMA)
        event = self.sql("SELECT event_key,payload FROM push_event WHERE event_key='end-1'")[0]
        version = P._event_version(P.LEGACY_VERSION, event["event_key"], json.loads(event["payload"]))
        source = P._json({"session": self.api.metadata, "messages": self.api.rows, "explicit_test_marker": False})
        self.sql("INSERT INTO push_sync_state(session_id,event_key,version,status,attempts,updated_at,source_version,source_json) VALUES (?,?,?,'completed',1,?,?,?)",
                 (str(SID), event["event_key"], version, NOW, version, source))
        return copy.deepcopy(self.api.rows)

    def target_messages(self, controls=False):
        return self.sql("SELECT seq,role,content,ts FROM raw_message WHERE session_id=? "
                        + ("" if controls else "AND role<>'source_identity' ") + "ORDER BY seq", (str(SID),))

    def test_success_atomic_scoped_extraction_source_metadata_and_idempotence(self):
        before = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        old_clusters = self.sql("SELECT * FROM q_cluster ORDER BY run_id,cluster_id")
        self.api.rows.reverse()
        result = self.sync()
        self.assertEqual((result["completed"], result["qa_pairs"]), (1, 2))
        self.assertEqual(self.api.calls, [("one", SID), ("messages", SID)])
        pairs = self.sql("SELECT * FROM qa_pair WHERE session_id=? ORDER BY idx", (str(SID),))
        self.assertEqual([p["answer"] for p in pairs], ["可在订单页面申请电子发票。", "以订单页面提示为准。"])
        self.assertTrue(all(p["cluster_id"] > 0 for p in pairs))
        self.assertEqual(self.sql("SELECT * FROM qa_pair WHERE session_id<>? ORDER BY qa_id", (str(SID),)), before)
        self.assertEqual(self.sql("SELECT * FROM q_cluster ORDER BY run_id,cluster_id"), old_clusters)
        state = self.state()
        source = json.loads(state["source_json"])
        self.assertEqual([m["id"] for m in source["messages"]], [1, 2, 3, 4])
        self.assertEqual(source["messages"][0]["externalUserId"], "external-test-customer")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.sync()["claimed"], 0)
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.state()["attempts"], 1)

    def test_only_newest_end_per_sid_is_consumed_and_invalid_ids_ignored(self):
        self.event("end-new", metadata(), int(NOW * 1000) + 10)
        self.event("wrong-event", metadata(id=1002), event_type="SESSION_END")
        for i, payload in enumerate(({"id": "1003"}, {"id": True}, {"data": {"id": 1004}}, [])):
            self.event("invalid-" + str(i), payload)
        result = self.sync()
        self.assertEqual((result["scanned"], result["invalid_events"], result["completed"]), (1, 4, 1))
        self.assertEqual(self.state()["event_key"], "end-new")
        self.assertEqual(len(self.api.calls), 2)

    def test_incomplete_response_never_overwrites_existing_qa_or_messages(self):
        self.sync()
        before = self.target_messages()
        before_pairs = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        source = self.state()["source_json"]
        self.event("end-new", metadata(), int(NOW * 1000) + 1)
        self.api.rows = self.api.rows[:2]
        self.assertEqual(self.sync()["failed"], 1)
        self.assertIn("响应不完整", self.state()["safe_error"])
        self.assertEqual(self.target_messages(), before)
        self.assertEqual(self.sql("SELECT * FROM qa_pair ORDER BY qa_id"), before_pairs)
        self.assertEqual(self.state()["source_json"], source)

    def test_snapshot_cannot_lose_source_id_even_if_new_counts_are_smaller(self):
        self.sync()
        before = self.target_messages()
        self.api.rows = self.api.rows[:2]
        self.api.metadata.update(staffMessageCount=1, userMessageCount=1)
        self.event("end-new", metadata(staffMessageCount=1, userMessageCount=1), int(NOW * 1000) + 1)
        self.assertEqual(self.sync()["failed"], 1)
        self.assertIn("缺失或改变", self.state()["safe_error"])
        self.assertEqual(self.target_messages(), before)

    def test_existing_originals_must_match_verbatim_or_sync_is_refused(self):
        db = app.get_db(self.cfg)
        db.upsert_sessions([metadata()])
        db.upsert_messages(str(SID), [{"role": "user", "content": "原有未匹配原文", "ts": 1}])
        before = self.target_messages()
        self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual(self.target_messages(), before)
        self.assertIn("无法完整匹配", self.state()["safe_error"])

    def test_existing_matching_subset_can_gain_verified_new_messages(self):
        db = app.get_db(self.cfg)
        db.upsert_sessions([metadata()])
        first = self.api.rows[0]
        db.upsert_messages(str(SID), [{"role": "user", "content": first["msg"], "ts": first["time"]}])
        self.assertEqual(self.sync()["completed"], 1)
        self.assertEqual(len(self.target_messages()), 4)

    def test_directions_identity_status_and_unknown_types_never_become_human_answers(self):
        self.api.rows = [message(), message(2, 0, "机器人说法", staffId=0),
                         message(3, 0, "撤回正文", status=2), message(4, 2, "未知方向"),
                         message(5, 1, "接下来应该怎么操作？"), message(6, 0, "系统提示", mType=999),
                         message(7, 1, "另外如何提交订单？"), message(8, 0, "真正客服答复"),
                         message(9, 0, "机器人消息类型", mType=110),
                         message(10, 1, "未知访客身份", userId=0)]
        self.api.metadata.update(staffMessageCount=5, userMessageCount=4)
        self.sql("DELETE FROM push_event")
        self.event(payload=self.api.metadata)
        self.assertEqual(self.sync()["completed"], 1)
        roles = [m["role"] for m in self.target_messages()]
        self.assertEqual(roles, ["user", "bot", "boundary", "boundary", "user", "boundary", "user", "staff", "bot", "user"])
        answers = [r["answer"] for r in self.sql("SELECT answer FROM qa_pair WHERE session_id=?", (str(SID),))]
        self.assertEqual([a for a in answers if a], ["真正客服答复"])

    def test_calls_hold_no_sqlite_transaction_and_claim_prevents_reentry(self):
        nested = []
        def probe():
            conn = sqlite3.connect(self.cfg.runtime.db_path, timeout=.1)
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            conn.close()
            nested.append(self.sync())
        self.api.on_one = probe
        self.assertEqual(self.sync()["completed"], 1)
        self.assertEqual(nested[0]["claimed"], 0)
        self.assertEqual(len(self.api.calls), 2)

    def test_numeric_text_session_identity_matches_integer_message_identity(self):
        self.api.metadata["userId"] = "101"
        self.assertEqual(self.sync()["completed"], 1)
        self.assertEqual([m["role"] for m in self.target_messages()], ["user", "staff", "user", "staff"])
        self.assertEqual(json.loads(self.state()["source_json"])["session"]["userId"], "101")

    def test_distinct_visitor_senders_are_separated_and_survive_general_reextract(self):
        self.api.metadata.update(userId=999, staffMessageCount=2, userMessageCount=3)
        self.api.rows = [message(1, 1, "商品发票应该怎么申请？", externalUserId="visitor-A", userId=11, userName="访客甲"),
                         message(2, 1, "退款应该如何申请？", externalUserId="visitor-B", userId=22, userName="访客乙"),
                         message(3, 0, "可以在订单页面发起退款。"),
                         message(4, 1, "发票需要提交哪些资料？", externalUserId="visitor-A", userId=11, userName="访客甲"),
                         message(5, 0, "请按开票页面填写抬头。")]
        self.sql("DELETE FROM push_event")
        self.event(payload=self.api.metadata)
        self.assertEqual(self.sync()["completed"], 1)
        fields = "qa_id,idx,customer_id,customer_name,question,answer,asked_at"
        before = self.sql(f"SELECT {fields} FROM qa_pair WHERE session_id=? ORDER BY idx", (str(SID),))
        self.assertEqual([row["customer_id"] for row in before], ["qiyu-external:visitor-A", "qiyu-external:visitor-B", "qiyu-external:visitor-A"])
        self.assertEqual([row["customer_name"] for row in before], ["访客甲", "访客乙", "访客甲"])
        self.assertEqual([row["answer"] for row in before], ["", "可以在订单页面发起退款。", "请按开票页面填写抬头。"])
        controls = [row["content"] for row in self.target_messages(controls=True) if row["role"] == SOURCE_IDENTITY_ROLE]
        self.assertEqual(len(controls), 3)
        # This is a synthetic DB only: emulate the existing webpage clean/rebuild.
        app.stage_extract(self.cfg, rebuild=True)
        self.assertEqual(self.sql(f"SELECT {fields} FROM qa_pair WHERE session_id=? ORDER BY idx", (str(SID),)), before)
        for row in before:
            public = web_api.detail(self.cfg, row["qa_id"])
            self.assertTrue(all(m["role"] != SOURCE_IDENTITY_ROLE for m in public["messages"]))
            self.assertTrue(all(m["content"] not in controls for m in public["messages"]))
        transcript = app.get_db(self.cfg).get_transcript(str(SID))
        self.assertTrue(all(control not in transcript for control in controls))
        with voc._connection(self.cfg) as conn:
            scope = voc._scope(conn, {})
        context = next(pair["messages"] for pair in scope.values() if pair["session_id"] == str(SID))
        self.assertTrue(all(m["role"] != SOURCE_IDENTITY_ROLE and m["content"] not in controls for m in context))
        visitor_a = [pair for pair in scope.values() if pair["customer_id"] == "qiyu-external:visitor-A"]
        visitor_b = [pair for pair in scope.values() if pair["customer_id"] == "qiyu-external:visitor-B"]
        self.assertTrue(visitor_a and visitor_b)
        for pair in visitor_a:
            texts = [m["content"] for m in pair["messages"]]
            self.assertNotIn("退款应该如何申请？", texts)
            self.assertNotIn("可以在订单页面发起退款。", texts)
            self.assertIn("发票需要提交哪些资料？", texts)
        for pair in visitor_b:
            texts = [m["content"] for m in pair["messages"]]
            self.assertEqual(texts, ["退款应该如何申请？", "可以在订单页面发起退款。"])

    def test_sender_falls_back_to_explicit_user_id_and_unknown_turns_do_not_merge(self):
        rows = [message(1, 1, "发票应该怎么申请？", externalUserId="", userId=42),
                message(2, 1, "退款应该怎么申请？", externalUserId="", userId=0),
                message(3, 1, "运费应该如何处理？", externalUserId="", userId=0)]
        projected = P._project(metadata(staffMessageCount=0, userMessageCount=3), rows,
                               metadata(staffMessageCount=0, userMessageCount=3), SID)
        pairs = extract_pairs(projected, {"id": str(SID), "user_id": "meta-id"})
        self.assertEqual([p["customer_id"] for p in pairs], ["qiyu-user:42", f"qiyu-session:{SID}", f"qiyu-session:{SID}"])
        self.assertTrue(all("\n" not in p["question"] for p in pairs))

    def test_detail_returns_only_selected_visitor_context_without_internal_controls(self):
        self.api.metadata["userId"] = 999
        self.api.rows = [message(1, 1, "发票应该怎么申请？", externalUserId="visitor-A", userId=11, userName="访客甲"),
                         message(2, 0, "请在订单页面填写发票抬头。"),
                         message(3, 1, "退款应该怎么申请？", externalUserId="visitor-B", userId=22, userName="访客乙"),
                         message(4, 0, "请在订单页面提交退款申请。")]
        self.assertEqual(self.sync()["completed"], 1)
        pairs = self.sql("SELECT qa_id,customer_id FROM qa_pair WHERE session_id=? ORDER BY idx", (str(SID),))
        expected = {"qiyu-external:visitor-A": ["发票应该怎么申请？", "请在订单页面填写发票抬头。"],
                    "qiyu-external:visitor-B": ["退款应该怎么申请？", "请在订单页面提交退款申请。"]}
        for pair in pairs:
            detail = web_api.detail(self.cfg, pair["qa_id"])
            self.assertEqual([m["content"] for m in detail["messages"]], expected[pair["customer_id"]])
            self.assertTrue(all(m["role"] in ("user", "staff") for m in detail["messages"]))
            self.assertNotIn(SOURCE_IDENTITY_ROLE, json.dumps(detail["messages"]))
            self.assertFalse(detail["archived"])
        # A selected sender can have no safe contextual segment while original
        # messages still exist. That is different from raw-message archival.
        self.sql("UPDATE qa_pair SET customer_id='missing-sender' WHERE qa_id=?", (pairs[0]["qa_id"],))
        filtered = web_api.detail(self.cfg, pairs[0]["qa_id"])
        self.assertEqual(filtered["messages"], [])
        self.assertFalse(filtered["archived"])
        self.sql("DELETE FROM raw_message WHERE session_id=?", (str(SID),))
        self.assertTrue(web_api.detail(self.cfg, pairs[0]["qa_id"])["archived"])

    def test_control_schema_is_strict_and_user_text_cannot_forge_sender_identity(self):
        control = encode_identity(str(SID), "qiyu-external:sender", "访客")
        source = [{"role": "user", "content": control, "ts": 1}, {"role": "staff", "content": "原始客户文字的答复", "ts": 2}]
        pairs = extract_pairs(source, {"id": str(SID), "user_id": "ordinary-user"})
        self.assertTrue(pairs)
        self.assertEqual(pairs[0]["customer_id"], "ordinary-user")
        base = json.loads(control)
        for change in ({"version": True}, {"session_id": "other"}, {"customer_id": "qiyu-external:"}, {"customer_id": "untrusted"}, {"extra": "unexpected"}):
            self.assertIsNone(decode_identity(json.dumps({**base, **change}), str(SID)))

    def test_v1_zero_qa_projection_migrates_once_from_saved_source_without_network(self):
        before = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        original_source = self.legacy_snapshot()
        self.assertEqual([r["role"] for r in self.target_messages()], ["boundary", "staff", "boundary", "staff"])
        self.assertEqual(self.sync()["completed"], 1)
        self.assertEqual(self.api.calls, [])
        self.assertEqual([r["role"] for r in self.target_messages()], ["user", "staff", "user", "staff"])
        self.assertEqual(self.sql("SELECT * FROM qa_pair WHERE session_id<>? ORDER BY qa_id", (str(SID),)), before)
        snapshot = json.loads(self.state()["source_json"])
        self.assertEqual(snapshot["projection_version"], P.VERSION)
        self.assertEqual(snapshot["messages"], original_source)
        self.assertEqual(self.sync()["claimed"], 0)
        self.assertEqual(self.api.calls, [])

    def test_v1_migration_refuses_modified_raw_messages_or_existing_qa(self):
        self.legacy_snapshot()
        self.sql("INSERT INTO raw_message(session_id,seq,role,content,ts) VALUES (?,99,'user',?,1)",
                 (str(SID), "人工补充的原文"))
        before = self.target_messages(controls=True)
        self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual(self.target_messages(controls=True), before)
        self.assertEqual(self.api.calls, [])
        self.assertIn("迁移条件不满足", self.state()["safe_error"])

    def test_v1_existing_question_assets_are_not_automatically_migrated(self):
        self.legacy_snapshot()
        app.get_db(self.cfg).upsert_qa_pairs([{"session_id": str(SID), "idx": 1, "question": "已有人工核对的问答", "answer": "保留原答案"}])
        before = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        self.assertEqual(self.sync()["claimed"], 0)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.sql("SELECT * FROM qa_pair ORDER BY qa_id"), before)

    def test_v1_archived_snapshot_is_not_revived_by_projection_version_bump(self):
        self.legacy_snapshot()
        self.sql("DELETE FROM raw_message WHERE session_id=?", (str(SID),))
        self.assertEqual(self.state()["status"], "archived")
        self.assertEqual(self.sync()["claimed"], 0)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.state()["source_json"], "")

    def test_visitor_direction_does_not_require_metadata_identity_namespace(self):
        for user_id in ("102", "external-101", None, True):
            with self.subTest(user_id=user_id):
                projected = P._project(metadata(userId=user_id), sources(), metadata(), SID)
                self.assertEqual([m["role"] for m in projected if m["role"] != SOURCE_IDENTITY_ROLE], ["user", "staff", "user", "staff"])

    def test_failure_safe_error_backoff_retry_and_lease_recovery(self):
        def fail():
            raise RuntimeError("api-key customer-content signed-url-secret")
        self.api.on_one = fail
        self.assertEqual(self.sync()["failed"], 1)
        state = self.state()
        self.assertEqual(state["retry_after"], NOW + 60)
        self.assertNotIn("secret", state["safe_error"])
        self.assertNotIn("customer-content", json.dumps(state))
        self.assertEqual(self.sync(NOW + 1)["claimed"], 0)
        self.sql("UPDATE push_sync_state SET status='running',claim_until=?,owner='dead-owner'", (NOW + 600,))
        self.api.on_one = None
        self.assertEqual(self.sync(NOW + 2)["claimed"], 0)
        self.assertEqual(self.sync(NOW + 1000)["completed"], 1)
        self.assertEqual(self.state()["attempts"], 2)

    def test_source_or_metadata_sid_mismatch_never_writes(self):
        for field in ("metadata", "message"):
            with self.subTest(field=field):
                self.api = FakeExporter()
                if field == "metadata":
                    self.api.metadata["id"] = SID + 1
                else:
                    self.api.rows[0]["sessionId"] = SID + 1
                self.assertEqual(self.sync(NOW + (100 if field == "message" else 0))["failed"], 1)
                self.assertEqual(self.target_messages(), [])
                self.assertFalse(self.sql("SELECT 1 FROM raw_session WHERE session_id=?", (str(SID),)))

    def test_event_changes_during_network_is_retried_without_partial_write(self):
        self.api.on_messages = lambda: self.event("end-new", metadata(), int(NOW * 1000) + 1)
        self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual(self.target_messages(), [])
        self.api.on_messages = None
        self.assertEqual(self.sync(NOW + 1)["completed"], 1)
        self.assertEqual(self.state()["attempts"], 1)

    def test_extraction_failure_rolls_back_metadata_messages_qa_and_completion(self):
        before = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        with patch("src.push_sync._extract_one", side_effect=RuntimeError("secret")):
            self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual(self.target_messages(), [])
        self.assertFalse(self.sql("SELECT 1 FROM raw_session WHERE session_id=?", (str(SID),)))
        self.assertEqual(self.sql("SELECT * FROM qa_pair ORDER BY qa_id"), before)
        self.assertEqual(self.state()["source_json"], "")

    def test_archive_clears_source_snapshot_and_completed_event_does_not_refetch(self):
        self.sync()
        before_pairs = self.sql("SELECT * FROM qa_pair ORDER BY qa_id")
        db = app.get_db(self.cfg)
        db.rotate(window_days=60, now_ms=int((NOW + 90 * 86400) * 1000))
        self.assertEqual((self.state()["status"], self.state()["source_json"]), ("archived", ""))
        self.assertEqual(self.sync(NOW + 90 * 86400)["claimed"], 0)
        self.assertEqual(len(self.api.calls), 2)
        self.assertEqual(self.sql("SELECT * FROM qa_pair ORDER BY qa_id"), before_pairs)

    def test_retention_during_first_request_cannot_be_downgraded_or_restored_by_retry(self):
        db = app.get_db(self.cfg)
        db.upsert_sessions([metadata()])
        first = self.api.rows[0]
        db.upsert_messages(str(SID), [{"role": "user", "content": first["msg"], "ts": first["time"]}])
        self.api.on_messages = lambda: db.rotate(window_days=60, grace_days=0, now_ms=int((NOW + 90 * 86400) * 1000))
        self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual((self.state()["status"], self.state()["archive_reason"]), ("archived", "retention"))
        self.assertEqual(self.target_messages(), [])
        self.api.on_messages = None
        self.api.calls.clear()
        self.assertEqual(self.sync(NOW + 120)["claimed"], 0)
        self.assertEqual(self.api.calls, [])

    def test_legacy_retention_marker_prevents_first_official_backfill(self):
        db = app.get_db(self.cfg)
        db.upsert_sessions([metadata()])
        db.rotate(window_days=60, grace_days=0, now_ms=int((NOW + 90 * 86400) * 1000))
        self.assertTrue(db.originals_archived(str(SID)))
        self.assertEqual(self.sync(NOW + 120)["claimed"], 0)
        self.assertEqual(self.api.calls, [])

    def test_raw_message_delete_during_first_sync_cannot_be_repopulated(self):
        db = app.get_db(self.cfg)
        db.upsert_sessions([metadata()])
        first = self.api.rows[0]
        db.upsert_messages(str(SID), [{"role": "user", "content": first["msg"], "ts": first["time"]}])
        self.api.on_messages = lambda: self.sql("DELETE FROM raw_message WHERE session_id=?", (str(SID),))
        self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual((self.state()["status"], self.state()["archive_reason"]), ("archived", "raw_message_deleted"))
        self.assertEqual(self.target_messages(), [])

    def test_late_legacy_events_cannot_append_or_close_a_verified_source(self):
        self.assertEqual(self.sync()["completed"], 1)
        db = app.get_db(self.cfg)
        before = self.target_messages(controls=True)
        db.bind_uid_session("synthetic-live", str(SID))
        db.close_uid_session("synthetic-live", 1)
        self.assertEqual(db.append_message(str(SID), "staff", "非已核验消息", 1), -1)
        self.assertEqual(self.target_messages(controls=True), before)
        row = self.sql("SELECT end_ts FROM raw_session WHERE session_id=?", (str(SID),))[0]
        self.assertEqual(row["end_ts"], self.api.metadata["endTime"])

    def test_late_old_end_is_archived_without_requesting_source(self):
        self.sql("DELETE FROM push_event")
        self.event(payload=metadata(endTime=int((NOW - 90 * 86400) * 1000)))
        result = self.sync()
        self.assertEqual((result["archived"], result["claimed"]), (1, 0))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.state()["source_json"], "")

    def test_missing_event_end_time_is_rejected_before_network(self):
        self.sql("DELETE FROM push_event")
        self.event(payload=metadata(endTime="unknown"))
        self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.target_messages(), [])

    def test_source_metadata_cannot_bypass_retention_with_current_callback_time(self):
        self.api.metadata["endTime"] = int((NOW - 90 * 86400) * 1000)
        self.assertEqual(self.sync()["failed"], 1)
        self.assertEqual(self.target_messages(), [])
        self.assertIn("原文保留期", self.state()["safe_error"])

    def test_explicit_test_marker_is_reported_without_printing_customer_text(self):
        self.api.rows[0]["msg"] = "这是接口联调测试，发票应该怎么申请？"
        result = self.sync()
        self.assertEqual(result["test_marked_sessions"], 1)
        self.assertTrue(json.loads(self.state()["source_json"])["explicit_test_marker"])
        self.assertNotIn("接口联调测试", json.dumps(result))

    def test_mock_and_missing_credentials_never_call_external_api(self):
        for cfg in (replace(self.cfg, runtime=replace(self.cfg.runtime, mock=True)),
                    replace(self.cfg, qiyu=replace(self.cfg.qiyu, app_secret=""))):
            with self.assertRaises(P.PushSyncError):
                P.sync_pending(cfg, exporter=self.api, now=NOW)
        self.assertEqual(self.api.calls, [])

    def test_request_pacing_and_cli_safe_counts(self):
        with patch("src.push_sync.time.sleep") as sleep:
            self.assertEqual(P.sync_pending(self.cfg, exporter=self.api, now=NOW)["completed"], 1)
        self.assertTrue(any(0 < call.args[0] <= .25 for call in sleep.call_args_list))
        with patch("src.push_sync.load_settings", return_value=self.cfg), patch("src.push_sync.logging.disable"), patch("builtins.print") as output:
            self.assertEqual(P.main(["--limit", "1", "--session-id", str(SID)]), 0)
        self.assertEqual(json.loads(output.call_args.args[0])["claimed"], 0)
