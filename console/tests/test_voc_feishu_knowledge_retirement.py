"""Withdraw only source-invalid knowledge from a confirmed cumulative export."""

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from deploy.feishu_sync import run, support


GUIDE = "sop_" + "1" * 24
HISTORY = "sop_" + "2" * 24
TOPIC = "t_" + "3" * 24


def url(character):
    return "https://qiyu.fanwulink.cn/guest/evidence/" + character * 43


def cells(title, character):
    return {"标题": title, "详情": title + "内容", "摘要": title + "摘要", "来源": url(character)}


def record(key, fields, *, knowledge=False):
    return {"key": key, "title": fields["标题"], "detail": fields["详情"],
            "summary": fields["摘要"], "url": fields["来源"],
            **({"knowledge_source": True} if knowledge else {})}


def snapshot(*, knowledge=(), complete=True):
    guide = record(GUIDE, cells("客服来源指引", "a"), knowledge=True)
    return {
        "marker": "2026-11-24T17:00+08:00",
        "knowledge_inventory_complete": complete,
        "knowledge_keys": list(knowledge),
        "documents": {
            "topics": {"start": "2026-11-18", "end": "2026-11-24", "records": [], "count": 0, "total": 0},
            "sops": {"start": "2026-11-01", "end": "2026-11-24",
                     "records": [guide] if GUIDE in knowledge else [],
                     "count": int(GUIDE in knowledge), "total": int(GUIDE in knowledge)},
        },
    }


class KnowledgeRetirementTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "full-table.json"
        self.target = run.sync.Target(
            "lark-cli", "baseExample", "tblExample",
            "https://example.feishu.cn/base/baseExample?table=tblExample",
            {"title": "标题", "detail": "详情", "summary": "摘要", "source": "来源"},
            Path(self.temp.name) / "write.lock",
        )
        self.known = {
            GUIDE: {"id": "recGuide", "fields": cells("客服来源指引", "a")},
            HISTORY: {"id": "recHistory", "fields": cells("已登记案例SOP", "b")},
            TOPIC: {"id": "recTopic", "fields": cells("原有高频问题", "c")},
        }
        self.rows = {entry["id"]: deepcopy(entry["fields"]) for entry in self.known.values()}
        self.rows["recManual"] = {**cells("人工登记", "d"), "自定义字段": "必须保留"}
        self.initial = deepcopy(self.rows)
        support._write_state(self.path, {
            "table": self.target.table_id, "records": self.known,
            "stable_keys": True, "fifo": True, "retired": [], "knowledge_keys": [GUIDE],
        })
        self.previous = snapshot(knowledge=[GUIDE])
        self.batch_calls = []
        self.deleted = []
        self.created = 0

    def read_rows(self, _target, *, creation_times=None):
        rows = deepcopy(self.rows)
        if creation_times is not None:
            creation_times.update({key: index + 1 for index, key in enumerate(rows)})
        links = {}
        for record_id, fields in rows.items():
            links.setdefault(fields["来源"], []).append(record_id)
        return rows, links

    def batch(self, _target, operation, records):
        self.batch_calls.append((operation, deepcopy(records)))
        for item in records:
            if operation == "update":
                self.rows[item["record_id"]].update(deepcopy(item["fields"]))
            elif operation == "create":
                self.created += 1
                self.rows[f"recNew{self.created:04d}"] = deepcopy(item["fields"])
            else:
                self.fail("Unexpected operation")

    def delete(self, _target, record_ids):
        self.deleted.extend(record_ids)
        for record_id in record_ids:
            self.rows.pop(record_id)

    def synchronize(self, stage):
        with ExitStack() as stack:
            stack.enter_context(patch.object(run, "_read_rows", side_effect=self.read_rows))
            stack.enter_context(patch.object(run, "_delete", side_effect=self.delete))
            stack.enter_context(patch.object(run.sync, "_batch", side_effect=self.batch))
            return run._sync_table(self.target, self.path, stage, self.previous, display_field=None)

    def test_complete_inventory_withdraws_knowledge_and_preserves_other_rows(self):
        result = self.synchronize(snapshot())
        self.assertEqual(result["removed"], 1)
        self.assertEqual(self.deleted, ["recGuide"])
        self.assertEqual(self.rows, {key: value for key, value in self.initial.items() if key != "recGuide"})
        state = support._read_state(self.path)
        self.assertNotIn(GUIDE, state["records"])
        self.assertEqual(state["knowledge_keys"], [])
        self.assertEqual(state["retired"], [])
        self.assertFalse(self.path.with_suffix(".fifo.json").exists())
        self.assertFalse(self.path.with_suffix(".pending.json").exists())
        self.assertEqual(self.batch_calls, [])

    def test_partial_inventory_keeps_previous_knowledge_available(self):
        result = self.synchronize(snapshot(complete=False))
        self.assertEqual(result["removed"], 0)
        self.assertEqual(self.rows, self.initial)
        self.assertEqual(support._read_state(self.path)["knowledge_keys"], [GUIDE])

    def test_external_knowledge_edits_or_extra_fields_abort_before_any_write(self):
        for change in ({"详情": "人工修改后的指引"}, {"人工备注": "请保留"}):
            with self.subTest(change=change):
                self.rows = deepcopy(self.initial)
                self.rows["recGuide"].update(change)
                before = deepcopy(self.rows)
                journal = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "手动修改或额外字段"):
                    self.synchronize(snapshot())
                self.assertEqual(self.rows, before)
                self.assertEqual(self.path.read_bytes(), journal)
                self.assertEqual(self.deleted, [])
                self.assertEqual(self.batch_calls, [])

    def test_valid_source_can_return_without_retired_key_blocking_registration(self):
        self.synchronize(snapshot())
        result = self.synchronize(snapshot(knowledge=[GUIDE]))
        self.assertEqual((result["created"], result["removed"]), (1, 0))
        state = support._read_state(self.path)
        self.assertEqual(state["knowledge_keys"], [GUIDE])
        self.assertNotIn(GUIDE, state["retired"])
        self.assertNotEqual(state["records"][GUIDE]["id"], "recGuide")
        self.assertEqual(state["records"][GUIDE]["fields"], cells("客服来源指引", "a"))
        self.assertEqual(self.rows["recManual"], self.initial["recManual"])

    def test_withdrawn_knowledge_releases_capacity_without_entering_topic_retirement(self):
        with patch.object(run, "REGISTER_LIMIT", 3):
            # The original four rows must shrink to the bounded three by
            # withdrawing the guide and evicting only the oldest topic.
            # A separate restored guide consumes the newly released slot.
            replacement = snapshot()
            new_key = "sop_" + "4" * 24
            new_row = record(new_key, cells("新的来源指引", "e"), knowledge=True)
            replacement["knowledge_keys"] = [new_key]
            replacement["documents"]["sops"].update(records=[new_row], count=1, total=1)
            result = self.synchronize(replacement)
        self.assertEqual((result["removed"], result["created"]), (2, 1))
        self.assertEqual(len(self.rows), 3)
        state = support._read_state(self.path)
        self.assertEqual(state["retired"], [TOPIC])
        self.assertEqual(state["knowledge_keys"], [new_key])
        self.assertIn(HISTORY, state["records"])
        self.assertEqual(self.rows["recManual"], self.initial["recManual"])

    def test_missing_complete_manifest_fails_without_changing_rows(self):
        stage = snapshot()
        stage.pop("knowledge_keys")
        with self.assertRaisesRegex(ValueError, "完整登记清单"):
            self.synchronize(stage)
        self.assertEqual(self.rows, self.initial)
        self.assertEqual(self.batch_calls, [])
        self.assertEqual(self.deleted, [])


class KnowledgeInventoryTests(TestCase):
    def test_complete_inventory_drops_only_identified_knowledge_from_history(self):
        previous = snapshot(knowledge=[GUIDE])
        history = record(HISTORY, cells("历史案例SOP", "b"))
        previous["published_sops"] = [{"start": "2026-11-01", "end": "2026-11-24",
                                      "count": 1, "records": [history]}]
        current = snapshot()
        inventory = support._sop_inventory(previous, current)
        self.assertEqual([item["records"][0]["key"] for item in inventory], [HISTORY])
        partial = snapshot(complete=False)
        self.assertEqual({item["records"][0]["key"] for item in support._sop_inventory(previous, partial)},
                         {GUIDE, HISTORY})

    def test_reusable_knowledge_refreshes_same_key_instead_of_duplicate_inventory(self):
        previous = snapshot(knowledge=[GUIDE])
        current = snapshot(knowledge=[GUIDE])
        current["documents"]["sops"]["records"][0]["detail"] = "新的客服来源版本"
        inventory = support._sop_inventory(previous, current)
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["records"][0]["detail"], "新的客服来源版本")

