from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import voc_agent
from src.config import LLMConfig
from src.model_protocol import ModelProtocolError


class TestVocAgent(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(llm=LLMConfig(timeout=90))
        self.params = {
            'question': '场景为何未命中？请看 https://private.example.cn/path 和 13812345678',
            'scope': {'label': '最近 7 天', 'staff': '内部客服昵称'},
            'observations': [
                {'tool': 'overview', 'status': 'found', 'summary': '有 4 条问答', 'id': 'unpublished-record'},
                {'tool': 'top_topics', 'status': 'found', 'summary': 't_0123456789abcdef01234567'},
            ],
            'tools': {'evidence': '会话依据', 'sops': '核验 SOP', 'delete': '删除'},
            'topics': [{'ref': 'T1', 'label': '场景未命中', 'id': 'secret-topic-key'}],
            'completed': {('top_topics', '', ''), ('evidence', '物流 同步', 'T1')},
            'deadline': 110,
        }

    def _choose(self, data):
        with (
            patch.object(voc_agent.time, 'monotonic', return_value=100),
            patch.object(voc_agent, 'complete_json', return_value={'data': data, 'usage_tokens': 29}),
        ):
            return voc_agent.choose_followup(self.cfg, **self.params)

    def test_selects_two_read_only_followups_and_minimizes_model_payload(self):
        reads = [
            {'name': 'evidence', 'query': '场景未命中', 'topic_ref': 'T1'},
            {'name': 'sops', 'query': '', 'topic_ref': 'T1'},
        ]
        with (
            patch.object(voc_agent.time, 'monotonic', return_value=100),
            patch.object(voc_agent, 'complete_json', return_value={'data': {'reads': reads}, 'usage_tokens': 29}) as model,
        ):
            self.assertEqual(voc_agent.choose_followup(self.cfg, **self.params), (reads, 29, ''))
        config, _, payload = model.call_args.args
        self.assertEqual(config.timeout, 6)
        self.assertEqual(model.call_args.kwargs['max_tokens'], 1000)
        self.assertEqual(set(payload['tools']), {'evidence', 'sops'})
        self.assertEqual(payload['scope'], {'label': '最近 7 天'})
        self.assertEqual(payload['observations'][0], {'tool': 'overview', 'status': 'found', 'summary': '有 4 条问答'})
        serialized = repr(payload)
        for private in ('private.example.cn', '13812345678', '内部客服昵称', 'unpublished-record', 'secret-topic-key',
                        't_0123456789abcdef01234567'):
            self.assertNotIn(private, serialized)

    def test_no_followup_is_successful(self):
        self.assertEqual(self._choose({'reads': []}), ([], 29, ''))

    def test_rejects_the_entire_batch_if_any_read_is_invalid(self):
        valid = {'name': 'sops', 'query': '场景未命中', 'topic_ref': 'T1'}
        invalid = (
            {**valid, 'name': 'delete'}, {**valid, 'topic_ref': 'T2'}, {**valid, 'topic_ref': None},
            {**valid, 'query': '退款 2026-11-12'}, {**valid, 'query': '过去七天'},
            {**valid, 'query': '/api/voc2/knowledge'}, {**valid, 'query': 't_0123456789abcdef01234567'},
            {**valid, 'query': 'SELECT name FROM customer'}, {**valid, 'query': 'x' * 181},
            {**valid, 'query': '商家编号 A12345'}, {**valid, 'scope': {'days': 15}},
            {**valid, 'name': 'evidence', 'query': '物流同步。'},
            {**valid, 'query': '场景未命中。'},
        )
        for read in invalid:
            with self.subTest(read=read):
                reads, usage, warning = self._choose({'reads': [valid, read]})
                self.assertEqual((reads, usage), ([], 29))
                self.assertTrue(warning)
        for value in ({'reads': [valid] * 3}, {'reads': [], 'days': 30}, {'reads': None}):
            with self.subTest(value=value):
                self.assertTrue(self._choose(value)[2])

    def test_budget_and_gateway_failures_keep_current_evidence(self):
        with (
            patch.object(voc_agent.time, 'monotonic', return_value=109.2),
            patch.object(voc_agent, 'complete_json') as model,
        ):
            self.assertEqual(voc_agent.choose_followup(self.cfg, **self.params), ([], 0, ''))
            model.assert_not_called()
        with (
            patch.object(voc_agent.time, 'monotonic', return_value=108.2),
            patch.object(voc_agent, 'complete_json', side_effect=ModelProtocolError('private gateway detail')) as model,
        ):
            reads, tokens, warning = voc_agent.choose_followup(self.cfg, **self.params)
            self.assertEqual((reads, tokens), ([], 0))
            self.assertEqual(model.call_args.args[0].timeout, 1)
            self.assertTrue(warning)
            self.assertNotIn('private gateway detail', warning)
        with (
            patch.object(voc_agent.time, 'monotonic', side_effect=[104, 110]),
            patch.object(voc_agent, 'complete_json', return_value={'data': {'reads': []}, 'usage_tokens': 29}),
        ):
            reads, tokens, warning = voc_agent.choose_followup(self.cfg, **self.params)
            self.assertEqual((reads, tokens), ([], 29))
            self.assertTrue(warning)
