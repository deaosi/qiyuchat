"""Evidence-composition regressions with synthetic data and no provider traffic."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import voc_answer as answer


def completion(data, tokens=11):
    return {'data': data, 'usage_tokens': tokens, 'model': 'synthetic-model'}


class EvidenceBoundComposition(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(llm=SimpleNamespace(model_main='any-provider-model'))
        self.scope = {'label': '本轮筛选范围'}

    def source(self, *, scope='仅适用于子账号。', answer_text='如果使用子账号，请检查商品权限。',
               kind='knowledge', knowledge_kind='troubleshooting_advice', acceptance='unknown', auto=False,
               effect='not_established', reuse=None, evidence=None):
        row = {'kind': knowledge_kind, 'answer': answer_text, 'scope': scope,
               'conversation_verification': {'status': 'accepted' if auto else 'unknown',
                   'acceptance': acceptance, 'effect': effect, 'auto_reusable': auto}}
        if reuse is not None:
            row['knowledge_reuse'] = {'level': reuse, 'reason_codes': []}
            if evidence is not None:
                row['knowledge_reuse']['evidence'] = evidence
        return answer.material(kind, 'source-1', answer_text, answer_text,
                               citation={'kind': 'knowledge', 'id': 'private-source-id'}, row=row)

    def draft(self, text='仅适用于子账号。可以先核对商品权限，再结合实际反馈继续排查。',
              *, quote='如果使用子账号，请检查商品权限。', identity='source-1', follow_up=''):
        return {'paragraphs': [{'text': text, 'references': [{'id': identity, 'quote': quote}]}],
                'follow_up': follow_up}

    def compose(self, draft=None, *, sources=None, verdict=None, history=None):
        draft = draft if draft is not None else self.draft()
        sources = sources if sources is not None else [self.source()]
        verdict = verdict if verdict is not None else {'supported': True, 'unsupported': []}
        with patch.object(answer, 'complete_json', side_effect=[completion(draft, 11), completion(verdict, 7)]) as model:
            result = answer.compose(self.cfg, '子账号的场景为什么没有触发？', history or [], self.scope, sources)
        return result, model

    def assert_rejected(self, result):
        composed, tokens, warning = result
        self.assertIsNone(composed)
        self.assertGreaterEqual(tokens, 0)
        self.assertTrue(warning)

    def test_natural_answer_requires_both_aligned_evidence_and_a_positive_verdict(self):
        history = [{'role': 'user', 'content': f'合成追问 {index}'} for index in range(6)]
        (composed, tokens, warning), model = self.compose(history=history)
        self.assertEqual(composed['answer'], self.draft()['paragraphs'][0]['text'])
        self.assertEqual(composed['used_ids'], {'source-1'})
        self.assertEqual(tokens, 18)
        self.assertEqual(warning, '')
        self.assertEqual(model.call_count, 2)
        self.assertEqual(model.call_args_list[0].args[1], answer.WRITE)
        self.assertEqual(model.call_args_list[1].args[1], answer.VERIFY)
        payload = model.call_args_list[0].args[2]
        self.assertEqual(payload['history'], history)
        self.assertNotIn('_citation', payload['sources'][0])

    def test_unknown_reference_is_rejected_before_semantic_verification(self):
        result, model = self.compose(self.draft(identity='not-in-retrieval'))
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_non_original_quote_is_rejected_before_semantic_verification(self):
        result, model = self.compose(self.draft(quote='请打开自动修复菜单，一键解决。'))
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_fabricated_chinese_adjacent_numbers_do_not_reach_the_verifier(self):
        for text in ('等待999秒即可同步。', '当前已处理99条咨询。', '命中率达到99.9%。'):
            with self.subTest(text=text):
                result, model = self.compose(self.draft('仅适用于子账号。' + text))
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)

    def test_source_numbers_can_be_reworded_without_being_invented(self):
        for kind in ('knowledge', 'case'):
            with self.subTest(kind=kind):
                source = self.source(scope='', answer_text='建议等待30秒后再测试场景。', kind=kind)
                text = '可以等待30秒，再测试场景。'
                (composed, _, warning), model = self.compose(self.draft(text, quote=source['proof']), sources=[source])
                self.assertEqual(composed['answer'], text)
                self.assertFalse(warning)
                self.assertEqual(model.call_count, 2)

    def test_computed_statistics_cannot_be_reworded_in_generated_prose(self):
        source = self.source(scope='', answer_text='当前来源问答12条，已整理8条。', kind='computed')
        for text, follow_up in (
                ('目前有12条来源，其中8条已经整理。', ''),
                ('目前有8条来源，其中12条已经整理。', ''),
                ('目前已整理999条来源。', ''),
                ('当前还有来源尚未整理。', '当前来源为8条、已整理12条，你想了解哪项？')):
            with self.subTest(text=text, follow_up=follow_up):
                result, model = self.compose(self.draft(text, quote=source['proof'], follow_up=follow_up), sources=[source])
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)

    def test_computed_statistics_allow_a_number_free_natural_explanation(self):
        source = self.source(scope='', answer_text='当前来源问答12条，已整理8条。', kind='computed')
        text = '当前还有来源尚未整理，现有结果只反映已整理部分。'
        (composed, _, warning), model = self.compose(self.draft(text, quote=source['proof']), sources=[source])
        self.assertEqual(composed['answer'], text)
        self.assertFalse(warning)
        self.assertEqual(model.call_count, 2)

    def test_computed_zero_verified_answers_cannot_be_promoted_to_a_success_claim(self):
        source = self.source(scope='', answer_text='会话验证通过0条，有来源知识3条。', kind='computed')
        result, model = self.compose(self.draft('答案已验证通过，可直接复用。', quote=source['proof']), sources=[source])
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_missing_applicability_cannot_be_accepted_even_if_the_verifier_would_agree(self):
        result, model = self.compose(self.draft('可以检查商品权限。'))
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_omitted_capability_negation_is_rejected(self):
        source = self.source(scope='', knowledge_kind='capability_fact', answer_text='当前不支持跨平台同步。')
        result, model = self.compose(self.draft('可以跨平台同步。', quote=source['proof']), sources=[source])
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_unclosed_advice_and_cases_cannot_claim_a_verified_fix(self):
        for kind in ('knowledge', 'case'):
            with self.subTest(kind=kind):
                source = self.source(scope='', kind=kind)
                result, model = self.compose(self.draft('问题已经修复，可以直接复用。'), sources=[source])
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)

    def test_understanding_feedback_cannot_be_promoted_to_operational_success(self):
        source = self.source(scope='', acceptance='understood', auto=True)
        result, model = self.compose(self.draft('客户已经操作成功。'), sources=[source])
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_guidance_with_silent_feedback_can_be_reused_with_scope(self):
        source = self.source(reuse='guidance')
        text = '仅适用于子账号。可以检查商品权限，此指引可按条件复用。'
        (composed, _, warning), model = self.compose(self.draft(text), sources=[source])
        self.assertEqual(composed['answer'], text)
        self.assertFalse(warning)
        self.assertEqual(model.call_args_list[0].args[2]['sources'][0]['knowledge_reuse'], {'level': 'guidance'})

    def test_effect_claim_requires_both_current_verified_reuse_and_reported_success(self):
        for level, acceptance, effect in (
            ('guidance', 'resolved', 'customer_reported_success'),
            ('reference', 'resolved', 'customer_reported_success'),
            ('verified', 'understood', 'not_established'),
            ('verified', 'resolved', 'not_established'),
        ):
            with self.subTest(level=level, acceptance=acceptance, effect=effect):
                source = self.source(scope='', reuse=level, acceptance=acceptance, auto=True, effect=effect)
                result, model = self.compose(self.draft('来源案例中的设置已经生效。'), sources=[source])
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)
        source = self.source(scope='', acceptance='resolved', auto=True, effect='customer_reported_success')
        text = '来源案例中的设置已经生效。'
        (composed, _, warning), _ = self.compose(self.draft(text), sources=[source])
        self.assertEqual(composed['answer'], text)
        self.assertFalse(warning)

    def effect_feedback(self):
        quote = '我已经打开商品权限，重新测试后场景已经生效。'
        return {'quote': quote, 'role': 'merchant', 'kind': 'resolved', 'message_id': 'synthetic-message', 'ts': 123,
                'qa_id': 5, 'span_coordinate': 'clean_visible_text',
                'source_refs': [{'message_id': 'synthetic-message', 'start': 0, 'end': len(quote)}]}

    def test_exact_reuse_feedback_supports_effect_even_when_legacy_closure_unknown(self):
        feedback = self.effect_feedback()
        source = self.source(scope='', reuse='verified', evidence=[feedback])
        text = '来源案例中，客户开启权限后复测，设置已经生效。'
        (composed, _, warning), model = self.compose(self.draft(text, quote=feedback['quote']), sources=[source])
        self.assertEqual(composed['answer'], text)
        self.assertFalse(warning)
        material = model.call_args_list[0].args[2]['sources'][0]
        self.assertEqual(material['verification']['status'], 'unknown')
        self.assertEqual(material['knowledge_reuse']['level'], 'verified')
        self.assertIn(feedback['quote'], material['proof'])
        self.assertNotIn('synthetic-message', str(material))

    def test_operational_claim_must_cite_complete_feedback_when_available(self):
        feedback = self.effect_feedback()
        source = self.source(scope='', reuse='verified', evidence=[feedback])
        for quote in ('如果使用子账号，请检查商品权限。', '场景已经生效。'):
            with self.subTest(quote=quote):
                result, model = self.compose(self.draft('来源案例中的设置已经生效。', quote=quote), sources=[source])
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)

    def test_missing_or_malformed_explicit_feedback_never_falls_back_to_legacy_success(self):
        feedback = self.effect_feedback()
        for evidence in (None, [], [{**feedback, 'role': 'staff'}], [{**feedback, 'kind': 'understood'}],
                         [{**feedback, 'source_refs': [{**feedback['source_refs'][0], 'end': 0}]}]):
            with self.subTest(evidence=evidence):
                source = self.source(scope='', reuse='verified', evidence=evidence, acceptance='resolved',
                                     auto=True, effect='customer_reported_success')
                self.assertEqual(source['knowledge_reuse']['level'], 'reference')
                result, model = self.compose(self.draft('来源案例中的设置已经生效。'), sources=[source])
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)

    def test_legacy_weak_acknowledgements_cannot_be_labeled_reusable(self):
        source = self.source(scope='', acceptance='weak_closure', auto=True)
        result, model = self.compose(self.draft('这条处理方法可直接复用。'), sources=[source])
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_explicitly_unconfirmed_fix_is_not_misread_as_a_success_claim(self):
        source = self.source(scope='', answer_text='当前无法确认故障是否修复，请提供测试结果。')
        text = '目前没有依据确认问题已经修复。'
        (composed, _, warning), model = self.compose(self.draft(text, quote=source['proof']), sources=[source])
        self.assertIsNotNone(composed)
        self.assertEqual(composed['answer'], text)
        self.assertFalse(warning)
        self.assertEqual(model.call_count, 2)

    def test_follow_up_cannot_smuggle_an_unsupported_number(self):
        result, model = self.compose(self.draft(follow_up='系统将在999秒后自动恢复，你是否看到了？'))
        self.assert_rejected(result)
        self.assertEqual(model.call_count, 1)

    def test_follow_up_cannot_promote_unclosed_cases_or_knowledge_to_a_verified_fix(self):
        for kind in ('case', 'knowledge'):
            with self.subTest(kind=kind):
                source = self.source(scope='', kind=kind)
                result, model = self.compose(self.draft('可以先检查商品权限。', follow_up='问题已经修复，你要继续吗？'), sources=[source])
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)

    def case_with_domain_conditions(self):
        proof = '在抖音平台，请确认已开启场景；子账号接待时先检查商品权限。'
        row = {'domain': {
            'platforms': [{'quote': '抖音平台', 'source': 'question', 'role': 'merchant'}],
            'prerequisites': [{'quote': '已开启场景', 'source': 'answer', 'role': 'staff'}],
            'reproduction_context': [{'quote': '子账号接待', 'source': 'question', 'role': 'merchant'}],
        }}
        return answer.material('case', 'source-1', proof, proof, row=row)

    def test_case_cannot_drop_its_platform_prerequisite_or_reproduction_context(self):
        source = self.case_with_domain_conditions()
        for text in ('已开启场景且属于子账号接待时，可以检查商品权限。',
                     '抖音平台的子账号接待可以检查商品权限。',
                     '抖音平台已开启场景时，可以检查商品权限。'):
            with self.subTest(text=text):
                result, model = self.compose(self.draft(text, quote=source['proof']), sources=[source])
                self.assert_rejected(result)
                self.assertEqual(model.call_count, 1)

    def test_case_can_be_explained_with_all_domain_conditions_preserved(self):
        source = self.case_with_domain_conditions()
        text = '在抖音平台，已开启场景且属于子账号接待时，可先检查商品权限。'
        (composed, _, warning), model = self.compose(self.draft(text, quote=source['proof']), sources=[source])
        self.assertEqual(composed['answer'], text)
        self.assertFalse(warning)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(model.call_args_list[0].args[2]['sources'][0]['constraints'], ['抖音平台', '已开启场景', '子账号接待'])

    def test_the_semantic_verifier_can_veto_an_aligned_but_unsupported_paraphrase(self):
        for verdict in ({'supported': False, 'unsupported': ['结论超出原文']},
                        {'supported': True, 'unsupported': ['结论仍有问题']},
                        {'supported': 1, 'unsupported': []},
                        {'supported': True, 'unsupported': [], 'extra': 'invalid'}):
            with self.subTest(verdict=verdict):
                result, model = self.compose(verdict=verdict)
                self.assert_rejected(result)
                self.assertEqual(result[1], 18)
                self.assertEqual(model.call_count, 2)

    def test_writer_failure_requests_source_fallback_without_leaking_provider_details(self):
        source = self.source()
        original = copy.deepcopy(source)
        with patch.object(answer, 'complete_json', side_effect=RuntimeError('private-key-and-upstream-body')) as model:
            result = answer.compose(self.cfg, '读取说明', [], self.scope, [source])
        self.assert_rejected(result)
        self.assertEqual(result[1], 0)
        self.assertNotIn('private-key', result[2])
        self.assertNotIn('upstream-body', result[2])
        self.assertEqual(source, original)
        model.assert_called_once()

    def test_verifier_failure_never_returns_the_unverified_draft(self):
        with patch.object(answer, 'complete_json', side_effect=[completion(self.draft(), 11), RuntimeError('private checker response')]):
            result = answer.compose(self.cfg, '读取说明', [], self.scope, [self.source()])
        self.assert_rejected(result)
        self.assertEqual(result[1], 11)
        self.assertNotIn('private checker response', result[2])

    def test_arbitrary_gateway_model_ids_use_the_same_evidence_contract(self):
        for model_id in ('qwen-test', 'another-vendor/family-2026', 'office-channel-custom-model'):
            with self.subTest(model=model_id):
                self.cfg.llm.model_main = model_id
                (composed, _, warning), model = self.compose()
                self.assertIsNotNone(composed)
                self.assertFalse(warning)
                self.assertEqual(model.call_args_list[0].args[0].model_main, model_id)
                self.assertEqual(model.call_args_list[1].args[0].model_main, model_id)

    def test_no_complete_source_means_no_model_call(self):
        with patch.object(answer, 'complete_json') as model:
            result = answer.compose(self.cfg, '读取说明', [], self.scope, [])
        self.assert_rejected(result)
        self.assertEqual(result[1], 0)
        model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
