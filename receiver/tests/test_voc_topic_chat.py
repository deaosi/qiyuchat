from dataclasses import replace
from unittest.mock import Mock, patch

from src import voc_assistant
from tests.test_local import LocalFixture


class TestTopicChat(LocalFixture):
    def test_topic_compare_stays_in_exact_scope_when_prior_context_does_not_match(self):
        cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, voc_domain='zhihuige'))
        topic, other = 't_' + 'a' * 24, 't_' + 'b' * 24
        filters = {'start': '2026-11-05', 'end': '2026-11-11', 'focus_topic': topic}
        engine = Mock()
        engine.topics.return_value = {
            'items': [{'id': other, 'question': '其他问题', 'consultation_count': 12},
                      {'id': topic, 'question': '场景没触发', 'consultation_count': 4,
                       'trend': {'previous_consultation_count': 2}}],
            'comparison': {'comparable': False, 'note': '覆盖不足，数量仅反映已整理来源。'},
        }
        body = {'message': '比较这个问题，只给数字', 'context': {
            'query_filters': {**filters, 'focus_topic': other, 'start': '2026-10-01'},
            'citations': [{'kind': 'topic', 'id': other}],
        }}
        plan = {'intent': 'compare', 'query': '这个问题', 'days': None, 'limit': 5,
                'support_intent': None, 'diagnosis_status': None, 'explain': False}
        with patch.object(voc_assistant, '_engine', return_value=engine), patch.object(voc_assistant, '_plan', return_value=(plan, 0)):
            result = voc_assistant.answer(cfg, filters, body)

        self.assertIn('2026-11-05 至 2026-11-11：4 次主题咨询', result['answer'])
        self.assertIn('2026-10-29 至 2026-11-04：2 次主题咨询', result['answer'])
        self.assertIn('覆盖不足', result['answer'])
        self.assertNotIn('环比', result['answer'])
        self.assertEqual(result['scope']['query_filters']['focus_topic'], topic)
        self.assertEqual([ref['id'] for ref in result['citations']], [topic])
        self.assertEqual(engine.topics.call_args.args[1]['focus_topic'], topic)
        self.assertEqual(engine.topics.call_args.kwargs, {'workbench': True})
        engine.relevant_topics.assert_not_called()

    def test_focused_episode_topic_can_retrieve_original_knowledge(self):
        topic, legacy = 't_'+'a'*24, 't_'+'b'*24
        original = {'id':'k-original','topic_id':legacy,'question':'场景未触发的检查步骤'}
        engine = Mock()
        engine.topic_detail.return_value={'topic':{'id':topic,'question':'场景没触发'},'events':[]}
        engine.knowledge.return_value={'items':[original]}
        engine.relevant_items.side_effect=lambda cfg, rows, query, **kwargs: rows
        result = voc_assistant._focused_search(engine, self.cfg, {'focus_topic':topic},
                                              {'query':'这个问题怎么排查','intent':'knowledge'}, [])
        self.assertEqual(result['knowledge'],[original])
