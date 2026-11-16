"""Verify that only the exact files attached to a feedback cell can be forwarded."""

import json
from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import patch

from deploy.feishu_sync import ticket_feedback_media as media
from src import voc_feishu_sync as sync


class TicketFeedbackMediaTests(unittest.TestCase):
    def setUp(self):
        self.target = sync.Target("lark-cli", "base_token", "tblTicket", "https://example.feishu.cn", {}, Path("lock"))
        self.files = [{"name": "复现.pdf", "file_token": "mediaExample", "size": 3}]
        self.record_id = "recTicket"
        self.field_id = "fldMedia"

    def test_revision_excludes_expiring_urls(self):
        decorated = {**self.files[0], "tmp_url": "https://invalid.example/temporary", "type": "application/pdf"}
        self.assertEqual(media.parse_cell([decorated]), self.files)

    def test_reply_limits_apply_to_downloaded_files_and_allow_larger_cell_history(self):
        crowded = [{**self.files[0], "file_token": f"mediaExample{index}"}
                   for index in range(media.MAX_REPLY_ATTACHMENTS + 1)]
        oversized = [{**self.files[0], "size": media.MAX_REPLY_ATTACHMENT_BYTES + 1}]
        for files, error in ((crowded, f"{media.MAX_REPLY_ATTACHMENTS} 个附件"), (oversized, "5 MiB")):
            with self.subTest(error=error), patch.object(media.subprocess, "run") as execute:
                self.assertEqual(media.parse_cell(files), files)
                with self.assertRaisesRegex(sync.SyncError, error) as rejected:
                    media.download(self.target, self.record_id, self.field_id, files)
                self.assertIn("复现.pdf", str(rejected.exception))
                self.assertIn(f"{files[0]['size']:,} 字节", str(rejected.exception))
                execute.assert_not_called()

    def test_only_returns_original_bytes_when_identity_and_length_match(self):
        def mock_download(args, *, cwd, **kwargs):
            output = args[args.index("--output") + 1]
            Path(cwd, output).write_bytes(content)
            response = {"ok": True, "identity": "bot", "data": {"downloaded": [{
                "record_id": self.record_id, "field_id": field, "file_token": args[args.index("--file-token") + 1],
                "name": self.files[0]["name"], "size_bytes": 3,
            }]}}
            return CompletedProcess(args, 0, json.dumps(response).encode(), b"")

        with patch.object(media.subprocess, "run", side_effect=mock_download):
            field, content = self.field_id, b"abc"
            self.assertEqual(media.download(self.target, self.record_id, self.field_id, self.files), [("复现.pdf", b"abc")])
            field = "fldOther"
            with self.assertRaisesRegex(sync.SyncError, "已变动"):
                media.download(self.target, self.record_id, self.field_id, self.files)
            field, content = self.field_id, b"ab"
            with self.assertRaisesRegex(sync.SyncError, "未完整下载"):
                media.download(self.target, self.record_id, self.field_id, self.files)
