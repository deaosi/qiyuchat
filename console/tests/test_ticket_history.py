import unittest

from deploy.feishu_sync import ticket_history


_STAMP = 1_794_648_720_000
_FILE = "https://shop.qiyukf.net/0123456789abcdef0123456789abcdef?signature=private"
_C = ticket_history.COLUMNS


def _event(event_id, offset, message, *, action="回复", action_type=20, operator=37, attachments=()):
    return {"id": event_id, "action": action, "actionType": action_type, "time": _STAMP + offset,
            "operatorId": operator, "operator": "工单经办人", "info": [{"content": message}],
            "customerSee": False, "attachments": list(attachments)}


class TicketHistoryTests(unittest.TestCase):
    def test_both_directions_appear_in_order_and_latest_contains_native_reply(self):
        feishu = _event(900, 1_000, "语海 Voc · 飞书技术反馈\n处理状态：已复现\n〔语海同步 1234567890abcdef〕")
        supplemental = _event(901, 2_000, "补充信息\n具体操作如下。", attachments=[{"name": "现场.png", "url": _FILE}])
        edit = _event(902, 3_000, "补充信息写明版本号", action="修改工单回复", action_type=101)
        other_operation = _event(903, 4_000, "普通工单更新不会成为回复", action="更新工单")
        created = {"actionType": 1, "operatorId": 37}
        value = ticket_history.project("56", [supplemental, edit, created, feishu, supplemental, other_operation])
        self.assertIn("提报人补充 · 工单经办人\n补充信息\n具体操作如下。\n附件 1 个", value[_C["latest"]])
        self.assertNotIn("已复现", value[_C["latest"]])
        timeline = value[_C["history"]]
        self.assertIn("飞书 → 七鱼 · 技术反馈\n处理状态：已复现", timeline)
        self.assertIn("七鱼 → 飞书 · 修改回复", timeline)
        self.assertEqual(timeline.count("具体操作如下。"), 1)
        self.assertLess(timeline.index("已复现"), timeline.index("具体操作如下。"))
        self.assertLess(timeline.index("具体操作如下。"), timeline.index("补充信息写明版本号"))
        self.assertNotIn("普通工单更新", timeline)
        self.assertNotIn("1234567890abcdef", timeline)
        self.assertNotIn(_FILE, repr(value))
        self.assertEqual(value[_C["attachments"]]["link"], "https://qiyu.fanwulink.cn/#tickets?id=56")

    def test_sanitizes_reply_but_preserves_marker_like_text_and_html_break(self):
        event = _event(901, 0, "<p>第一行<br>第二行</p><img src='" + _FILE + "'>\n密码：secretCode"
                              "\n〔语海同步 1234567890abcdef〕")
        value = ticket_history.project("56", [event], submitter_id=37)
        self.assertIn("提报人补充", value[_C["latest"]])
        self.assertIn("第一行\n第二行", value[_C["latest"]])
        self.assertIn("〔语海同步 1234567890abcdef〕", value[_C["history"]])
        self.assertNotIn("secretCode", repr(value))
        self.assertNotIn(_FILE, repr(value))
        self.assertIn("附件 1 个", value[_C["history"]])

    def test_history_keeps_recent_entries_and_identical_replies_with_distinct_ids(self):
        events = [_event(900 + n, n * 1000, "重复回复" if n > 10 else f"旧回复{n}") for n in range(20)]
        value = ticket_history.project("56", events)
        timeline = value[_C["history"]]
        self.assertTrue(timeline.startswith("以下为最近 12 条回复"))
        self.assertNotIn("旧回复7", timeline)
        self.assertEqual(timeline.count("重复回复"), 9)
        self.assertIsNone(value[_C["attachments"]])
        self.assertEqual(ticket_history.project("56", []),
                         {_C["latest"]: "", _C["history"]: "", _C["attachments"]: None})

    def test_long_feishu_feedback_keeps_its_direction(self):
        body = "语海 Voc · 飞书技术反馈\n" + "很长" * 5000 + "\n〔语海同步 1234567890abcdef〕"
        value = ticket_history.project("56", [_event(901, 0, body)])
        self.assertEqual(value[_C["latest"]], "")
        self.assertIn("飞书 → 七鱼 · 技术反馈", value[_C["history"]])
        self.assertNotIn("1234567890abcdef", value[_C["history"]])
