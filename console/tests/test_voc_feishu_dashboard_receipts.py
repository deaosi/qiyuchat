from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.feishu_sync import run
from src.web import feishu_robot


def snapshot():
    topic = {"key": "t_" + "a" * 24, "title": "同步异常", "summary": "2 次咨询", "detail": "操作指引",
             "url": "https://qiyu.fanwulink.cn/guest/evidence/" + "a" * 43, "consultations": 2}
    return {"filters": {"days": 7}, "marker": "2026-11-24T17:00+08:00",
            "documents": {"topics": {"start": "2026-11-18", "end": "2026-11-24", "count": 1, "total": 1, "records": [topic]},
                          "sops": {"start": "2026-11-07", "end": "2026-11-24", "count": 0, "total": 0, "records": []}},
            "source_metrics": {"topics": {"with_case": 1}}, "published_at": 1_795_511_000}


class DashboardReceiptsTests(unittest.TestCase):
    def exercise(self, folder, *, refresh):
        state = Path(folder)
        previous = snapshot()
        (state / "full-published.json").write_text(json.dumps(previous), encoding="utf-8")
        target = SimpleNamespace(lock_path=state / "sync.lock", destination="https://example.feishu.cn/base/example",
                                 fields={key: key for key in ("title", "detail", "summary", "source")})

        def fetch(last, apply=False, **kwargs):
            if kwargs.get("probe"):
                return {"status": "ready", "marker": previous["marker"]}
            if kwargs.get("job_only"):
                return {"job": None}
            return deepcopy(previous)

        def sync_table(target, path, stage, old, *, top=False, **kwargs):
            return {"topics": 1, "sops": 0, "updated": 1, "created": 0}

        with (patch.object(run.support, "_safe_state"), patch.object(run.support, "target_table", return_value=target),
              patch.object(run.sync, "_write_lock", side_effect=lambda path: nullcontext()),
              patch.object(run, "_ensure_updated_field"), patch.object(run, "_ensure_display_field", return_value="语海详情"),
              patch.object(run.source, "fetch", side_effect=fetch), patch.object(run.support, "_schema", return_value=target.fields),
              patch.object(run.support, "_compact", return_value=True), patch.object(run.top_table, "ensure_top", return_value=target),
              patch.object(run.support, "verify_guest_links"), patch.object(run.support, "_sop_links"),
              patch.object(run, "_sync_table", side_effect=sync_table) as writer,
              patch.object(run.dashboard, "update", side_effect=run.sync.SyncError("permission denied", 403))):
            result = run.run(state, True, refresh=refresh)
        return result, writer.call_count, json.loads((state / "full-published.json").read_text(encoding="utf-8"))

    def test_dashboard_failure_does_not_undo_confirmed_table_publication(self):
        with TemporaryDirectory() as folder:
            result, calls, saved = self.exercise(folder, refresh=True)
            self.assertEqual((result["status"], result["dashboard_status"], calls), ("published", "pending", 2))
            self.assertIn("published_sops", saved)
            self.assertFalse((Path(folder) / "full-pending.json").exists())

    def test_same_slot_retries_only_dashboard_and_receipt_keeps_real_success(self):
        with TemporaryDirectory() as folder:
            result, calls, _ = self.exercise(folder, refresh=False)
            self.assertEqual((result["status"], result["dashboard_status"], calls), ("already_checked", "pending", 0))
            state = Path(folder) / "health.json"
            now = int(feishu_robot.time())
            published = {"status": "published", "dashboard_status": "pending",
                         "all": {"topics": 2, "sops": 4}, "top": {"topics": 2}}
            first = feishu_robot.receipt(state, "voc", published, timestamp=now - 1)
            self.assertEqual(first["last_published_at"], now - 1)
            self.assertEqual(first["events"][-1]["code"], "voc_published_dashboard_pending")
            state.write_text(json.dumps(first), encoding="utf-8")
            second = feishu_robot.receipt(state, "voc", result, timestamp=now)
            self.assertEqual(second["last_success_at"], now)
            self.assertEqual(second["last_published_at"], now - 1)
            self.assertEqual(second["events"][-1]["code"], "voc_checked_dashboard_pending")


if __name__ == "__main__":
    unittest.main()
