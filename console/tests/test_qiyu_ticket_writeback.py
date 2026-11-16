from __future__ import annotations

import json
from base64 import b64decode
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from src.config import QiyuConfig
from src.qiyu import ticket


class TicketFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.client = ticket.TicketClient(QiyuConfig(app_key="test-key", app_secret="test-secret"))
        self.reply_text = "来自飞书的处理结果\n已复现，正在修复"
        self.holder = 37
        self.status = 10
        self.cross_corp = 0
        self.history = []
        self.record_reply = True
        self.lose_confirmation = False
        self.customer_see = False
        self.record_attachments = True
        self.error_code = None
        self.calls = []

    def respond(self, method, url, *, body, **kwargs):
        self.assertEqual(method, "POST")
        self.assertEqual(kwargs["retries"], 1)
        path = urlsplit(url).path
        payload = json.loads(body)
        self.calls.append((path, payload))
        if path == ticket.HISTORY:
            return {"code": 200, "data": list(self.history)}
        if path == ticket.DETAIL:
            return {"code": 200, "data": {"id": 12, "holderId": self.holder, "status": self.status,
                                          "isCrossCorp": self.cross_corp}}
        self.assertEqual(path, ticket.REPLY)
        if self.error_code is not None:
            return {"code": self.error_code}
        if self.record_reply:
            event = {"action": "回复", "actionType": 20, "customerSee": self.customer_see,
                     "info": [{"title": "", "titleLang": "", "content": payload["comment"]}]}
            if self.record_attachments and payload["attachments"]:
                event["attachments"] = [
                    {"name": item["fileName"], "url": f"https://qiyukf.net/{index:032x}",
                     "size": len(b64decode(item["payload"]))}
                    for index, item in enumerate(payload["attachments"])
                ]
            self.history.append(event)
        if self.lose_confirmation:
            raise TimeoutError("response lost")
        return {"code": 200, "message": True}

    def test_writes_native_reply_once_and_verifies_it_through_history(self):
        self.history.append({"actionType": 100, "info": [{"content": self.reply_text}]})
        with patch("src.qiyu.ticket.request", side_effect=self.respond):
            self.assertTrue(self.client.post_feedback(12, self.reply_text))
            self.assertFalse(self.client.post_feedback(12, self.reply_text))
        self.assertEqual(self.calls, [
            (ticket.HISTORY, {"ticketId": 12}),
            (ticket.DETAIL, {"ticketId": 12}),
            (ticket.REPLY, {"ticketId": 12, "staffId": 37, "comment": self.reply_text, "attachments": []}),
            (ticket.HISTORY, {"ticketId": 12}),
            (ticket.HISTORY, {"ticketId": 12}),
        ])

    def test_distinguishes_rejected_reply_from_unconfirmed_effect(self):
        self.lose_confirmation = True
        self.record_reply = False
        with patch("src.qiyu.ticket.request", side_effect=self.respond):
            with self.assertRaises(ticket.TicketReplyUnconfirmed):
                self.client.post_feedback(12, self.reply_text)
            self.assertFalse(self.client.has_feedback(12, self.reply_text))
        self.assertEqual([path for path, _ in self.calls],
                         [ticket.HISTORY, ticket.DETAIL, ticket.REPLY, ticket.HISTORY, ticket.HISTORY])
        self.error_code = "14004"
        with patch("src.qiyu.ticket.request", side_effect=self.respond):
            with self.assertRaises(ticket.TicketError) as caught:
                self.client.post_feedback(12, self.reply_text)
        self.assertNotIsInstance(caught.exception, ticket.TicketReplyUnconfirmed)
        self.assertEqual(caught.exception.upstream_code, 14004)

    def test_resolves_lost_confirmation_and_flags_customer_visible_reply(self):
        self.lose_confirmation = True
        with patch("src.qiyu.ticket.request", side_effect=self.respond):
            self.assertTrue(self.client.post_feedback(12, self.reply_text))
            self.customer_see = True
            with self.assertRaises(ticket.TicketReplyVisible):
                self.client.post_feedback(12, "新的技术回复")
            with self.assertRaises(ticket.TicketReplyVisible):
                self.client.has_feedback(12, "新的技术回复")
        self.assertEqual(sum(path == ticket.REPLY for path, _ in self.calls), 2)

    def test_returns_native_attachment_bytes_and_reconciles_the_same_reply_once(self):
        files = [("截图.png", b"image-data"), ("开发日志.txt", b"file-content")]
        with patch("src.qiyu.ticket.request", side_effect=self.respond):
            self.assertTrue(self.client.post_feedback(12, self.reply_text, attachments=files))
            self.assertFalse(self.client.post_feedback(12, self.reply_text, attachments=files))
        posted = [payload["attachments"] for path, payload in self.calls if path == ticket.REPLY]
        self.assertEqual(len(posted), 1)
        self.assertEqual([item["type"] for item in posted[0]], [1, 1])
        self.assertEqual([(item["fileName"], b64decode(item["payload"])) for item in posted[0]], files)

    def test_tagged_text_requires_exact_attachment_names_and_sizes(self):
        name = "同名截图.png"
        files = [(name, b"first image"), (name, b"second image")]
        manifest = [(filename, len(data)) for filename, data in files]
        self.record_attachments = False
        with patch("src.qiyu.ticket.request", side_effect=self.respond):
            with self.assertRaisesRegex(ticket.TicketReplyUnconfirmed, "附件仍待日志确认"):
                self.client.post_feedback(12, self.reply_text, attachments=files)
            self.history[-1]["attachments"] = [{"name": name, "url": "https://qiyukf.net/first", "size": manifest[0][1]}]
            with self.assertRaises(ticket.TicketReplyUnconfirmed):
                self.client.post_feedback(12, self.reply_text, attachments=files)
            self.history[-1]["attachments"].append({"name": name, "url": "https://qiyukf.net/second", "size": manifest[0][1]})
            self.assertTrue(self.client.has_feedback(12, self.reply_text, attachment_names=[name, name]))
            with self.assertRaises(ticket.TicketReplyUnconfirmed):
                self.client.has_feedback(12, self.reply_text, attachment_manifest=manifest)
            self.history[-1]["attachments"][1]["size"] = manifest[1][1]
            self.assertTrue(self.client.has_feedback(12, self.reply_text, attachment_manifest=manifest))
            with self.assertRaises(ticket.TicketReplyUnconfirmed):
                self.client.has_feedback(12, self.reply_text, attachment_manifest=())
        self.assertEqual(sum(path == ticket.REPLY for path, _ in self.calls), 1)

    def test_rejects_too_many_or_oversize_attachments_before_the_reply(self):
        for files in (
            [("附件.txt", b"x")] * (ticket.MAX_REPLY_ATTACHMENTS + 1),
            [("附件.txt", b"x" * (ticket.MAX_REPLY_ATTACHMENT_BYTES + 1))],
        ):
            with self.subTest(count=len(files)), patch("src.qiyu.ticket.request") as request:
                with self.assertRaises(ticket.TicketError):
                    self.client.post_feedback(12, self.reply_text, attachments=files)
                request.assert_not_called()

    def test_ambiguous_http_failure_and_rate_limit_each_send_the_reply_at_most_once(self):
        for status in (500, 429):
            with self.subTest(status=status):
                paths = []

                def respond(request, *, timeout):
                    path = urlsplit(request.full_url).path
                    paths.append(path)
                    if path == ticket.HISTORY:
                        return BytesIO(b'{"code":200,"data":[]}')
                    if path == ticket.DETAIL:
                        return BytesIO(b'{"code":200,"data":{"id":12,"holderId":37,"status":10}}')
                    self.assertEqual(path, ticket.REPLY)
                    error = HTTPError(request.full_url, status, "failure", {}, BytesIO(b""))
                    self.addCleanup(error.close)
                    raise error

                with patch("src.http.urllib.request.urlopen", side_effect=respond):
                    with self.assertRaises(ticket.TicketError) as caught:
                        self.client.post_feedback(12, self.reply_text, attachments=[("截图.png", b"image")])
                if status == 500:
                    self.assertIsInstance(caught.exception, ticket.TicketReplyUnconfirmed)
                    self.assertEqual(paths, [ticket.HISTORY, ticket.DETAIL, ticket.REPLY, ticket.HISTORY])
                else:
                    self.assertNotIsInstance(caught.exception, ticket.TicketReplyUnconfirmed)
                    self.assertEqual(caught.exception.upstream_code, 429)
                    self.assertEqual(paths, [ticket.HISTORY, ticket.DETAIL, ticket.REPLY])

    def test_refuses_closed_unassigned_or_cross_company_tickets(self):
        for status, holder, cross_corp in ((20, 37, 0), (10, 0, 0), (10, 37, 1)):
            with self.subTest(status=status, holder=holder, cross_corp=cross_corp):
                self.calls.clear()
                self.status, self.holder, self.cross_corp = status, holder, cross_corp
                with patch("src.qiyu.ticket.request", side_effect=self.respond):
                    with self.assertRaises(ticket.TicketError) as error:
                        self.client.post_feedback(12, self.reply_text)
                self.assertEqual(error.exception.http_status, 409)
                self.assertNotIn(ticket.REPLY, [path for path, _ in self.calls])
