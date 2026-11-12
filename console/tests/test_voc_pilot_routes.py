import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import voc_engine, voc_service


class TestPilotRoutes(unittest.TestCase):
    def test_issue_route_hides_internal_member_records(self):
        result = {"items": [{"id": "issue-a", "cases": [], "_members": [{"qa_id": 7}]}],
                  "total": 1, "coverage": {"source_events": 1}}
        with patch.object(voc_engine, "_read_issue_pilot", return_value=result):
            public = voc_service.get(object(), "issue-pilot", {"days": 7})
        self.assertEqual(public["items"], [{"id": "issue-a", "cases": []}])

    def test_first_page_preserves_scope_totals_and_normal_list_order(self):
        issues = [{"id": f"issue-{index}"} for index in range(11)]
        pilot = {"items": issues, "total": len(issues), "coverage": {"source_events": 16}}
        filters = {"days": 7}
        with patch.object(voc_engine, "issue_pilot", return_value=pilot):
            self.assertEqual(voc_service.get(object(), "issue-pilot", filters, first_page=True),
                             {**pilot, "items": issues[:6], "first_page_total": len(issues)})
            self.assertEqual(voc_service.get(object(), "issue-pilot", filters), pilot)

        for path_count in (0, 5, 9):
            with self.subTest(path_count=path_count):
                paths = [{"id": f"case-{index}"} for index in range(path_count)]
                playbooks = {"items": issues, "case_paths": paths, "total": len(issues)}
                with patch.object(voc_engine, "playbooks", return_value=playbooks):
                    page = voc_service.get(object(), "playbooks", filters, first_page=True)
                    self.assertEqual(page["case_paths"] + page["items"], (paths + issues)[:8])
                    self.assertEqual(page["first_page_total"], len(paths) + len(issues))
                    self.assertEqual(page["total"], len(issues))
                    self.assertEqual(voc_service.get(object(), "playbooks", filters), playbooks)

    def test_case_paths_can_surface_a_handling_record_before_an_issue_is_grouped(self):
        cfg = SimpleNamespace(runtime=SimpleNamespace(voc_domain="zhihuige", mock=False))
        source = {7: {"qa_id": 7}}
        example = {"id": "case-a", "qa_id": 7}
        with (patch.object(voc_engine, "_sources", return_value=source),
              patch.object(voc_engine, "_topic_view", return_value=[]),
              patch("src.voc_case_paths.build", return_value=[example]) as build):
            result = voc_service.get(cfg, "playbooks", {"days": 7})
        self.assertEqual(result, {"items": [], "total": 0, "case_paths": [example]})
        build.assert_called_once_with(cfg, source)

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
