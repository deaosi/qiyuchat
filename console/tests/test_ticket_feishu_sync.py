from copy import deepcopy
from datetime import date
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deploy.feishu_sync import tickets
from src import voc_feishu_sync as sync
from src.config import RuntimeConfig, Settings


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
        self.cfg = Settings(runtime=RuntimeConfig(db_path=str(Path(folder.name) / "test.db"),
                                                 state_dir=folder.name))

    def fake_api(self, target, method, resource, *, params=None, data=None):
        self.assertEqual(target.table_id, self.target.table_id)
        if resource == "/records/search":
            conditions = data.get("filter", {}).get("conditions", [])
            if not conditions:
                return {"items": deepcopy(list(self.rows.values())), "has_more": False}
            if conditions[0]["operator"] == "isNotEmpty":
                matching = [row for row in self.rows.values()
                            if any(row["fields"].get(column["field_name"]) for column in conditions)]
                return {"items": deepcopy(matching), "has_more": False}
            keys = {condition["value"][0] for condition in conditions}
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
            return tickets._projection(self.cfg, [item], re.compile("张客户"))

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
        self.assertTrue(tickets.ticket_guest.matches(self.cfg, "42", fields["语海详情"]["link"]))
        self.assertFalse(tickets.ticket_guest.matches(self.cfg, "43", fields["语海详情"]["link"]))
        self.assertNotIn("private", str(fields))
        self.assertEqual(self.writes, ["/records/batch_create", "/records/batch_update"])

    def test_operation_window_refreshes_older_tickets(self):
        def fetch(_cfg, **filters):
            if "start" in filters:
                self.assertEqual((filters["start"], filters["end"]), ("2026-10-16", "2026-11-14"))
                rows = [{"id": "42", "updated_at": 1_794_500_000_000, "status_code": 10, "submitter_id": 31}]
            else:
                self.assertEqual((filters["updated_start"], filters["updated_end"]), ("2026-11-08", "2026-11-14"))
                rows = [{"id": "42", "updated_at": 1_794_600_000_000, "status_code": 20, "submitter_id": 31}, {"id": "43"}]
            return {"items": rows, "total": len(rows), "has_more": False, "source": "qiyu"}

        with (patch.object(tickets, "list_tickets", side_effect=fetch),
              patch.object(tickets, "_private_names", return_value=None)):
            projected, scope, staff_ids = tickets._recent(self.cfg, date(2026, 11, 14))
        self.assertEqual(set(projected), {"42", "43"})
        self.assertEqual(projected["42"]["状态"], "已完结")
        self.assertEqual(scope["updated_scope"], "qiyu")
        self.assertEqual(staff_ids, {"42": 31, "43": None})

    def test_submitter_uses_matching_creation_log_and_caches_it(self):
        projected = tickets._projection(self.cfg, [{"id": "42"}, {"id": "43"}])
        staff_ids = {"42": 31, "43": 32}
        history = [
            {"actionType": 20, "operatorId": 31, "operator": "回复人员"},
            {"actionType": 2, "operatorId": "31", "operator": " 提报员工 "},
        ]
        state_dir = Path(self.cfg.runtime.state_dir)
        with patch.object(tickets.TicketClient, "history", return_value=history) as fetch:
            self.assertEqual(tickets._submitters(self.cfg, projected, staff_ids, state_dir),
                             {"fetched": 2, "filled": 1, "pending": 1, "deferred": 0, "unavailable": 0})
            self.assertEqual(projected["42"]["提报人"], "提报员工")
            self.assertNotIn("提报人", projected["43"])
            fetch.reset_mock()
            self.assertEqual(tickets._submitters(self.cfg, projected, staff_ids, state_dir)["fetched"], 0)
            fetch.assert_not_called()

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

    def test_imports_older_technical_reply_and_preserves_input_columns(self):
        source = {"id": "42", "title": "历史工单", "created_at": 1_734_500_000_000, "status_code": 10}
        fields = tickets._projection(self.cfg, [source])["42"]
        fields.update({"技术处理状态": "已处理", "技术回复": [{"text": "已修复，待验证"}], "解决版本": "1.6.3"})
        self.rows["42"] = {"record_id": "recFixture42", "fields": fields}
        result = {"status": "已回传", "returned_at": 1_794_600_000_000, "changed": True, "error": ""}
        cfg = self.cfg
        with (patch.object(sync, "_call", side_effect=self.fake_api),
              patch.object(tickets.ticket_feedback, "ingest", return_value=result) as ingest):
            self.assertEqual(tickets._import_feedback(cfg, self.target),
                             {"checked": 1, "imported": 1, "awaiting_review": 0, "failed": 0})
            self.assertEqual(tickets._upsert(self.target, tickets._projection(cfg, [{**source, "status_code": 20}])), (0, 1))
            result["changed"] = False
            self.assertEqual(tickets._import_feedback(cfg, self.target)["imported"], 0)
        ingest.assert_called_with(cfg, ticket_id="42", record_id="recFixture42",
                                  feedback={"status": "已处理", "reply": "已修复，待验证", "version": "1.6.3"},
                                  feishu_url=self.target.destination)
        self.assertEqual(fields["回传状态"], "已回传")
        self.assertEqual(fields["回传时间"], result["returned_at"])
        self.assertEqual(fields["技术回复"], [{"text": "已修复，待验证"}])
        self.assertEqual(fields["状态"], "已完结")
        self.assertEqual(self.writes, ["/records/batch_update", "/records/batch_update"])

    def test_blocks_feedback_if_link_points_to_different_ticket(self):
        fields = tickets._projection(self.cfg, [{"id": "42"}])["42"]
        fields["语海详情"] = {"text": "查看工单", "link": tickets.ticket_guest.url_for(self.cfg, "43")}
        fields["技术回复"] = "测试反馈"
        self.rows["42"] = {"record_id": "recFixture42", "fields": fields}
        with (patch.object(sync, "_call", side_effect=self.fake_api),
              patch.object(tickets.ticket_feedback, "ingest") as ingest):
            with self.assertRaisesRegex(sync.SyncError, "无法唯一匹配"):
                tickets._import_feedback(self.cfg, self.target)
        ingest.assert_not_called()
        self.assertEqual(self.writes, [])

    def test_migrates_historical_link_without_overwriting_technical_feedback(self):
        fields = {"工单ID": "42", "语海详情": {"text": "查看工单", "link": tickets._legacy_source_link("42")},
                  "技术回复": "需要复核"}
        self.rows["42"] = {"record_id": "recFixture42", "fields": fields}
        with patch.object(sync, "_call", side_effect=self.fake_api):
            self.assertEqual(tickets._migrate_old_links(self.cfg, self.target, {}), 1)
            self.assertEqual(tickets._migrate_old_links(self.cfg, self.target, {}), 0)
        self.assertTrue(tickets.ticket_guest.matches(self.cfg, "42", fields["语海详情"]["link"]))
        self.assertEqual(fields["技术回复"], "需要复核")

    def test_completed_schema_and_link_migrations_do_not_run_again(self):
        state = Path(self.cfg.runtime.state_dir)
        scope = {"created_scope": "qiyu", "updated_scope": "qiyu"}
        with (patch.object(tickets, "settings", return_value=self.cfg),
              patch.object(tickets.support, "target_table", return_value=self.target),
              patch.object(tickets, "_table", return_value=(self.target, "fldSubmitter")),
              patch.object(tickets, "_recent", return_value=({}, scope, {})),
              patch.object(tickets, "_import_feedback", return_value={}),
              patch.object(tickets, "_position_submitter") as position,
              patch.object(tickets, "_migrate_old_links", side_effect=[sync.SyncError("retry"), 2]) as migrate):
            with self.assertRaisesRegex(sync.SyncError, "retry"):
                tickets.run(state)
            self.assertEqual(tickets.run(state)["historical_guest_links_updated"], 2)
            self.assertEqual(tickets.run(state)["historical_guest_links_updated"], 0)
        position.assert_called_once_with(self.target)
        self.assertEqual(migrate.call_count, 2)

    def test_maintenance_skips_ticket_sync(self):
        with (patch.object(tickets, "settings", return_value=object()),
              patch.object(tickets, "maintenance", return_value=True),
              patch.object(tickets.support, "target_table") as target_table):
            self.assertEqual(tickets.run(Path(".")), {"status": "maintenance"})
        target_table.assert_not_called()
