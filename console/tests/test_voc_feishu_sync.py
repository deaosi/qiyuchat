from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from src import voc_feishu_sync as sync


def document(start="2026-11-01", end="2026-11-07", detail="按步骤排查"):
    return {"title": "高频问题报告", "count": 1, "start": start, "end": end, "records": [
        {"key": "t_" + "1" * 24, "title": "主题", "summary": "咨询 3 次",
         "detail": detail, "url": "https://qiyu.fanwulink.cn/guest/evidence/" + "x" * 43},
    ]}


class TestFeishuSync(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.target = sync.Target("lark-cli", "baseFixture", "tblFixture",
                                  "https://test.feishu.cn/wiki/wikiFixture?table=tblFixture",
                                  {"key": "来源编号", "title": "标题", "detail": "内容", "source": "来源链接"},
                                  Path(folder.name) / "write.lock")
        self.rows = {}
        self.commands = []
        self.types = {"来源编号": 1, "标题": 1, "内容": 1, "来源链接": 15}
        self.fail_after_insert = False

    def fake_api(self, target, method, resource, *, params=None, data=None):
        self.assertEqual(target, self.target)
        self.commands.append((method, resource, params, deepcopy(data)))
        if resource == "/fields":
            return {"items": [{"field_name": name, "type": kind} for name, kind in self.types.items()],
                    "has_more": False}
        if resource == "/records/search":
            keys = {condition["value"][0] for condition in data["filter"]["conditions"]}
            return {"items": [deepcopy(row) for row in self.rows.values() if row["fields"]["来源编号"] in keys],
                    "has_more": False}
        if resource == "/records/batch_create":
            self.assertEqual(UUID(params["client_token"]).version, 4)
            records = []
            for incoming in data["records"]:
                rid = "recFixture" + str(len(self.rows))
                row = {"record_id": rid, "fields": deepcopy(incoming["fields"])}
                self.rows[rid] = row
                records.append(row)
            if self.fail_after_insert:
                self.fail_after_insert = False
                raise sync.SyncError("simulated timeout", 504)
            return {"records": records}
        if resource == "/records/batch_update":
            records = []
            for row in data["records"]:
                rid = row["record_id"]
                self.rows[rid]["fields"].update(row["fields"])
                records.append({"record_id": rid})
            return {"records": records}
        raise AssertionError(resource)

    def publish(self, content, filters, kind="topics"):
        with (
            patch.object(sync, "configured_target", return_value=self.target),
            patch.object(sync, "_call", side_effect=self.fake_api),
        ):
            return sync.publish(content, kind, filters)

    def test_rolling_scope_updates_one_record_and_unchanged_publish_reuses_it(self):
        scope = {"days": 7}
        first = self.publish(document(), scope)
        self.assertEqual((first["created"], first["updated"], first["count"]), (1, 0, 1))
        self.assertEqual(next(iter(self.rows.values()))["fields"]["来源链接"],
                         {"text": "查看访客页", "link": "https://qiyu.fanwulink.cn/guest/evidence/" + "x" * 43})
        second = self.publish(document("2026-11-02", "2026-11-08", "排查步骤更新"), scope)
        self.assertEqual((second["created"], second["updated"]), (0, 1))
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(next(iter(self.rows.values()))["fields"]["内容"], "排查步骤更新")
        self.assertTrue(self.publish(document("2026-11-02", "2026-11-08", "排查步骤更新"), scope)["reused"])
        new_scope = self.publish(document(), {"days": 7, "group": "另一个分组"})
        self.assertEqual(new_scope["created"], 1)

    def test_retry_after_remote_create_timeout_checks_existing_key(self):
        self.fail_after_insert = True
        with self.assertRaises(sync.SyncError) as caught:
            self.publish(document(), {"days": 7})
        self.assertEqual(caught.exception.status, 504)
        self.assertTrue(self.publish(document(), {"days": 7})["reused"])
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(sum(resource == "/records/batch_create" for _, resource, _, _ in self.commands), 1)

    def test_incompatible_fields_and_duplicate_source_keys_never_write(self):
        self.types["来源编号"] = 2
        with self.assertRaises(sync.SyncError):
            self.publish(document(), {"days": 7})
        self.assertEqual([resource for _, resource, _, _ in self.commands], ["/fields"])
        self.types["来源编号"] = 1
        self.publish(document(), {"days": 7})
        original = deepcopy(next(iter(self.rows.values())))
        original["record_id"] = "recDuplicated"
        self.rows["recDuplicated"] = original
        self.commands.clear()
        with self.assertRaises(sync.SyncError) as caught:
            self.publish(document(), {"days": 7})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual([resource for _, resource, _, _ in self.commands], ["/fields", "/records/search"])

    def test_numeric_consultations_preserve_rank_and_allow_blank_sops(self):
        self.target = replace(self.target, fields={**self.target.fields, "consultations": "咨询次数"})
        content = document()
        first = content["records"][0]
        first["key"] = "t_" + "f" * 24
        first["consultations"] = 8
        content["records"].append({**first, "key": "t_" + "0" * 24, "consultations": 3})
        content["count"] = 2
        self.types["咨询次数"] = 1
        with self.assertRaises(sync.SyncError):
            self.publish(content, {"days": 7})
        self.types["咨询次数"] = 2
        first["consultations"] = 0
        with self.assertRaises(sync.SyncError):
            self.publish(content, {"days": 7})
        self.assertFalse(self.rows)
        first["consultations"] = 8
        self.assertEqual(self.publish(content, {"days": 7})["created"], 2)
        self.assertEqual([row["fields"]["咨询次数"] for row in self.rows.values()], [8, 3])

        sop = document()
        sop["records"][0]["key"] = "sop_" + "a" * 24
        self.assertEqual(self.publish(sop, {"days": 7}, "sops")["created"], 1)
        sop_row = self.rows["recFixture2"]
        self.assertIsNone(sop_row["fields"].pop("咨询次数"))
        self.assertTrue(self.publish(sop, {"days": 7}, "sops")["reused"])

    def test_cli_json_files_bot_identity_and_error_redaction(self):
        def fake_cli(args, *, cwd, env, **kwargs):
            self.assertEqual(args[0:4], ["lark-cli", "api", "POST", self.target.api_path + "/records/search"])
            self.assertIn("--as", args)
            self.assertEqual(args[args.index("--as") + 1], "bot")
            self.assertNotIn("QIYU_ADMIN_KEY", env)
            self.assertEqual(json.loads((Path(cwd) / "body.json").read_text(encoding="utf-8")),
                             {"field_names": ["来源编号"]})
            return subprocess.CompletedProcess(args, 1, b"", json.dumps({
                "ok": False, "error": {"type": "authorization", "code": 99991672, "message": "sensitive internal detail"}
            }).encode())

        with patch.dict("os.environ", {"QIYU_ADMIN_KEY": "unused"}), patch.object(
                sync.subprocess, "run", side_effect=fake_cli):
            with self.assertRaises(sync.SyncError) as caught:
                sync._call(self.target, "POST", "/records/search", data={"field_names": ["来源编号"]})
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn("sensitive", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
