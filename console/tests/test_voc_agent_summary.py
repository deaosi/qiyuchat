from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import voc_agent_summary as summary
from src.config import LLMConfig
from src.model_protocol import ModelProtocolError


class TestAgentSummary(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(llm=LLMConfig(model_main='office/custom-model', timeout=90))
        self.question = '回复异常有哪些问法？'
        self.scope = {'label': '最近 7 天', 'staff': '内部客服昵称'}
        self.blocks = [
            {'id': 'D1', 'kind': 'computed', 'title': '咨询排行', 'text': '回复异常 7 次'},
            {'id': 'S1', 'kind': 'knowledge', 'title': '已核验 SOP',
             'text': '只适用于抖音。启用场景后复测，客户反馈生效。'},
            {'id': 'C1', 'kind': 'case', 'title': '实际会话',
             'text': '客服说查看设置。联系电话 13812345678，后台 https://private.example.cn',
             'citation': 'internal-anchor-id'},
        ]

    def call(self, *, budget=20, blocks=None):
        return summary.summarize(self.cfg, self.question, self.scope,
                                 self.blocks if blocks is None else blocks, time_budget=budget)

    def test_model_only_selects_and_orders_provided_block_ids(self):
        result = {'data': {'ids': ['S1', 'D1', 'C1']}, 'usage_tokens': 59}
        with (
            patch.object(summary.time, 'monotonic', return_value=100),
            patch.object(summary, 'complete_json', return_value=result) as model,
        ):
            self.assertEqual(self.call(), (['S1', 'D1', 'C1'], 59, ''))
        model.assert_called_once()
        llm, _, payload = model.call_args.args
        self.assertEqual((llm.model_main, llm.timeout), ('office/custom-model', 20))
        self.assertEqual(model.call_args.kwargs['max_tokens'], 500)
        self.assertEqual(payload['scope'], '最近 7 天')
        self.assertIn('只适用于抖音', payload['blocks'][1]['text'])
        serialized = repr(payload)
        for private in ('private.example.cn', '13812345678', '内部客服昵称', 'internal-anchor-id'):
            self.assertNotIn(private, serialized)
        self.assertIn('13812345678', self.blocks[2]['text'])

    def test_input_and_selection_are_bounded(self):
        blocks = [{'id': f'B{i}', 'kind': 'case', 'title': '很' * 800, 'text': '长' * 800} for i in range(1, 18)]
        with patch.object(summary, 'complete_json', return_value={'data': {'ids': ['B13']}, 'usage_tokens': 23}) as model:
            ids, tokens, warning = self.call(blocks=blocks)
        self.assertIsNone(ids)
        self.assertEqual(tokens, 23)
        self.assertTrue(warning)
        payload = model.call_args.args[2]
        self.assertEqual(len(payload['blocks']), 12)
        self.assertEqual(len(payload['blocks'][0]['title']), 100)
        self.assertEqual(len(payload['blocks'][0]['text']), 440)

    def test_rejects_malformed_duplicate_and_unseen_model_ids(self):
        blocks = self.blocks + [{**self.blocks[0], 'id': f'D{i}'} for i in range(2, 8)]
        malformed = ([], {'ids': 'D1'}, {'ids': ['D1', 'D1']}, {'ids': ['D1', 'D9']}, {'ids': [1]},
                     {'ids': ['D1'], 'answer': '无依据结论'}, {'ids': [f'D{i}' for i in range(1, 7)]})
        for data in malformed:
            with (
                self.subTest(data=data),
                patch.object(summary, 'complete_json', return_value={'data': data, 'usage_tokens': 19}),
            ):
                ids, tokens, warning = self.call(blocks=blocks)
                self.assertIsNone(ids)
                self.assertEqual(tokens, 19)
                self.assertTrue(warning)

    def test_empty_selection_and_no_material_are_valid(self):
        with patch.object(summary, 'complete_json', return_value={'data': {'ids': []}, 'usage_tokens': 3}) as model:
            self.assertEqual(self.call(), ([], 3, ''))
            model.reset_mock()
            self.assertEqual(self.call(blocks=[]), ([], 0, ''))
            self.assertIsNone(self.call(blocks=[self.blocks[0]] * 2)[0])
            self.assertIsNone(self.call(blocks=[{**self.blocks[0], 'id': 't_0123456789abcdef01234567'}])[0])
            model.assert_not_called()

    def test_gateway_or_deadline_errors_never_emit_partial_selection(self):
        with patch.object(summary, 'complete_json') as model:
            ids, tokens, warning = self.call(budget=0.5)
            self.assertIsNone(ids)
            self.assertEqual(tokens, 0)
            self.assertIn('超时', warning)
            model.assert_not_called()
        for status in (502, 504):
            with patch.object(summary, 'complete_json', side_effect=ModelProtocolError('private provider response', status)):
                ids, tokens, warning = self.call()
            self.assertIsNone(ids)
            self.assertEqual(tokens, 0)
            self.assertNotIn('private provider response', warning)
            self.assertEqual('超时' in warning, status == 504)
        with (
            patch.object(summary.time, 'monotonic', side_effect=[100, 101, 120]),
            patch.object(summary, 'complete_json', return_value={'data': {'ids': ['D1']}, 'usage_tokens': 15}),
        ):
            ids, tokens, warning = self.call()
        self.assertIsNone(ids)
        self.assertEqual(tokens, 15)
        self.assertIn('超时', warning)
