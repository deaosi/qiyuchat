"""Recover omitted current software targets without borrowing adjacent facts."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from src import voc_domain as support, voc_engine as engine
from src.voc_extract import LangExtractAligner
from src.voc_model import VocModelError
from tests import test_voc_engine as fixtures
from tests.test_voc_domain import domain, ref


class TestCapabilityContext(unittest.TestCase):
    sql=fixtures.TestEngine.sql
    seed=fixtures.TestEngine.seed
    run_engine=fixtures.TestEngine.run_engine

    def setUp(self):
        fixtures.TestEngine.setUp(self)
        self.cfg=replace(self.cfg,runtime=replace(self.cfg.runtime,voc_domain="zhihuige"))
        self.aligner=LangExtractAligner()

    def source(self,question="一个店铺的也不行吗？",answer="目前暂不支持导出数据。",**changes):
        return {"qa_id":1,"question":question,"answer":answer,"_staff_verified":True,**changes}

    def span(self,src,quote=None):
        quote=quote or src["question"];start=src["question"].index(quote)
        return {"quote":quote,"start":start,"end":start+len(quote)}

    def proof(self,src,quote=None):
        return support.current_software_capability(src,self.span(src,quote),self.aligner)

    def normalized(self,src,*,primary="configuration_guidance",fragment="follow_up",quote=None):
        qspan=self.span(src,quote)
        raw=domain(primary_intent=primary,fragment_role=fragment,merchant_goal=ref("question",qspan["quote"]),
            diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]})
        return support.validate_domain(raw,src,qspan,self.aligner)

    def test_current_verified_answer_supplies_omitted_software_object(self):
        src=self.source(_answer_parts=[{"message_id":"source:s:2","start":0,"end":13}])
        src["_answer_parts"][0]["end"]=len(src["answer"])
        proof=self.proof(src)
        self.assertTrue(proof["strong_request"] and proof["answer_target"])
        self.assertEqual(proof["answer_refs"][0]["role"],"staff")
        self.assertEqual(proof["answer_refs"][0]["source_refs"][0]["message_id"],"source:s:2")
        result=self.normalized(src)
        self.assertFalse(result["needs_context"])
        self.assertEqual((result["modules"],result["platforms"]),([],[]))

    def test_wrong_unclear_classification_has_only_a_static_correction_path(self):
        src=self.source(question="PRIVATE_SYNTHETIC_NAME，一个店铺的也不行吗？")
        with self.assertRaises(VocModelError) as caught:self.normalized(src,primary="unclear")
        self.assertIn("domain.primary_intent",str(caught.exception))
        self.assertNotIn("PRIVATE_SYNTHETIC_NAME",str(caught.exception))
        self.assertTrue(caught.exception.retryable)

    def test_explicit_knowledge_export_request_is_not_silently_context_only(self):
        src=self.source(question="请帮忙导出这个店铺的全部知识文档。",answer="目前不支持任何导出，节后我给您导出可以吗？")
        self.assertTrue(self.proof(src)["strong_request"])
        with self.assertRaises(VocModelError) as caught:self.normalized(src,primary="unclear",fragment="request")
        self.assertIn("domain.primary_intent",str(caught.exception))
        with self.assertRaises(VocModelError) as caught:self.normalized(src,fragment="reproduction_context")
        self.assertIn("domain.fragment_role",str(caught.exception))

    def test_model_gets_at_most_one_correction_then_current_fact_compiles(self):
        self.seed(question="一个店铺的也不行吗？",answer="目前暂不支持导出数据哈。")
        attempts=[]
        def payload(src,candidates,original):
            attempts.append(1)
            raw=domain(primary_intent="unclear" if len(attempts)==1 else "configuration_guidance",fragment_role="follow_up",
                merchant_goal=ref("question",src["question"]),diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]})
            return fixtures.extracted(src,candidates,question="当前是否支持导出数据？",theme="软件能力",boundary="当前软件数据导出能力",domain=raw)
        self.provider=fixtures.FakeProvider(payload)
        result=self.run_engine(max_calls=2)
        self.assertEqual((result["job"]["processed"],result["job"]["failed"],result["job"]["calls"]),(1,0,2))
        asset=engine.knowledge(self.cfg,{})["items"][0]
        self.assertEqual((asset["kind"],asset["answer"]),("capability_fact","目前暂不支持导出数据哈。"))
        self.assertEqual(engine.summary(self.cfg,{})["coverage"]["context_needed_pairs"],0)
        self.assertEqual(self.run_engine()["job"]["calls"],0)

    def test_plain_name_and_one_off_relogin_still_need_context(self):
        src=self.source(question="某某游泳用品店",answer="已修改了，辛苦退出重新登录一下。")
        self.assertFalse(self.proof(src)["strong_request"])
        self.assertTrue(self.normalized(src,primary="unclear",fragment="reproduction_context")["needs_context"])

    def test_a_bare_software_name_is_not_itself_a_usage_request(self):
        src=self.source(question="知汇鸽？")
        self.assertFalse(self.proof(src)["strong_request"])

    def test_sku_or_spec_question_does_not_borrow_an_export_answer(self):
        for question in ("SKU A01这个规格也不行吗？","蓝色款500ml这个尺寸也不行吗？"):
            with self.subTest(question=question):
                src=self.source(question=question)
                self.assertFalse(self.proof(src)["answer_target"])
                self.assertTrue(self.normalized(src,primary="unclear")["needs_context"])

    def test_pure_buyer_policy_does_not_turn_into_a_software_consultation(self):
        for question in ("这个商品支持7天退货吗？","退款什么时候到账？","这个商品多少钱？"):
            with self.subTest(question=question):
                src=self.source(question=question)
                self.assertFalse(self.proof(src)["strong_request"])

    def test_notification_or_future_manual_service_is_not_a_current_target(self):
        for answer in ("有结果后这边同步给您。","双十一后我可以给您导出数据。","目前不支持导出数据，节后我给您导出可以吗？"):
            with self.subTest(answer=answer):
                src=self.source(answer=answer)
                self.assertFalse(self.proof(src)["answer_target"])
                self.assertTrue(self.normalized(src,primary="unclear")["needs_context"])

    def test_unknown_role_tentative_support_or_adjacent_fact_cannot_recover(self):
        cases=[self.source(_staff_verified=False),self.source(answer="可能暂时不支持导出数据。"),
               self.source(answer="",_context=[{"role":"staff","content":"目前暂不支持导出数据。","comparison_only":True}])]
        for src in cases:
            with self.subTest(answer_length=len(src["answer"])):
                self.assertFalse(self.proof(src)["answer_target"])

    def test_answer_from_another_current_intent_is_not_available_to_the_ellipsis(self):
        first="一个店铺的也不行吗？"
        src=self.source(question=first+"知汇鸽报表怎么导出？",answer="后台目前不支持导出报表。")
        proof=self.proof(src,first)
        self.assertFalse(proof["strong_request"])
        self.assertTrue(self.normalized(src,primary="unclear",quote=first)["needs_context"])

    def test_mixed_export_and_manual_offer_stays_nonready_after_correcting_demand(self):
        question="请帮忙导出全部知识文档。";answer="目前不支持任何导出，节后我给您导出可以吗？"
        self.seed(question=question,answer=answer)
        raw=domain(primary_intent="configuration_guidance",merchant_goal=ref("question",question),
            diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]})
        self.provider=fixtures.FakeProvider(lambda src,candidates,original:fixtures.extracted(src,candidates,question="知识文档是否支持导出？",domain=copy.deepcopy(raw)))
        self.run_engine()
        self.assertEqual(engine.topics(self.cfg,{})["items"][0]["event_count"],1)
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],0)


if __name__=="__main__":unittest.main()
