from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src import voc_assistant as assistant
from src.config import LLMConfig
from src.voc_service import ServiceError


class TestAssistantInsights(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(runtime=SimpleNamespace(voc_domain='zhihuige'),
                                   llm=SimpleNamespace(model_main='fixture-model'))
        self.engine = Mock()
        engine_patch = patch.object(assistant, '_engine', return_value=self.engine)
        engine_patch.start()
        self.addCleanup(engine_patch.stop)

    def test_model_summary_shares_one_time_budget_across_writing_and_verification(self):
        writer = assistant.voc_answer
        source = writer.material('computed', 'D1', '该范围有来源数据。', '该范围有来源数据。')
        draft = {'paragraphs': [{'text': '该范围有来源数据。',
                                 'references': [{'id': 'D1', 'quote': '该范围有来源数据。'}]}], 'follow_up': ''}
        cfg = SimpleNamespace(llm=LLMConfig(timeout=180))
        with (
            patch.object(writer.time, 'monotonic', side_effect=[100, 103, 126]),
            patch.object(writer, 'complete_json', side_effect=[{'data': draft, 'usage_tokens': 1},
                         {'data': {'supported': True, 'unsupported': []}, 'usage_tokens': 1}]) as model,
        ):
            composed, _, warning = writer.compose(cfg, '做个复盘', [], {'label': '测试范围'}, [source], time_budget=30)
        self.assertIsNotNone(composed)
        self.assertFalse(warning)
        self.assertEqual([call.args[0].timeout for call in model.call_args_list], [27, 4])

    def test_review_uses_five_reads_and_retains_stats_when_writer_is_unavailable(self):
        self.engine.topics.return_value = {'items': []}
        self.engine.summary.side_effect = [{'consultation_count': 8}, {'consultation_count': 4}]
        with (
            patch.object(assistant, '_plan') as planner,
            patch.object(assistant.voc_assistant_metrics, 'read_metric',
                         side_effect=[('来源问答 12 条', [], '来源问答 12 条'), ('峰值 5 条', [], '峰值 5 条')]) as metric,
            patch.object(assistant.voc_assistant_solutions, 'read_solution', return_value=('暂无记录', [])),
            patch.object(assistant.voc_answer, 'compose', return_value=(None, 0, '模型暂不可用')) as writer,
        ):
            result = assistant.answer(self.cfg, {'days': 30, 'group': '测试群'}, {'message': '最近 7 天 VOC 复盘'})
        planner.assert_not_called()
        self.assertEqual([tool['name'] for tool in result['tools']], list(assistant.REPORT_READS))
        self.assertEqual(result['answer_mode'], 'computed')
        self.assertIn('来源问答 12 条', result['answer'])
        self.assertIn('峰值 5 条', result['answer'])
        self.assertEqual(result['warnings'], ['模型暂不可用'])
        self.assertEqual(writer.call_args.kwargs['time_budget'], 30)
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

    def test_follow_up_examples_uses_only_preceding_references_in_the_same_scope(self):
        ref = {'kind': 'event', 'id': 'episode_1'}
        row = {'id': ref['id'], 'question': '商家实际咨询'}
        case = {'citation': {**ref, 'title': '商家实际咨询'}, 'question': '原话一', 'reply': '原话二'}
        self.engine.event_detail.return_value = {'event': row}
        with (
            patch.object(assistant, '_plan') as planner,
            patch.object(assistant, 'build_examples', return_value=[case]) as extract,
        ):
            result = assistant.answer(self.cfg, {'group': '甲群'}, {
                'message': '给个真实案例',
                'context': {'query_filters': {'group': '甲群', 'start': '2026-11-06', 'end': '2026-11-12'},
                            'citations': [ref]},
            })
        self.assertEqual(result['examples'], [case])
        self.assertEqual(result['citations'], [case['citation']])
        self.assertEqual(result['answer_mode'], 'source_reference')
        scoped = extract.call_args.args[2]
        self.assertEqual((scoped['group'], scoped['start'], scoped['end']), ('甲群', '2026-11-06', '2026-11-12'))
        self.assertEqual(extract.call_args.args[3], [(case['citation'], row)])
        self.engine.search.assert_not_called()
        planner.assert_not_called()

    def test_example_does_not_use_a_source_omitted_from_the_grounded_answer(self):
        event_one = {'id': 'episode_1', 'question_quote': '已选来源问题'}
        event_two = {'id': 'episode_2', 'question_quote': '未选来源问题'}
        events = {row['id']: row for row in (event_one, event_two)}
        self.engine.search.return_value = {'events': [event_one, event_two]}
        self.engine.event_detail.side_effect = lambda _, scoped, identity: {'event': events[identity]}
        plan = {'intent': 'evidence', 'query': '场景配置', 'days': 7, 'limit': 3,
                'support_intent': None, 'diagnosis_status': None}
        citation = {'kind': 'event', 'id': 'episode_1', 'title': '会话依据'}
        example = {'citation': citation, 'question': '已选来源问题', 'reply': '实际回复'}
        with (
            patch.object(assistant, '_plan', return_value=(plan, 0)),
            patch.object(assistant.voc_answer, 'compose',
                         return_value=({'answer': '已选依据。', 'used_ids': ['S1']}, 0, '')),
            patch.object(assistant, 'build_examples', return_value=[example]) as extract,
        ):
            result = assistant.answer(self.cfg, {}, {'message': '场景配置会话里是怎么说的？'})
        self.assertEqual(result['examples'], [example])
        self.assertEqual(extract.call_args.args[3], [(citation, event_one)])

    def test_knowledge_can_fall_back_to_a_scoped_staff_case_without_the_model_writer(self):
        question = '退款场景配置好了，为什么订单没有命中？'
        reply = '请先确认订单状态是否符合场景的触发条件。'
        event = {'id': 'episode_exact', 'qa_id': 12, 'source_fingerprint': 'snapshot',
                 'question': question, 'question_quote': question,
                 'question_span': {'quote': question, 'start': 0, 'end': len(question), 'alignment': 'exact'},
                 'answer': reply, 'answer_message_ids': ['staff-1'], 'staff_source_verified': True,
                 'source_kind': 'source-anchored', 'claims': []}
        self.engine.search.side_effect = [{'events': [], 'knowledge': []}, {'events': [event]}]
        self.engine.event_detail.return_value = {'event': event}
        plan = {'intent': 'knowledge', 'query': '退款场景未命中', 'days': 7, 'limit': 3,
                'support_intent': None, 'diagnosis_status': None}
        with (
            patch.object(assistant, '_plan', return_value=(plan, 0)),
            patch.object(assistant.voc_answer, 'compose', return_value=(None, 0, '模型暂不可用')),
        ):
            result = assistant.answer(self.cfg, {'group': '甲群'}, {'message': '退款场景未命中怎么排查？'})
        targeted_search = self.engine.search.call_args
        self.assertEqual(targeted_search.args[1]['group'], '甲群')
        self.assertEqual(targeted_search.args[2], plan['query'])
        self.assertTrue(targeted_search.kwargs['require_staff'])
        self.assertEqual([(case['question'], case['reply']) for case in result['examples']], [(question, reply)])
        self.assertNotIn('outcome', result['examples'][0])
        self.assertEqual(result['answer_mode'], 'source_reference')

    def test_plan_rejects_unknown_tool_and_duplicate_tool(self):
        base = {'intent': 'insights', 'query': '', 'days': None, 'limit': 3,
                'support_intent': None, 'diagnosis_status': None}
        for names in (['delete'], ['overview', 'overview']):
            plan = {**base, 'reads': [{'name': name, 'query': ''} for name in names]}
            with (self.subTest(names=names),
                  patch.object(assistant, 'complete_json', return_value={'data': plan, 'usage_tokens': 0}),
                  self.assertRaises(ServiceError)):
                assistant._plan(self.cfg, '组合查询', [], {})
