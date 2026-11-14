from copy import deepcopy
from datetime import date
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deploy.feishu_sync import run, tickets
from src import voc_feishu_sync as sync


class TestTicketFeishuSync(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.target = sync.Target(
            "lark-cli", "baseFixture", "tblFixture", "https://test.feishu.cn/base/baseFixture?table=tblFixture",
            tickets._FIELDS, Path(folder.name) / "write.lock",
        )
        self.rows = {}
        self.writes = []
        self.fail_after_insert = False

    def fake_api(self, target, method, resource, *, params=None, data=None):
        self.assertEqual(target, self.target)
        if resource == "/records/search":
            keys = {condition["value"][0] for condition in data["filter"]["conditions"]}
            return {"items": [deepcopy(row) for key, row in self.rows.items() if key in keys], "has_more": False}
        if resource == "/records/batch_create":
            self.writes.append(resource)
            added = []
            for item in data["records"]:
                fields = item["fields"]
                key = fields["工单ID"]
                self.assertNotIn(key, self.rows)
                record = {"record_id": "recFixture" + key, "fields": deepcopy(fields)}
                self.rows[key] = record
                added.append(record)
            if self.fail_after_insert:
                raise sync.SyncError("simulated timeout", 504)
            return {"records": added}
        if resource == "/records/batch_update":
            self.writes.append(resource)
            by_record_id = {row["record_id"]: row for row in self.rows.values()}
            for item in data["records"]:
                by_record_id[item["record_id"]]["fields"].update(deepcopy(item["fields"]))
            return {"records": [{"record_id": item["record_id"]} for item in data["records"]]}
        raise AssertionError(resource)

    def test_timeout_retry_and_status_update_do_not_duplicate_rows(self):
        source = {"id": "42", "title": "张客户的打印机故障 13912345678", "owner": "private contact", "status_code": 10,
                  "created_at": 1_794_500_000_000, "linked_session_count": 0}
        def project(item):
            return tickets._projection([item], re.compile("张客户"))

        with patch.object(sync, "_call", side_effect=self.fake_api):
            self.fail_after_insert = True
            with self.assertRaises(sync.SyncError):
                tickets._upsert(self.target, project(source))
            self.fail_after_insert = False
            self.assertEqual(tickets._upsert(self.target, project(source)), (0, 0))
            self.assertEqual(tickets._upsert(self.target, project({**source, "status_code": 20})), (0, 1))
        fields = self.rows["42"]["fields"]
        self.assertEqual(fields["状态"], "已完结")
        self.assertEqual(fields["工单标题"], "[名称]的打印机故障 139****5678")
        self.assertEqual(fields["语海详情"]["link"], "https://qiyu.fanwulink.cn/#tickets?id=42")
        self.assertNotIn("private", str(fields))
        self.assertEqual(self.writes, ["/records/batch_create", "/records/batch_update"])

    def test_operation_window_refreshes_older_tickets(self):
        def fetch(_cfg, **filters):
            if "start" in filters:
                self.assertEqual((filters["start"], filters["end"]), ("2026-10-16", "2026-11-14"))
                rows = [{"id": "42", "updated_at": 1_794_500_000_000, "status_code": 10}]
            else:
                self.assertEqual((filters["updated_start"], filters["updated_end"]), ("2026-11-08", "2026-11-14"))
                rows = [{"id": "42", "updated_at": 1_794_600_000_000, "status_code": 20}, {"id": "43"}]
            return {"items": rows, "total": len(rows), "has_more": False, "source": "qiyu"}

        with (patch.object(tickets, "list_tickets", side_effect=fetch),
              patch.object(tickets, "_private_names", return_value=None)):
            projected, scope = tickets._recent(object(), date(2026, 11, 14))
        self.assertEqual(set(projected), {"42", "43"})
        self.assertEqual(projected["42"]["状态"], "已完结")
        self.assertEqual(scope["updated_scope"], "qiyu")

    def test_repeated_or_incomplete_source_pages_refuse_publication(self):
        first = {"items": [{"id": "42"}], "total": 2, "has_more": True, "source": "qiyu"}
        repeated = {**first, "has_more": False}
        with patch.object(tickets, "list_tickets", side_effect=[first, repeated]):
            with self.assertRaisesRegex(ValueError, "无法唯一匹配"):
                tickets._pages(object())
        truncated = {**first, "has_more": False}
        with patch.object(tickets, "list_tickets", return_value=truncated):
            with self.assertRaisesRegex(ValueError, "数量不一致"):
                tickets._pages(object())

    def test_main_report_failure_does_not_prevent_ticket_sync_and_maintenance_skips(self):
        with (patch.object(run, "run", side_effect=sync.SyncError("report unavailable")),
              patch.object(tickets, "run", return_value={"status": "published"}) as sync_tickets):
            result = run._run_schedule(Path("."), refresh=False)
        sync_tickets.assert_called_once()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tickets"]["status"], "published")
        with (patch.object(tickets, "settings", return_value=object()),
              patch.object(tickets, "maintenance", return_value=True),
              patch.object(tickets.support, "target_table") as target_table):
            self.assertEqual(tickets.run(Path(".")), {"status": "maintenance"})
        target_table.assert_not_called()
