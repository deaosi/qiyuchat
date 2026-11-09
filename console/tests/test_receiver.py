"""七鱼「消息与事件接收url」接收端测试：验签、幂等、uid→session 绑定、10s ack 契约。"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse

from src import receiver as R
from src.db import DB

SECRET = "test-app-secret"
PATH = "/qiyu/event"


class ReceiverFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recv-")
        self.db = DB(os.path.join(self.tmp, "t.db"))
        self.httpd = R.serve(self.db, host="127.0.0.1", port=0, path=PATH,
                             app_secret=SECRET, verify=True)
        self.port = self.httpd.server_port

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 工具 ----------

    def post(self, event_type: str, payload: dict, *, ts: int | None = None,
             secret: str = SECRET, body: bytes | None = None, path: str = PATH):
        raw = body if body is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        stamp = ts if ts is not None else int(time.time())
        checksum = R.compute_checksum(secret, raw, str(stamp))
        qs = urllib.parse.urlencode({"eventType": event_type, "time": stamp, "checksum": checksum})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", f"{path}?{qs}", body=raw,
                         headers={"Content-Type": "application/json;charset=utf-8"})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()


    def post_customer(self, payload: dict, *, token: str = SECRET, path: str = R.CUSTOMER_PATH):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=raw,
                         headers={"Content-Type": "application/json;charset=utf-8",
                                  "X-Receiver-Token": token})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()


class TestReceiver(ReceiverFixture):
    def test_ack_is_empty_string(self):
        """官方契约：成功必须回空字符串，回任何内容七鱼都会重发。"""
        status, body = self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"})
        self.assertEqual(status, 200)
        self.assertEqual(body, "")

    def test_bad_checksum_rejected(self):
        status, _ = self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"},
                              secret="wrong-secret")
        self.assertEqual(status, 401)

    def test_session_start_binds_uid(self):
        self.post(R.EVENT_SESSION_START,
                  {"uid": "u1", "sessionId": "s1", "staffId": "9", "staffName": "小李",
                   "timeStamp": 1700000000000})
        self.assertEqual(self.db.current_session_for_uid("u1"), "s1")
        row = self.db._conn.execute(
            "SELECT staff_name FROM raw_session WHERE session_id='s1'").fetchone()
        self.assertEqual(row["staff_name"], "小李")

    def test_msg_appended_to_bound_session(self):
        self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"})
        self.post(R.EVENT_MSG, {"uid": "u1", "msgId": "m1", "content": "您好，已为您催发货",
                                "timeStamp": 1700000001000, "msgType": "TEXT"})
        self.post(R.EVENT_MSG, {"uid": "u1", "msgId": "m2", "content": "预计今天出库",
                                "timeStamp": 1700000002000, "msgType": "TEXT"})
        msgs = self.db.get_messages("s1")
        self.assertEqual([m["content"] for m in msgs], ["您好，已为您催发货", "预计今天出库"])
        self.assertEqual({m["role"] for m in msgs}, {"staff"})

    def test_msg_dedup_by_msg_id(self):
        """七鱼超时重发是常态，同 msgId 只能落一条。"""
        self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"})
        for _ in range(3):
            self.post(R.EVENT_MSG, {"uid": "u1", "msgId": "m1", "content": "重复消息",
                                    "timeStamp": 1700000001000})
        self.assertEqual(len(self.db.get_messages("s1")), 1)

    def test_session_start_dedup(self):
        payload = {"uid": "u1", "sessionId": "s1"}
        self.post(R.EVENT_SESSION_START, payload, ts=1700000000)
        self.post(R.EVENT_SESSION_START, payload, ts=1700000000)
        n = self.db._conn.execute("SELECT COUNT(*) AS n FROM push_event").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_msg_before_session_start_uses_live_session(self):
        """MSG 早到也不能丢，先挂到 live-{uid}，日志里会有告警。"""
        self.post(R.EVENT_MSG, {"uid": "u9", "msgId": "m9", "content": "在吗",
                                "timeStamp": 1700000001000})
        self.assertEqual(self.db.current_session_for_uid("u9"), "live-u9")
        self.assertEqual(len(self.db.get_messages("live-u9")), 1)

    def test_session_end_closes(self):
        self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"})
        self.post(R.EVENT_SESSION_END, {"uid": "u1", "sessionId": "s1", "closeReason": 5},
                  ts=1700000009)
        row = self.db._conn.execute(
            "SELECT ended_at FROM uid_session WHERE uid='u1'").fetchone()
        self.assertTrue(row["ended_at"])

    def test_unknown_event_acked(self):
        """没见过的 eventType 也要 ack，否则七鱼会一直重发。"""
        status, body = self.post("SOME_NEW_EVENT", {"uid": "u1"})
        self.assertEqual((status, body), (200, ""))

    def test_bad_json_returns_400(self):
        status, _ = self.post(R.EVENT_MSG, {}, body=b"not-json{")
        self.assertEqual(status, 400)

    def test_wrong_path_404(self):
        status, _ = self.post(R.EVENT_MSG, {"uid": "u1"}, path="/other")
        self.assertEqual(status, 404)

    def test_health(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read().decode("utf-8"), "ok")
        finally:
            conn.close()

    def test_no_content_msg_is_recorded_not_appended(self):
        """非文本消息（图片/文件）没有正文，只记账，不要塞空消息进正文。"""
        self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"})
        self.post(R.EVENT_MSG, {"uid": "u1", "msgId": "m1", "content": "",
                                "msgType": "IMAGE", "timeStamp": 1700000001000})
        self.assertEqual(self.db.get_messages("s1"), [])
        n = self.db._conn.execute(
            "SELECT COUNT(*) AS n FROM push_event WHERE event_type='MSG'").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_checksum_accepts_uppercase(self):
        """后台手填的 checksum 有时是大写，比对必须大小写不敏感。"""
        raw = json.dumps({"uid": "u1", "sessionId": "s1"}).encode("utf-8")
        stamp = int(time.time())
        checksum = R.compute_checksum(SECRET, raw, str(stamp)).upper()
        qs = urllib.parse.urlencode({"eventType": "SESSION_START", "time": stamp,
                                     "checksum": checksum})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", f"{PATH}?{qs}", body=raw,
                         headers={"Content-Type": "application/json;charset=utf-8"})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()

    def test_customer_mirror_completes_qa_pair(self):
        """七鱼只推客服侧消息；客户侧必须我们自己镜像，否则凑不成问答对。"""
        from src.extract.qa import extract_pairs

        self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"})
        self.post_customer({"uid": "u1", "msgId": "c1", "content": "滤芯多久换一次",
                            "timeStamp": 1700000001000})
        self.post(R.EVENT_MSG, {"uid": "u1", "msgId": "m1", "content": "建议 6-8 个月换一次",
                                "timeStamp": 1700000002000})
        msgs = self.db.get_messages("s1")
        self.assertEqual([m["role"] for m in msgs], ["user", "staff"])

        pairs = extract_pairs(msgs, {"session_id": "s1", "user_id": "u1"}, min_len=2)
        self.assertEqual(len(pairs), 1)
        self.assertIn("滤芯", pairs[0]["question"])
        self.assertIn("6-8", pairs[0]["answer"])

    def test_customer_requires_token(self):
        status, _ = self.post_customer({"uid": "u1", "content": "hi"}, token="wrong")
        self.assertEqual(status, 401)

    def test_customer_dedup(self):
        self.post(R.EVENT_SESSION_START, {"uid": "u1", "sessionId": "s1"})
        for _ in range(3):
            self.post_customer({"uid": "u1", "msgId": "c1", "content": "重复客户消息",
                                "timeStamp": 1700000001000})
        self.assertEqual(len(self.db.get_messages("s1")), 1)

    def test_customer_without_uid_rejected(self):
        status, _ = self.post_customer({"content": "没 uid"})
        self.assertEqual(status, 400)

    def test_verify_can_be_disabled(self):
        httpd = R.serve(self.db, host="127.0.0.1", port=0, path=PATH,
                        app_secret=SECRET, verify=False)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
            try:
                raw = b'{"uid":"u2","sessionId":"s2"}'
                conn.request("POST", f"{PATH}?eventType=SESSION_START&time=1&checksum=bad",
                             body=raw, headers={"Content-Type": "application/json"})
                self.assertEqual(conn.getresponse().status, 200)
            finally:
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
