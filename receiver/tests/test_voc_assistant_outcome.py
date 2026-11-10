import unittest
from types import SimpleNamespace
from unittest.mock import patch
from src import voc_assistant as assistant


class ConversationCertificateRendering(unittest.TestCase):
    def row(self, status='unknown', acceptance='unknown', auto=False):
        return {'id':'k1','question':'如何检查权限？','answer':'如果使用子账号，请检查商品权限。',
                'kind':'troubleshooting_advice','reply_patterns':['请先检查子账号商品权限。'],
                'conversation_verification':{'status':status,'acceptance':acceptance,
                    'effect':'not_established','auto_reusable':auto,'evidence':[{'role':'user','quote':'明白了'}]}}

    def test_understanding_does_not_claim_success(self):
        text=assistant._knowledge_text(self.row('accepted','understood',True))
        self.assertIn('操作效果未据此确认',text)
        self.assertIn('后续会话证据：明白了',text)
        self.assertNotIn('客户反馈操作成功',text)

    def test_unknown_and_negative_are_not_reusable_reply_claims(self):
        for status in ('unknown','weak','negative'):
            with self.subTest(status=status):
                text=assistant._knowledge_text(self.row(status))
                self.assertIn('来源参考',text)
                self.assertIn('闭环未通过',text)
                self.assertNotIn('可复用话术：',text)

    def test_selector_gets_program_verdict(self):
        material=assistant._selection_material(self.row('accepted','understood',True))
        self.assertEqual(material['conversation_verification']['auto_reusable'],True)
        self.assertEqual(material['conversation_verification']['effect'],'not_established')

    def test_coverage_reports_processed_pending_and_verified_separately(self):
        cfg=SimpleNamespace(runtime=SimpleNamespace(voc_domain='zhihuige'),llm=SimpleNamespace(model_main='test-model'))
        plan={'intent':'coverage','query':'','days':None,'limit':5,'support_intent':None,'diagnosis_status':None}
        data={'source_pairs':100,'total_events':8,'consultation_count':6,'ready_knowledge':3,
              'auto_reusable_knowledge':1,'exception_count':2,'coverage':{'processed_pairs':17,'pending_pairs':83}}
        engine=SimpleNamespace(summary=lambda cfg,filters:data)
        with patch.object(assistant,'_plan',return_value=(plan,0)),patch.object(assistant,'_engine',return_value=engine):
            answer=assistant.answer(cfg,{}, {'message':'整理了多少，多少可复用？'})['answer']
        for expected in ('来源共 100 条','已整理 17 条、待整理 83 条','有来源的软件知识 3 条','可直接复用 1 条'):
            self.assertIn(expected,answer)


if __name__=='__main__':unittest.main()
