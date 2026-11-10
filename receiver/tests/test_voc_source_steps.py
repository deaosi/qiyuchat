"""Recover redundant action metadata from exact current staff step claims only."""
from __future__ import annotations

import unittest
from dataclasses import replace

from src import voc_engine as engine
from src.voc_extract import LangExtractAligner
from tests import test_voc_engine as fixtures
from tests.test_voc_domain import domain, ref


class TestSourceSteps(unittest.TestCase):
    sql=fixtures.TestEngine.sql
    seed=fixtures.TestEngine.seed
    run_engine=fixtures.TestEngine.run_engine

    def setUp(self):
        fixtures.TestEngine.setUp(self)
        self.cfg=replace(self.cfg,runtime=replace(self.cfg.runtime,voc_domain="zhihuige"))
        self.aligner=LangExtractAligner()

    def install_provider(self, *, quote=None, claim_quote=None, claim_type="step", correct_once=False, fragment="follow_up"):
        attempts=[]
        def payload(src,candidates,original):
            attempts.append(1);question=quote or src["question"]
            raw=domain(primary_intent="unclear" if correct_once and len(attempts)==1 else "configuration_guidance",
                fragment_role=fragment,merchant_goal=ref("question",question),
                diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]},actions=[])
            return fixtures.extracted(src,candidates,question="子账号同步商品时如何检查权限？",theme="账号授权",boundary="当前子账号商品权限检查",
                question_quote=question,domain=raw,claims=[{"type":claim_type,"quote":claim_quote or src["answer"]}])
        self.provider=fixtures.FakeProvider(payload)

    def knowledge(self):return engine.knowledge(self.cfg,{})["items"]

    def test_declarative_authorization_followup_recovers_current_conditional_check(self):
        question="我这里开店的时候授权的就是全部店铺"
        answer="请问是使用的子账号同步商品吗 如果是子账号同步确认一下是否有商品权限"
        self.seed(question=question,answer=answer)
        self.install_provider();before=self.sql("SELECT * FROM qa_pair")
        result=self.run_engine()
        self.assertEqual((result["job"]["processed"],result["job"]["failed"]),(1,0))
        item=self.knowledge()[0]
        self.assertEqual(item["answer"],answer)
        self.assertIn("如果是子账号",item["scope"])
        self.assertEqual((item["effect_status"],item["diagnosis_status"]),("suggested","not_applicable"))
        self.assertTrue(item["source_steps_recovered"])
        self.assertTrue(any("尚未确认故障根因或验证修复效果" in caution for caution in item["cautions"]))
        self.assertEqual(item["evidence"][0]["claims"][0]["role"],"staff")
        self.assertTrue(item["evidence"][0]["claims"][0]["source_refs"])
        self.assertEqual(self.sql("SELECT * FROM qa_pair"),before)

    def test_wrong_unclear_is_corrected_once_without_requiring_domain_actions(self):
        self.seed(question="所有店铺都是已经授权过的",answer="如果使用子账号，请确认商品权限后再同步。")
        self.install_provider(correct_once=True)
        result=self.run_engine(max_calls=2)
        self.assertEqual((result["job"]["processed"],result["job"]["failed"],result["job"]["calls"]),(1,0,2))
        self.assertEqual(len(self.knowledge()),1)
        self.assertEqual(self.run_engine()["job"]["calls"],0)

    def test_investigation_promise_is_not_a_customer_method(self):
        self.seed(question="所有店铺都是已经授权过的",answer="我先检查子账号权限，有结果再同步给您。")
        self.install_provider(fragment="request");self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_screenshot_collection_is_not_a_recovered_check(self):
        self.seed(question="所有店铺都已授权了",answer="请进入账号权限页面截图发我，我帮您核查。")
        self.install_provider(fragment="request");self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_name_only_and_one_off_relogin_are_not_promoted(self):
        self.seed(question="某某游泳用品店",answer="已修改了，辛苦退出重新登录一下。")
        self.install_provider(fragment="request");self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_completed_staff_status_is_not_a_reusable_step(self):
        self.seed(question="所有店铺都已授权了",answer="这边已经同步完成。")
        self.install_provider(fragment="request");self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_fact_label_alone_never_creates_an_action(self):
        self.seed(question="所有店铺都已授权了",answer="如果使用子账号，请确认商品权限后再同步。")
        self.install_provider(claim_type="fact");self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_partial_step_cannot_drop_its_antecedent(self):
        self.seed(question="所有店铺都已授权了",answer="如果是子账号，请检查商品权限。")
        self.install_provider(claim_quote="请检查商品权限")
        self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_foreign_step_cannot_be_borrowed_from_another_intent(self):
        current="所有店铺都已授权了。";other="报表导出失败怎么处理？"
        first="如果是子账号，请检查商品权限。";step="进入报表导出页面并保存设置。"
        self.seed(question=current+other,answer=first+step)
        self.install_provider(quote=current,claim_quote=step)
        self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_unknown_role_never_recovers_a_source_step(self):
        self.seed(question="所有店铺都已授权了",answer="如果使用子账号，请确认商品权限后再同步。")
        self.sql("UPDATE raw_message SET role='user' WHERE role='staff'")
        self.install_provider();self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_actual_missing_dependency_survives_action_metadata_recovery(self):
        self.seed(question="我在知汇鸽配置里已经操作过了",answer="配置文件尚未提供，暂时无法确认具体导入步骤。请先进入配置页面。")
        self.install_provider(fragment="request");self.run_engine()
        self.assertEqual(self.knowledge(),[])


if __name__=="__main__":unittest.main()
