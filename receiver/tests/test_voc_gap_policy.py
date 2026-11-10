"""Merchant incident progress is not automatically a reusable-knowledge gap."""
from __future__ import annotations
import unittest
from tests import test_voc_support_engine as support
from tests.test_voc_engine import extracted
from src import voc_engine as engine
from src.voc_extract import validate,correction_feedback
from src.voc_model import VocModelError


class TestDomainGapPolicy(unittest.TestCase):
    sql=support.TestSupportEngine.sql
    seed=support.TestSupportEngine.seed
    run_engine=support.TestSupportEngine.run_engine
    install_provider=support.TestSupportEngine.install_provider
    def setUp(self):support.TestSupportEngine.setUp(self)

    def configure(self,question,answer,*,gap=None,method=True):
        self.seed(question=question,answer=answer)
        self.install_provider(primary='functional_anomaly',methods={1:[('bounded_check',answer,'suggested')]} if method else {},diagnoses={1:{'status':'pending','cause_code':'','cause':None,'evidence':[]}})
        actual=self.provider.extract
        def payload(cfg,source,candidates,limit):
            result=actual(cfg,source,candidates,limit)
            result['payload']['intents'][0]['missing_info']=gap or []
            return result
        self.provider.extract=payload

    def gap(self,request,quote,kind='missing_dependency'):
        return [{'kind':kind,'request_quote':request,'evidence_source':'answer','evidence_quote':quote,'reason':'需要进一步了解当前个案条件。'}]

    def test_conditional_permission_check_survives_unproven_case_parameter_gap(self):
        q='知汇鸽子账号同步商品没有成功，该如何排查？'
        a='您是在用子账号同步商品吗？如果是子账号，请先核对商品权限后再同步。'
        self.configure(q,a,gap=self.gap('该如何排查','如果是子账号，请先核对商品权限后再同步。'))
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(1,0))
        row=engine._events(self.cfg,{})[0]
        self.assertEqual(row['data']['missing_info'],[])
        self.assertEqual(row['data']['gap_audit'][0]['authority'],'model_annotation')
        item=engine.knowledge(self.cfg,{})['items'][0]
        self.assertEqual((item['kind'],item['diagnosis_status'],item['effect_status']),('troubleshooting_advice','pending','suggested'))
        self.assertIn('如果是子账号',item['answer']);self.assertNotIn('需要进一步了解',item['answer'])

    def test_shop_identity_collection_is_processed_but_never_ready(self):
        q='知汇鸽的机器人没有触发，该怎么办？';a='请提供店铺名称，我帮您继续核查。'
        self.configure(q,a,gap=self.gap('该怎么办',a,'explicit_unknown'))
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(1,0))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_future_notification_is_processed_but_never_ready(self):
        q='知汇鸽问题排查进展怎么样？';a='有排查结果后会再同步给您。'
        self.configure(q,a,gap=self.gap('排查进展怎么样',a))
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(1,0))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_unknown_requested_recovery_time_not_washed_by_wrong_gap_kind_and_method(self):
        q='这个软件功能什么时候恢复？';a='该功能恢复时间尚未确定，请先关闭场景再开启测试。'
        self.configure(q,a,gap=self.gap('什么时候恢复','请先关闭场景再开启测试。'))
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(1,0))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_unknown_requested_parameter_not_washed_by_wrong_gap_kind(self):
        q='知汇鸽回调接口地址应填什么？';a='回调地址尚未给出，无法确定当前配置参数。请先打开回调设置页面。'
        self.configure(q,a,gap=self.gap('应填什么','请先打开回调设置页面。'))
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(1,0))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_actual_missing_dependency_remains_gap_even_without_model_annotation(self):
        q='知汇鸽如何导入这组配置？';a='配置文件尚未提供，暂时无法确认具体导入步骤。请先进入配置页面。'
        self.configure(q,a)
        self.run_engine()
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_illegal_or_foreign_reference_is_still_rejected(self):
        q='知汇鸽子账号同步如何排查？';a='如果使用子账号，请核对商品权限后再同步。'
        self.configure(q,a,gap=self.gap('另一个请求',a))
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(0,1))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_true_unanswered_request_remains_blocking_and_never_ready(self):
        q='知汇鸽导出报表应该在哪里操作？'
        self.configure(q,'',gap=[{'kind':'unanswered_request','request_quote':q,'evidence_source':'question','evidence_quote':q,'reason':'当前请求无客服答复。'}],method=False)
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(1,0))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)
        self.assertEqual(engine._events(self.cfg,{})[0]['data']['missing_info'][0]['kind'],'unanswered_request')

    def test_answer_reference_from_another_intent_is_not_downgraded_to_audit(self):
        from tests.test_voc_domain import domain,ref
        q='如何同步知识？如何导出报表？';a='请同步知识并保存。报表导出的具体参数尚未确定。'
        source={'qa_id':1,'question':q,'answer':a,'_staff_verified':True,'_domain_mode':'zhihuige'}
        d=domain(primary_intent='configuration_guidance',merchant_goal=ref('question','如何同步知识？'),diagnosis={'status':'not_applicable','cause_code':'','cause':None,'evidence':[]})
        payload=extracted(source,question_quote='如何同步知识？',domain=d,missing_info=self.gap('同步知识','报表导出的具体参数尚未确定。','explicit_unknown'))
        with self.assertRaises(VocModelError) as raised:validate(payload,source,[],self.aligner)
        self.assertIn('evidence_quote',correction_feedback(raised.exception)['field_path'])

    def test_invalid_gap_enum_is_still_contract_error_with_controlled_feedback(self):
        source={'qa_id':1,'question':'如何同步商品？','answer':'请核对商品权限后再同步。'}
        payload=extracted(source,missing_info=self.gap('同步商品',source['answer'],'needs_secret_CHECK'))
        with self.assertRaises(VocModelError) as raised:validate(payload,source,[],self.aligner)
        feedback=correction_feedback(raised.exception)
        self.assertEqual((feedback['code'],feedback['field_path']),('unsupported_blocking_gap','intents[0].missing_info[0].kind'))
        self.assertNotIn('needs_secret',str(feedback))

    def test_domain_classification_feedback_has_its_own_safe_code_and_path(self):
        for path in ('domain.primary_intent','domain.fragment_role'):
            error=VocModelError('领域合同：'+path+': 当前来源有明确软件目标，不得误标背景。')
            feedback=correction_feedback(error)
            self.assertEqual((feedback['code'],feedback['field_path']),('domain_classification',path))

    def test_condition_check_is_not_blocked_by_a_lexically_plausible_pending_annotation(self):
        q='知汇鸽商品同步该如何排查？';a='如果使用子账号，需要确认商品权限后再同步。'
        self.configure(q,a,gap=self.gap('该如何排查','需要确认商品权限后再同步。','explicit_unknown'))
        result=self.run_engine()
        self.assertEqual((result['job']['processed'],result['job']['failed']),(1,0))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],1)
        self.assertTrue(engine._events(self.cfg,{})[0]['data']['gap_audit'])

    def test_generic_unsupported_gap_stays_strict_and_feedback_identifies_kind(self):
        source={'qa_id':1,'question':'子账号同步如何排查？','answer':'如果使用子账号，请核对商品权限后再同步。'}
        payload=extracted(source,missing_info=self.gap('同步如何排查',source['answer']))
        with self.assertRaises(VocModelError) as raised:validate(payload,source,[],self.aligner)
        feedback=correction_feedback(raised.exception)
        self.assertEqual(feedback['code'],'unsupported_blocking_gap')
        self.assertEqual(feedback['field_path'],'intents[0].missing_info[0].kind')


if __name__=='__main__':unittest.main()
