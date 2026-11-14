import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from unittest import TestCase

from deploy.feishu_sync import ticket_work


class TestTicketWork(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "queue" / "pending.db"

    def test_completing_one_batch_leaves_later_work_and_other_direction(self):
        ticket_work.enqueue(self.path, "out", {"1": {"title": "问题"}, "2": {"title": "异常"}}, now_ms=10)
        ticket_work.enqueue(self.path, "in", {"1": {"record_id": "rec100"}}, now_ms=10)

        batch = ticket_work.due(self.path, "out", now_ms=11, limit=1)
        self.assertEqual(batch, {"1": {"title": "问题"}})
        ticket_work.finish(self.path, "out", batch)

        self.assertEqual(ticket_work.count(self.path, "out"), 1)
        self.assertEqual(ticket_work.due(self.path, "out", now_ms=11, limit=1), {"2": {"title": "异常"}})
        self.assertEqual(ticket_work.due(self.path, "in", now_ms=11, limit=1), {"1": {"record_id": "rec100"}})
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_unchanged_source_replay_preserves_retry_but_new_payload_runs_now(self):
        ticket_work.enqueue(self.path, "out", {"1": {"status": "初始", "title": "问题"}}, now_ms=10)
        ticket_work.defer(self.path, "out", ["1"], until_ms=1000)
        ticket_work.enqueue(self.path, "out", {"1": {"title": "问题", "status": "初始"}}, now_ms=20)

        self.assertEqual(ticket_work.due(self.path, "out", now_ms=20, limit=1), {})
        self.assertEqual(ticket_work.next_due(self.path, "out"), 1000)

        changed = {"1": {"title": "问题", "status": "已解决"}}
        ticket_work.enqueue(self.path, "out", changed, now_ms=21, expedite_changes=False)
        self.assertEqual(ticket_work.due(self.path, "out", now_ms=1000, limit=1), changed)
        self.assertEqual(ticket_work.next_due(self.path, "out"), 1000)

        changed["1"]["status"] = "已完结"
        ticket_work.enqueue(self.path, "out", changed, now_ms=21)
        self.assertEqual(ticket_work.due(self.path, "out", now_ms=21, limit=1), changed)

    def test_interrupted_enqueue_keeps_previous_rows_and_never_commits_a_partial_batch(self):
        ticket_work.enqueue(self.path, "out", {"1": {"title": "问题"}}, now_ms=10)
        with self.assertRaises(TypeError):
            ticket_work.enqueue(self.path, "out", {"2": {}, "3": {"unserializable": object()}}, now_ms=20)
        self.assertEqual(ticket_work.due(self.path, "out", now_ms=30, limit=10), {"1": {"title": "问题"}})
