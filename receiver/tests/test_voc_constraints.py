"""Compile-only applicability projection from exact current-intent evidence."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

from src import voc_engine as engine, voc_variants as variants
from src.voc_extract import LangExtractAligner
from tests import test_voc_engine as fixtures
from tests.test_voc_domain import domain, ref, confirmed


class TestVocConstraints(unittest.TestCase):
    sql=fixtures.TestEngine.sql
    seed=fixtures.TestEngine.seed
    run_engine=fixtures.TestEngine.run_engine

    def setUp(self):
        fixtures.TestEngine.setUp(self)
        self.cfg=replace(self.cfg,runtime=replace(self.cfg.runtime,voc_domain="zhihuige"))
        self.aligner=LangExtractAligner()

    def install_provider(self, *, scopes=None, domains=None, questions=None, claims=None):
        def payload(source,candidates,original):
            qa=source["qa_id"];question=(questions or {}).get(qa,source["question"])
            raw=copy.deepcopy((domains or {}).get(qa)) or domain(primary_intent="configuration_guidance",
                merchant_goal=ref("question",question),diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]},
                actions=[{"method_code":"save_software_settings","instruction":ref("answer",source["answer"]),"effect_status":"suggested","verification":None}])
            return fixtures.extracted(source,candidates,question="知汇鸽知识配置如何操作？",question_quote=question,theme="软件配置",boundary="软件配置及受限操作",
                domain=raw,scope=copy.deepcopy((scopes or {}).get(qa,[])),claims=[{"type":"step","quote":quote} for quote in (claims or {}).get(qa,[source["answer"]])])
        self.provider=fixtures.FakeProvider(payload)

    def knowledge(self):return engine.knowledge(self.cfg,{})["items"]

    def test_generic_version_survives_missing_domain_prerequisites(self):
        answer="进入场景设置页面并保存。"
        self.seed(question="知汇鸽2.8版本的场景怎么设置？",answer=answer)
        self.install_provider(scopes={1:[{"kind":"version","source":"question","quote":"2.8版本"}]})
        self.run_engine()
        item=self.knowledge()[0]
        self.assertIn("2.8版本",item["scope"])
        version=next(ref for ref in item["applicability"] if ref["quote"]=="2.8版本")
        self.assertEqual((version["role"],version["source"],version["alignment"]),("merchant","question","exact"))
        self.assertEqual(version["source_refs"][0]["start"],3)

    def test_all_generic_scope_kinds_merge_with_domain_constraints(self):
        question="知汇鸽2.8版本专业套餐仅管理员在活动期间使用知识导出怎么设置？"
        self.seed(question=question,answer="进入知识导出页面并保存设置。")
        values=[("version","2.8版本"),("product","专业套餐"),("audience","管理员"),("time","活动期间"),("condition","使用知识导出")]
        raw=domain(primary_intent="configuration_guidance",merchant_goal=ref("question",question),modules=[ref("question","知识导出")],
            diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]},
            actions=[{"method_code":"save_settings","instruction":ref("answer","进入知识导出页面并保存设置。"),"effect_status":"suggested","verification":None}])
        self.install_provider(scopes={1:[{"kind":kind,"source":"question","quote":quote} for kind,quote in values]},domains={1:raw})
        self.run_engine();item=self.knowledge()[0]
        for _,quote in values:self.assertIn(quote,item["scope"])
        self.assertIn("知识导出",{ref["quote"] for ref in item["applicability"]})

    def test_redundant_exact_ref_is_once_and_roles_are_program_derived(self):
        self.seed(question="知汇鸽2.8版本怎么配置场景？",answer="进入场景设置页面并保存。")
        self.install_provider(scopes={1:[{"kind":"version","source":"question","quote":"2.8版本"}]})
        self.run_engine();row=engine._events(self.cfg,{})[0]
        row["data"]["domain"]["prerequisites"]=[{**row["data"]["scope"][0],"role":"staff","source_refs":[{"message_id":"FAKE","start":0,"end":1}]}]
        result=variants.constraint_projection(row)
        self.assertFalse(result["invalid"])
        self.assertEqual(len(result["refs"]),1)
        self.assertEqual(result["refs"][0]["role"],"merchant")
        self.assertNotIn("FAKE",str(result["refs"]))

    def test_generic_answer_audience_keeps_staff_role_and_exact_offsets(self):
        answer="仅管理员可操作。进入知识导出页面并保存设置。"
        self.seed(question="知汇鸽知识导出怎么配置？",answer=answer)
        raw=domain(primary_intent="configuration_guidance",merchant_goal=ref("question","知汇鸽知识导出怎么配置？"),
            diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]},
            actions=[{"method_code":"save_export_settings","instruction":ref("answer","进入知识导出页面并保存设置。"),"effect_status":"suggested","verification":None}])
        self.install_provider(scopes={1:[{"kind":"audience","source":"answer","quote":"管理员"}]},domains={1:raw})
        self.run_engine();item=self.knowledge()[0]
        audience=next(ref for ref in item["applicability"] if ref["quote"]=="管理员")
        self.assertEqual((audience["role"],audience["source_refs"][0]["start"],audience["source_refs"][0]["end"]),("staff",1,4))
        self.assertIn("仅管理员",item["answer"])

    def test_foreign_question_scope_does_not_borrow_another_intent(self):
        current="知汇鸽知识如何导出？";answer="进入知识导出页面并保存设置。"
        self.seed(question="知汇鸽2.8版本登录异常怎么办？"+current,answer=answer)
        self.install_provider(scopes={1:[{"kind":"version","source":"question","quote":"2.8版本"}]},questions={1:current})
        self.run_engine()
        self.assertEqual(self.knowledge(),[])
        self.assertEqual(engine.exceptions(self.cfg,{})["items"][0]["kind"],"scope_unbound")

    def test_foreign_answer_scope_does_not_borrow_a_different_reply(self):
        current="知汇鸽知识如何导出？";step="进入知识导出页面并保存设置。"
        self.seed(question="知汇鸽登录怎么处理？"+current,answer="登录模块仅2.8版本支持。"+step)
        raw=domain(primary_intent="configuration_guidance",merchant_goal=ref("question",current),
            diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]},
            actions=[{"method_code":"export_knowledge","instruction":ref("answer",step),"effect_status":"suggested","verification":None}])
        self.install_provider(scopes={1:[{"kind":"version","source":"answer","quote":"2.8版本"}]},questions={1:current},domains={1:raw},claims={1:[step]})
        self.run_engine()
        self.assertEqual(self.knowledge(),[])
        self.assertEqual(engine.exceptions(self.cfg,{})["items"][0]["kind"],"scope_unbound")

    def test_invalid_cached_span_withholds_instead_of_silently_widening(self):
        self.seed(question="知汇鸽2.8版本如何配置？",answer="进入场景设置页面并保存。")
        self.install_provider(scopes={1:[{"kind":"version","source":"question","quote":"2.8版本"}]})
        self.run_engine();row=engine._events(self.cfg,{})[0]
        row["data"]["scope"][0]["start"]+=1
        plan=variants.plans(self.cfg,{"id":row["topic_id"]},[row])[0]
        result=variants.evaluate(self.cfg,engine.path(self.cfg),self.vectors,{"id":row["topic_id"]},plan)
        self.assertEqual((result["status"],result["kind"],result["answer"]),("exception","scope_unbound",""))

    def test_two_explicit_versions_have_different_branch_identity(self):
        step="进入场景设置页面并保存。"
        self.seed(question="知汇鸽2.8版本如何配置？",answer=step)
        self.seed(2,question="知汇鸽2.9版本如何配置？",answer=step)
        self.install_provider(scopes={qa:[{"kind":"version","source":"question","quote":version}] for qa,version in ((1,"2.8版本"),(2,"2.9版本"))})
        self.run_engine();items=self.knowledge()
        self.assertEqual(len(items),2)
        self.assertEqual(len({item["variant_key"] for item in items}),2)
        self.assertTrue(all(len(item["evidence"])==1 for item in items))

    def test_missing_both_metadata_fields_recovers_staff_version_from_complete_claim(self):
        from src.voc_domain import DOMAIN_CASES
        case=DOMAIN_CASES[6];question,answer=case["question"],case["answer"]
        cause,step=answer.split("。",1);cause+="。"
        self.seed(question=question,answer=answer)
        raw=domain(primary_intent="functional_anomaly",merchant_goal=ref("question",question),diagnosis=confirmed(cause,"defect_confirmed","version_trigger_defect"),
            actions=[{"method_code":"toggle_scene_save","instruction":ref("answer",step),"effect_status":"verified","verification":ref("answer",step)}])
        self.install_provider(domains={1:raw});self.run_engine();item=self.knowledge()[0]
        self.assertEqual(item["kind"],"verified_workaround")
        self.assertIn("2.8版本",item["scope"])
        version=next(ref for ref in item["applicability"] if ref["quote"]=="2.8版本")
        self.assertEqual((version["source"],version["role"]),("answer","staff"))

    def test_missing_both_metadata_fields_keep_complete_audience_premise(self):
        answer="仅管理员可操作。进入场景设置页面并保存。"
        self.seed(question="知汇鸽场景如何配置？",answer=answer)
        raw=domain(primary_intent="configuration_guidance",merchant_goal=ref("question","知汇鸽场景如何配置？"),
            diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]},
            actions=[{"method_code":"save_scene","instruction":ref("answer","进入场景设置页面并保存。"),"effect_status":"suggested","verification":None}])
        self.install_provider(domains={1:raw});self.run_engine()
        self.assertIn("仅管理员可操作。",self.knowledge()[0]["scope"])

    def test_shared_if_premise_does_not_hide_exclusive_rule_values(self):
        answers=["如果已启用跨店授权，消息触发间隔必须设置为7秒，其他数值都无法触发。","如果已启用跨店授权，消息触发间隔必须设置为30秒，其他数值都无法触发。"]
        for qa,answer in enumerate(answers,1):self.seed(qa,question="知汇鸽跨店授权后的消息触发间隔怎么设置？",answer=answer)
        self.install_provider();self.run_engine()
        self.assertEqual(self.knowledge(),[])
        self.assertTrue(all(item["kind"]=="number_conflict" for item in engine.exceptions(self.cfg,{})["items"]))

    def test_scope_compile_upgrade_does_not_make_new_model_requests(self):
        self.seed(question="知汇鸽2.8版本如何配置？",answer="进入场景设置页面并保存。")
        self.install_provider(scopes={1:[{"kind":"version","source":"question","quote":"2.8版本"}]})
        with patch.object(variants,"VARIANT_VERSION","software-branches-v3"):self.run_engine()
        calls=len(self.provider.seen);before=self.sql("SELECT * FROM qa_pair")
        self.assertEqual(self.knowledge(),[])
        result=self.run_engine()
        self.assertEqual((result["job"]["calls"],len(self.provider.seen)),(0,calls))
        self.assertEqual(self.sql("SELECT * FROM qa_pair"),before)
        self.assertTrue(self.knowledge()[0]["quality_version"].endswith(variants.VARIANT_VERSION))


if __name__=="__main__":unittest.main()
