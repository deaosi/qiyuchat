"""Scheduled exports retain cumulative knowledge across rolling topic windows."""

from contextlib import ExitStack, closing
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from deploy.feishu_sync import run, snapshot, support
from src import voc_engine, voc_feishu_content as content
from src.web import guest_evidence as guest


GUIDE = "sop_" + "1" * 24
TOPIC = "t_" + "2" * 24
TOPIC_SCOPE = {"days": 0, "start": "2026-11-18", "end": "2026-11-24"}
KNOWLEDGE_SCOPE = {**TOPIC_SCOPE, "start": "2026-11-01"}


def link(character):
    return "https://qiyu.fanwulink.cn/guest/evidence/" + character * 43


def guide():
    return {"key": GUIDE, "title": "客服来源指引", "summary": "适用条件内的操作说明",
            "detail": "完整处理步骤", "url": link("a"), "knowledge_source": True}


def topic():
    return {"key": TOPIC, "title": "高频问题", "summary": "3 次咨询", "detail": "实际问法",
            "url": link("b"), "consultations": 3}


def stage(*, topics=False, knowledge=True):
    topic_rows = [topic()] if topics else []
    knowledge_rows = [guide()] if knowledge else []
    return {
        "filters": {"days": 7}, "marker": "2026-11-24T17:00+08:00",
        "knowledge_inventory_complete": True, "knowledge_keys": [GUIDE] if knowledge else [],
        "documents": {
            "topics": {"start": TOPIC_SCOPE["start"], "end": TOPIC_SCOPE["end"],
                       "records": topic_rows, "count": len(topic_rows), "total": len(topic_rows)},
            "sops": {"start": KNOWLEDGE_SCOPE["start"], "end": KNOWLEDGE_SCOPE["end"],
                     "records": knowledge_rows, "count": len(knowledge_rows), "total": len(knowledge_rows)},
        },
        "source_metrics": {"topics": {"with_case": len(topic_rows)}, "sops": {"with_case": len(knowledge_rows)}},
    }


class ScheduledKnowledgeScopeTests(TestCase):
    def test_empty_source_still_emits_complete_manifest_for_last_knowledge_withdrawal(self):
        with TemporaryDirectory() as folder:
            cfg = SimpleNamespace(runtime=SimpleNamespace(mock=False, db_path=str(Path(folder) / "source.db")))
            registry = Path(folder) / "guest.db"
            request = {"apply": False, "probe": False, "marker": "", "links": {"topics": {}, "sops": {}}}
            with (
                patch("src.config.settings", return_value=cfg),
                patch("src.voc_service.maintenance", return_value=False),
                patch.object(snapshot, "_knowledge_scope", side_effect=lambda _cfg, scope: scope),
                patch.object(snapshot, "records_in_scope", return_value={"topics": ([], 0), "sops": ([], 0)}),
                patch.object(guest, "_registry", return_value=registry),
            ):
                result = snapshot.build_snapshot(request)
            self.assertNotIn("status", result)
            self.assertIs(result["knowledge_inventory_complete"], True)
            self.assertEqual(result["knowledge_keys"], [])
            self.assertEqual(result["documents"]["sops"]["count"], 0)
            self.assertFalse(registry.exists())

    def test_earliest_retained_source_sets_cumulative_knowledge_scope(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "source.db"
            cfg = SimpleNamespace(runtime=SimpleNamespace(db_path=str(path)))
            for unit in ("seconds", "milliseconds"):
                with self.subTest(unit=unit):
                    when = datetime(2026, 11, 1, 10, tzinfo=snapshot.ZONE).timestamp()
                    value = int(when * (1000 if unit == "milliseconds" else 1))
                    with closing(sqlite3.connect(path)) as db:
                        db.execute("CREATE TABLE IF NOT EXISTS qa_pair(asked_at INTEGER)")
                        db.execute("DELETE FROM qa_pair")
                        db.executemany("INSERT INTO qa_pair VALUES(?)", [(0,), (-1,), (value,)])
                        db.commit()
                    original = deepcopy(TOPIC_SCOPE)
                    self.assertEqual(snapshot._knowledge_scope(cfg, TOPIC_SCOPE), KNOWLEDGE_SCOPE)
                    self.assertEqual(TOPIC_SCOPE, original)

    def test_scope_probe_never_creates_a_missing_source_database(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "not-created.db"
            cfg = SimpleNamespace(runtime=SimpleNamespace(db_path=str(path)))
            self.assertEqual(snapshot._knowledge_scope(cfg, TOPIC_SCOPE), TOPIC_SCOPE)
            self.assertFalse(path.exists())

    def test_topics_and_full_knowledge_are_read_with_separate_scopes(self):
        cfg = object()
        many_guides = [{**guide(), "key": f"sop_{index:024x}"} for index in range(30)]
        original_limit = content._LIMIT
        original_rank = voc_engine.feishu_topics
        with (
            patch.object(content, "_private_names", return_value=None),
            patch.object(content, "_topic_records", return_value=([], "empty ranking")) as topics,
            patch.object(content, "_sop_records", return_value=(many_guides, "knowledge")) as knowledge,
        ):
            records = snapshot.records_in_scope(cfg, TOPIC_SCOPE, knowledge_scope=KNOWLEDGE_SCOPE)
        topics.assert_called_once_with(cfg, TOPIC_SCOPE, None)
        knowledge.assert_called_once_with(cfg, KNOWLEDGE_SCOPE, "", None)
        self.assertEqual(records["sops"], (many_guides, 30))
        self.assertEqual(records["topics"], ([], 0))
        self.assertEqual(content._LIMIT, original_limit)
        self.assertIs(voc_engine.feishu_topics, original_rank)

    def test_prepare_pins_each_guest_entry_to_its_own_publication_period(self):
        cfg = object()
        ranking = {**topic(), "_guest_view": {}, "_guest_anchors": [],
                   "register_key": TOPIC, "member_keys": [TOPIC]}
        knowledge = {**guide(), "_guest_view": {}, "_guest_anchors": []}
        request = {"apply": False, "links": {"topics": {}, "sops": {}}}
        payload = {"cases": [{"title": "原始案例"}], "topic_variants": []}
        with patch.object(guest, "_make_payload", return_value=payload) as make:
            documents, prepared, metrics = snapshot.prepare(
                cfg, {"topics": ([ranking], 1), "sops": ([knowledge], 1)}, TOPIC_SCOPE,
                None, 1_795_500_000, request, knowledge_scope=KNOWLEDGE_SCOPE,
            )
        entries = {call.args[3]["kind"]: call.args[3] for call in make.call_args_list}
        self.assertEqual((entries["topics"]["start"], entries["topics"]["end"]),
                         (TOPIC_SCOPE["start"], TOPIC_SCOPE["end"]))
        self.assertEqual((entries["sops"]["start"], entries["sops"]["end"]),
                         (KNOWLEDGE_SCOPE["start"], KNOWLEDGE_SCOPE["end"]))
        self.assertEqual(documents["sops"]["start"], KNOWLEDGE_SCOPE["start"])
        self.assertIs(documents["sops"]["records"][0]["knowledge_source"], True)
        self.assertEqual(prepared, [])
        self.assertEqual(metrics["topics"]["with_case"], 1)
        self.assertEqual(metrics["sops"]["with_case"], 1)


class ScheduledManifestTests(TestCase):
    def test_knowledge_only_publication_passes_with_full_source_manifest(self):
        value = stage()
        self.assertIs(run._validate(value), value)

    def test_incomplete_or_contradictory_manifest_is_rejected(self):
        variants = []
        for keys in ([], [GUIDE, GUIDE], ["sop_" + "3" * 24], [TOPIC]):
            value = stage()
            value["knowledge_keys"] = keys
            variants.append(value)
        value = stage()
        value["knowledge_inventory_complete"] = False
        variants.append(value)
        for value in variants:
            with self.subTest(value=value["knowledge_keys"]):
                with self.assertRaisesRegex(ValueError, "累计知识清单"):
                    run._validate(value)

    def test_full_empty_inventory_is_valid_to_withdraw_last_invalid_knowledge(self):
        value = stage(knowledge=False)
        self.assertIs(run._validate(value), value)

    def test_knowledge_dates_cannot_move_past_or_outside_topic_end(self):
        for dates in ({"start": "2026-11-19"}, {"end": "2026-11-25"}):
            with self.subTest(dates=dates):
                value = stage()
                value["documents"]["sops"].update(dates)
                with self.assertRaisesRegex(ValueError, "统计范围"):
                    run._validate(value)


class KnowledgeOnlyPublicationTests(TestCase):
    def test_empty_topics_neither_sample_invalid_indexes_nor_clear_previous_top(self):
        with TemporaryDirectory() as folder:
            state_dir = Path(folder)
            target = run.sync.Target("lark-cli", "baseExample", "tblExample",
                "https://example.feishu.cn/base/baseExample?table=tblExample",
                {"title": "标题", "detail": "详情", "summary": "摘要", "source": "来源"}, state_dir / "write.lock")
            top_target = run.sync.Target("lark-cli", "baseExample", "tblTopExample",
                "https://example.feishu.cn/base/baseExample?table=tblTopExample",
                target.fields, target.lock_path)
            previous = stage(topics=True, knowledge=False)
            previous["marker"] = "2026-11-24T16:30+08:00"
            support._write_state(state_dir / "full-published.json", previous)
            top_state = state_dir / "top-table.json"
            support._write_state(top_state, {"table": top_target.table_id, "records": {
                "t_" + "a" * 24: {"id": "recPriorTop1", "fields": {}},
                "t_" + "b" * 24: {"id": "recPriorTop2", "fields": {}},
            }})
            original_top = top_state.read_bytes()
            current = stage()

            def fetch(_previous, apply=False, **kwargs):
                if kwargs.get("probe"):
                    return {"status": "ready", "marker": current["marker"]}
                if kwargs.get("job_only"):
                    return {"job": None}
                self.assertTrue(apply)
                return deepcopy(current)

            with ExitStack() as stack:
                stack.enter_context(patch.object(support, "target_table", return_value=target))
                stack.enter_context(patch.object(support, "_compact", return_value=True))
                stack.enter_context(patch.object(run.source, "fetch", side_effect=fetch))
                stack.enter_context(patch.object(run, "_ensure_updated_field"))
                stack.enter_context(patch.object(run, "_ensure_display_field", return_value=None))
                stack.enter_context(patch.object(run.top_table, "ensure_top", return_value=top_target))
                sync_table = stack.enter_context(patch.object(run, "_sync_table", return_value={
                    "topics": 42, "sops": 1, "created": 1, "updated": 0, "removed": 0}))
                verify_topics = stack.enter_context(patch.object(support, "verify_guest_links"))
                verify_knowledge = stack.enter_context(patch.object(support, "_sop_links", return_value=1))
                stack.enter_context(patch.object(run.dashboard, "update", return_value="https://example.feishu.cn/base/log"))
                # _schema verifies the mapped names before a publish can proceed.
                stack.enter_context(patch.object(support, "_schema", return_value={
                    name: 1 for name in target.fields.values()}))
                result = run.run(state_dir, True)

            self.assertEqual(result["status"], "published")
            self.assertIs(result["top"]["retained"], True)
            self.assertEqual(result["top"]["topics"], 2)
            self.assertEqual(top_state.read_bytes(), original_top)
            sync_table.assert_called_once()
            self.assertIs(sync_table.call_args.args[0], target)
            self.assertFalse(sync_table.call_args.kwargs.get("top", False))
            verify_topics.assert_not_called()
            verify_knowledge.assert_called_once_with(current["documents"]["sops"])
            self.assertFalse((state_dir / "full-pending.json").exists())
            committed = support._read_state(state_dir / "full-published.json")
            self.assertTrue(committed["top_retained"])
            self.assertEqual(committed["knowledge_keys"], [GUIDE])

