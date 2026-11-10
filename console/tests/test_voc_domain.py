"""Domain contracts on synthetic, source-aligned merchant-support records."""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import unittest

from src.voc_domain import DOMAIN_CASES, PROMPT_APPENDIX, VERSION, validate_domain
from src.voc_model import VocModelError


class ExactAligner:
    def align(self, text, quote, occurrence=1):
        cursor = 0
        for _ in range(occurrence):
            start = text.find(quote, cursor)
            if start < 0:
                raise VocModelError("synthetic quote absent")
            cursor = start + len(quote)
        return {"quote":quote,"start":start,"end":cursor,"alignment":"exact"}


def ref(source, quote):
    return {"source":source,"quote":quote}


def domain(**changes):
    value = {"primary_intent":"functional_anomaly","fragment_role":"request",
             "platforms":[],"modules":[],"merchant_goal":None,"expected":None,"actual":None,
             "reproduction_context":[],"prerequisites":[],"operation_path":[],
             "diagnosis":{"status":"pending","cause_code":"","cause":None,"evidence":[]},
             "actions":[],"tags":[]}
    value.update(changes)
    return value


def source(answer="已核查，该场景未启用导致机器人没有触发。", question="知汇鸽机器人场景为什么没有触发？", **changes):
    return {"qa_id":1,"question":question,"answer":answer,"_staff_verified":True,**changes}


def confirmed(text, status="config_cause_confirmed", code="scene_disabled"):
    return {"status":status,"cause_code":code,"cause":ref("answer",text),"evidence":[ref("answer",text)]}


class TestVocDomain(unittest.TestCase):
    def check(self, raw, src=None, span=None, aligner=None):
        src = src or source()
        span = span or {"start":0,"end":len(src["question"]),"quote":src["question"]}
        return validate_domain(raw,src,span,aligner or ExactAligner())

    def test_no_invented_platform_or_module_and_role_computed_by_program(self):
        src=source()
        raw=domain(modules=[ref("question","机器人场景")],actual=ref("question","没有触发"))
        result=self.check(raw,src)
        self.assertEqual((result["platforms"],result["modules"][0]["role"],result["actual"]["role"]),([],"merchant","merchant"))
        self.assertEqual(result["version"],VERSION)
        raw["platforms"]=[ref("question","淘宝")]
        with self.assertRaises(VocModelError):self.check(raw,src)

    def test_prompt_ref_types_and_complete_raw_example_are_valid(self):
        self.assertIn("platforms/modules/reproduction_context/prerequisites/operation_path: Ref[]",PROMPT_APPENDIX)
        self.assertIn("merchant_goal/expected/actual: Ref|null",PROMPT_APPENDIX)
        self.assertIn("diagnosis.cause: Ref|null",PROMPT_APPENDIX)
        self.assertIn("diagnosis.evidence: Ref[]",PROMPT_APPENDIX)
        match=re.search(r"```json\n(.*?)\n```",PROMPT_APPENDIX,re.S)
        self.assertIsNotNone(match)
        raw=json.loads(match.group(1))
        src=source(question="知汇鸽退货场景怎么配置？",answer="进入退货场景设置页面并保存。")
        normalized=self.check(raw,src)
        self.assertEqual(normalized["primary_intent"],"configuration_guidance")
        self.assertEqual(normalized["actions"][0]["instruction"]["role"],"staff")
        self.assertEqual(normalized["actions"][0]["effect_status"],"suggested")
        self.assertEqual(VERSION,"zhg-support-v5")

    def test_ref_errors_name_static_field_paths_without_echoing_values_or_keys(self):
        secret="SENSITIVE_SYNTHETIC_TEXT"
        cases=[]
        raw=domain(modules=[secret]);cases.append((raw,"domain.modules[0]","string"))
        raw=domain(modules=[{"source":"question","quote":secret,secret:"unknown"}]);cases.append((raw,"domain.modules[0]","object"))
        raw=domain(merchant_goal=[secret]);cases.append((raw,"domain.merchant_goal","array"))
        raw=domain();raw["diagnosis"]["evidence"]=[None];cases.append((raw,"domain.diagnosis.evidence[0]","null"))
        raw=domain(actions=[{"method_code":"test","instruction":{"source":[secret],"quote":secret},"effect_status":"suggested","verification":None}]);cases.append((raw,"domain.actions[0].instruction.source","array"))
        for raw,path,type_name in cases:
            with self.subTest(path=path),self.assertRaises(VocModelError) as caught:self.check(raw)
            self.assertIn(path,str(caught.exception))
            self.assertIn("类型="+type_name,str(caught.exception))
            self.assertNotIn(secret,str(caught.exception))
            self.assertNotIn("unknown",str(caught.exception))
        class PrivateErrorAligner:
            def align(self,*args):raise VocModelError(secret)
        with self.assertRaises(VocModelError) as caught:
            self.check(domain(modules=[ref("question","知汇鸽")]),aligner=PrivateErrorAligner())
        self.assertIn("domain.modules[0]",str(caught.exception))
        self.assertNotIn(secret,str(caught.exception))

    def test_model_cannot_supply_role_span_or_extra_buyer_policy(self):
        raw=domain(actual={"source":"question","quote":"没有触发","role":"staff"})
        with self.assertRaises(VocModelError):self.check(raw)
        raw=domain(buyer_policy="7天退货")
        with self.assertRaises(VocModelError):self.check(raw)

    def test_unverified_answer_is_unknown_and_cannot_confirm_cause(self):
        src=source(_staff_verified=False)
        result=self.check(domain(operation_path=[ref("answer",src["answer"])]),src)
        self.assertEqual(result["operation_path"][0]["role"],"unknown")
        with self.assertRaises(VocModelError):self.check(domain(diagnosis=confirmed(src["answer"])),src)

    def test_scene_disabled_is_confirmed_config_not_automatic_defect(self):
        src=source();result=self.check(domain(diagnosis=confirmed(src["answer"])),src)
        self.assertEqual(result["diagnosis"]["status"],"config_cause_confirmed")
        self.assertEqual(result["diagnosis"]["cause"]["role"],"staff")
        with self.assertRaises(VocModelError):
            self.check(domain(diagnosis=confirmed(src["answer"],"defect_confirmed","system_defect")),src)

    def test_not_bug_may_confirm_config_but_never_confirm_defect(self):
        text="已核查，这不是系统bug，而是退货场景未启用导致未触发。"
        src=source(answer=text)
        self.assertEqual(self.check(domain(diagnosis=confirmed(text)),src)["diagnosis"]["status"],"config_cause_confirmed")
        with self.assertRaises(VocModelError):self.check(domain(diagnosis=confirmed(text,"defect_confirmed","system_defect")),src)

    def test_tentative_forwarding_or_customer_bug_claim_is_not_confirmation(self):
        for text in ("可能是场景配置问题，我已经反馈研发。","应该是触发缺陷，需要排查确认。","我帮您看下，再反馈技术。"):
            with self.subTest(text=text),self.assertRaises(VocModelError):
                self.check(domain(diagnosis=confirmed(text)),source(answer=text))
        src=source(question="知汇鸽机器人没触发，这肯定是系统bug。")
        raw=domain(diagnosis={"status":"defect_confirmed","cause_code":"system_defect","cause":ref("question",src["question"]),"evidence":[ref("question",src["question"])]})
        with self.assertRaises(VocModelError):self.check(raw,src)

    def test_confirmed_ref_must_keep_entire_negative_sentence(self):
        src=source(answer="研发已确认这不是知汇鸽的触发缺陷。")
        raw=domain(diagnosis=confirmed("知汇鸽的触发缺陷","defect_confirmed","trigger_defect"))
        with self.assertRaises(VocModelError):self.check(raw,src)

    def test_explicit_platform_limit_has_staff_provenance(self):
        text="已核查，微信小店平台不提供该类型实时回调，这是平台限制。"
        result=self.check(domain(diagnosis=confirmed(text,"platform_limit_confirmed","callback_unavailable")),source(answer=text))
        self.assertEqual(result["diagnosis"]["status"],"platform_limit_confirmed")

    def test_same_symptom_can_keep_distinct_confirmed_causes(self):
        first=source();second=source(answer="已核查，平台的新知识尚未同步到知汇鸽，导致场景无法触发。")
        a=self.check(domain(diagnosis=confirmed(first["answer"])),first)
        b=self.check(domain(diagnosis=confirmed(second["answer"],code="knowledge_not_synced")),second)
        self.assertNotEqual(a["diagnosis"]["cause_code"],b["diagnosis"]["cause_code"])

    def test_version_defect_and_verified_workaround_have_separate_complete_quotes(self):
        cause="研发已确认这是知汇鸽2.8版本的触发缺陷。"
        action="临时关闭再开启场景并保存，已复测机器人可以正常触发。"
        raw=domain(diagnosis=confirmed(cause,"defect_confirmed","version_trigger_defect"),actions=[
            {"method_code":"toggle_scene_save","instruction":ref("answer",action),"effect_status":"verified","verification":ref("answer",action)}])
        result=self.check(raw,source(answer=cause+action))
        self.assertEqual(result["actions"][0]["verification"]["role"],"staff")
        self.assertEqual(result["diagnosis"]["status"],"defect_confirmed")

    def test_suggested_steps_do_not_require_success_but_must_be_full_staff_instructions(self):
        text="请开启场景并保存后测试。"
        raw=domain(actions=[{"method_code":"enable_scene_save","instruction":ref("answer",text),"effect_status":"suggested","verification":None}])
        self.assertEqual(self.check(raw,source(answer=text))["actions"][0]["effect_status"],"suggested")
        raw["actions"][0]["instruction"]=ref("question","没有触发")
        with self.assertRaises(VocModelError):self.check(raw)
        raw["actions"][0]["instruction"]=ref("answer",text)
        with self.assertRaises(VocModelError):self.check(raw,source(answer=text,_staff_verified=False))
        raw["actions"][0]["instruction"]=ref("answer","开启场景")
        with self.assertRaises(VocModelError):self.check(raw,source(answer="仅管理员可以开启场景并保存。"))

    def test_expectation_or_failed_retest_is_not_verified_effect(self):
        for text in ("开启后即可触发。","请尝试开启场景并保存。","应该已经恢复，请试一下。","已复测，但仍无法触发。"):
            with self.subTest(text=text):
                raw=domain(actions=[{"method_code":"enable_scene_save","instruction":ref("answer",text),"effect_status":"verified","verification":ref("answer",text)}])
                with self.assertRaises(VocModelError):self.check(raw,source(answer=text))

    def test_diagnosis_and_action_cannot_borrow_another_intent(self):
        question="知汇鸽机器人为什么没有触发？导出报表为什么失败？"
        first="已核查，机器人场景未启用导致未触发。"
        other="研发已确认这是导出报表功能的缺陷。"
        src=source(answer=first+other,question=question)
        end=question.index("？")+1;span={"start":0,"end":end,"quote":question[:end]}
        with self.assertRaises(VocModelError):self.check(domain(diagnosis=confirmed(other,"defect_confirmed","export_defect")),src,span)
        raw=domain(actions=[{"method_code":"export_report","instruction":ref("answer",other),"effect_status":"suggested","verification":None}])
        with self.assertRaises(VocModelError):self.check(raw,src,span)

    def test_pending_cannot_hide_confirmed_cause_under_a_code(self):
        raw=domain();raw["diagnosis"]["cause_code"]="scene_disabled"
        with self.assertRaises(VocModelError):self.check(raw)
        raw=domain();raw["diagnosis"]["cause"]=ref("answer",source()["answer"])
        with self.assertRaises(VocModelError):self.check(raw)
        raw=domain();raw["diagnosis"]["status"]=[]
        with self.assertRaises(VocModelError):self.check(raw)

    def test_sku_and_buyer_policy_are_background_refs_not_buyer_knowledge(self):
        q="SKU A01买家测试话术是7天退货，我要在知汇鸽里配置对应回复。"
        raw=domain(primary_intent="configuration_guidance",reproduction_context=[ref("question","SKU A01"),ref("question","7天退货")],merchant_goal=ref("question","在知汇鸽里配置对应回复"),diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]})
        result=self.check(raw,source(question=q))
        self.assertEqual([r["role"] for r in result["reproduction_context"]],["merchant","merchant"])
        self.assertFalse(result["needs_context"])
        self.assertNotIn("buyer_policy",result)

    def test_pure_background_or_ambiguous_follow_up_needs_context_but_explicit_follow_up_can_count(self):
        raw=domain(primary_intent="unclear",fragment_role="reproduction_context")
        self.assertTrue(self.check(raw,source(question="SKU A01，500ml，蓝色款。"))["needs_context"])
        self.assertTrue(self.check(domain(fragment_role="follow_up"),source(question="还是不行。"))["needs_context"])
        q="刚保存后机器人仍没有触发。"
        self.assertFalse(self.check(domain(fragment_role="follow_up",actual=ref("question","机器人仍没有触发")),source(question=q))["needs_context"])

    def test_repeated_current_request_is_aligned_to_its_own_occurrence(self):
        text="机器人没有触发。";src=source(question=text+text)
        span={"start":len(text),"end":2*len(text),"quote":text}
        result=self.check(domain(actual=ref("question",text)),src,span)
        self.assertEqual(result["actual"]["start"],len(text))

    def test_full_goal_can_keep_same_request_locative_or_polite_prefix(self):
        intent="怎么设置买家问退货时的回复？"
        for prefix in ("在知汇鸽里","请问，在知汇鸽里", "您好，请问在平台后台", "在控制台中，请问"):
            with self.subTest(prefix=prefix):
                question=prefix+intent;start=len(prefix)
                span={"quote":intent,"start":start,"end":len(question)}
                raw=domain(merchant_goal=ref("question",question))
                goal=self.check(raw,source(question=question),span)["merchant_goal"]
                self.assertEqual((goal["start"],goal["end"],goal["quote"],goal["role"]),(0,len(question),question,"merchant"))

    def test_goal_prefix_expansion_never_includes_other_or_parallel_requests(self):
        current="怎么设置退货回复？"
        cases=[
            ("在知汇鸽里"+current+"怎么导出报表？",current),
            ("先导出报表，再"+current,current),
            ("怎么导出报表并"+current,current),
            ("在知汇鸽里。"+current,current),
            ("请问怎么导出报表？在知汇鸽里"+current,current),
            ("在知汇鸽里怎么设置退货回复并导出报表？","怎么设置退货回复"),
        ]
        for question,quote in cases:
            with self.subTest(question=question):
                start=question.index(quote);span={"quote":quote,"start":start,"end":start+len(quote)}
                with self.assertRaises(VocModelError):
                    self.check(domain(merchant_goal=ref("question",question)),source(question=question),span)

    def test_ref_maps_cross_message_intersections_without_inventing_missing_anchors(self):
        first,second="机器人没有触发","另一个SKU也没触发"
        question=first+"\n"+second
        src=source(question=question,_question_parts=[
            {"message_id":"source:s:11","start":0,"end":len(first)},
            {"message_id":"source:s:12","start":len(first)+1,"end":len(question)}])
        quote=question[2:-2]
        result=self.check(domain(actual=ref("question",quote)),src)["actual"]
        self.assertEqual(result["source_refs"],[
            {"message_id":"source:s:11","start":2,"end":len(first)},
            {"message_id":"source:s:12","start":0,"end":len(second)-2}])
        src.pop("_question_parts")
        self.assertEqual(self.check(domain(actual=ref("question",quote)),src)["actual"]["source_refs"],[])
        answer=first+"\n"+second
        src=source(answer=answer,_answer_parts=[
            {"message_id":"source:s:21","start":0,"end":len(first)},
            {"message_id":"source:s:22","start":len(first)+1,"end":len(answer)}])
        path=self.check(domain(operation_path=[ref("answer",answer)]),src)["operation_path"][0]
        self.assertEqual(path["source_refs"],[
            {"message_id":"source:s:21","start":0,"end":len(first)},
            {"message_id":"source:s:22","start":0,"end":len(second)}])

    def test_input_not_mutated_and_cases_keep_generic_eval_contract(self):
        raw=domain();before=copy.deepcopy(raw);self.check(raw)
        self.assertEqual(raw,before)
        required={"question","answer","expected_topic","answer_kind","must_not_be_ready","expected_primary_intent","expected_diagnosis","expected_knowledge_kind"}
        self.assertTrue(all(required<=set(case) for case in DOMAIN_CASES))
        self.assertGreaterEqual(sum(case["expected_topic"]=="scenario_not_triggered" for case in DOMAIN_CASES),4)

    @unittest.skipUnless(importlib.util.find_spec("langextract"),"optional real aligner dependency")
    def test_real_langextract_validates_full_staff_ref_and_rejects_partial_negative(self):
        from src.voc_extract import LangExtractAligner
        src=source();aligner=LangExtractAligner()
        result=self.check(domain(diagnosis=confirmed(src["answer"])),src,aligner=aligner)
        self.assertEqual(result["diagnosis"]["cause"]["alignment"],"exact")
        src=source(answer="研发已确认这不是知汇鸽触发缺陷。")
        with self.assertRaises(VocModelError):self.check(domain(diagnosis=confirmed("知汇鸽触发缺陷","defect_confirmed","trigger_defect")),src,aligner=aligner)


if __name__ == "__main__":
    unittest.main()
