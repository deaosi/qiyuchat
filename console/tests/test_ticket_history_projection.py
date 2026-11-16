from unittest import TestCase

from src.web import tickets


class TicketHistoryProjectionTests(TestCase):
    def test_status_history_distinguishes_initial_state_from_later_changes(self):
        logs = [
            {"id": 11, "actionType": 1, "action": "新建工单", "time": 1794537000000,
             "info": [{"title": "工单状态", "content": "受理中"}]},
            {"id": 12, "actionType": 100, "action": "更新工单", "time": 1794537100000,
             "info": [{"titleLang": "工单状态", "content": "由受理中更改为已完结"}]},
            {"id": 13, "actionType": 25, "action": "完结", "time": 1794537200000,
             "info": [{"title": "工单状态", "content": "已完结"}, {"title": "备注", "content": "排查完成"}]},
        ]

        history = tickets.project_history(logs, lambda _: ([], []))

        self.assertEqual([event["time"] for event in history], [1794537200000, 1794537100000, 1794537000000])
        self.assertEqual([event["details"] for event in history], [
            [{"label": "当时状态", "value": "已完结"}, {"label": "备注", "value": "排查完成"}],
            [{"label": "状态变更", "value": "由受理中更改为已完结"}],
            [{"label": "新建时状态", "value": "受理中"}],
        ])

    def test_forwarded_reply_requires_a_complete_title_and_marker(self):
        body = "技术回复：检查完成"
        token = "0123456789abcdef"
        for title in ("飞书技术反馈", "语海 Voc · 飞书技术反馈"):
            with self.subTest(title=title):
                self.assertEqual(tickets.forwarded_reply_parts(f"{title}\n{body}\n〔语海同步 {token}〕"), (body, token))
                self.assertIsNone(tickets.forwarded_reply_parts(f"{title}\n{body}"))
                self.assertIsNone(tickets.forwarded_reply_parts(f"回复：{title}\n{body}\n〔语海同步 {token}〕"))

    def test_distinguishes_feishu_author_from_qiyu_reply_operator(self):
        technical_body = "技术支持排查结果：已定位旧版本\n技术回复：已验证新版本"
        feishu_reply = {
            "id": 12, "actionType": 20, "action": "回复工单", "operator": "七鱼受理客服",
            "time": 1794538000000, "info": [
                {"title": "回复", "content": f"语海 Voc · 飞书技术反馈\r\n{technical_body}\r\n〔语海同步 0123456789abcdef〕"},
            ],
            "attachments": [{"name": "截图.png"}, {"name": "排查日志.zip"}],
        }
        native_reply = {
            "id": 11, "actionType": 20, "action": "回复工单", "operator": "回复客服",
            "time": 1794537000000, "info": [{"title": "回复", "content": "已通知客户"}],
        }

        def media(items):
            return ([item for item in items if item["name"].endswith(".png")],
                    [item for item in items if item["name"].endswith(".zip")])

        history = tickets.project_history(
            [native_reply, feishu_reply], media, reply_authors={"0123456789abcdef": "实际飞书回复人"},
        )
        self.assertEqual([entry["id"] for entry in history], ["12", "11"])
        forwarded, native = history
        self.assertEqual(native["operator"], "回复客服")
        self.assertNotIn("reply_source", native)
        self.assertEqual(forwarded["operator"], "七鱼受理客服")
        self.assertEqual(forwarded["reply_source"], "feishu")
        self.assertEqual(forwarded["reply_author"], "实际飞书回复人")
        self.assertEqual(forwarded["details"], [{"label": "回复", "value": technical_body}])
        self.assertEqual(forwarded["images"], [{"name": "截图.png"}])
        self.assertEqual(forwarded["attachments"], [{"name": "排查日志.zip"}])

        without_author = tickets.project_history([feishu_reply], media)[0]
        self.assertEqual(without_author["operator"], "七鱼受理客服")
        self.assertNotIn("reply_author", without_author)
