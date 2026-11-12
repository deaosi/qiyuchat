from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src import voc_assistant as assistant
from src.voc_service import ServiceError


class TestAssistantInsights(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(runtime=SimpleNamespace(voc_domain='zhihuige'),
                                   llm=SimpleNamespace(model_main='fixture-model'))
        self.engine = Mock()
        engine_patch = patch.object(assistant, '_engine', return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)

    def test_review_uses_five_reads_and_retains_stats_when_writer_is_unavailable(self):
        self.engine.topics.return_value = {'items': []}
        self.engine.summary.side_effect = [{'consultation_count': 8}, {'consultation_count': 4}]
        with (
            patch.object(assistant, '_plan') as planner,
            patch.object(assistant.voc_assistant_metrics, 'read_metric',
                         side_effect=[('来源问答 12 条', [], '来源问答 12 条'), ('峰值 5 条', [], '峰值 5 条')]) as metric,
            patch.object(assistant.voc_assistant_solutions, 'read_solution', return_value=('暂无记录', [])),
            patch.object(assistant.voc_answer, 'compose', return_value=(None, 0, '模型暂不可用')),
        ):
            result = assistant.answer(self.cfg, {'days': 30, 'group': '测试群'}, {'message': '最近 7 天 VOC 复盘'})
        planner.assert_not_called()
        self.assertEqual([tool['name'] for tool in result['tools']], list(assistant.REPORT_READS))
        self.assertEqual(result['answer_mode'], 'computed')
        self.assertIn('来源问答 12 条', result['answer'])
        self.assertIn('峰值 5 条', result['answer'])
        self.assertEqual(result['warnings'], ['模型暂不可用'])
        self.assertEqual(metric.call_args.args[1]['group'], '测试群')
        self.assertEqual(self.engine.summary.call_count, 2)

    def test_named_distribution_is_only_in_display_data_and_combines_with_scoped_sop(self):
        plan = {'intent': 'insights', 'query': '', 'days': 7, 'limit': 3, 'support_intent': None,
                'diagnosis_status': None, 'reads': [{'name': 'group_breakdown', 'query': ''},
                                                   {'name': 'sops', 'query': '场景未触发'}]}
        ref = {'kind': 'topic', 'id': 'topic-1', 'title': '场景未触发'}
        with (
            patch.object(assistant, '_plan', return_value=(plan, 5)),
            patch.object(assistant.voc_assistant_metrics, 'read_metric',
                         return_value=('商家甲群 12 条', [], '客户群 1 共 12 条')),
            patch.object(assistant.voc_assistant_solutions, 'read_solution',
                         return_value=('已核验的 SOP', [ref])) as solution,
            patch.object(assistant.voc_answer, 'compose', return_value=({'answer': '按来源汇总。'}, 7, '')) as writer,
        ):
            result = assistant.answer(self.cfg, {'days': 30, 'staff': '测试客服'}, {'message': '看分布和未触发 SOP'})
        self.assertEqual(result['citations'], [ref])
        self.assertEqual(result['answer_mode'], 'grounded_model')
        self.assertEqual(result['usage_tokens'], 12)
        self.assertIn('商家甲群', result['answer'])
        self.assertNotIn('商家甲群', str(writer.call_args.args[-1]))
        self.assertEqual(solution.call_args.args[3], '场景未触发')
        self.assertEqual(solution.call_args.args[1]['staff'], '测试客服')

    def test_ambiguous_topic_metric_does_not_count_whole_scope(self):
        self.engine.relevant_topics.return_value = [{'id': 'a', 'question': '测试 A'}, {'id': 'b', 'question': '测试 B'}]
        plan = {'intent': 'insights', 'query': '', 'days': None, 'limit': 3, 'support_intent': None,
                'diagnosis_status': None, 'explain': False, 'reads': [{'name': 'daily_trend', 'query': '场景'}]}
        with (patch.object(assistant, '_plan', return_value=(plan, 0)),
              patch.object(assistant.voc_assistant_metrics, 'read_metric') as metric):
            result = assistant.answer(self.cfg, {'days': 7}, {'message': '场景相关的逐日趋势'})
        metric.assert_not_called()
        self.assertIn('多个相关主题', result['answer'])
        self.assertEqual([ref['id'] for ref in result['citations']], ['a', 'b'])

    def test_plan_rejects_unknown_tool_and_duplicate_tool(self):
        base = {'intent': 'insights', 'query': '', 'days': None, 'limit': 3,
                'support_intent': None, 'diagnosis_status': None}
        for names in (['delete'], ['overview', 'overview']):
            plan = {**base, 'reads': [{'name': name, 'query': ''} for name in names]}
            with (self.subTest(names=names),
                  patch.object(assistant, 'complete_json', return_value={'data': plan, 'usage_tokens': 0}),
                  self.assertRaises(ServiceError)):
                assistant._plan(self.cfg, '组合查询', [], {})
