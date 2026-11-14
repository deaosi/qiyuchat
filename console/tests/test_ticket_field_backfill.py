from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from deploy.feishu_sync import ticket_field_backfill as backfill
from deploy.feishu_sync import tickets
from src import voc_feishu_sync as sync
from src.config import RuntimeConfig, Settings
from src.qiyu.ticket import TicketError


class TicketFieldBackfillTests(TestCase):
    def test_backfill_checks_source_and_archive_then_updates_existing_detail_cells(self):
        with TemporaryDirectory() as directory:
            state = Path(directory)
            cfg = Settings(runtime=RuntimeConfig(db_path=str(state / "test.db"), state_dir=directory, mock=False))
            report = sync.Target("lark-cli", "baseFixture", "tblReports",
                                 "https://example.test/base/baseFixture?table=tblReports", {}, state / "write.lock")
            links = {key: tickets.ticket_guest.url_for(cfg, key) for key in ("10", "11", "12", "13", "14")}
            summary = tickets._DETAIL["summary"]
            rows = {
                key: {"record_id": "recFixture" + key, "fields": {
                    "工单ID": key, "语海详情": {"text": "查看工单", "link": links["10" if key == "11" else key]},
                    "技术回复": "保留反馈", "状态": "处理中",
                }}
                for key in links
            }
            snapshots = deepcopy(list(rows.values()))
            rows["12"]["fields"]["自动关闭时间"] = 1794537000000
            writes = []

            def feishu(target, method, resource, *, params=None, data=None):
                self.assertEqual(target.table_id, "tblTickets")
                self.assertEqual(method, "POST")
                if resource == "/records/search":
                    conditions = data["filter"]["conditions"]
                    if conditions[0]["operator"] == "isEmpty":
                        self.assertEqual(params, {"page_size": 5, "page_token": "previous"})
                        return {"items": snapshots, "has_more": True, "page_token": "following"}
                    return {"items": [deepcopy(rows[rule["value"][0]]) for rule in conditions], "has_more": False}
                self.assertEqual(resource, "/records/batch_update")
                self.assertEqual(data["records"], [{"record_id": "recFixture10", "fields": {summary: "提报原文"}}])
                writes.extend(data["records"])
                rows["10"]["fields"].update(data["records"][0]["fields"])
                return {"records": [{"record_id": "recFixture10"}]}

            def detail(key):
                if key == 13:
                    raise TicketError("could not read", http_status=404)
                if key == 14:
                    raise TicketError("retry", http_status=503)
                return {"id": key}

            def summarize(raw, _names, *, source_link):
                self.assertEqual(source_link, links[str(raw["id"])])
                return {summary: "提报原文"}

            with (patch.object(tickets, "settings", return_value=cfg),
                  patch.object(tickets, "maintenance", return_value=False),
                  patch.object(tickets.support, "target_table", return_value=report),
                  patch.object(tickets.top, "_find_table", return_value="tblTickets"),
                  patch.object(tickets, "_summary", side_effect=summarize),
                  patch.object(backfill.TicketClient, "detail", side_effect=detail) as fetch,
                  patch.object(sync, "_call", side_effect=feishu)):
                first = backfill.run(state, limit=5, after="previous")
                retry = backfill.run(state, limit=5, after=first["next_cursor"])
            self.assertEqual([call.args[0] for call in fetch.call_args_list], [10, 12, 13, 14] * 2)
            self.assertEqual(len(writes), 1)
            self.assertEqual((first["updated"], first["skipped"], first["missing"], first["failed"]), (1, 2, 1, 1))
            self.assertTrue(first["retry"])
            self.assertEqual((retry["next_cursor"], retry["updated"], retry["unchanged"]), ("previous", 0, 1))
            for key, row in rows.items():
                cells = row["fields"]
                self.assertEqual(cells["技术回复"], "保留反馈")
                self.assertEqual(cells["状态"], "处理中")
                if key != "10":
                    self.assertNotIn(summary, cells)
