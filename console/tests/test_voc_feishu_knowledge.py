"""Knowledge exports retain source evidence and stable table identities."""

from copy import deepcopy
from contextlib import closing
import sqlite3
from unittest import TestCase
from unittest.mock import patch

from src import voc_engine as engine, voc_feishu_content as content
from src.web import guest_evidence as guest
from tests import test_voc_knowledge_reuse as fixtures
from tests import test_voc_feishu_sync as remote_fixtures


class KnowledgeFeishuTests(TestCase):
    setUp = fixtures.TestKnowledgeReuse.setUp
    seed = fixtures.TestKnowledgeReuse.seed
    compile = fixtures.TestKnowledgeReuse.compile
    scope = {"days": 0, "start": "2026-11-01", "end": "2026-11-24"}

    def test_real_staff_guidance_exports_full_reply_time_and_scoped_guest_context(self):
        compiled = self.compile()
        records = content._knowledge_records(self.cfg, self.scope, None)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["knowledge_source"])
        self.assertRegex(record["key"], r"^sop_[a-f0-9]{24}$")
        self.assertIn("客服来源指引", record["summary"])
        self.assertIn("效果待验证", record["summary"])
        self.assertNotIn("效果已验证", record["detail"])
        view = record["_guest_view"]
        case = view["cases"][0]
        self.assertEqual(case["anchor_id"], "source:15429:1")
        self.assertEqual(case["time"], "2026-11-07 05:20:02")
        self.assertIn(compiled["evidence"][0]["answer"], str(case["sections"]))

        document = {**self.scope, "records": [record]}
        published = content.attach_guest_links(self.cfg, document, "sops")
        token = published["records"][0]["url"].rsplit("/", 1)[-1]
        payload = guest.resolve_payload(self.cfg, token)
        self.assertEqual(payload["entry"]["kind"], "sops")
        self.assertEqual(len(payload["cases"]), 1)
        messages = payload["cases"][0]["messages"]
        self.assertTrue(any(message["role"] == "staff" and message["time"] == case["time"]
                            and compiled["evidence"][0]["answer"] in message["text"] for message in messages))
        self.assertTrue(any(message["role"] == "user" for message in messages))
        html = guest.resolve(self.cfg, token).decode()
        self.assertNotIn('href="/panel', html)
        self.assertNotIn('href="/api/', html)
        self.assertIsNone(guest.resolve_payload(self.cfg, "A" * 43))

    def test_source_revision_is_withheld_and_single_record_preview_resolves_stable_key(self):
        self.compile()
        record = content._knowledge_records(self.cfg, self.scope, None)[0]
        selected = content.build_document(self.cfg, self.scope, "sops", record["key"])
        self.assertEqual([item["key"] for item in selected["records"]], [record["key"]])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE push_sync_state SET source_version='unavailable'")
        self.assertEqual(content._knowledge_records(self.cfg, self.scope, None), [])

    def test_verified_reuse_keeps_its_observed_result_and_full_customer_followup(self):
        feedback = "我按您说的步骤做完后重新测试，已经触发了。"
        self.compile(feedback, configuration=False)
        record = content._knowledge_records(self.cfg, self.scope, None)[0]
        self.assertIn("效果已验证", record["summary"])
        published = content.attach_guest_links(self.cfg, {**self.scope, "records": [record]}, "sops")
        token = published["records"][0]["url"].rsplit("/", 1)[-1]
        messages = guest.resolve_payload(self.cfg, token)["cases"][0]["messages"]
        self.assertTrue(any(message["role"] == "user" and feedback in message["text"] for message in messages))

    def test_updated_asset_reuses_same_table_key_while_different_scope_stays_distinct(self):
        self.compile()
        source = engine.knowledge_library(self.cfg, self.scope)["items"][0]
        revised = {**deepcopy(source), "id": "knowledge_999999", "answer": "更新后的处理方法。"}
        with patch.object(engine, "knowledge_library", return_value={"items": [source]}):
            first = content._knowledge_records(self.cfg, self.scope, None)[0]
        with patch.object(engine, "knowledge_library", return_value={"items": [revised]}):
            later = content._knowledge_records(self.cfg, self.scope, None)[0]
        self.assertEqual(first["key"], later["key"])
        self.assertNotEqual(first["detail"], later["detail"])
        self.assertNotEqual(content._knowledge_key(source),
                            content._knowledge_key({**source, "variant_key": "different-applicability"}))
        remote = remote_fixtures.TestFeishuSync()
        remote.setUp()
        self.addCleanup(remote.doCleanups)
        def document(record):
            return {**self.scope, "count": 1, "title": "知识沉淀", "records": [
                {**record, "url": "https://qiyu.fanwulink.cn/guest/evidence/" + "x" * 43}]}
        self.assertEqual(remote.publish(document(first), self.scope, "sops")["created"], 1)
        result = remote.publish(document(later), self.scope, "sops")
        self.assertEqual((result["updated"], result["created"], len(remote.rows)), (1, 0, 1))
        self.assertEqual(sum(resource == "/records/batch_create" for _, resource, _, _ in remote.commands), 1)

    def test_long_original_reply_survives_guest_section_line_limits(self):
        self.compile()
        source = deepcopy(engine.knowledge_library(self.cfg, self.scope)["items"][0])
        reply = "第一段操作内容。" * 450 + "\n第二段检查内容。" * 100
        source["sources"][0]["answer"] = reply
        with patch.object(engine, "knowledge_library", return_value={"items": [source]}):
            record = content._knowledge_records(self.cfg, self.scope, None)[0]
        case = record["_guest_view"]["cases"][0]
        sections = guest._sections(case["sections"], None)
        text = "".join(sections[0]["lines"]).replace("\n", "")
        self.assertIn(reply.replace("\n", ""), text)
        self.assertTrue(all(len(line) <= 3000 for line in sections[0]["lines"]))
