"""Share ranked views only within the same scope and database revision."""

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from src import voc_service
from tests.test_local import LocalFixture


class TestWorkbenchViews(LocalFixture):
    def test_rank_reuses_aggregation_across_trends_with_fresh_status(self):
        engine = Mock()
        engine.path.return_value = Path(self.cfg.runtime.db_path).with_name("derived-test.db")
        items = [{"trend": {"status": trend}} for trend in ("new", "persistent", "persistent")]
        engine.topics.return_value = {"items": items, "total": 3, "category_total": 3, "event_count": 8}
        with (patch.object(voc_service, "_engine", return_value=engine),
              patch.object(voc_service, "_workbench_status", side_effect=[
                  {"latest_job": None}, {"latest_job": {"status": "running"}}])):
            scope = {"days": 7, "group": "测试客户群"}
            all_rows = voc_service.get(self.cfg, "topics", scope, workbench=True)
            filtered = voc_service.get(self.cfg, "topics", scope, workbench=True, trend="persistent")
        engine.topics.assert_called_once_with(self.cfg, scope, workbench=True)
        self.assertEqual(all_rows["total"], 3)
        self.assertEqual(filtered["items"], items[1:])
        self.assertEqual((filtered["total"], filtered["category_total"], filtered["event_count"]), (2, 3, 8))
        self.assertEqual(filtered["status"]["latest_job"]["status"], "running")

    def test_view_rebuilds_on_scope_and_business_or_derived_changes(self):
        derived = Path(self.cfg.runtime.db_path).with_name("derived-test.db")
        load = Mock(side_effect=lambda: {"total": load.call_count})
        scope = {"days": 7}
        with (patch.object(voc_service, "_path", return_value=derived),
              closing(sqlite3.connect(self.cfg.runtime.db_path)) as business,
              closing(sqlite3.connect(derived)) as analysis):
            for db in (business, analysis):
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS view_cache_test (marker INTEGER)")
                db.commit()
            read = lambda filters: voc_service._snapshot_view(self.cfg, "complete-sops", filters, load)
            self.assertEqual(read(scope), read(scope))
            self.assertNotEqual(read(scope), read({"days": 7, "group": "另一个群"}))
            for db in (business, analysis):
                previous = read(scope)
                db.execute("INSERT INTO view_cache_test VALUES (1)")
                db.commit()
                self.assertNotEqual(previous, read(scope))
