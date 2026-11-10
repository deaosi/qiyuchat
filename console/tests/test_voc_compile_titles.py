"""Branch-local titles and manual dispositions survive compiler-only changes."""
from __future__ import annotations
import unittest
from unittest.mock import patch
from tests import test_voc_support_engine as support
from tests.test_voc_domain import domain,ref,confirmed
from tests.test_voc_engine import FakeProvider,extracted
from src import voc_engine as engine,voc_assets as assets,voc_variants as variants


class TestKnowledgeTitles(unittest.TestCase):
    sql=support.TestSupportEngine.sql
    seed=support.TestSupportEngine.seed
    run_engine=support.TestSupportEngine.run_engine
    install_provider=support.TestSupportEngine.install_provider
    def setUp(self):support.TestSupportEngine.setUp(self)

    def test_branch_title_does_not_inherit_another_incident_reproduction_context(self):
        cause1='已核查，该退货场景未启用导致机器人没有触发。';step1='请开启场景并保存后测试。'
        cause2='研发已确认这是知汇鸽2.8版本的触发缺陷。';step2='临时关闭再开启场景并保存，已复测机器人可以正常触发。'
        self.seed(1,question='知汇鸽退货场景不触发怎么办？',answer=cause1+step1)
        self.seed(2,question='知汇鸽2.8版本机器人场景无法触发，有临时办法吗？',answer=cause2+step2)
        titles={1:'知汇鸽退货场景不触发怎么办？',2:'知汇鸽机器人场景无法触发时如何临时处理？'}
        def payload(source,candidates,original):
            qa=source['qa_id'];cause=cause1 if qa==1 else cause2;step=step1 if qa==1 else step2
            d=domain(merchant_goal=ref('question',source['question']),diagnosis=confirmed(cause,'config_cause_confirmed' if qa==1 else 'defect_confirmed','scene_disabled' if qa==1 else 'version_trigger_defect'),actions=[{'method_code':'enable_scene' if qa==1 else 'toggle_scene','instruction':ref('answer',step),'effect_status':'suggested' if qa==1 else 'verified','verification':None if qa==1 else ref('answer',step)}])
            return extracted(source,candidates,question=titles[qa],theme='机器人触发异常',scope=[] if qa==1 else [{'kind':'version','source':'question','quote':'2.8版本'}],domain=d)
        self.provider=FakeProvider(payload)
        self.run_engine(max_tokens=400000)
        topic=engine.topics(self.cfg,{})['items'][0]
        self.assertIn('退货',topic['question'])
        item=next(x for x in engine.knowledge(self.cfg,{})['items'] if any(e['qa_id']==2 for e in x['evidence']))
        self.assertEqual(item['topic_id'],topic['id'])
        self.assertEqual(item['question'],titles[2]);self.assertNotIn('退货',item['question'])
        self.assertEqual(item['knowledge_question'],titles[2])
        self.assertEqual(engine.topics(self.cfg,{})['items'][0]['question'],topic['question'])

    def test_projection_key_upgrade_keeps_latest_manual_evidence_disposition(self):
        step='请开启场景并保存后测试。';self.seed(question='知汇鸽场景怎么配置？',answer=step)
        self.install_provider(methods={1:[('enable_scene',step,'suggested')]})
        actual=variants.plans
        def old_plans(cfg,topic,rows):
            return [{**p,'key':p['key']+'_legacy_projection'} for p in actual(cfg,topic,rows)]
        with patch.object(variants,'VARIANT_VERSION','software-branches-v3'),patch.object(variants,'plans',side_effect=old_plans):self.run_engine()
        with assets.connection(engine.path(self.cfg)) as conn:old=dict(conn.execute('SELECT * FROM v2_asset ORDER BY id DESC LIMIT 1').fetchone())
        engine.review(self.cfg,kind='knowledge',item_id=str(old['id']),status='ignored',revision=old['revision'])
        result=self.run_engine()
        self.assertEqual(result['job']['calls'],0)
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)
        with assets.connection(engine.path(self.cfg)) as conn:new=dict(conn.execute('SELECT * FROM v2_asset ORDER BY id DESC LIMIT 1').fetchone())
        self.assertNotEqual(old['variant_key'],new['variant_key'])
        self.assertEqual((new['status'],new['manual']),('ignored',1))
        # A later explicit human activation must win over historical ignore.
        engine.review(self.cfg,kind='knowledge',item_id=str(new['id']),status='ready',revision=new['revision'])
        with patch.object(variants,'VARIANT_VERSION',variants.VARIANT_VERSION+'-next'):
            second=self.run_engine()
            self.assertEqual(second['job']['calls'],0)
            self.assertEqual(engine.knowledge(self.cfg,{})['total'],1)


if __name__=='__main__':unittest.main()
