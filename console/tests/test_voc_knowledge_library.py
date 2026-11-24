from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from src import voc_engine as engine, voc_knowledge_library as library
from tests import test_voc_knowledge_reuse as fixtures


class KnowledgeLibraryTests(TestCase):
    setUp = fixtures.TestKnowledgeReuse.setUp
    seed = fixtures.TestKnowledgeReuse.seed
    compile = fixtures.TestKnowledgeReuse.compile

    def test_source_backed_guidance_is_visible_with_real_reply_anchors_without_inference(self):
        source = self.compile()
        business = Path(self.cfg.runtime.db_path)
        derived = engine.path(self.cfg)
        before = (business.read_bytes(), derived.read_bytes())
        with (patch.object(engine, "complete_sops", return_value={"items": [], "total": 0}),
              patch.object(engine.GatewayProvider, "extract", side_effect=AssertionError("reads cannot invoke models"))):
            result = engine.knowledge_library(self.cfg, {})
        self.assertEqual((business.read_bytes(), derived.read_bytes()), before)
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual((item["type"], item["status"]), ("knowledge", "guidance"))
        self.assertEqual((item["answer"], item["scope"], item["steps"]),
                         (source["answer"], source["scope"], source["steps"]))
        self.assertEqual(item["source_count"], 1)
        self.assertEqual(item["sources"][0]["answer"], source["evidence"][0]["answer"])
        ref = item["evidence"][0]
        self.assertEqual(ref["message_id"], "source:15429:2")
        self.assertGreater(ref["ts"], item["sources"][0]["asked_at"])
        self.assertEqual(ref["quote"], source["answer"])

    def test_unverified_or_changed_sources_do_not_become_published_answers(self):
        source = self.compile()
        current = engine._sources(self.cfg, {})
        for level in ("reference", "unknown"):
            with self.subTest(level=level):
                altered = {**source, "knowledge_reuse": {"level": level}}
                self.assertEqual(library.build([altered], current, [], engine._when)["total"], 0)
        for changed in ("fingerprint", "staff", "timestamp"):
            with self.subTest(changed=changed):
                altered = deepcopy(current)
                row = altered[source["evidence"][0]["qa_id"]]
                if changed == "fingerprint":
                    row["_fingerprint"] = "updated"
                elif changed == "staff":
                    row["_staff_verified"] = False
                else:
                    row["_answer_parts"][0]["ts"] = 0
                self.assertEqual(library.build([source], altered, [], engine._when)["total"], 0)

    def test_verified_sop_keeps_its_proven_branches(self):
        ref = {"qa_id": 9, "message_id": "reply-9", "quote": "保存场景后重新测试。", "ts": 123456}
        sop = {
            "id": "sop_example", "title": "场景设置", "status": "verified", "checked_cases": 1,
            "branches": [{"condition": "当前店铺场景", "condition_evidence": [ref],
                          "steps": [{"text": ref["quote"], "evidence": [ref]}],
                          "retest_evidence": [ref], "result_evidence": [ref]}],
            "evidence": [],
        }
        item = library.build([], {}, [sop], engine._when)["items"][0]
        self.assertEqual((item["type"], item["status"], item["source_count"]), ("sop", "verified", 1))
        self.assertEqual(item["branches"], sop["branches"])
        self.assertEqual(item["evidence"], [ref])
