from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src.voc_episode_topics import LocalReranker, group


def issue(identity: str, **overrides) -> dict:
    return {"id": identity, "kind": "anomaly", "title": "退款场景已开启但机器人未触发",
            "module": "退款场景", "platform": "抖店", "goal": "机器人触发退款场景",
            "symptom": "开启后未触发", "question": {"quote": f"会话原句{identity}"},
            "session_key": "s" + identity, "customer_key": "c" + identity, **overrides}


class SameVector:
    def encode(self, texts):
        return [[1., 0.] for _ in texts]


class EpisodeTopicsTest(unittest.TestCase):
    def test_exact_core_across_platforms_preserves_consultation_and_customer_counts(self):
        rows = [issue("1"), issue("2", title="在京东也没有触发退款场景", platform="京东"),
                issue("3", session_key="s1", customer_key="c1")]
        topics = group(rows)
        self.assertEqual(topics, group(list(reversed(rows))))
        self.assertEqual([topic["episode_ids"] for topic in topics], [["1", "2", "3"]])
        self.assertEqual((topics[0]["consultation_count"], topics[0]["customer_count"]), (2, 2))

    def test_configuration_and_contrasting_anomalies_remain_separate_at_max_similarity(self):
        rows = [issue("1"), issue("2", kind="configuration", title="如何启用退款场景", goal="开启退款场景", symptom=""),
                issue("3", title="退款场景触发了两次", symptom="重复触发"),
                issue("4", module="自动回复", title="退款场景已开启但机器人未触发"),
                issue("5", kind="configuration", title="如何停用退款场景", goal="关闭退款场景", symptom="")]
        rerank = Mock(side_effect=lambda pairs: [1.] * len(pairs))
        topics = group(rows, embedder=SameVector(), rerank=rerank)
        self.assertEqual({tuple(topic["episode_ids"]) for topic in topics}, {(str(i),) for i in range(1, 6)})
        rerank.assert_not_called()

    def test_explicit_incompatible_error_codes_stay_separate(self):
        rows = [issue("a", title="退款场景 HTTP 401"), issue("b", title="退款场景 HTTP 403")]
        self.assertEqual(len(group(rows, embedder=SameVector(), rerank=lambda _: [1.])), 2)

    def test_generic_configuration_goal_does_not_force_an_exact_match(self):
        rows = [issue("a", kind="configuration", module="机器人管理", title="怎样修改欢迎语", goal="设置", symptom=""),
                issue("b", kind="configuration", module="机器人管理", title="如何配置接待上限", goal="设置", symptom="")]
        self.assertEqual(len(group(rows)), 2)

    def test_identical_generated_labels_without_detail_require_matching_source_text(self):
        rows = [issue("a", module="", title="机器人有问题", goal="", symptom="", question={"quote": "回复语设置在哪里"}),
                issue("b", module="", title="机器人有问题", goal="", symptom="", question={"quote": "修改后还是发了两次"}),
                issue("c", module="", title="机器人有问题", goal="", symptom="", question={"quote": "回复语设置在哪里？"})]
        self.assertEqual({tuple(topic["episode_ids"]) for topic in group(rows)}, {("a", "c"), ("b",)})

    def test_dense_recall_reranks_paraphrases_with_both_source_quotes(self):
        rows = [issue("a", kind="configuration", module="机器人管理", title="怎样让机器人不再应答", goal="停止应答", symptom=""),
                issue("b", kind="configuration", module="机器人管理", title="如何暂停自动回复", goal="暂停自动回复", symptom="")]
        self.assertEqual(len(group(rows)), 2)
        rerank = Mock(return_value=[.96])
        topics = group(rows, embedder=SameVector(), rerank=rerank)
        self.assertEqual([topic["episode_ids"] for topic in topics], [["a", "b"]])
        first, second = rerank.call_args.args[0][0]
        self.assertIn("会话原句a", first)
        self.assertIn("会话原句b", second)
        self.assertEqual(len(group(rows, embedder=SameVector(), rerank=lambda _: [.35])), 2)

    def test_cross_platform_similarity_without_same_core_is_not_a_merge(self):
        rows = [issue("a"), issue("b", platform="京东", title="退货时机器人未触发退款流程", goal="开启退货自动处理")]
        self.assertEqual(len(group(rows, embedder=SameVector(), rerank=lambda _: [1.])), 2)

    def test_ambiguous_bridge_and_unclear_cases_remain_unassigned(self):
        rows = [issue("a", title="退款场景在发送订单卡片时未触发", goal="发送订单卡片"),
                issue("b", title="退款机器人在问售后时未触发", goal="回复售后咨询"),
                issue("c", title="退款场景在用户申请退货时未触发", goal="回复退货申请"),
                issue("d", kind="unclear")]

        def rerank(pairs):
            return [.92 if any(part.endswith("会话原句b") for part in pair) else .3 for pair in pairs]

        topics = group(rows, embedder=SameVector(), rerank=rerank)
        self.assertEqual({tuple(topic["episode_ids"]) for topic in topics}, {("a",), ("c",)})
        self.assertEqual(topics, group(list(reversed(rows)), embedder=SameVector(), rerank=rerank))

    def test_single_best_match_does_not_chain_to_weakly_related_issue(self):
        rows = [issue("a", title="退款场景在发送订单卡片时未触发", goal="发送订单卡片"),
                issue("b", title="退款机器人在问售后时未触发", goal="回复售后咨询"),
                issue("c", title="退款场景在用户申请退货时未触发", goal="回复退货申请")]

        def rerank(pairs):
            scores = {("a", "b"): .99, ("b", "c"): .88, ("a", "c"): .34}
            return [scores[tuple(part[-1] for part in pair)] for pair in pairs]

        self.assertEqual({tuple(topic["episode_ids"]) for topic in group(rows, embedder=SameVector(), rerank=rerank)},
                         {("a", "b"), ("c",)})

    def test_malformed_model_scores_and_duplicate_episode_ids_fail_closed(self):
        rows = [issue("a"), issue("b", title="机器人回复失败", goal="命中场景")]
        with self.assertRaisesRegex(ValueError, "重排"):
            group(rows, embedder=SameVector(), rerank=lambda _: [float("nan")])
        with self.assertRaisesRegex(ValueError, "编号重复"):
            group([rows[0], rows[0]])

    def test_local_reranker_loads_once_with_cpu_and_no_network_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            fake_model = Mock(num_labels=1)
            fake_model.predict.return_value = [.9]
            constructor = Mock(return_value=fake_model)
            activation = object()
            modules = {"sentence_transformers": SimpleNamespace(CrossEncoder=constructor),
                       "torch.nn": SimpleNamespace(Sigmoid=lambda: activation)}
            with patch.dict(sys.modules, modules):
                rerank = LocalReranker(temp)
                self.assertEqual(rerank([]), [])
                constructor.assert_not_called()
                self.assertEqual(rerank([("甲", "乙")]), [.9])
                self.assertEqual(rerank([("丙", "丁")]), [.9])
                constructor.assert_called_once_with(str(Path(temp).resolve()), device="cpu", max_length=512,
                                                    local_files_only=True, trust_remote_code=False, token=False)
                self.assertIs(fake_model.predict.call_args.kwargs["activation_fn"], activation)
            with self.assertRaisesRegex(ValueError, "本机模型目录"):
                LocalReranker(Path(temp) / "missing")


if __name__ == "__main__":
    unittest.main()
