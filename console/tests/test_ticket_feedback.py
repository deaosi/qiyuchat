from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src import ticket_feedback
from src.config import RuntimeConfig, Settings
from src.qiyu.ticket import TicketError, TicketReplyUnconfirmed


class TestTicketFeedback(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = Settings(runtime=RuntimeConfig(db_path=str(Path(folder.name) / "qiyu.db")))
        self.values = {"status": "已修复", "reply": "检查订单\r\n已验证  ", "version": "3.4.0"}
        self.feishu_url = "https://fixture.feishu.cn/base/example?table=tblFixture"

    def ingest(self, *, ticket_id="9007199254740993", record_id="recExample1", feedback=None):
        return ticket_feedback.ingest(
            self.cfg, ticket_id=ticket_id, record_id=record_id,
            feedback=self.values if feedback is None else feedback, feishu_url=self.feishu_url,
        )

    def test_posts_normalized_feedback_once_as_a_native_reply(self):
        ticket_id = "9007199254740993"
        with patch.object(ticket_feedback.TicketClient, "post_feedback") as write, \
             patch.object(ticket_feedback.TicketClient, "has_feedback") as verify:
            first = self.ingest()
            duplicate = self.ingest(feedback={**self.values, "reply": "检查订单\n已验证"})
        write.assert_called_once()
        verify.assert_not_called()
        written_ticket, text = write.call_args.args
        self.assertEqual(written_ticket, int(ticket_id))
        self.assertTrue(text.startswith(
            "语海 Voc · 飞书技术反馈\n处理状态：已修复\n解决版本：3.4.0\n技术回复：检查订单\n已验证\n"
        ))
        self.assertRegex(text.splitlines()[-1], r"^〔语海同步 [0-9a-f]{16}〕$")
        self.assertTrue(first["changed"])
        self.assertFalse(duplicate["changed"])
        self.assertEqual(duplicate["returned_at"], first["returned_at"])
        confirmed = ticket_feedback.get(self.cfg, ticket_id)
        self.assertEqual(confirmed["writeback_status"], "succeeded")
        self.assertEqual(confirmed["reply"], "检查订单\n已验证")
        self.assertEqual(confirmed["resolved_version"], "3.4.0")
        self.assertEqual(confirmed["feishu_url"], self.feishu_url)
        self.assertGreater(confirmed["written_at"], 0)

    def test_changes_update_existing_feedback_and_reject_swapped_record_bindings(self):
        ticket_id = "9007199254740993"
        with patch.object(ticket_feedback.TicketClient, "post_feedback") as write:
            self.ingest()
            changed = self.ingest(feedback={**self.values, "reply": "需要复核"})
            confirmed = ticket_feedback.get(self.cfg, ticket_id)
            self.assertTrue(changed["changed"])
            self.assertEqual(confirmed["reply"], "需要复核")
            with self.assertRaisesRegex(ValueError, "关联已变化"):
                self.ingest(record_id="recExample2")
            with self.assertRaisesRegex(ValueError, "关联已变化"):
                self.ingest(ticket_id="75")
            self.assertEqual(write.call_count, 2)
        self.assertEqual(ticket_feedback.get(self.cfg, ticket_id), confirmed)
        self.assertIsNone(ticket_feedback.get(self.cfg, "75"))

    def test_keeps_failed_feedback_available_and_retries_the_same_revision(self):
        with patch.object(ticket_feedback.TicketClient, "post_feedback",
                          side_effect=[TicketError("七鱼暂时不可用"), True]) as write:
            failed = self.ingest()
            self.assertIsNone(failed["returned_at"])
            saved = ticket_feedback.get(self.cfg, "9007199254740993")
            self.assertEqual(saved["writeback_status"], "failed")
            self.assertEqual(saved["error"], "七鱼暂时不可用")
            self.assertEqual(saved["reply"], "检查订单\n已验证")
            self.ingest()
            self.ingest()
            self.assertEqual(write.call_count, 2)
        self.assertEqual(ticket_feedback.get(self.cfg, "9007199254740993")["writeback_status"], "succeeded")

    def test_unconfirmed_native_reply_only_reconciles_sevenfish_log(self):
        with patch.object(ticket_feedback.TicketClient, "post_feedback",
                          side_effect=TicketReplyUnconfirmed("七鱼回复尚未核对")) as write, \
             patch.object(ticket_feedback.TicketClient, "has_feedback",
                          side_effect=[False, TicketError("七鱼日志暂不可用"), True]) as verify:
            first = self.ingest()
            self.assertIsNone(first["returned_at"])
            self.assertEqual(ticket_feedback.get(self.cfg, "9007199254740993")["writeback_status"], "uncertain")
            for _ in range(2):
                still_uncertain = self.ingest()
                self.assertIsNone(still_uncertain["returned_at"])
                self.assertEqual(ticket_feedback.get(self.cfg, "9007199254740993")["writeback_status"], "uncertain")
            verified = self.ingest()
            self.assertGreater(verified["returned_at"], 0)
            self.ingest()
        write.assert_called_once()
        self.assertEqual(verify.call_count, 3)
        self.assertEqual(verify.call_args.args, write.call_args.args)
        self.assertEqual(ticket_feedback.get(self.cfg, "9007199254740993")["writeback_status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
