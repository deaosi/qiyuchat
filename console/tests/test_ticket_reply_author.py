"""Check that reply authors are tied to the exact Feishu input changes."""

import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.feishu_sync import ticket_reply_author as authors, tickets


NOW = 1794796610000
REPLY = "技术回复"
STATUS = "处理状态"
INVESTIGATION = "技术支持排查结果"
RECORD_ID = "recExample"
OLD_FILE = {"name": "已回传.png", "file_token": "oldExample", "size": 10}
NEW_FILE = {"name": "复现.zip", "file_token": "newExample", "size": 30}


def edit(field, before, after):
    return {"field_id": field, "before": before, "after": after}


def event(revision, operator, *changes, offset=0):
    return {"rev": revision, "operator": operator, "create_time": NOW // 1000 + offset,
            "activity_type": "update", "field_changes": list(changes)}


def page(*items, next_cursor=None):
    payload = {"ok": True, "identity": "bot",
               "data": {"items": items, "has_more": next_cursor is not None, "next_max_version": next_cursor}}
    return subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b"")


class TicketReplyAuthorTests(unittest.TestCase):
    def setUp(self):
        self.target = SimpleNamespace(cli="lark-cli", base_token="base", table_id="tblTicket",
                                      approved={REPLY: ("fldReply", 1), STATUS: ("fldStatus", 3)},
                                      investigation_field=INVESTIGATION, investigation_id="fldInvestigation",
                                      attachment_id="fldAttachment")

    def author(self, text=None, files=None):
        return authors.resolve(self.target, RECORD_ID, changed_text=text or {}, files=files or [],
                               at_ms=NOW, normalize_reply=tickets._reply_cell)

    def test_matches_bound_text_even_if_row_was_later_edited_by_other_actor(self):
        history = page(
            event(7, "后来回复人", edit("fldReply", "排查完成", "再次回复"), offset=1),
            event(6, "飞书 CLI", edit("fldOutput", "", "排查完成")),
            event(5, "技术同事", edit("fldReply", "", [{"type": "text", "text": " 排查完成\r\n"}])),
        )
        with patch.object(authors.subprocess, "run", return_value=history) as run:
            self.assertEqual(self.author({REPLY: "排查完成"}), "技术同事")
        args = run.call_args.args[0]
        self.assertIn("+record-history-list", args)
        self.assertEqual(args[args.index("--record-id") + 1], RECORD_ID)
        self.assertEqual(args[args.index("--as") + 1], "bot")

    def test_does_not_reuse_author_from_older_value(self):
        history = page(
            event(5, "最新编辑者", edit("fldReply", "旧内容", "新内容")),
            event(4, "旧内容作者", edit("fldReply", "", "旧内容")),
        )
        with patch.object(authors.subprocess, "run", return_value=history):
            self.assertEqual(self.author({REPLY: "旧内容"}), "")

    def test_finds_new_upload_through_later_attachment_cell_edit(self):
        history = page(
            event(5, "整理旧附件", edit("fldAttachment", [OLD_FILE, NEW_FILE], [NEW_FILE])),
            event(4, "附件上传人", edit("fldAttachment", [OLD_FILE], [OLD_FILE, NEW_FILE])),
        )
        with patch.object(authors.subprocess, "run", return_value=history):
            self.assertEqual(self.author(files=[NEW_FILE]), "附件上传人")
            self.assertEqual(self.author(files=[{**NEW_FILE, "size": 40}]), "")
            self.assertEqual(self.author(files=[OLD_FILE]), "")

    def test_mixed_operators_and_opaque_operator_do_not_claim_one_author(self):
        history = page(
            event(5, "回复人", edit("fldReply", "", "已复现")),
            event(4, "附件上传人", edit("fldAttachment", [], [NEW_FILE])),
            event(3, "ou_y7PYQ", edit("fldStatus", "", "处理中")),
        )
        with patch.object(authors.subprocess, "run", return_value=history):
            self.assertEqual(self.author({REPLY: "已复现"}, [NEW_FILE]), "")
            self.assertEqual(self.author({STATUS: "处理中"}), "")

    def test_supports_read_only_field_and_bounded_pagination(self):
        with patch.object(authors.subprocess, "run", side_effect=[
            page(event(5, "飞书 CLI", edit("fldOutput", "", "已定位")), next_cursor=5),
            page(event(4, "技术同事", edit("fldInvestigation", "", "已定位"))),
        ]) as run:
            self.assertEqual(self.author({INVESTIGATION: "已定位"}), "技术同事")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][-2:], ["--max-version", "5"])

    def test_unbound_fields_and_unavailable_history_supply_no_author(self):
        with patch.object(authors.subprocess, "run", side_effect=subprocess.TimeoutExpired("cli", 8)) as run:
            self.assertEqual(self.author({"未绑定的回复": "已复现"}), "")
            run.assert_not_called()
            self.assertEqual(self.author({REPLY: "已复现"}), "")
            run.assert_called_once()
