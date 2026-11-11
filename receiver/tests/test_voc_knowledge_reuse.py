"""End-to-end read-time reuse decisions from reproducible official sessions."""
from __future__ import annotations

import copy
from dataclasses import replace
import sqlite3
import unittest

from src import voc_engine as engine, voc_knowledge_reuse as reuse
from tests import test_voc_engine as fixtures, test_voc_episode_replay as official
from tests.test_voc_domain import domain, ref


class TestKnowledgeReuse(unittest.TestCase):
    seed = official.TestEpisodeReplay.seed

    def setUp(self):
        official.TestEpisodeReplay.setUp(self)
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, dry_run=False, api_key="synthetic-only"))

    def compile(self, *feedback, configuration=True, answer=None, question=None, actions=True):
        question = question or ("知汇鸽机器人场景如何开启？" if configuration else "知汇鸽机器人场景为什么没有触发？")
        answer = answer or "进入机器人场景设置页面，开启场景开关并保存。"
        turns = [(1, question, "merchant-a", {}), (0, answer, "merchant-a", {})]
        turns.extend((1, text, "merchant-a", {}) for text in feedback)
        self.seed(turns)

        def payload(source, candidates, original):
            if source["question"] != question:
                return {**original, "excluded": True, "exclusion_reason": "测试后续反馈", "intents": []}
            domain_data = domain(primary_intent="configuration_guidance" if configuration else "functional_anomaly",
                                 merchant_goal=ref("question", question),
                                 modules=[ref("question", "机器人场景")])
            if configuration:
                domain_data["diagnosis"]["status"] = "not_applicable"
            if actions:
                domain_data["actions"] = [{"method_code": "scene_setting", "instruction": ref("answer", answer),
                                           "effect_status": "suggested", "verification": None}]
            return fixtures.extracted(source, candidates, question=question, domain=domain_data)

        result = engine.run(self.cfg, {}, embedder=fixtures.TestVectors(), provider=fixtures.FakeProvider(payload),
                            aligner=fixtures.ExactTestAligner())
        self.assertEqual(result["job"]["status"], "completed")
        return engine.knowledge(self.cfg, {})["items"][0]

    def test_silent_complete_configuration_is_reusable_without_claiming_effect(self):
        item = self.compile()
        self.assertEqual(item["knowledge_reuse"]["level"], "guidance")
        self.assertFalse(item["conversation_verification"]["auto_reusable"])
        self.assertEqual(item["conversation_verification"]["effect"], "unknown")
        self.assertEqual(item["applicability"][0]["source_refs"][0]["message_id"], "source:15429:1")
        self.assertEqual(item["evidence"][0]["claims"][0]["source_refs"][0]["message_id"], "source:15429:2")
        self.assertEqual(engine.knowledge(self.cfg, {}, item["id"])["knowledge"]["knowledge_reuse"], item["knowledge_reuse"])
        result = engine.summary(self.cfg, {})
        self.assertEqual((result["auto_reusable_knowledge"], result["guidance_knowledge"], result["verified_effect_knowledge"]), (1, 1, 0))

    def test_source_backed_read_only_diagnosis_is_guidance(self):
        item = self.compile(configuration=False, answer="进入机器人场景页面，检查当前开关是否开启。")
        self.assertEqual(item["knowledge_reuse"], {"level": "guidance", "reason_codes": ["complete_source_backed_diagnostic"]})

    def test_check_followed_by_configuration_change_needs_effect(self):
        item = self.compile(configuration=False, answer="检查机器人场景规则后关闭场景开关。")
        self.assertEqual(item["knowledge_reuse"], {"level": "reference", "reason_codes": ["effect_verification_missing"]})

    def test_interpreted_acceptance_is_not_a_verified_fix(self):
        item = self.compile("明白了，谢谢。", configuration=False)
        self.assertTrue(item["conversation_verification"]["auto_reusable"])
        self.assertEqual(item["knowledge_reuse"]["level"], "reference")

    def test_explicit_complete_execution_and_effect_are_verified(self):
        item = self.compile("我按您说的步骤做完后重新测试，已经触发了。", configuration=False)
        self.assertEqual(item["knowledge_reuse"]["level"], "verified")
        self.assertEqual(item["knowledge_reuse"]["evidence"][0]["message_id"], "source:15429:3")
        # The optional matcher does not rewrite the existing closure facts.
        self.assertFalse(item["conversation_verification"]["auto_reusable"])
        result = engine.summary(self.cfg, {})
        self.assertEqual((result["auto_reusable_knowledge"], result["guidance_knowledge"], result["verified_effect_knowledge"]), (1, 0, 1))

    def test_one_operation_in_multi_step_report_cannot_verify_whole_procedure(self):
        item = self.compile("已经保存好了，生效了。", configuration=False,
                            answer="关闭机器人场景开关，再开启开关并保存。")
        self.assertEqual(item["knowledge_reuse"]["level"], "reference")

    def test_later_negative_overrides_explicit_retest(self):
        item = self.compile("我按您说的步骤做完后重新测试，已经触发了。", "重新测还是没有触发。")
        self.assertEqual(item["knowledge_reuse"]["level"], "reference")

    def test_open_followup_keeps_guidance_out_of_reuse(self):
        item = self.compile("那个开关在哪里？")
        self.assertEqual(item["conversation_verification"]["acceptance"], "continuing")
        self.assertEqual(item["knowledge_reuse"]["level"], "reference")

    def test_missing_official_source_withholds_silent_guidance(self):
        item = self.compile()
        self.assertEqual(item["knowledge_reuse"]["level"], "guidance")
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE push_sync_state SET source_version='invalid'")
            db.commit()
        finally:
            db.close()
        self.assertEqual(engine.knowledge(self.cfg, {}, item["id"])["knowledge"]["knowledge_reuse"]["level"], "reference")

    def test_unbound_scope_claim_and_partial_flag_withhold_knowledge(self):
        item = self.compile()
        events = engine._events(self.cfg, {})
        cert = item["conversation_verification"]
        for key in ("source_steps_recovered", "one_off_dependent"):
            self.assertEqual(reuse.evaluate(self.cfg, {**item, key: True}, events, cert, events)["level"], "reference")
        broadened = copy.deepcopy(item)
        broadened["applicability"][0]["quote"] = "所有店铺"
        self.assertEqual(reuse.evaluate(self.cfg, broadened, events, cert, events)["level"], "reference")
        spoofed = copy.deepcopy(item)
        spoofed["evidence"][0]["claims"][0]["source_refs"][0]["message_id"] = "foreign"
        self.assertEqual(reuse.evaluate(self.cfg, spoofed, events, cert, events)["level"], "reference")

    def test_capability_assertion_remains_reference_even_after_understanding(self):
        item = self.compile("明白了。", question="知汇鸽机器人场景支持导出吗？",
                            answer="目前不支持的呢。", actions=False)
        self.assertEqual(item["kind"], "capability_fact")
        self.assertEqual(item["knowledge_reuse"]["level"], "reference")

    def test_destructive_action_requires_more_than_one_reported_effect(self):
        item = self.compile("我按您说的步骤做完后重新测试，已经触发了。",
                            answer="进入机器人场景设置页面，清空全部规则并保存。")
        self.assertEqual(item["knowledge_reuse"], {"level": "reference", "reason_codes": ["higher_risk_needs_evidence"]})
