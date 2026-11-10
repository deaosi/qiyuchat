"""Extractive software capability facts; synthetic provider, real alignment."""
from __future__ import annotations

import unittest
from dataclasses import replace

from src import voc_engine as engine, voc_variants as variants
from src.voc_extract import LangExtractAligner
from tests import test_voc_engine as fixtures
from tests.test_voc_domain import domain, ref


class TestVocCapability(unittest.TestCase):
    sql = fixtures.TestEngine.sql
    seed = fixtures.TestEngine.seed
    run_engine = fixtures.TestEngine.run_engine

    def setUp(self):
        fixtures.TestEngine.setUp(self)
        self.cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, voc_domain="zhihuige"))
        self.aligner = LangExtractAligner()

    def install_provider(self, *, actions=None, scopes=None, answer_kind="resolved_answer"):
        def payload(source, candidates, original):
            qa = source["qa_id"]
            raw = domain(primary_intent="configuration_guidance", merchant_goal=ref("question",source["question"]),
                         diagnosis={"status":"not_applicable","cause_code":"","cause":None,"evidence":[]})
            raw["actions"] = [{"method_code":"software_operation","instruction":ref("answer",text),
                               "effect_status":"suggested","verification":None} for text in (actions or {}).get(qa,[])]
            return fixtures.extracted(source,candidates,question="知汇鸽如何使用知识导出功能？",theme="软件使用",boundary="当前软件能力及操作",
                                      domain=raw,scope=(scopes or {}).get(qa,[]),answer_kind=answer_kind)
        self.provider = fixtures.FakeProvider(payload)

    def knowledge(self):
        return engine.knowledge(self.cfg,{})["items"]

    def test_short_current_limitation_uses_only_current_question_target(self):
        question="知汇鸽历史问答能导出吗？";answer="目前不支持的呢。"
        self.seed(question=question,answer=answer)
        self.install_provider(answer_kind="contextual")
        before=self.sql("SELECT * FROM qa_pair")
        self.run_engine()
        item=self.knowledge()[0]
        self.assertEqual((item["kind"],item["answer"],item["source_level"]),("capability_fact",answer,"single_source"))
        self.assertEqual(item["steps"],[])
        self.assertEqual(item["evidence"][0]["question_quote"],question)
        self.assertEqual(item["evidence"][0]["claims"][0]["source_refs"][0]["start"],0)
        self.assertTrue(any("来源当时" in text for text in item["cautions"]))
        self.assertEqual(self.sql("SELECT * FROM qa_pair"),before)

    def test_oral_affirmative_is_a_fact_not_verified_operation(self):
        self.seed(question="知汇鸽知识可以导出吗？",answer="支持的。")
        self.install_provider();self.run_engine()
        item=self.knowledge()[0]
        self.assertEqual((item["kind"],item["answer"],item["effect_status"]),("capability_fact","支持的。","suggested"))

    def test_current_version_and_admin_restrictions_remain_complete(self):
        answer="仅管理员可用。当前1.8版本支持知识导出。"
        self.seed(question="知汇鸽知识能导出吗？",answer=answer)
        self.install_provider();self.run_engine()
        self.assertEqual(self.knowledge()[0]["answer"],"仅管理员可用。\n当前1.8版本支持知识导出。")

    def test_unverified_answer_cannot_become_fact(self):
        self.seed(question="知汇鸽知识能导出吗？",answer="当前支持知识导出。")
        self.sql("UPDATE raw_message SET role='user' WHERE role='staff'")
        self.install_provider();self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_buyer_policy_without_software_goal_never_becomes_capability(self):
        answer="本店商品支持7天无理由退货，退款3天到账。"
        self.seed(question="这件商品能7天无理由退货吗？",answer=answer)
        self.install_provider();self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_future_wish_and_uncertainty_are_not_current_capability(self):
        for answer in ("希望之后支持知识导出。","目前是否支持知识导出还未确认。"):
            with self.subTest(answer=answer):
                self.assertFalse(variants._capability_unit(answer))

    def test_followup_notification_is_not_data_synchronization(self):
        answer="这个目前还未确定，出结果后这边同步给您哈。"
        self.seed(question="知汇鸽知识同步问题有结果了吗？",answer=answer)
        self.install_provider(actions={1:[answer]});self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_staff_offer_to_investigate_is_not_a_capability(self):
        answer="我可以帮您核查场景触发记录，并转交研发处理。"
        self.seed(question="知汇鸽场景没有触发怎么办？",answer=answer)
        self.install_provider();self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_golden_pending_checks_are_not_methods_even_if_model_calls_them_actions(self):
        from src.voc_domain import DOMAIN_CASES
        for qa in (1,2):
            case=DOMAIN_CASES[qa-1]
            self.seed(qa,question=case["question"],answer=case["answer"])
        # The most permissive wrong labels must not turn an agent's own future
        # investigation/assignment status into a user-executable software method.
        self.install_provider(actions={qa:[DOMAIN_CASES[qa-1]["answer"]] for qa in (1,2)},answer_kind="resolved_answer")
        result=self.run_engine()
        self.assertEqual((result["job"]["processed"],result["job"]["failed"]),(2,0))
        self.assertEqual(self.knowledge(),[])

    def test_unlabelled_proposed_action_is_not_laundered_into_capability(self):
        answer="可以先重新登录一下试试。"
        self.seed(question="知汇鸽后台没有刷新怎么办？",answer=answer)
        self.install_provider();self.run_engine()
        item=self.knowledge()[0]
        # A complete verified generic step can now recover a missing redundant
        # domain action. It remains suggested method evidence, not a fact about
        # permanent product capability or a verified repair.
        self.assertNotEqual(item["kind"],"capability_fact")
        self.assertEqual(item["effect_status"],"suggested")
        self.assertTrue(any("尚未确认故障根因或验证修复效果" in caution for caution in item["cautions"]))

    def test_manual_export_offer_never_becomes_self_service(self):
        answer="目前不支持导出，双十一结束后我给您导出可以吗？"
        self.seed(question="知汇鸽知识如何导出？",answer=answer)
        self.install_provider(actions={1:[answer]});self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_separate_current_fact_can_survive_manual_offer_without_laundering(self):
        fact="目前不支持知识导出。";offer="双十一结束后我给您导出。"
        self.seed(question="知汇鸽知识如何导出？",answer=fact+offer)
        self.install_provider();self.run_engine()
        item=self.knowledge()[0]
        self.assertEqual((item["kind"],item["answer"]),("capability_fact",fact))
        self.assertEqual(item["evidence"][0]["answer"],fact+offer)

    def test_same_scope_positive_and_negative_facts_block_each_other(self):
        self.seed(question="知汇鸽知识能导出吗？",answer="当前支持知识导出。")
        self.seed(2,question="知汇鸽知识能导出吗？",answer="当前不支持知识导出。")
        self.install_provider();self.run_engine()
        self.assertEqual(self.knowledge(),[])
        self.assertTrue(all(item["kind"]=="capability_conflict" for item in engine.exceptions(self.cfg,{})["items"]))

    def test_capability_and_operational_guide_conflict_across_kinds(self):
        unsupported="当前后台不支持知识导出。";step="进入后台点击知识导出按钮。"
        self.seed(question="知汇鸽知识能导出吗？",answer=unsupported)
        self.seed(2,question="知汇鸽知识如何导出？",answer=step)
        self.install_provider(actions={2:[step]});self.run_engine()
        self.assertEqual(self.knowledge(),[])
        items=engine.exceptions(self.cfg,{})["items"]
        self.assertEqual(len(items),2)
        self.assertTrue(all(item["kind"]=="capability_conflict" for item in items))
        self.assertTrue(all("能力或必要参数" in item["reason"] for item in items))

    def test_explicit_different_versions_keep_their_own_facts_and_steps(self):
        step="进入后台点击知识导出按钮。"
        self.seed(question="知汇鸽1.0版知识能导出吗？",answer="当前不支持知识导出。")
        self.seed(2,question="知汇鸽2.0版知识如何导出？",answer=step)
        self.install_provider(actions={2:[step]},scopes={1:[{"kind":"version","source":"question","quote":"1.0版"}],2:[{"kind":"version","source":"question","quote":"2.0版"}]})
        self.run_engine()
        self.assertEqual({item["kind"] for item in self.knowledge()},{"capability_fact","configuration_guide"})

    def test_subaccount_permission_check_is_a_bounded_diagnostic_method(self):
        answer="请问是使用子账号同步商品吗？可以确认一下子账号是否有商品权限。"
        self.seed(question="知汇鸽子账号授权后商品同步失败怎么办？",answer=answer)
        self.install_provider(actions={1:[answer]});self.run_engine()
        item=self.knowledge()[0]
        self.assertEqual(item["answer"],answer.replace("？","？\n"))
        self.assertNotEqual(item["diagnosis_status"],"config_cause_confirmed")

    def test_permission_page_screenshot_is_still_collection_only(self):
        answer="请进入账号权限页面截图发我，我帮您核查。"
        self.seed(question="知汇鸽子账号同步失败怎么办？",answer=answer)
        self.install_provider(actions={1:[answer]});self.run_engine()
        self.assertEqual(self.knowledge(),[])

    def test_relogin_keeps_prior_one_off_change_and_is_not_a_general_fix(self):
        answer="本次已修改。请重新登录一下。"
        self.seed(question="知汇鸽后台显示设置未生效怎么办？",answer=answer)
        self.install_provider(actions={1:["请重新登录一下。"]});self.run_engine()
        item=self.knowledge()[0]
        self.assertEqual(item["answer"],"本次已修改。\n请重新登录一下。")
        self.assertIn("本次修改",item["scope"])
        self.assertTrue(any("不能推广" in text for text in item["cautions"]))
        self.assertEqual(item["effect_status"],"suggested")

    def test_oral_same_sentence_relogin_also_keeps_one_off_boundary(self):
        answer="已修改了，重新登录一下。"
        self.seed(question="知汇鸽后台显示未生效怎么办？",answer=answer)
        self.install_provider(actions={1:[answer]});self.run_engine()
        item=self.knowledge()[0]
        self.assertEqual(item["answer"],answer)
        self.assertIn("本次修改",item["scope"])

    def test_proposed_numeric_settings_still_coexist(self):
        answers=["请在知汇鸽设置中将消息触发间隔设为7秒试试并保存。","请在知汇鸽设置中将消息触发间隔设为30秒试试并保存。"]
        for qa,answer in enumerate(answers,1):self.seed(qa,question="知汇鸽消息触发间隔怎么设置？",answer=answer)
        self.install_provider(actions={qa:[answer] for qa,answer in enumerate(answers,1)})
        self.run_engine()
        self.assertEqual(len(self.knowledge()),2)

    def test_variant_version_invalidates_old_compile_without_new_inference(self):
        from unittest.mock import patch
        self.seed(question="知汇鸽知识能导出吗？",answer="目前不支持的呢。")
        self.install_provider()
        with patch.object(variants,"VARIANT_VERSION","software-branches-v2"):self.run_engine()
        calls=len(self.provider.seen)
        self.assertEqual(self.knowledge(),[])
        self.assertEqual(engine.check(self.cfg,{})["needs_compile"],1)
        result=self.run_engine()
        self.assertEqual((result["job"]["calls"],len(self.provider.seen)),(0,calls))
        self.assertTrue(self.knowledge()[0]["quality_version"].endswith(variants.VARIANT_VERSION))


if __name__=="__main__":unittest.main()
