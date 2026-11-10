"""Software-domain integration, real alignment and synthetic inference only."""
from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from src import voc_engine as engine, voc_variants as variants
from src.voc_extract import LangExtractAligner, validate
from tests import test_voc_engine as engine_tests
from tests.test_voc_engine import FakeProvider, extracted
from tests.test_voc_domain import domain, ref, confirmed


class TestSupportEngine(unittest.TestCase):
    sql = engine_tests.TestEngine.sql
    seed = engine_tests.TestEngine.seed
    run_engine = engine_tests.TestEngine.run_engine

    def setUp(self):
        engine_tests.TestEngine.setUp(self)
        self.cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, voc_domain="zhihuige"))
        self.aligner = LangExtractAligner()

    def install_provider(self, *, primary="configuration_guidance", methods=None, diagnoses=None, question="知汇鸽场景如何配置？", context_ids=()):
        def payload(source, candidates, original):
            qa = source["qa_id"]
            raw = domain(primary_intent=primary, diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]})
            raw["modules"] = [ref("question", "场景")] if "场景" in source["question"] else []
            raw["merchant_goal"] = ref("question", source["question"])
            if qa in context_ids:
                raw.update(primary_intent="unclear",fragment_role="reproduction_context",merchant_goal=None)
            if diagnoses and qa in diagnoses:
                raw["diagnosis"] = diagnoses[qa]
            if methods and qa in methods:
                raw["actions"] = [{"method_code": code,"instruction":ref("answer", text),"effect_status":effect,"verification":ref("answer",text) if effect=="verified" else None} for code,text,effect in methods[qa]]
            return extracted(source, candidates, question=question, theme="知汇鸽场景", boundary="场景配置与触发", domain=raw)
        self.provider = FakeProvider(payload)

    def test_domain_output_budget_is_separate_and_engine_reserves_same_limit(self):
        from src.voc_extract import output_token_limit, request_payload
        from unittest.mock import patch
        self.assertEqual(output_token_limit({"_domain_mode":"zhihuige"}),12000)
        self.assertEqual(output_token_limit({}),3000)
        step="请开启场景并保存后测试。"
        self.seed(question="知汇鸽场景怎么配置？",answer=step)
        self.install_provider(methods={1:[("enable_scene",step,"suggested")]})
        actual=self.provider.extract;limits=[];requests=[]
        def capture(cfg,source,candidates,max_output_tokens):
            limits.append(max_output_tokens)
            requests.append(request_payload(cfg,source,candidates,max_output_tokens))
            return actual(cfg,source,candidates,max_output_tokens)
        self.provider.extract=capture
        result=self.run_engine()
        self.assertEqual(limits,[12000])
        self.assertEqual(requests[0]["max_tokens"],12000)
        expected=len(engine.assets.dump(requests[0]).encode("utf-8"))+12000
        self.assertEqual(result["job"]["reserved_tokens"],expected)

    def test_default_engine_budget_admits_a_complete_domain_source(self):
        import inspect
        step="请开启场景并保存后测试。"
        self.seed(question="知汇鸽场景怎么配置？",answer=step)
        self.install_provider(methods={1:[("enable_scene",step,"suggested")]})
        self.assertEqual(inspect.signature(engine.run).parameters["max_tokens"].default,400000)
        result=engine.run(self.cfg,{},embedder=self.vectors,provider=self.provider,aligner=self.aligner)
        self.assertEqual((result["job"]["processed"],result["job"]["calls"]),(1,1))

    def test_domain_task_uses_software_mother_topics_without_changing_generic_task(self):
        from src.voc_extract import request_payload, TASK
        source={"qa_id":1,"question":"如何使用软件？","answer":"","_domain_mode":"zhihuige"}
        task=request_payload(self.cfg.llm,source,[],6000)["messages"][0]["content"]
        conflict="仅原文明示的实质产品、版本、适用人群冲突或不同最终业务目标才区分主题。"
        self.assertNotIn(conflict,task)
        self.assertIn("软件能力+现象或使用目标",task)
        self.assertIn("不因此另立高频主题",task)
        self.assertIn("不能把未知模块强归",task)
        self.assertIn("quote应完全位于当前intent.question_quote内",task)
        generic={key:value for key,value in source.items() if key!="_domain_mode"}
        original=request_payload(self.cfg.llm,generic,[],3000)
        self.assertEqual(original["messages"][0]["content"],TASK)
        self.assertIn(conflict,TASK)

    def test_contextual_pending_diagnosis_is_processed_without_ready_knowledge(self):
        question="商家后台已设置，知汇鸽机器人没有触发，是不是系统问题？"
        answer="收到，我帮您看下，目前还需要排查。"
        self.seed(question=question,answer=answer)
        pending={"status":"pending","cause_code":"","cause":None,"evidence":[]}
        self.install_provider(primary="functional_anomaly",diagnoses={1:pending})
        actual=self.provider.extract
        def payload(cfg,source,candidates,limit):
            result=actual(cfg,source,candidates,limit)
            item=result["payload"]["intents"][0]
            item.update(answer_kind="contextual",missing_info=[{"kind":"explicit_unknown","request_quote":"是不是系统问题？",
                "evidence_source":"answer","evidence_quote":"目前还需要排查。","reason":"当前所问原因仍待定位。"}])
            return result
        self.provider.extract=payload
        result=self.run_engine()
        self.assertEqual((result["job"]["processed"],result["job"]["failed"]),(1,0))
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],0)
        self.assertEqual(engine.exceptions(self.cfg,{})["items"][0]["kind"],"pending_diagnosis")
        event=engine._events(self.cfg,{})[0]
        self.assertEqual(event["data"]["domain"]["diagnosis"]["status"],"pending")
        self.assertIsNone(event["data"]["domain"]["diagnosis"]["cause"])

    def test_software_subset_omits_buyer_policy_but_keeps_original_evidence(self):
        step="进入退货场景设置页面并保存。"
        policy="你店商品支持7天退货，退款3天到账。"
        self.seed(question="知汇鸽退货场景怎么配置？", answer=step+policy)
        self.install_provider(methods={1:[("save_return_scene",step,"suggested")]})
        before=self.sql("SELECT * FROM qa_pair")
        self.run_engine()
        item=engine.knowledge(self.cfg,{})["items"][0]
        self.assertEqual(item["answer"],step)
        self.assertNotIn("3天",item["answer"])
        self.assertEqual(item["evidence"][0]["answer"],step+policy)
        self.assertEqual(item["evidence"][0]["claims"][0]["quote"],step)
        self.assertEqual(item["evidence"][0]["claims"][0]["source_refs"][0]["start"],0)
        self.assertEqual(item["kind"],"configuration_guide")
        self.assertEqual(self.sql("SELECT * FROM qa_pair"),before)

    def test_policy_disguised_as_domain_action_never_gets_software_exemption(self):
        answer="本店商品支持7天无理由退货，退款将在3天内到账。"
        self.seed(question="商品可以7天无理由退货吗？",answer=answer)
        self.install_provider(methods={1:[("return_policy",answer,"suggested")]})
        self.run_engine()
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],0)

    def test_ambiguous_mixed_sentence_not_cut_at_comma_to_launder_policy(self):
        answer="进入退货场景设置页面并保存，你店商品支持7天退货，退款3天到账。"
        self.seed(question="知汇鸽退货场景怎么配置？",answer=answer)
        self.install_provider(methods={1:[("save_return_scene",answer,"suggested")]})
        self.run_engine()
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],0)

    def test_unlabelled_staff_premise_preserved_with_software_action(self):
        premise="仅管理员可以操作。";step="进入场景设置页面并保存。"
        self.seed(question="知汇鸽场景怎么配置？",answer=premise+step)
        self.install_provider(methods={1:[("save_scene",step,"suggested")]})
        self.run_engine()
        item=engine.knowledge(self.cfg,{})["items"][0]
        self.assertEqual(item["answer"],premise+"\n"+step)

    def test_suggested_numeric_strategies_same_method_can_coexist(self):
        answers=["请在知汇鸽设置中将消息触发间隔设为7秒试试并保存。","请在知汇鸽设置中将消息触发间隔设为30秒试试并保存。"]
        for qa,answer in enumerate(answers,1):self.seed(qa,question="知汇鸽消息触发间隔怎么设置？",answer=answer)
        self.install_provider(methods={i:[("set_trigger_interval",a,"suggested")] for i,a in enumerate(answers,1)})
        self.run_engine()
        items=engine.knowledge(self.cfg,{})["items"]
        self.assertEqual(len(items),2)
        self.assertEqual({x["answer"] for x in items},set(answers))
        self.assertTrue(all(x["effect_status"]=="suggested" for x in items))

    def test_exclusive_parameter_rules_meet_across_method_aliases(self):
        answers=["当前版本的消息触发间隔必须设置为7秒，其他数值都无法触发。","当前版本的消息触发间隔必须设置为30秒，其他数值都无法触发。"]
        self.seed(question="知汇鸽当前版本的消息触发间隔有什么限制？",answer=answers[0])
        codes=["set_trigger_interval","configure_trigger_delay"]
        self.install_provider(methods={i:[(c,a,"suggested")] for i,(c,a) in enumerate(zip(codes,answers),1)})
        self.run_engine()
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],1)
        self.seed(2,question="知汇鸽当前版本的消息触发间隔有什么限制？",answer=answers[1])
        self.run_engine()
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],0)
        self.assertTrue(all(x["kind"]=="number_conflict" for x in engine.exceptions(self.cfg,{})["items"]))

    def test_same_symptom_distinct_causes_and_pending_event_keep_useful_branches(self):
        cause1="已核查，该场景未启用导致机器人没有触发。";step1="请开启场景并保存后测试。"
        cause2="已核查，知识未同步到知汇鸽导致场景没有命中。";step2="请同步知识并保存后测试场景。"
        self.seed(1,question="SKU A01的知汇鸽场景为何没触发？",answer=cause1+step1)
        self.seed(2,question="SKU B02的知汇鸽场景为何没触发？",answer=cause2+step2)
        self.seed(3,question="SKU C03的知汇鸽场景为何没触发？",answer="请提供场景截图，我帮您核对。")
        self.install_provider(primary="functional_anomaly",methods={1:[("enable_scene",step1,"suggested")],2:[("sync_knowledge",step2,"suggested")]},diagnoses={1:confirmed(cause1),2:confirmed(cause2,code="knowledge_not_synced"),3:{"status":"pending","cause_code":"","cause":None,"evidence":[]}})
        self.run_engine()
        self.assertEqual(engine.topics(self.cfg,{})["total"],1)
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],2)
        self.assertEqual(engine.topics(self.cfg,{})["items"][0]["consultation_count"],3)
        self.assertEqual(engine.knowledge(self.cfg,{"diagnosis_status":"pending"})["total"],0)
        self.assertTrue(all(x["effect_status"]=="suggested" for x in engine.knowledge(self.cfg,{})["items"]))

    def test_cautions_keep_diagnosis_and_effect_separate(self):
        for status in ("config_cause_confirmed", "platform_limit_confirmed", "defect_confirmed"):
            text=variants.method_caution({"diagnosis_status":status,"effect_status":"suggested"})
            self.assertIn("来源已确认当前诊断",text)
            self.assertIn("该操作结果尚无复测依据",text)
            self.assertNotIn("原因尚未确认",text)
        self.assertEqual(variants.method_caution({"diagnosis_status":"not_applicable","effect_status":"suggested"}),"")
        pending=variants.method_caution({"diagnosis_status":"pending","effect_status":"suggested"})
        self.assertIn("当前原因尚未确认",pending)
        verified=variants.method_caution({"diagnosis_status":"defect_confirmed","effect_status":"verified"})
        self.assertIn("已验证的操作效果",verified)
        self.assertNotIn("尚无复测依据",verified)
        self.assertIn("不能自动推定所有同症状",verified)

    def test_caution_policy_upgrade_recompiles_without_reextracting_source(self):
        from unittest.mock import patch
        cause="已核查，该场景未启用导致机器人没有触发。";step="请开启场景并保存后测试。"
        self.seed(question="知汇鸽场景为何没触发？",answer=cause+step)
        self.install_provider(primary="functional_anomaly",methods={1:[("enable_scene",step,"suggested")]},diagnoses={1:confirmed(cause)})
        with patch.object(variants,"VARIANT_VERSION","software-branches-v1"):
            self.run_engine()
        self.assertEqual(engine.knowledge(self.cfg,{})["total"],0)
        self.assertEqual(engine.check(self.cfg,{})["needs_compile"],1)
        calls=len(self.provider.seen)
        result=self.run_engine()
        self.assertEqual((result["job"]["calls"],len(self.provider.seen)),(0,calls))
        asset=engine.knowledge(self.cfg,{})["items"][0]
        self.assertTrue(asset["quality_version"].endswith(variants.VARIANT_VERSION))
        self.assertTrue(any("来源已确认当前诊断" in text for text in asset["cautions"]))
        self.assertFalse(any("原因尚未确认" in text for text in asset["cautions"]))

    def test_background_fragment_processed_without_inflating_topics(self):
        self.seed(question="SKU A01，500ml，蓝色款。",answer="")
        self.install_provider(context_ids=(1,))
        first=self.run_engine()
        self.assertEqual(first["job"]["processed"],1)
        summary=engine.summary(self.cfg,{})
        self.assertEqual((summary["total_events"],summary["topic_count"],summary["coverage"]["pending_pairs"],summary["coverage"]["context_needed_pairs"]),(0,0,0,1))
        self.assertEqual(engine.exceptions(self.cfg,{})["items"][0]["kind"],"context_needed")
        self.assertEqual(self.run_engine()["job"]["calls"],0)

    def test_domain_cache_isolated_and_same_visitor_followups_count_one_consultation(self):
        step="请开启场景并保存后测试。"
        for qa in (1,2):self.seed(qa,question="知汇鸽场景为什么没触发？",answer=step,session="one-session",customer="one-visitor")
        self.install_provider(primary="functional_anomaly",methods={qa:[("enable_scene",step,"suggested")] for qa in (1,2)})
        self.run_engine()
        topic=engine.topics(self.cfg,{})["items"][0]
        self.assertEqual((topic["event_count"],topic["consultation_count"],topic["session_count"]),(2,1,1))
        generic=replace(self.cfg,runtime=replace(self.cfg.runtime,voc_domain=""))
        self.assertEqual(engine.knowledge(generic,{})["total"],0)
        self.assertEqual(engine.summary(generic,{})["coverage"]["pending_pairs"],2)

    def test_nine_domain_cases_harness_checks_variants_and_context_separately(self):
        from src import voc_eval
        from src.voc_domain import DOMAIN_CASES
        from unittest.mock import patch
        for i,case in enumerate(DOMAIN_CASES,1):
            self.seed(i,question=case["question"],answer=case["answer"])
        seen_topics={}
        def payload(source,candidates,original):
            i=source["qa_id"];case=DOMAIN_CASES[i-1];answer=source["answer"]
            raw=domain(primary_intent=case["expected_primary_intent"],fragment_role=case.get("expected_fragment_role","request"),merchant_goal=ref("question",source["question"]) if i!=9 else None,
                       diagnosis={"status":case["expected_diagnosis"],"cause_code":"","cause":None,"evidence":[]})
            if i in (4,5,7):
                sentence=answer[:answer.index("。")+1]
                raw["diagnosis"]=confirmed(sentence,case["expected_diagnosis"],case.get("expected_cause_code","version_trigger_defect"))
            if not case["must_not_be_ready"]:
                step=answer if i==3 else answer[answer.index("。")+1:]
                raw["actions"]=[{"method_code":{3:"configure_return_scene",4:"enable_scene",5:"sync_knowledge",7:"toggle_scene_save",8:"bind_scene_save"}[i],"instruction":ref("answer",step),"effect_status":"verified" if i==7 else "suggested","verification":ref("answer",step) if i==7 else None}]
            titles={"scenario_not_triggered":"知汇鸽退货场景为何不触发？","configure_return_scene":"知汇鸽如何配置退货场景？","scene_trigger_known_defect":"知汇鸽版本触发缺陷的临时办法？","reproduction_context":"背景片段待关联"}
            target=next((c["id"] for c in candidates if c["question"]==titles[case["expected_topic"]]),None)
            return extracted(source,candidates,question=titles[case["expected_topic"]],theme="知汇鸽使用支持",topic_id=target,answer_kind=case["answer_kind"],domain=raw)
        self.provider=FakeProvider(payload)
        # The fake embedding deliberately makes unrelated titles identical; this
        # harness test supplies the expected candidate semantic decision directly.
        with patch("src.voc_engine.retrieve",side_effect=lambda store,emb,q,candidates,**kw: [] if kw.get("minimum")==0.86 else candidates[:5]):
            result=engine.run(self.cfg,{},limit=9,max_calls=9,max_tokens=650000,embedder=self.vectors,provider=self.provider,aligner=self.aligner)
        report=voc_eval.measure(self.cfg,DOMAIN_CASES,result)
        self.assertTrue(report["passed"],{k:v for k,v in report.items() if k not in ("events","topics","knowledge","exceptions")})
        self.assertEqual((report["covered_cases"],report["covered_request_cases"],report["context_needed_cases"]),(9,8,1))
        self.assertEqual(report["knowledge_kind_accuracy"],1)
        self.assertEqual(report["positive_ready_recall"],1)
        self.assertEqual(report["topic_count"],2)
        by_qa={row["qa_id"]:row for row in report["events"]}
        self.assertEqual(by_qa[7]["topic_id"],by_qa[1]["topic_id"])
        self.assertEqual(by_qa[7]["domain"]["diagnosis"]["status"],"defect_confirmed")
        workaround=next(item for item in report["knowledge"] if any(e["qa_id"]==7 for e in item["evidence"]))
        self.assertEqual((workaround["kind"],workaround["effect_status"]),("verified_workaround","verified"))
        self.assertEqual(len(engine.defects(self.cfg,{})["items"]),1)
        self.assertEqual(engine.defects(self.cfg,{"diagnosis_status":"pending"})["total"],0)


if __name__=="__main__":unittest.main()
