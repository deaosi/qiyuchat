from __future__ import annotations

from dataclasses import replace
import http.client
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import re
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from src.app import get_db
from src.qiyu import ticket
from src.qiyu.sign import build_signature
from src.web import case_media, ticket_guest
from src.web.server import DashboardServer
from tests.test_local import LocalFixture


class TicketModuleTests(LocalFixture):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, qiyu=replace(self.cfg.qiyu, app_key="test-key", app_secret="test-secret"),
                           runtime=replace(self.cfg.runtime, state_dir=self.temp.name))
        self.server = DashboardServer(("127.0.0.1", 0), self.cfg, admin_key="fixture")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def fetch_raw(self, path: str, *, admin: bool = True):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            client.request("GET", path, headers={"X-Admin-Key": "fixture"} if admin else {})
            response = client.getresponse()
            return response.status, response.headers, response.read()
        finally:
            client.close()

    def fetch(self, path: str, *, admin: bool = True):
        status, _, data = self.fetch_raw(path, admin=admin)
        return status, json.loads(data)

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

    def test_submitter_uses_verified_cache_in_list_and_checks_creation_history_in_detail(self):
        cache = Path(self.temp.name) / "ticket-submitters.json"
        cache.write_text(json.dumps({
            "12": {"staff_id": 31, "name": "提交客服"},
            "13": {"staff_id": 31, "name": "无关员工"},
            "14": {"staff_id": 33, "name": "旧日志名称"},
        }), encoding="utf-8")
        rows = [
            {"id": identifier, "staffId": str(staff_id), "holderId": 91, "holderName": "受理客服", "userName": "访客"}
            for identifier, staff_id in ((12, 31), (13, 32), (14, 33), (15, 34))
        ]
        histories = {
            12: [],
            13: [{"actionType": 1, "operatorId": 32, "operator": " 新提交客服 "},
                 {"actionType": 20, "operatorId": 32, "operator": "后续回复人员"},
                 {"actionType": 1, "operatorId": 91, "operator": "受理客服"}],
            14: [{"actionType": 1, "operatorId": 33, "operator": "不同日志名称"}],
            15: [{"actionType": 1, "operatorId": 34, "operator": "冲突一"},
                 {"actionType": 2, "operatorId": 34, "operator": "冲突二"}],
        }
        with (patch.dict("os.environ", {"YUHAI_TICKET_SUBMITTERS_PATH": str(cache)}),
              patch.object(ticket.TicketClient, "search", return_value=(len(rows), rows)),
              patch.object(ticket.TicketClient, "detail", side_effect=lambda identifier: rows[identifier - 12]),
              patch.object(ticket.TicketClient, "history", side_effect=histories.get) as history):
            status, result = self.fetch("/api/tickets")
            self.assertEqual(status, 200)
            self.assertEqual([(item["submitter"], item["owner"]) for item in result["items"]],
                             [("提交客服", "受理客服"), ("", "受理客服"), ("旧日志名称", "受理客服"), ("", "受理客服")])
            history.assert_not_called()
            for identifier, name in ((12, "提交客服"), (13, "新提交客服"), (14, "不同日志名称"), (15, "")):
                with self.subTest(identifier=identifier):
                    status, detail = self.fetch(f"/api/tickets/detail?id={identifier}")
                    self.assertEqual((status, detail["ticket"]["submitter"]), (200, name))

    def test_projects_template_fields_and_only_serves_authenticated_ticket_media(self):
        image_url = "https://shop.qiyukf.net/0123456789abcdef0123456789abcdef"
        file_url = "https://qiyukf.net/f123456789abcdef0123456789abcdef"
        foreign_url = "https://shop.qiyukf.net.example.org/0123456789abcdef0123456789abcdef"
        fields = [
            {"name": "客户群名称", "value": "示例客户群"},
            {"name": "平台&版本", "value": json.dumps({"path": "示例平台/新版", "idpath": [12, 34]})},
            {"name": "手机号码", "value": "13812345678"},
            {"name": "附件/截图", "value": json.dumps([
                {"name": "截图.jpg", "response": {"url": image_url}},
                {"name": "截图.jpg", "url": image_url},
                {"name": "说明.pdf", "url": file_url},
                {"name": "假图.png", "url": foreign_url},
            ])},
        ]
        detail = {"id": 12, "title": "示例工单", "custom": fields, "isRichText": 1,
                  "content": f'<p>故障描述<img src="{image_url}" alt="截图.jpg"><script>ignore</script></p>',
                  "attachments": [{"name": "截图.jpg", "url": image_url}]}
        logs = [{"id": 44, "attachments": [{"name": "截图.jpg", "url": image_url}]}]
        with patch("src.qiyu.ticket.request", side_effect=[{"code": 200, "data": detail}, {"code": 200, "data": logs}]):
            status, projected = self.fetch("/api/tickets/detail?id=12")
        self.assertEqual(status, 200)
        ticket_detail = projected["ticket"]
        self.assertEqual(ticket_detail["description"], "故障描述")
        first, second, media = ticket_detail["fields"]
        self.assertEqual(first, {"label": "客户群名称", "value": "示例客户群"})
        self.assertEqual(second, {"label": "平台&版本", "value": "示例平台/新版"})
        self.assertEqual((media["label"], media["value"]), ("附件/截图", ""))
        self.assertEqual([image["name"] for image in media["images"]], ["截图.jpg"])
        self.assertEqual([file["name"] for file in media["attachments"]], ["说明.pdf"])
        self.assertEqual(ticket_detail["images"], media["images"])
        self.assertEqual(projected["history"][0]["images"], media["images"])
        self.assertNotIn(foreign_url, json.dumps(projected))

        class Upstream(BytesIO):
            status = 200

            def __init__(self, data, mime):
                super().__init__(data)
                self.headers = Message()
                self.headers["Content-Type"] = mime
                self.headers["Content-Length"] = str(len(data))

        png = b"\x89PNG\r\n\x1a\ncontent"
        image_link = media["images"][0]["url"]
        attachment_link = media["attachments"][0]["url"]
        with patch.object(case_media._IMAGE_OPENER, "open") as upstream:
            self.assertEqual(self.fetch(image_link, admin=False)[0], 401)
            self.assertEqual(self.fetch(image_link + "a")[0], 404)
            self.assertEqual(self.fetch("/api/tickets/media?url=" + image_url)[0], 400)
            upstream.assert_not_called()
            for recorded_type in ("image/jpeg", "application/octet-stream"):
                upstream.return_value = Upstream(png, recorded_type)
                code, headers, content = self.fetch_raw(image_link)
                self.assertEqual((code, headers.get_content_type(), content), (200, "image/png", png))
                self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
                self.assertTrue(headers["Content-Disposition"].endswith(".png"))
                self.assertEqual(upstream.call_args.args[0].full_url, image_url)
            upstream.return_value = Upstream(b"<script>ignore</script>", "text/html")
            code, headers, content = self.fetch_raw(attachment_link)
            self.assertEqual((code, headers.get_content_type()), (200, "application/octet-stream"))
            self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))
            self.assertEqual(upstream.call_args.args[0].full_url, file_url)

    def test_guest_ticket_link_requires_its_opaque_scope_and_excludes_internal_data(self):
        first = ticket_guest.url_for(self.cfg, "581000")
        second = ticket_guest.url_for(self.cfg, "581001")
        self.assertEqual(first, ticket_guest.url_for(self.cfg, "581000"))
        self.assertTrue(ticket_guest.matches(self.cfg, "581000", first))
        self.assertFalse(ticket_guest.matches(self.cfg, "581001", first))
        self.assertFalse(ticket_guest.matches(self.cfg, "581000", first + "?id=581001"))
        self.assertNotIn("581000", first)
        path, other = urlsplit(first).path, urlsplit(second).path
        shot = "https://shop.qiyukf.net/0123456789abcdef0123456789abcdef?signature=secret"
        detail = {"id": 581000, "title": "隐私商家名下单失败", "status": 10, "priority": 8,
                  "holderName": "技术内部人员", "content": "隐私商家名 设备报错 13812345678 http://example.org/key 工单号 581000",
                  "custom": [
                      {"name": "店铺名", "value": "隐私商家名"},
                      {"name": "平台&版本", "value": "抖音"},
                      {"name": "附件/截图", "value": json.dumps([{"name": "隐私图片名.png", "url": shot},
                          {"name": "私有原文件.pdf", "url": "https://shop.qiyukf.net/1123456789abcdef0123456789abcdef"}])},
                      {"name": "技术回复", "value": "内网答复"},
                      {"name": ["malformed"], "value": "ignored"},
                  ],
                  "attachments": [{"name": "internal-log.png", "url": "https://shop.qiyukf.net/2123456789abcdef0123456789abcdef"}]}
        static = Path(self.temp.name) / "static"
        (static / "guest").mkdir(parents=True)
        (static / "guest" / "index.html").write_bytes(b"<html><!--VOC_GUEST_DATA--></html>")

        def respond(method, target, *, body, **kwargs):
            self.assertEqual(urlsplit(target).path, ticket.DETAIL)
            number = json.loads(body)["ticketId"]
            row = detail if number == 581000 else {"id": number, "title": "其他工单"}
            return {"code": 200, "data": row}

        with (patch("src.web.server.STATIC", static),
              patch("src.qiyu.ticket.request", side_effect=respond) as upstream,
              patch.object(case_media, "fetch_bytes", return_value=(b"\x89PNG\r\n\x1a\ncontent", "image/png")) as media):
            self.assertEqual(self.fetch_raw("/guest/ticket/" + "0" * 43, admin=False)[0], 404)
            self.assertEqual(self.fetch_raw(path + "?id=581001", admin=False)[0], 404)
            self.assertEqual(self.fetch("/api/tickets/detail?id=581000", admin=False)[0], 401)
            upstream.assert_not_called()

            status, headers, body = self.fetch_raw(path, admin=False)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("noindex", headers["X-Robots-Tag"])
            payload = json.loads(re.search(rb'<script[^>]+>(.+?)</script>', body).group(1))
            self.assertEqual(payload["kind"], "ticket")
            self.assertEqual(payload["ticket"]["priority"], "紧急")
            self.assertEqual(payload["ticket"]["fields"][0], {"label": "平台&版本", "value": "抖音"})
            raw_data = body.decode("utf-8")
            for private in ("581000", "隐私商家名", "技术内部人员", "内网答复", "13812345678", "example.org",
                            "私有原文件", "隐私图片名", "signature=secret", "internal-log"):
                self.assertNotIn(private, raw_data)
            asset = payload["ticket"]["fields"][1]["images"][0]["url"]
            self.assertTrue(asset.startswith(path + "/images/"))
            status, headers, body = self.fetch_raw(asset, admin=False)
            self.assertEqual((status, headers.get_content_type()), (200, "image/png"))
            self.assertTrue(body.startswith(b"\x89PNG"))
            self.assertEqual(upstream.call_count, 1)
            self.assertEqual(self.fetch_raw(asset + "?url=https://example.org/evil.png", admin=False)[0], 404)
            self.assertEqual(self.fetch_raw(asset.replace(path, other), admin=False)[0], 404)
            media.assert_called_once_with(shot, image_only=True, ticket_attachment=True)

    def test_guest_ticket_keeps_readable_overlapping_public_values(self):
        detail = {
            "id": 581010,
            "title": "测试1",
            "userName": "实际访客名称",
            "holderName": "否",
            "content": ("实际访客名称反馈测试1失败 联系人 user-private@example.org 13812345678 "
                        "token=private-token Authorization: Bearer private.bearer.value"),
            "custom": [
                {"name": "店铺名", "value": "测试1"},
                {"name": "客户群名称", "value": "测试1"},
                {"name": "后台负责人", "value": "测试1"},
                {"name": "商家名称", "value": "v1.2.3"},
                {"name": "平台&版本", "value": "测试1"},
                {"name": "软件版本", "value": "v1.2.3"},
                {"name": "是否已点击报告问题", "value": "否"},
                {"name": "问题需求", "value": "测试1失败"},
            ],
        }
        payload, _ = ticket_guest._project(detail, "A" * 43)
        guest = payload["ticket"]
        self.assertEqual(guest["title"], "测试1")
        self.assertIn("反馈测试1失败", guest["description"])
        self.assertEqual({field["label"]: field["value"] for field in guest["fields"]}, {
            "平台&版本": "测试1", "软件版本": "v1.2.3",
            "是否已点击报告问题": "否", "问题需求": "测试1失败",
        })
        for private in ("实际访客名称", "user-private@example.org", "13812345678",
                        "private-token", "private.bearer.value", "581010"):
            self.assertNotIn(private, json.dumps(payload, ensure_ascii=False))

    def test_guest_ticket_without_identity_does_not_redact_empty_matches(self):
        payload, _ = ticket_guest._project({"title": "打印故障", "content": "请检查打印设置"}, "A" * 43)
        self.assertEqual(payload["ticket"]["title"], "打印故障")
        self.assertEqual(payload["ticket"]["description"], "请检查打印设置")
