from __future__ import annotations

from dataclasses import replace
import http.client
import json
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from src.app import get_db
from src.qiyu import ticket
from src.qiyu.sign import build_signature
from src.web.server import DashboardServer
from tests.test_local import LocalFixture


class TicketModuleTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, qiyu=replace(self.cfg.qiyu, app_key="test-key", app_secret="test-secret"))
        self.server = DashboardServer(("127.0.0.1", 0), self.cfg, admin_key="fixture")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def fetch(self, path: str, *, admin: bool = True):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            client.request("GET", path, headers={"X-Admin-Key": "fixture"} if admin else {})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def test_authenticated_projection_keeps_ticket_links_and_drops_private_fields(self):
        self.seed()
        db = get_db(self.cfg)
        with db.tx() as conn:
            conn.execute("UPDATE raw_session SET worksheet_ids = '134, 12' WHERE session_id = 's1'")
            conn.execute("UPDATE raw_session SET worksheet_ids = '112, 129' WHERE session_id = 's2'")
        raw = {"id": 12, "title": "设备问题 13812345678", "status": 10, "priority": 8,
               "createTime": 1794537000000, "userName": "访客姓名", "userMobile": "13812345678"}

        def respond(method, target, *, body, **kwargs):
            url = urlsplit(target)
            query = parse_qs(url.query)
            timestamp, checksum, signed = build_signature("test-secret", body, int(query["time"][0]))
            self.assertEqual((method, query["appKey"], query["checksum"], signed),
                             ("POST", ["test-key"], [checksum], body))
            payload = json.loads(body)
            if url.path == ticket.SEARCH:
                self.assertEqual(payload["limit"], 20)
                self.assertEqual(payload["end"] - payload["start"], 86399999)
                return {"code": 200, "message": json.dumps({"total": 1, "tickets": [raw]})}
            self.assertEqual(payload, {"ticketId": 12})
            if url.path == ticket.DETAIL:
                return {"code": 200, "data": {**raw, "isRichText": 1, "content": "<p>故障<br>已确认</p>"}}
            self.assertEqual(url.path, ticket.HISTORY)
            return {"code": 200, "data": [{"id": 44, "time": 1794538000000, "action": "更新工单",
                    "info": [{"title": "工单备注", "content": "已确认"},
                             {"title": "用户姓名", "content": "访客姓名"}]}]}

        with patch("src.qiyu.ticket.request", side_effect=respond) as upstream:
            self.assertEqual(self.fetch("/api/tickets", admin=False)[0], 401)
            self.assertEqual(self.fetch("/api/tickets/detail?id=12", admin=False)[0], 401)
            upstream.assert_not_called()
            self.assertEqual(self.fetch("/api/tickets/detail?id=0")[0], 400)
            status, result = self.fetch("/api/tickets?start=2026-11-13&end=2026-11-13")
            self.assertEqual((status, result["total"], result["has_more"]), (200, 1, False))
            row = result["items"][0]
            self.assertEqual((row["id"], row["linked_session_count"], row["status"], row["priority"]),
                             ("12", 1, "处理中", "紧急"))
            self.assertEqual(row["title"], "设备问题 138****5678")
            status, detail = self.fetch("/api/tickets/detail?id=12")
            self.assertEqual(status, 200)
            self.assertEqual(detail["ticket"]["description"], "故障\n已确认")
            self.assertEqual(detail["ticket"]["updated_at"], 1794538000000)
            self.assertEqual([item["session_id"] for item in detail["sessions"]], ["s1"])
            self.assertGreater(detail["sessions"][0]["qa_id"], 0)
            self.assertEqual(detail["history"][0]["details"], [{"label": "工单备注", "value": "已确认"}])
            self.assertNotIn("访客姓名", json.dumps(detail, ensure_ascii=False))

    def test_title_and_status_filter_pagination_and_upstream_errors(self):
        def respond(method, target, *, body, **kwargs):
            offset = json.loads(body)["offset"]
            data = [{"id": 11, "title": "设备故障", "status": 20}] if offset == 0 else [
                {"id": 10, "title": "设备调试", "status": 10},
                {"id": 9, "title": "设备报修", "status": 10},
            ]
            return {"code": 200, "message": {"total": 3, "tickets": data}}

        with patch("src.qiyu.ticket.request", side_effect=respond) as upstream:
            code, result = self.fetch("/api/tickets?q=%E8%AE%BE%E5%A4%87&ticket_status=10&offset=1&limit=1")
            self.assertEqual((code, result["total"], result["has_more"]), (200, 2, False))
            self.assertEqual([item["id"] for item in result["items"]], ["9"])
            self.assertEqual(upstream.call_count, 2)

        with patch("src.qiyu.ticket.request", return_value={"code": 14106, "message": "private"}) as upstream:
            code, result = self.fetch("/api/tickets/detail?id=12")
            self.assertEqual(code, 404)
            self.assertNotIn("private", result["error"])
            upstream.assert_called_once()
        with patch("src.qiyu.ticket.request", return_value={"code": 200, "message": {"total": 1, "tickets": []}}):
            self.assertEqual(self.fetch("/api/tickets")[0], 502)
