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

    def test_detail_export_replaces_old_cache_with_actual_form_values_and_scoped_screenshot(self):
        state_dir = Path(self.cfg.runtime.state_dir)
        projected = tickets._projection(self.cfg, [{"id": "42"}])
        row = projected["42"]
        link = row["语海详情"]["link"]
        tickets.support._write_state(state_dir / "ticket-summaries.json", {
            "42": {"stamp": [row["创建时间"], row["更新时间"], row["工单标题"]],
                   "fields": {"工单描述": "旧摘要", "平台": "旧平台", "软件版本": "旧版本"}},
        })
        values = {"客户群名称": "门店答疑群", "后台负责人": "值班小陈", "平台&版本": "淘宝企业版",
                  "店铺名": "测试工具店", "软件版本": "3.1", "是否已点击报告问题": "是",
                  "问题需求": "机器人未回复。\n密码：privateSecret"}
        image = "https://shop.qiyukf.net/0123456789abcdef0123456789abcdef?signature=hidden"
        media = {"name": "现场截图.png", "url": image}
        raw = {"id": 42, "content": "原始描述", "custom": [
            *({"name": key, "value": value} for key, value in values.items()),
            {"name": "附件/截图", "value": '[{"name":"现场截图.png","url":"' + image + '"}]'},
        ], "attachments": [media]}
        logs = [
            {"id": 1, "actionType": 20, "action": "回复", "operator": "工程师", "time": 1794648432000,
             "info": [{"title": "备注", "content": "语海 Voc · 飞书技术反馈\n技术回复：请提供截图\n〔语海同步 0123456789abcdef〕"}]},
            {"id": 2, "actionType": 20, "action": "回复", "operator": "提报人员", "time": 1794648722000,
             "info": [{"title": "备注", "content": "补充了现场截图"}], "attachments": [media]},
        ]
        with (patch.object(tickets.TicketClient, "detail", return_value=raw) as fetch,
              patch.object(tickets.TicketClient, "history", return_value=logs) as history):
            result = tickets._enrich(self.cfg, projected, state_dir, incremental=True)
            self.assertEqual(result["fetched"], 1)
            fetch.assert_called_once_with(42)
            refreshed = tickets._projection(self.cfg, [{"id": "42"}])
            self.assertEqual(tickets._enrich(self.cfg, refreshed, state_dir, incremental=True)["fetched"], 0)
            self.assertEqual(refreshed, projected)
            history.assert_called_once_with(42)
        self.assertEqual(row["客户群名称"], values["客户群名称"])
        self.assertEqual(row["店铺名"], values["店铺名"])
        self.assertEqual(row["平台&版本"], values["平台&版本"])
        self.assertEqual(row["软件版本"], values["软件版本"])
        self.assertIn("机器人未回复。\n", row["问题需求"])
        self.assertNotIn("privateSecret", row["问题需求"])
        self.assertEqual(row["附件/截图"]["text"], "现场截图.png")
        self.assertTrue(row["附件/截图"]["link"].startswith(link + "/images/"))
        self.assertNotIn("signature", row["附件/截图"]["link"])
        self.assertIn("补充了现场截图", row["最新七鱼跟进"])
        self.assertLess(row["工单往来记录"].index("请提供截图"), row["工单往来记录"].index("补充了现场截图"))
        self.assertNotIn("语海同步", row["工单往来记录"])
        self.assertEqual(row["跟进附件"]["link"], "https://qiyu.fanwulink.cn/#tickets?id=42")
        legacy = tickets._summary({"id": 42, "content": "原生工单描述"}, None, source_link=link)
        self.assertEqual(legacy["问题需求"], "原生工单描述")
        self.assertEqual(legacy["客户群名称"], "")
        self.assertIsNone(legacy["附件/截图"])

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

    def test_incremental_checkpoints_do_not_drop_failed_feishu_read(self):
        from deploy.feishu_sync import support

        now = 1_794_600_000
        state = Path(self.cfg.runtime.state_dir)
        setup_path = state / "ticket-table-setup.json"
        cursor_path = state / "ticket-incremental.json"
        support._write_state(setup_path, {"table_id": self.target.table_id, "positioned_field_id": "fldSubmitter",
                                         "guest_links_migrated": True})
        before = now * 1000 - 60_000
        original = {"table_id": self.target.table_id, "schema_at": before, "qiyu_until": before,
                    "schema_version": tickets._SCHEMA_VERSION,
                    "feishu_until": before - 15_000, "reconciled_at": before, "daily_at": before,
                    "feedback_retry_at": before, "daily_feedback_at": before}
        support._write_state(cursor_path, original)
        coverage = {"created_scope": "qiyu", "updated_scope": "qiyu", "created_period": [before, now * 1000]}
        with (patch.object(tickets, "settings", return_value=self.cfg),
              patch.object(tickets.support, "target_table", return_value=self.target),
              patch.object(tickets, "time", return_value=now),
              patch.object(tickets, "_table") as table,
              patch.object(tickets, "_feedback_work", side_effect=sync.SyncError("network issue")) as receive,
              patch.object(tickets, "_incremental_projection", return_value=({}, coverage, {})) as project):
            result = tickets.run(state, incremental=True)
        self.assertEqual(result["status"], "partial")
        receive.assert_called_once()
        self.assertEqual(receive.call_args.args[-1], (before - 75_000, now * 1000 - 15_000))
        project.assert_called_once_with(self.cfg, before - 60_000, now * 1000)
        self.assertEqual(support._read_state(cursor_path),
                         {**original, "qiyu_until": now * 1000})
        table.assert_not_called()

    def test_feedback_waits_if_record_is_edited_during_scan(self):
        source = {"id": "42", "title": "工单"}
        fields = tickets._projection(self.cfg, [source])["42"]
        fields.update({"技术回复": "正在填写", tickets.ticket_reply_delta.TIME_FIELD: 101})
        self.rows["42"] = {"record_id": "recFixture42", "fields": fields}
        with (patch.object(sync, "_call", side_effect=self.fake_api),
              patch.object(tickets, "_technical_rows", return_value={"42": "recFixture42"}),
              patch.object(tickets.ticket_feedback, "ingest") as ingest):
            result = tickets._import_feedback(self.cfg, self.target, window=(0, 100))
        self.assertEqual(result["checked"], 1)
        ingest.assert_not_called()
        self.assertFalse(self.writes)

    def test_queued_export_never_overwrites_a_closed_or_newer_row(self):
        items = [{"id": "42", "updated_at": 100}, {"id": "43", "updated_at": 100}, {"id": "44", "updated_at": 100}]
        desired = tickets._projection(self.cfg, items)
        closed = {**desired["42"], "状态": "已关闭", "自动关闭时间": 200}
        self.rows["42"] = {"record_id": "recFixture42", "fields": closed}
        self.rows["43"] = {"record_id": "recFixture43", "fields": {**desired["43"], "更新时间": 101}}
        with patch.object(sync, "_call", side_effect=self.fake_api):
            selected, _ = tickets._current_export(self.target, desired)
        self.assertEqual(set(selected), {"44"})

    def test_auto_close_only_changes_status_and_close_time(self):
        archive = tickets.ticket_archive
        now = 1_795_000_000_000
        cutoff = archive.six_month_cutoff(now)
        stamps = (cutoff - 1, cutoff, cutoff - 1)
        seen = []
        for key, stamp in zip(("42", "43", "44"), stamps):
            fields = tickets._projection(self.cfg, [{"id": key, "created_at": cutoff - 10_000}])[key]
            fields.update({"状态": "处理中", tickets.ticket_reply_delta.TIME_FIELD: stamp})
            self.rows[key] = {"record_id": "recFixture" + key, "fields": fields, "last_modified_time": stamp}
        self.rows["44"]["fields"]["技术回复"] = "待回传"
        original = deepcopy(self.rows)

        def api(target, method, resource, *, params=None, data=None):
            if resource == "/records/search" and data.get("automatic_fields"):
                seen.append(params["page_size"])
                return {"items": deepcopy(list(self.rows.values())), "has_more": False}
            return self.fake_api(target, method, resource, params=params, data=data)

        with patch.object(sync, "_call", side_effect=api):
            result = archive.archive(self.target, now_ms=now, key_field="工单ID", status_field="状态",
                                     created_field="创建时间", updated_field="更新时间", source_field="语海详情",
                                     feedback_fields=("技术回复",), writeback_field="回传状态",
                                     is_pending=lambda fields: bool(fields.get("技术回复")),
                                     eligible=lambda key, fields: True)
        self.assertEqual(seen, [5])
        self.assertEqual(result["closed"], 1)
        self.assertEqual(self.rows["42"]["fields"], {**original["42"]["fields"], "状态": "已关闭",
                                                   "自动关闭时间": now})
        self.assertEqual(self.rows["43"], original["43"])
        self.assertEqual(self.rows["44"], original["44"])
