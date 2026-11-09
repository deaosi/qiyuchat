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

    def target_messages(self):
        return self.sql("SELECT seq,role,content,ts FROM raw_message WHERE session_id=? ORDER BY seq", (str(SID),))

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
        self.assertEqual(roles, ["user", "bot", "boundary", "boundary", "user", "boundary", "user", "staff", "bot", "boundary"])
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

    def test_different_or_unknown_session_identity_never_becomes_a_customer(self):
        for user_id in ("102", "external-101", None, True):
            with self.subTest(user_id=user_id):
                projected = P._project(metadata(userId=user_id), sources(), metadata(), SID)
                self.assertEqual([m["role"] for m in projected], ["boundary", "staff", "boundary", "staff"])

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
