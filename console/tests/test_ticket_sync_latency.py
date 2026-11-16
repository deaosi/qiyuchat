from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.feishu_sync import ticket_reply_delta, tickets
from src import ticket_feedback, voc_feishu_sync as sync
from src.config import RuntimeConfig, Settings
from src.qiyu import ticket as qiyu


class TicketSyncLatencyTests(unittest.TestCase):
    def test_text_reply_confirms_within_a_minute_across_poll_boundary(self):
        first_poll = 1_794_798_000_000
        for edit_offset in (0, 14_999, 15_000, 15_001, 29_999):
            with self.subTest(edit_offset=edit_offset), TemporaryDirectory() as folder:
                directory = Path(folder)
                cfg = Settings(runtime=RuntimeConfig(state_dir=folder, db_path=str(directory / "voc.db")))
                target = sync.Target("lark-cli", "baseFixture", "tblFixture",
                                     "https://test.feishu.cn/base/baseFixture", tickets._FIELDS,
                                     directory / "sync.lock")
                record_id = "recFixture42"
                edited_at = first_poll + edit_offset
                fields = {
                    tickets._FIELDS["key"]: "42",
                    tickets._FIELDS["source"]: {"link": tickets.ticket_guest.url_for(cfg, "42")},
                    tickets._INPUT["reply"]: "排查完成",
                    ticket_reply_delta.TIME_FIELD: edited_at,
                }
                qiyu_replies = []
                now = first_poll

                def clock_seconds():
                    return now / 1000

                def feishu_request(_target, _method, path, *, params=None, data=None):
                    nonlocal now
                    started_at = now
                    now += 1000
                    if path == "/records/search":
                        available = started_at >= edited_at
                        rows = [{"record_id": record_id, "fields": dict(fields),
                                 "last_modified_time": fields[ticket_reply_delta.TIME_FIELD]}] if available else []
                        return {"items": rows, "has_more": False, "total": len(rows)}
                    self.assertEqual(path, "/records/batch_update")
                    for row in data["records"]:
                        self.assertEqual(row["record_id"], record_id)
                        fields.update(row["fields"])
                        fields[ticket_reply_delta.TIME_FIELD] = now
                    return {"records": [{"record_id": record_id}]}

                def qiyu_request(path, body, _result, *, before_send=None):
                    nonlocal now
                    if before_send is not None:
                        before_send()
                    now += 2000
                    if path == qiyu.HISTORY:
                        return qiyu_replies[:]
                    if path == qiyu.DETAIL:
                        return {"id": 42, "holderId": 23, "status": 10}
                    self.assertEqual(path, qiyu.REPLY)
                    qiyu_replies.append({"actionType": 20, "customerSee": False,
                                         "info": [{"content": body["comment"]}], "attachments": []})
                    return True

                queue = directory / "ticket-work.db"
                with (patch.object(sync, "_call", side_effect=feishu_request),
                      patch.object(qiyu.TicketClient, "_post", side_effect=qiyu_request),
                      patch.object(tickets, "time", side_effect=clock_seconds),
                      patch.object(ticket_feedback, "time", SimpleNamespace(time=clock_seconds))):
                    for poll in (first_poll + offset for offset in range(0, 121_000, 30_000)):
                        now = poll
                        window = (poll - 75_000, poll - 15_000)
                        tickets._feedback_work(cfg, target, queue, "feedback", poll, window, batch_state={})
                        tickets._drain_feedback(cfg, target, queue, "feedback", poll, window)

                self.assertEqual(len(qiyu_replies), 1)
                self.assertIn("技术回复：排查完成", qiyu_replies[0]["info"][0]["content"])
                self.assertEqual(fields[tickets._OUTPUT["status"]], "已回传")
                elapsed = fields[tickets._OUTPUT["returned_at"]] - edited_at
                self.assertGreaterEqual(elapsed, 15_000)
                self.assertLessEqual(elapsed, 60_000)
