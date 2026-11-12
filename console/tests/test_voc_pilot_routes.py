import unittest
from unittest.mock import patch

from src import voc_engine, voc_service


class TestPilotRoutes(unittest.TestCase):
    def test_issue_route_hides_internal_member_records(self):
        result = {"items": [{"id": "issue-a", "cases": [], "_members": [{"qa_id": 7}]}],
                  "total": 1, "coverage": {"source_events": 1}}
        with patch.object(voc_engine, "_read_issue_pilot", return_value=result):
            public = voc_service.get(object(), "issue-pilot", {"days": 7})
        self.assertEqual(public["items"], [{"id": "issue-a", "cases": []}])

    def test_date_filter_cannot_hide_counterevidence_or_show_out_of_scope_procedure(self):
        scoped = {"items": [{"id": "book-a", "status": "verified", "missing": [], "branches": [
            {"status": "verified", "condition": "同一功能", "steps": [{"text": "甲方法"}], "retest": "核对复测"},
        ]}], "total": 1}
        global_result = {"items": [{"id": "book-a", "status": "verified", "missing": ["conflicting_case"], "branches": [
            {"status": "verified", "condition": "同一功能", "steps": [{"text": "乙方法"}], "retest": "核对复测"},
        ]}], "total": 1}
        [item] = voc_engine._recheck_playbook_branches(scoped, global_result)["items"]
        self.assertEqual(item["branches"], [])
        self.assertEqual(item["status"], "investigation")
        self.assertIn("conflicting_case", item["missing"])
        self.assertTrue(scoped["items"][0]["branches"])
