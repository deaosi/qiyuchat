"""Scoped, synthetic retrieval regressions; no real model/network/source reads."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import voc_assets as assets, voc_engine as engine, voc_search as search
from src.voc_semantics import SemanticError, normalize


class SyntheticVectors:
    model = 'synthetic-voc-material-ranking'
    vocabulary = ('子账号', '发票', '安装', '权限', '授权', '微信', '开关', '淘宝')

    def encode(self, texts):
        result = []
        for text in texts:
            values = [float(word in text) for word in self.vocabulary]
            values.append(float(not any(values)))
            result.append(normalize(values))
        return result


def knowledge(identifier, answer='打开安装页面并固定底座。', **extra):
    return {'id': identifier, 'topic_id': 't1', 'question': '软件功能使用方法',
            'answer': answer, 'scope': '', 'steps': [], 'cautions': [], 'reply_patterns': [],
            'status': 'ready', 'evidence': [{'id': 'source-' + identifier}],
            'conversation_verification': {'auto_reusable': False}, **extra}


def event(identifier, question='子账号权限无法授权', *, staff=False, quote='以主账号重新授权子账号权限。'):
    return {'id': identifier, 'event_id': identifier, 'topic_id': 't1',
            'question_quote': question, 'question': question, 'answer': quote,
            'staff_source_verified': staff,
            'claims': [{'role': 'staff', 'quote': quote}] if staff else []}


class MaterialRetrieval(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Path(directory.name) / 'derived-only.db'
        assets.initialize(self.store)
        self.embedder = SyntheticVectors()
        self.cfg = object()
        for name, value in (('path', self.store), ('_semantic', self.embedder)):
            active = patch.object(engine, name, return_value=value)
            active.start(); self.addCleanup(active.stop)
        self.topics = [{'id': 't1', 'question': '软件功能使用问题', 'theme': '软件设置', 'samples': []}]

    def run_search(self, knowledge_items=(), event_items=(), *, query='子账号权限如何重新授权', limit=5, require_staff=False):
        scope = {'days': 7, 'group': 'synthetic-group', 'support_intent': 'configuration_guidance'}
        with patch.object(engine, 'topics', return_value={'items': self.topics}), \
             patch.object(engine, 'knowledge', return_value={'items': list(knowledge_items)}) as know, \
             patch.object(engine, '_events', return_value=list(event_items)) as events, \
             patch.object(engine.quality, 'evidence', side_effect=lambda row: row):
            result = engine.search(self.cfg, scope, query, limit, require_staff=require_staff)
            know.assert_called_once_with(self.cfg, scope)
            events.assert_called_once_with(self.cfg, scope)
            return result

    def test_sixth_answer_is_ranked_before_truncation_even_if_topic_title_is_broad(self):
        rows = [knowledge(f'k{i}') for i in range(1, 6)]
        rows.append(knowledge('k6', '以主账号重新授权子账号商品权限。'))
        before = deepcopy(rows)
        result = self.run_search(rows, limit=1)
        self.assertEqual([item['id'] for item in result['knowledge']], ['k6'])
        self.assertEqual(result['topics'][0]['id'], 't1')
        self.assertEqual(rows, before)
        self.assertFalse(result['knowledge'][0]['conversation_verification']['auto_reusable'])
        self.assertEqual(result['knowledge'][0]['evidence'], rows[-1]['evidence'])

    def test_same_topic_is_not_sufficient_to_select_an_unrelated_answer(self):
        result = self.run_search([knowledge('wrong')])
        self.assertEqual(result['knowledge'], [])

    def test_lexical_details_disambiguate_when_semantic_scores_are_identical(self):
        rows = [knowledge(f'k{i}') for i in range(5)]
        rows.append(knowledge('specific', '以主账号重新授权子账号商品权限。'))
        with patch.object(search, 'vectors', side_effect=lambda _store, _embedder, texts: [[1., 0.] for _ in texts]):
            result = self.run_search(rows, limit=1)
        self.assertEqual(result['knowledge'][0]['id'], 'specific')

    def test_semantic_paraphrase_can_match_without_shared_words(self):
        row = knowledge('paraphrase', '由主管账户再次授予访问能力。', question='解决访问受限的方法')
        with patch.object(search, 'vectors', side_effect=lambda _store, _embedder, texts: [[1., 0.] for _ in texts]):
            result = self.run_search([row], query='子账号权限重新授权', limit=1)
        self.assertEqual(result['knowledge'][0]['id'], 'paraphrase')
        self.assertEqual(result['knowledge'][0]['retrieval']['lexical'], 0)

    def test_all_evidence_is_retained_including_the_source_after_fifth(self):
        evidence = [{'id': f'e{i}', 'quote': 'source'} for i in range(8)]
        row = knowledge('many-sources', '以主账号重新授权子账号权限。', evidence=evidence)
        result = self.run_search([row], limit=1)
        self.assertEqual(result['knowledge'][0]['evidence'], evidence)

    def test_scope_and_operation_text_can_disambiguate_identical_questions(self):
        first = knowledge('unrelated', '请检查开关后保存。')
        second = knowledge('matching', '请按此路径设置。', scope='仅适用于需要重新授权的子账号权限。',
                           steps=['以主账号授权子账号商品权限。'])
        result = self.run_search([first, second], limit=1)
        self.assertEqual(result['knowledge'][0]['id'], 'matching')
        self.assertGreater(result['knowledge'][0]['retrieval']['lexical'], 0)

    def test_specific_clause_after_a_long_answer_is_not_lost(self):
        row = knowledge('late', '请检查开关并保存。' * 400 + '\n以主账号重新授权子账号商品权限。')
        result = self.run_search([row], limit=1)
        self.assertEqual(result['knowledge'][0]['id'], 'late')

    def test_human_reply_filter_is_applied_before_result_limit(self):
        rows = [event(f'user{i}') for i in range(8)]
        rows.append(event('staff9', staff=True))
        result = self.run_search(event_items=rows, limit=1, require_staff=True)
        self.assertEqual([item['id'] for item in result['events']], ['staff9'])
        self.assertTrue(result['events'][0]['staff_source_verified'])

    def test_evidence_queries_still_retrieve_visitor_context(self):
        result = self.run_search(event_items=[event('user-only')], limit=1)
        self.assertEqual(result['events'][0]['id'], 'user-only')
        self.assertFalse(result['events'][0]['staff_source_verified'])

    def test_staff_ranking_does_not_use_another_intents_full_answer(self):
        row = event('wrong-intent', question='安装底座如何操作', staff=True, quote='打开安装页面固定底座。')
        row['answer'] += '\n子账号权限重新授权。'
        result = self.run_search(event_items=[row], limit=1, require_staff=True)
        self.assertEqual(result['events'], [])

    def test_unverified_or_non_staff_claims_cannot_be_reply_candidates(self):
        rows = [event('unverified', staff=False), event('visitor-claim', staff=True)]
        rows[0]['claims'] = [{'role': 'staff', 'quote': '重新授权子账号权限。'}]
        rows[1]['claims'][0]['role'] = 'user'
        result = self.run_search(event_items=rows, require_staff=True)
        self.assertEqual(result['events'], [])

    def test_verified_pair_fallback_must_match_question_without_ranking_the_full_reply(self):
        verified = event('paired', staff=True)
        verified.update(claims=[], source_kind='source-anchored', answer_message_ids=['staff:1'])
        wrong = {**verified, 'id': 'other-intent', 'question': '安装底座如何操作', 'question_quote': '安装底座如何操作'}
        unanchored = {**verified, 'id': 'unanchored', 'answer_message_ids': []}
        result = self.run_search(event_items=[wrong, unanchored, verified], limit=1, require_staff=True)
        self.assertEqual([item['id'] for item in result['events']], ['paired'])

    def test_items_outside_the_scoped_topic_set_do_not_enter_results(self):
        rows = [knowledge('foreign', '重新授权子账号权限。', topic_id='outside')]
        result = self.run_search(rows)
        self.assertEqual(result['knowledge'], [])

    def test_relevant_topics_returns_the_complete_subset_not_only_ten(self):
        rows = [{'id': f't{i:02}', 'question': '子账号权限重新授权', 'theme': '权限', 'samples': []} for i in range(14)]
        rows.append({'id': 'irrelevant', 'question': '发票如何申请', 'theme': '发票', 'samples': []})
        with patch.object(engine, 'topics', return_value={'items': rows}):
            result = engine.relevant_topics(self.cfg, {'days': 7}, '子账号权限重新授权')
        self.assertEqual(len(result), 14)
        self.assertNotIn('irrelevant', {item['id'] for item in result})

    def test_missing_semantic_model_is_not_hidden_behind_a_keyword_fallback(self):
        with patch.object(search, 'vectors', side_effect=SemanticError('local model unavailable')):
            with self.assertRaises(engine.EngineError) as caught:
                engine.relevant_items(self.cfg, [knowledge('k', '重新授权子账号权限。')], '子账号权限')
        self.assertEqual(caught.exception.status, 503)

    def test_empty_scope_keeps_the_compatible_result_shape(self):
        with patch.object(engine, 'topics', return_value={'items': []}):
            self.assertEqual(engine.search(self.cfg, {}, '子账号权限'), {'topics': [], 'knowledge': [], 'events': []})


if __name__ == '__main__':
    unittest.main()
