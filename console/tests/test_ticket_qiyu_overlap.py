from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from deploy.feishu_sync import support, ticket_work, tickets
from src import voc_feishu_sync as sync
from src.config import RuntimeConfig, Settings


class TicketQiyuOverlapTests(unittest.TestCase):
    def test_dense_overlap_keeps_completed_rows_and_resumes_without_skipping_late_updates(self):
        frontier = 1_794_798_000_000
        with TemporaryDirectory() as folder:
            state_dir = Path(folder)
            target = sync.Target("lark-cli", "baseFixture", "tblFixture",
                                 "https://test.feishu.cn/base/baseFixture", tickets._FIELDS, state_dir / "sync.lock")
            cfg = Settings(runtime=RuntimeConfig(state_dir=folder, db_path=str(state_dir / "test.db")))
            state_path = state_dir / "ticket-incremental.json"
            support._write_state(state_path, {"table_id": target.table_id, "qiyu_until": frontier,
                                             "daily_at": frontier, "history_reconciled_at": frontier,
                                             "feedback_retry_at": frontier, "reconciled_at": frontier})
            first_end = frontier - 30_000
            window_results = iter((
                (frontier - 60_000, frontier + 30_000, first_end, "42"),
                (frontier + 1, frontier + 30_000, frontier + 30_000, "43"),
                (first_end + 1, frontier + 60_000, frontier + 60_000, "44"),
            ))

            def projection(_cfg, start, upper):
                expected_start, expected_upper, end, key = next(window_results)
                self.assertEqual((start, upper), (expected_start, expected_upper))
                fields = {"网易七鱼工单ID": key}
                coverage = {"created_scope": "qiyu", "updated_scope": "qiyu", "created_period": [start, end],
                            "updated_period": [start, end]}
                return {key: fields}, coverage, {key: None}

            with (patch.object(tickets, "settings", return_value=cfg),
                  patch.object(tickets, "maintenance", return_value=False),
                  patch.object(support, "target_table", return_value=target),
                  patch.object(tickets, "_configured_table", return_value=(target, {"guest_links_migrated": True})),
                  patch.object(tickets, "_feedback_work", return_value=""),
                  patch.object(tickets, "_drain_feedback", return_value={}),
                  patch.object(tickets, "_drain_exports", return_value=(0, 0, {}, {})),
                  patch.object(tickets, "_incremental_projection", side_effect=projection),
                  patch.object(tickets, "time", side_effect=((frontier + 30_000) / 1000, (frontier + 30_000) / 1000,
                                                             (frontier + 60_000) / 1000, (frontier + 60_000) / 1000))):
                tickets.run(state_dir, incremental=True)
                pending = support._read_state(state_path)
                self.assertEqual(pending["qiyu_until"], frontier + 30_000)
                self.assertEqual(pending["qiyu_replay_from"], first_end + 1)
                tickets.run(state_dir, incremental=True)

            self.assertNotIn("qiyu_replay_from", support._read_state(state_path))
            queued = ticket_work.due(state_dir / "ticket-work.db", f"records:{target.table_id}",
                                     now_ms=frontier + 60_000, limit=20)
            self.assertEqual(set(queued), {"42", "43", "44"})
