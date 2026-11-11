from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from src import voc_assets as assets
from src import voc_episode_catalog as catalog
from src.config import Settings
from tests.test_voc_episode_topics import SameVector, issue


class EpisodeCatalogTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = Settings()
        self.cfg = replace(cfg, runtime=replace(cfg.runtime, db_path=str(Path(folder.name) / "business.db"),
                                               voc_domain="zhihuige"))
        self.path = assets.store_path(self.cfg)

    def add_legacy(self, identity, *, intent="functional_anomaly", status="active"):
        assets.initialize(self.path)
        with assets.connection(self.path, write=True) as conn:
            conn.execute("""INSERT INTO v2_topic(id,question,theme,boundary,domain,primary_intent,scope_json,status,
                created_at,updated_at) VALUES (?,?,'旧主题','',?,?, '[]',?,0,0)""",
                         (identity, "历史问题", self.cfg.runtime.voc_domain, intent, status))

    def test_new_topic_discovery_persists_across_batches_and_model_titles(self):
        first = issue("z", question={"quote": "退款场景没有触发，机器人也没回复。"})
        identity = catalog.assign(self.cfg, [first])["z"]
        self.assertRegex(identity, r"^t_[a-f0-9]{24}$")
        self.assertEqual(catalog.assign(self.cfg, [{**first, "title": "模型改了概括"}]), {"z": identity})
        earlier = issue("a", question={"quote": "开启退款场景后机器人没有回复。"})
        self.assertEqual(catalog.assign(self.cfg, [earlier]), {"a": identity})
        self.assertEqual(catalog.topics(self.cfg), [{"id": identity, "question": first["question"]["quote"],
            "theme": first["module"], "boundary": first["goal"] + " · " + first["symptom"], "revision": 1,
            "primary_intent": "functional_anomaly"}])

    def test_same_kind_legacy_requires_exact_span_and_live_revision(self):
        self.add_legacy("t_old")
        first = issue("a")
        candidate = {"id": "t_old", "primary_intent": "functional_anomaly", "quote": "另一个机器人未触发问题", "revision": 1}
        result = catalog.assign(self.cfg, [first], legacy_candidates={"a": [candidate]})
        self.assertNotEqual(result["a"], "t_old")
        second = issue("b", goal="同步旧知识", symptom="同步未生效")
        exact = {**candidate, "quote": second["question"]["quote"] + "。"}
        self.assertEqual(catalog.assign(self.cfg, [second], legacy_candidates={"b": [exact]}), {"b": "t_old"})
        with assets.connection(self.path, write=True) as conn:
            conn.execute("UPDATE v2_topic SET status='ignored' WHERE id='t_old'")
        self.assertEqual(catalog.assign(self.cfg, [second]), {})
        self.assertEqual(catalog.assign(self.cfg, [{**second, "id": "later"}]), {})
        self.assertEqual([row["id"] for row in catalog.topics(self.cfg)], [result["a"]])

    def test_later_legacy_anchor_preserves_established_topic(self):
        first = issue("a")
        stable = catalog.assign(self.cfg, [first])["a"]
        self.add_legacy("t_later")
        second = issue("b")
        later_anchor = {"id": "t_later", "primary_intent": "functional_anomaly",
                        "quote": second["question"]["quote"], "revision": 1}
        self.assertEqual(catalog.assign(self.cfg, [second], legacy_candidates={"b": [later_anchor]}), {"b": stable})
        self.assertEqual([row["id"] for row in catalog.topics(self.cfg)], [stable])

    def test_multiple_live_legacy_matches_do_not_create_another_topic(self):
        self.add_legacy("t_one")
        self.add_legacy("t_two")
        episode = issue("a")
        candidates = [{"id": identity, "primary_intent": "functional_anomaly", "quote": episode["question"]["quote"],
                       "revision": 1} for identity in ("t_one", "t_two")]
        self.assertEqual(catalog.assign(self.cfg, [episode], legacy_candidates={"a": candidates}), {})
        self.assertEqual(catalog.topics(self.cfg), [])

    def test_repeated_symptoms_stay_distinct_even_with_high_similarity(self):
        first = catalog.assign(self.cfg, [issue("a")])["a"]
        second = catalog.assign(self.cfg, [issue("b", symptom="重复触发")], embedder=SameVector(),
                                rerank=lambda pairs: [1.] * len(pairs))["b"]
        self.assertNotEqual(first, second)

    def test_matching_stays_complete_across_batches(self):
        first = issue("a", goal="发送订单卡片")
        middle = issue("b", goal="回复售后咨询")
        third = issue("c", goal="回复退货申请")
        strong = {frozenset(("a", "b")), frozenset(("b", "c"))}

        def rerank(pairs):
            return [.96 if frozenset(text[-1] for text in pair) in strong else .35 for pair in pairs]

        initial = catalog.assign(self.cfg, [first])["a"]
        self.assertEqual(catalog.assign(self.cfg, [middle], embedder=SameVector(), rerank=rerank), {"b": initial})
        self.assertEqual(catalog.assign(self.cfg, [third], embedder=SameVector(), rerank=rerank), {})
        self.assertEqual(len(catalog.topics(self.cfg)), 1)

    def test_stale_segment_rolls_back_catalog_and_assignment_together(self):
        first = issue("a")

        def reject(conn, assignments):
            self.assertTrue(assignments["a"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM voc_episode_assignment").fetchone()[0], 1)
            return False

        with self.assertRaises(catalog.CatalogPublishRejected):
            catalog.assign(self.cfg, [first], publish=reject)
        self.assertEqual(catalog.topics(self.cfg), [])
        with assets.connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM voc_episode_assignment").fetchone()[0], 0)
        self.assertEqual(catalog.assign(self.cfg, [issue("unknown", kind="unclear")],
                                        publish=lambda _, assignments: assignments == {}), {})
        self.assertFalse(Path(self.cfg.runtime.db_path).exists())


if __name__ == "__main__":
    unittest.main()
