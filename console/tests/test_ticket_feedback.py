from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

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

    def ingest(self, *, ticket_id="9007199254740993", record_id="recExample1", feedback=None, download=None):
        return ticket_feedback.ingest(
            self.cfg, ticket_id=ticket_id, record_id=record_id,
            feedback=self.values if feedback is None else feedback, feishu_url=self.feishu_url, download=download,
        )

    @staticmethod
    def file(token, name):
        return {"file_token": token, "name": name, "size": 3}

    @staticmethod
    def download_files(files):
        return [(item["name"], b"abc") for item in files]

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
            self.assertEqual(ticket_feedback.retry_records(self.cfg), {"9007199254740993": "recExample1"})
            saved = ticket_feedback.get(self.cfg, "9007199254740993")
            self.assertEqual(saved["writeback_status"], "failed")
            self.assertEqual(saved["error"], "七鱼暂时不可用")
            self.assertEqual(saved["reply"], "检查订单\n已验证")
            self.ingest()
            self.ingest()
            self.assertEqual(write.call_count, 2)
        self.assertEqual(ticket_feedback.get(self.cfg, "9007199254740993")["writeback_status"], "succeeded")
        self.assertEqual(ticket_feedback.retry_records(self.cfg), {})

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

    def test_attachment_only_feedback_retries_download_then_confirms_without_resending(self):
        feedback = {"attachments": [{"name": "复现.png", "file_token": "boxExample1", "size": 3}]}
        download = Mock(side_effect=[ticket_feedback.AttachmentDownloadError("附件下载失败"), [("复现.png", b"png")]])
        with (patch.object(ticket_feedback.TicketClient, "post_feedback",
                           side_effect=TicketReplyUnconfirmed("附件待确认")) as write,
              patch.object(ticket_feedback.TicketClient, "has_feedback", return_value=True) as verify):
            def submit():
                return ticket_feedback.ingest(self.cfg, ticket_id="42", record_id="recExample1",
                                              feedback=feedback, download=download)
            self.assertEqual(submit()["status"], "回传失败")
            write.assert_not_called()
            self.assertEqual(ticket_feedback.retry_records(self.cfg), {"42": "recExample1"})
            self.assertEqual(submit()["status"], "待核对七鱼回复")
            self.assertEqual(submit()["status"], "已回传")
            self.assertFalse(submit()["changed"])
        self.assertEqual(download.call_count, 2)
        write.assert_called_once()
        self.assertEqual(write.call_args.kwargs["attachments"], [("复现.png", b"png")])
        self.assertIn("请查看随回复提交的附件。", write.call_args.args[1])
        verify.assert_called_once_with(*write.call_args.args, attachment_names=("复现.png",))
        self.assertTrue(ticket_feedback.confirmed(self.cfg, "42", feedback))

    def test_only_new_attachment_tokens_are_sent_after_earlier_replies(self):
        old_image = self.file("boxOldImage", "截图.png")
        new_image = self.file("boxNewImage", "截图.png")
        new_file = self.file("boxNewText", "说明.txt")
        initial = {**self.values, "attachments": [old_image]}
        edited = {**initial, "reply": "已确认"}
        appended = {**edited, "attachments": [old_image, new_file, new_image]}
        download = Mock(side_effect=self.download_files)
        with patch.object(ticket_feedback.TicketClient, "post_feedback") as write:
            self.assertEqual(self.ingest(feedback=initial, download=download)["status"], "已回传")
            self.assertEqual(self.ingest(feedback=edited, download=download)["status"], "已回传")
            download.assert_called_once_with([old_image])
            self.assertEqual(write.call_args.kwargs, {})
            sent = self.ingest(feedback=appended, download=download)
            for attachments in ([new_image, old_image, new_file], [new_image], appended["attachments"]):
                current = {**appended, "attachments": attachments}
                self.assertEqual(self.ingest(feedback=current, download=download)["returned_at"], sent["returned_at"])
                self.assertTrue(ticket_feedback.confirmed(self.cfg, "9007199254740993", current))
        self.assertEqual(download.call_args_list, [call([old_image]), call([new_image, new_file])])
        self.assertEqual(write.call_count, 3)
        self.assertIn("技术回复：已确认", write.call_args_list[1].args[1])
        self.assertEqual(write.call_args.kwargs["attachments"], [("截图.png", b"abc"), ("说明.txt", b"abc")])

    def test_legacy_confirmed_revision_recovers_only_the_matching_attachment_subset(self):
        previous = self.file("boxOldImage", "旧截图.png")
        addition = self.file("boxNewImage", "新截图.png")
        first = {**self.values, "attachments": [previous]}
        update = {**self.values, "reply": "补充说明", "attachments": [previous, addition]}
        download = Mock(side_effect=self.download_files)
        with patch.object(ticket_feedback.TicketClient, "post_feedback") as write:
            self.ingest(feedback=first, download=download)
            with ticket_feedback.connection(ticket_feedback._path(self.cfg), write=True) as conn:
                conn.execute("DELETE FROM ticket_feedback_delivery")
                conn.execute("DELETE FROM ticket_feedback_sent_file")
            self.assertEqual(self.ingest(feedback=update, download=download)["status"], "已回传")
        self.assertEqual(download.call_args_list, [call([previous]), call([addition])])
        self.assertEqual(write.call_args.kwargs["attachments"], [("新截图.png", b"abc")])

    def test_edited_reply_waits_for_prior_attachment_to_be_confirmed(self):
        first_image = self.file("boxFirstImage", "旧截图.png")
        added_image = self.file("boxLaterImage", "补充截图.png")
        first = {**self.values, "attachments": [first_image]}
        edit = {**self.values, "reply": "已验证新截图", "attachments": [first_image, added_image]}
        download = Mock(side_effect=self.download_files)
        with (patch.object(ticket_feedback.TicketClient, "post_feedback",
                           side_effect=[TicketReplyUnconfirmed("旧截图待确认"), True]) as write,
              patch.object(ticket_feedback.TicketClient, "has_feedback", side_effect=[False, True]) as verify):
            self.assertEqual(self.ingest(feedback=first, download=download)["status"], "待核对七鱼回复")
            self.assertEqual(self.ingest(feedback=edit, download=download)["status"], "待核对七鱼回复")
            write.assert_called_once()
            download.assert_called_once_with([first_image])
            self.assertEqual(self.ingest(feedback=edit, download=download)["status"], "已回传")
        prior = call(*write.call_args_list[0].args, attachment_names=("旧截图.png",))
        self.assertEqual(verify.call_args_list, [prior, prior])
        self.assertEqual(download.call_args_list, [call([first_image]), call([added_image])])
        self.assertIn("技术回复：已验证新截图", write.call_args.args[1])
        self.assertEqual(write.call_args.kwargs["attachments"], [("补充截图.png", b"abc")])


if __name__ == "__main__":
    unittest.main()
