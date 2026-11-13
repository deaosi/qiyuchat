from unittest import TestCase
from unittest.mock import Mock

from src.voc_assistant_solutions import read_solution, read_solution_evidence


def source(qa, message, quote='已在原会话核对'):
    return {'qa_id': qa, 'message_id': message, 'quote': quote}


def branch(identity, qa, *, verified):
    question = source(qa, f'question-{qa}')
    answer = source(qa, f'answer-{qa}')
    return {'id': identity, 'status': 'verified' if verified else 'guidance',
            'condition': '退款场景仅适用于子账号', 'condition_evidence': [question],
            'steps': [{'text': '重新设置退款触发条件并保存。', 'evidence': [answer]}],
            'retest': '模拟一笔退款请求并检查是否触发。', 'retest_evidence': [answer],
            'result_evidence': [source(qa, f'result-{qa}', '客户反馈复测已经触发。')] if verified else []}


def event(identity, qa, message):
    return {'id': identity, 'qa_id': qa,
            'source': {'_question_parts': [{'message_id': message, 'start': 0, 'end': 5}]},
            'data': {'question_span': {'start': 0, 'end': 5}, 'claims': []}}


class TestSolutionReads(TestCase):
    def setUp(self):
        self.cfg = object()
        self.filters = {'days': 0, 'start': '2026-11-06', 'end': '2026-11-12', 'group': '售后群'}
        self.engine = Mock()
        self.engine._sources.return_value = {1: {}}
        self.engine._topic_view.return_value = []

    def test_complete_sops_count_groups_and_only_show_verified_branches_with_exact_citations(self):
        self.engine.complete_sops.return_value = {'items': [{'id': 'playbook_0',
            'title': '退款触发排查 https://internal.example/private episode_abcdefabcdefabcd',
            'branches': [branch('branch_1', 1, verified=True), branch('branch_2', 2, verified=True),
                         branch('branch_3', 3, verified=False)]}]}
        self.engine._topic_view.return_value = [event('episode_wrong_qa', 2, 'question-1'),
                                              event('episode_wrong_anchor', 1, 'other-question'),
                                              event('episode_exact', 1, 'question-1')]

        answer, citations = read_solution(self.cfg, self.filters, 'sops', '', 1, engine=self.engine)

        self.engine.complete_sops.assert_called_once_with(self.cfg, self.filters)
        self.engine.playbooks.assert_not_called()
        self.engine._sources.assert_called_once_with(self.cfg, self.filters, qa_ids={1})
        self.assertIn('1 项完整 SOP，共 2 个已验证分支，展示 1 个分支', answer)
        self.assertIn('退款场景仅适用于子账号', answer)
        self.assertIn('模拟一笔退款请求并检查是否触发', answer)
        self.assertIn('客户反馈复测已经触发', answer)
        self.assertNotIn('internal.example', answer)
        self.assertNotIn('episode_abcdefabcdefabcd', answer)
        self.assertNotIn('question-1', answer)
        self.assertEqual([(ref['kind'], ref['id']) for ref in citations], [('event', 'episode_exact')])

    def test_playbooks_rank_relevant_handling_and_keep_unverified_case_outcome_explicit(self):
        self.engine.playbooks.return_value = {'items': [{'id': 'playbook_0', 'title': '退款场景',
            'branches': [branch('branch_1', 1, verified=False)]}, {'id': 'playbook_1', 'title': '店铺同步',
            'branches': [], 'missing': ['complete_procedure'], 'evidence': []}], 'case_paths': [
            {'id': 'case_1', 'problem': source(7, 'sync-question', '店铺同步出现缺口'), 'platform': '抖音',
             'solution': {'title': '同步处理记录', 'sections': [
                 {'kind': 'action', 'text': '核对店铺授权'},
                 {'kind': 'verification', 'mode': 'expected', 'text': '同步状态应当显示成功'},
             ]}}]}
        self.engine.relevant_items.side_effect = lambda cfg, rows, query, **kwargs: [
            row for row in rows if row['id'] != 'playbook_1'][::-1]

        answer, _ = read_solution(self.cfg, self.filters, 'playbooks', '同步与退款', 2, engine=self.engine)

        self.engine.playbooks.assert_called_once_with(self.cfg, self.filters)
        self.assertIn('1 项处理指引（1 个分支）、1 条单案记录、0 项待补依据的排查，展示 2 条', answer)
        self.assertIn('预期通过标准\n同步状态应当显示成功', answer)
        self.assertIn('本记录未提供实际复测结果', answer)
        self.assertIn('已核验指引，效果待复测', answer)
        self.assertNotIn('已有案例的客户反馈', answer)
        self.assertNotIn('店铺同步\n当前处理指引', answer)

    def test_visible_sop_evidence_uses_one_complete_scoped_witness_and_exact_quotes(self):
        entry = branch('branch_1', 1, verified=True)
        entry['condition_evidence'].append(source(2, 'question-2', '另一个账号的提问'))
        entry['result_evidence'].append(source(2, 'result-2', '另一个账号的反馈'))
        entry['steps'][0]['evidence'][0]['quote'] = '先保存退款触发条件，再测试 https://internal.example/signed'
        self.engine.complete_sops.return_value = {'items': [{'id': 'playbook_0', 'title': '退款触发排查',
            'branches': [entry, branch('branch_2', 3, verified=True)]}]}
        self.engine._topic_view.return_value = [event('episode_other', 2, 'question-2'),
                                              event('episode_exact', 1, 'question-1'),
                                              event('episode_hidden', 3, 'question-3')]

        _, _, evidence = read_solution_evidence(self.cfg, self.filters, 'sops', '', 1, engine=self.engine)

        self.assertEqual(len(evidence), 1)
        found = evidence[0]
        self.assertEqual(found['condition'], '退款场景仅适用于子账号')
        self.assertEqual(found['feedback'], ['客户反馈复测已经触发。'])
        self.assertEqual(found['citations'], [{'kind': 'event', 'id': 'episode_exact', 'title': '退款触发排查'}])
        self.assertIn('重新设置退款触发条件并保存。', found['body'])
        self.assertIn('先保存退款触发条件，再测试 [已隐藏]', found['proof'])
        self.assertNotIn('另一个账号', found['proof'])
        self.assertNotIn('另一个账号', found['body'])
        self.assertNotIn('internal.example', found['proof'])

    def test_sop_model_evidence_requires_a_full_witness_with_unique_scoped_event(self):
        entry = branch('branch_1', 1, verified=True)
        second = branch('branch_2', 2, verified=True)
        second['retest_evidence'] = [source(9, 'unrelated-retest')]
        self.engine.complete_sops.return_value = {'items': [{'id': 'playbook_0', 'title': '退款触发排查',
            'branches': [entry, second]}]}
        self.engine._topic_view.return_value = [event('episode_first', 1, 'question-1'),
                                              event('episode_second', 1, 'question-1'),
                                              event('episode_unbound', 2, 'question-2')]

        answer, _, evidence = read_solution_evidence(self.cfg, self.filters, 'sops', '', 2, engine=self.engine)

        self.assertIn('2 个已验证分支', answer)
        self.assertEqual(evidence, [])

    def test_ambiguous_event_anchors_do_not_link_an_unrelated_intent(self):
        self.engine.playbooks.return_value = {'items': [], 'case_paths': [
            {'id': 'case_1', 'problem': source(7, 'sync-question', '店铺同步出现缺口'), 'steps': []}]}
        self.engine._topic_view.return_value = [event('episode_first', 7, 'sync-question'),
                                              event('episode_second', 7, 'sync-question')]

        _, citations = read_solution(self.cfg, self.filters, 'playbooks', '', 1, engine=self.engine)

        self.assertEqual(citations, [])

    def test_gaps_keep_explicitly_scoped_topics_and_omit_unattributed_sources(self):
        self.engine.exceptions.return_value = {'items': [
            {'id': 'ex_1', 'kind': 'context_needed', 'topic_id': 'topic_1', 'question': '退款异常', 'reason': '缺少复测原话'},
            {'id': 'ex_2', 'kind': 'extraction_failed', 'topic_id': '', 'question': '另一个问题', 'reason': '整理失败'},
            {'id': 'ex_3', 'kind': 'context_needed', 'topic_id': 'topic_2', 'question': '另一主题', 'reason': '待核对'},
        ]}
        scoped = {**self.filters, 'focus_topic': 'topic_1'}

        answer, citations = read_solution(self.cfg, scoped, 'gaps', '', 10, engine=self.engine)

        self.engine.exceptions.assert_called_once_with(self.cfg, scoped)
        self.engine._sources.assert_not_called()
        self.assertIn('匹配 1 项整理或证据缺口', answer)
        self.assertIn('缺少复测原话', answer)
        self.assertNotIn('整理失败', answer)
        self.assertEqual(citations, [{'kind': 'topic', 'id': 'topic_1', 'title': '退款异常'}])
