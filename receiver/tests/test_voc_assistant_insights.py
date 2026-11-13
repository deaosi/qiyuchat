from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src import voc_assistant as assistant
from src.config import LLMConfig
from src.voc_service import ServiceError


class TestAssistantInsights(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(runtime=SimpleNamespace(voc_domain='zhihuige'),
                                   llm=LLMConfig(model_main='fixture-model'))
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
            patch.object(assistant.voc_agent_summary, 'summarize', return_value=(None, 0, '模型暂不可用')) as writer,
        ):
            result = assistant.answer(self.cfg, {'days': 30, 'group': '测试群'}, {'message': '最近 7 天 VOC 复盘'})
        planner.assert_not_called()
        self.assertEqual([tool['name'] for tool in result['tools']], list(assistant.REPORT_READS))
        self.assertEqual(result['answer_mode'], 'computed')
        self.assertIn('来源问答 12 条', result['answer'])
        self.assertIn('峰值 5 条', result['answer'])
        self.assertEqual(result['warnings'], ['模型暂不可用'])
        self.assertLessEqual(writer.call_args.kwargs['time_budget'], assistant._AGENT_SUMMARY_SECONDS)
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
            patch.object(assistant.voc_assistant_solutions, 'read_solution_evidence',
                         return_value=('已核验的 SOP', [ref], [])) as solution,
            patch.object(assistant.voc_agent, 'choose_followup', return_value=([], 0, '')),
            patch.object(assistant.voc_agent_summary, 'summarize', return_value=(['B2'], 7, '')) as writer,
        ):
            result = assistant.answer(self.cfg, {'days': 30, 'staff': '测试客服'}, {'message': '看分布和未触发 SOP'})
        self.assertEqual(result['citations'], [ref])
        self.assertEqual(result['answer_mode'], 'grounded_selection')
        self.assertEqual(result['usage_tokens'], 12)
        self.assertIn('商家甲群', result['answer'])
        self.assertTrue(result['answer'].startswith('已核验 SOP'))
        self.assertNotIn('商家甲群', str(writer.call_args.args[-1]))
        self.assertEqual(solution.call_args.args[3], '场景未触发')
        self.assertEqual(solution.call_args.args[1]['staff'], '测试客服')

    def test_guided_agent_reads_grouped_rank_then_scoped_variant_sop_and_conversation(self):
        first, second = 't_' + 'a' * 24, 't_' + 'b' * 24
        group = {'id': first, 'theme': '回复异常', 'question': '答错了怎么办', 'consultation_count': 11,
                 'event_count': 12, 'customer_count': 3, 'session_count': 10,
                 'members': [{'id': first, 'question': '答错了怎么办', 'consultation_count': 8},
                             {'id': second, 'question': '机器人没回复', 'consultation_count': 4}]}
        event = {'id': 'episode_agent', 'question_quote': '这条机器人答错了', 'topic_id': first}
        citation = {'kind': 'event', 'id': event['id'], 'title': event['question_quote']}
        example = {'citation': citation, 'question': '这条机器人答错了', 'reply': '请先核对当前接待状态。'}
        self.engine.topics.return_value = {'items': [group]}
        self.engine.topic_detail.return_value = {'events': [event]}
        self.engine.event_detail.return_value = {'event': event}
        with (
            patch.object(assistant.voc_agent, 'choose_followup') as follow,
            patch.object(assistant.voc_assistant_solutions, 'read_solution_evidence',
                         return_value=('当前范围未查到符合条件的完整 SOP。', [], [])) as solution,
            patch.object(assistant, 'build_examples', return_value=[example]),
            patch.object(assistant.voc_agent_summary, 'summarize', return_value=(['B3', 'B1'], 8, '')) as writer,
        ):
            result = assistant.answer(self.cfg, {'days': 7, 'group': '测试群'}, {'message': assistant._AGENT_EXAMPLE + '。'})
        self.assertEqual([tool['name'] for tool in result['tools']], ['top_topics', 'sops', 'evidence'])
        self.assertTrue(self.engine.topics.call_args.kwargs['workbench'])
        self.assertIn('机器人没回复（4 次）', result['answer'])
        self.assertIn('未查到符合条件的完整 SOP', result['answer'])
        self.assertTrue(result['answer'].startswith('相关会话依据'))
        self.assertEqual(result['answer_mode'], 'grounded_selection')
        self.assertEqual(result['examples'], [example])
        self.assertIn(example['reply'], str(writer.call_args.args[-1]))
        self.assertEqual(solution.call_args.args[1]['focus_topic'], first)
        self.assertEqual(solution.call_args.args[1]['group'], '测试群')
        self.assertNotIn('测试群', writer.call_args.args[2]['label'])
        follow.assert_not_called()

    def test_agent_keeps_first_read_if_follow_up_read_fails(self):
        plan = {'intent': 'insights', 'query': '', 'days': 7, 'limit': 3, 'support_intent': None,
                'diagnosis_status': None, 'reads': [{'name': 'overview', 'query': ''}]}
        with (
            patch.object(assistant, '_plan', return_value=(plan, 0)),
            patch.object(assistant.voc_agent, 'choose_followup', return_value=(
                [{'name': 'sops', 'query': '场景未触发', 'topic_ref': ''}], 1, '')),
            patch.object(assistant.voc_assistant_metrics, 'read_metric',
                         return_value=('来源问答 12 条', [], '来源问答 12 条')),
            patch.object(assistant.voc_assistant_solutions, 'read_solution_evidence', side_effect=OSError),
            patch.object(assistant.voc_agent_summary, 'summarize', return_value=(None, 0, '模型整理等待超时')),
        ):
            result = assistant.answer(self.cfg, {}, {'message': '查问答概况和处理流程'})
        self.assertIn('来源问答 12 条', result['answer'])
        self.assertEqual([tool['name'] for tool in result['tools']], ['overview'])
        self.assertIn('暂未查到结果', result['warnings'][0])
        self.assertIn('模型整理等待超时', result['warnings'])

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

    def test_freeform_planning_caps_gateway_timeout(self):
        payload = {'intent': 'analysis', 'query': '场景没命中', 'days': 7, 'limit': 3,
                   'support_intent': None, 'diagnosis_status': None}
        with patch.object(assistant, 'complete_json', return_value={'data': payload, 'usage_tokens': 12}) as model:
            self.assertEqual(assistant._plan(self.cfg, '场景没命中怎么处理', [], {}), (payload, 12))
        self.assertEqual(model.call_args.args[0].timeout, 22)

    def test_explicit_rank_and_case_requests_skip_gateway_planning(self):
        for message, tools in [
            ('最近7天有哪些高频问题？请用一条真实会话说明客服怎么答。', ['top_topics']),
            ('最近7天，先看高频问题和逐日咨询峰值，再用真实会话核对最常见问题的客服回复。',
             ['top_topics', 'daily_trend']),
        ]:
            with self.subTest(message=message):
                plan = assistant._direct_plan(message)
                self.assertEqual(plan['days'], 7)
                self.assertEqual([read['name'] for read in plan['reads']], tools)
                self.assertEqual(plan['rank_followups'], ('evidence',))
        self.assertIsNone(assistant._direct_plan('最近7天请分析回复错误是否增加，再核对抖音的客服回复。'))
