import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deploy.feishu_sync import run
from src.config import Settings
from src.web import feishu_robot


class FeishuRobotTests(unittest.TestCase):
    def test_reports_verified_outcomes_and_projects_only_safe_fields(self):
        with TemporaryDirectory() as folder:
            state = Path(folder)
            now = 1_795_000_000
            private = " customer nonce abc123 "
            (state / "target-config.json").write_text(private, encoding="utf-8")
            (state / "ticket-table-setup.json").touch()
            with patch.object(feishu_robot, "STATE_DIR", state), patch.object(feishu_robot, "time", return_value=now):
                cfg = Settings()
                self.assertEqual(feishu_robot.status(cfg, include_settings=False)["status"], "unverified")
                run._record_robot(state, "tickets", {
                    "status": "published", "created": 2, "updated": 1, "destination": private,
                    "queued": {"feedback": 1, "records": 0, "customer": private},
                    "feedback": {"imported": 0, "failed": 0, "message": private},
                }, timestamp=now - 30)
                run._record_robot(state, "voc", {"status": "published", "all": {"topics": 20, "sops": 2},
                                                 "top": {"topics": 20, "url": private}}, timestamp=now - 120)
                run._record_robot(state, "voc", {"status": "already_checked", "all_topics": 20, "top_topics": 20},
                                  timestamp=now - 60)
                public = feishu_robot.status(cfg, include_settings=False)
                permanent = feishu_robot.status(cfg, include_settings=True)
                self.assertEqual(public["status"], "connected")
                self.assertNotIn("settings", public)
                self.assertNotIn(private, json.dumps(permanent))
                self.assertTrue(all(lane["target_configured"] for lane in permanent["settings"]["lanes"]))
                self.assertEqual(public["logs"][0]["lane"], "tickets")
                self.assertEqual(public["lanes"][0]["counters"][0], {"label": "新增工单", "value": 2})
                voc = public["lanes"][1]
                self.assertEqual(voc["last_success_at"], feishu_robot._iso(now - 60))
                self.assertEqual(voc["last_published_at"], feishu_robot._iso(now - 120))

    def test_bounded_and_coalesced_history_preserves_last_success_after_failures(self):
        with TemporaryDirectory() as folder:
            state = Path(folder)
            now = 1_795_000_000
            with patch.object(feishu_robot, "STATE_DIR", state), patch.object(feishu_robot, "time", return_value=now):
                cfg = Settings()
                run._record_robot(state, "tickets", {"status": "published"}, timestamp=now - 100)
                run._record_robot(state, "tickets", {"status": "published"}, timestamp=now - 80)
                self.assertEqual(len(feishu_robot.status(cfg, include_settings=False)["logs"]), 1)
                for offset in range(50, 0, -1):
                    run._record_robot(state, "tickets", {"status": "failed", "message": "secret",
                                                         "http_status": 504 if offset % 2 else 403},
                                      timestamp=now - offset)
                summary = feishu_robot.status(cfg, include_settings=False)
                self.assertEqual(summary["status"], "degraded")
                self.assertEqual(len(summary["logs"]), 24)
                self.assertEqual(summary["lanes"][0]["last_success_at"], feishu_robot._iso(now - 80))
                self.assertEqual(summary["lanes"][0]["status"], "warning")
                self.assertIn("超时", summary["logs"][0]["message"])
                self.assertNotIn("secret", json.dumps(summary))
            with patch.object(feishu_robot, "STATE_DIR", state), patch.object(feishu_robot, "time", return_value=now + 181):
                self.assertEqual(feishu_robot.status(cfg, include_settings=False)["lanes"][0]["status"], "stale")

    def test_cli_only_records_the_scheduled_sync_modes(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            args = ["sync", "--state-dir", folder]
            with patch.object(run, "run", return_value={"status": "published"}), patch("builtins.print"):
                with patch.object(run.sys, "argv", args):
                    run.main()
                health_file = feishu_robot.receipt_path(path, "voc")
                self.assertFalse(health_file.exists())
                with patch.object(run.sys, "argv", [*args, "--apply"]):
                    run.main()
                self.assertTrue(health_file.is_file())
            with (patch.object(run.sys, "argv", [*args, "--apply", "--tickets-only", "--ticket-incremental"]),
                  patch("deploy.feishu_sync.tickets.run", side_effect=run.sync.SyncError("secret", 403))):
                with self.assertRaises(SystemExit):
                    run.main()
                ticket_health = feishu_robot.receipt_path(path, "tickets").read_text(encoding="utf-8")
                self.assertIn("permission", ticket_health)
                self.assertNotIn("secret", ticket_health)
