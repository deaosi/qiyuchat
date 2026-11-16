from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import call, patch

from deploy.feishu_sync import ticket_archive, ticket_work, tickets
from src import voc_feishu_sync as sync
from src.config import RuntimeConfig, Settings


class TestTicketArchiveGuard(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = Path(folder.name)
        self.cfg = Settings(runtime=RuntimeConfig(db_path=str(state / "test.db"), state_dir=folder.name))
        self.target = sync.Target(
            "lark-cli", "baseFixture", "tblFixture", "https://test.feishu.cn/base/baseFixture?table=tblFixture",
            tickets._FIELDS, state / "write.lock",
        )
        self.queue = state / "ticket-work.db"
        self.lanes = ("feedback:tblFixture", "records:tblFixture", "details:tblFixture", "verification:tblFixture")
        self.now = 1_794_600_120_000
        self.old = ticket_archive.six_month_cutoff(self.now) - 30_000
        self.rows = {}
        self.writes = []

    def add_candidate(self, key):
        fields = tickets._projection(self.cfg, [{
            "id": key, "title": "工单", "status_code": 10, "created_at": self.old, "updated_at": self.old,
        }])[key]
        fields[tickets.ticket_reply_delta.TIME_FIELD] = self.old
        self.rows[key] = {"record_id": "recFixture" + key, "last_modified_time": self.old, "fields": fields}

    def fake_feishu(self, target, method, resource, *, params=None, data=None):
        if resource == "/records/search":
            if data.get("automatic_fields"):
                return {"items": deepcopy(list(self.rows.values())), "has_more": False}
            keys = [condition["value"][0] for condition in data["filter"]["conditions"]]
            return {"items": deepcopy([self.rows[key] for key in keys]), "has_more": False}
        if resource == "/records/batch_update":
            records = data["records"]
            self.writes.extend(deepcopy(records))
            by_record_id = {row["record_id"]: row for row in self.rows.values()}
            for record in records:
                by_record_id[record["record_id"]]["fields"].update(record["fields"])
            return {"records": [{"record_id": item["record_id"]} for item in records]}
        raise AssertionError(resource)

    def archive(self):
        return tickets._archive_inactive(self.cfg, self.target, {}, self.now, queue=self.queue, lanes=self.lanes)

    def test_queued_work_and_fresh_history_protect_old_ticket_copies(self):
        for key in ("41", "42", "43"):
            self.add_candidate(key)
        ticket_work.enqueue(self.queue, "details:tblFixture", {"41": {"fields": {}, "staff_id": None}},
                            now_ms=self.now + 60_000)

        def detail(identifier):
            return {"id": identifier, "createTime": self.old, "modifyTime": self.old}

        def history(identifier):
            return [{"time": self.now - 3_000}] if identifier == 42 else [{"time": self.old}]

        with (patch.object(sync, "_call", side_effect=self.fake_feishu),
              patch.object(tickets.TicketClient, "detail", side_effect=detail) as read_detail,
              patch.object(tickets.TicketClient, "history", side_effect=history) as read_history):
            result = self.archive()

        self.assertEqual(result["closed"], 1)
        self.assertEqual(result["inspected"], 3)
        self.assertEqual(read_detail.call_args_list, [call(42), call(43)])
        self.assertEqual(read_history.call_args_list, [call(42), call(43)])
        self.assertEqual(self.writes, [{"record_id": "recFixture43",
                                      "fields": {"状态": "已关闭", ticket_archive.CLOSED_AT: self.now}}])
        self.assertTrue(ticket_work.has_pending(self.queue, "41", self.lanes))

    def test_incomplete_history_read_leaves_candidates_open_for_retry(self):
        self.add_candidate("42")
        detail = {"id": 42, "createTime": self.old, "modifyTime": self.old}
        with (patch.object(sync, "_call", side_effect=self.fake_feishu),
              patch.object(tickets.TicketClient, "detail", return_value=detail),
              patch.object(tickets.TicketClient, "history", side_effect=tickets.TicketError("history timeout"))):
            with self.assertRaisesRegex(tickets.TicketError, "history timeout"):
                self.archive()
        self.assertFalse(self.writes)
