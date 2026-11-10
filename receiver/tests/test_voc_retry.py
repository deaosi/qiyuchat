"""Bounded correction attempts: synthetic provider, real accounting and storage."""
from __future__ import annotations
import json
import sqlite3
import unittest
from pathlib import Path
from tests import test_voc_engine as base
from src import voc_engine as engine, voc_assets as assets
from src.voc_extract import request_payload,output_token_limit,MAX_SOURCE_CHARS,LangExtractAligner
from src.voc_model import VocModelError


class TestExtractionRetry(unittest.TestCase):
    sql=base.TestEngine.sql
    seed=base.TestEngine.seed
    run_engine=base.TestEngine.run_engine
    def setUp(self):
        base.TestEngine.setUp(self)
        self.aligner=LangExtractAligner()

    def test_validation_retry_recovers_once_without_unchecked_output_or_duplicate_evidence(self):
        self.seed();seen=[];requests=[];before=self.sql('SELECT * FROM qa_pair')
        def extract(cfg,source,candidates,limit):
            seen.append(source.copy());requests.append(request_payload(cfg,source,candidates,limit))
            for target in (Path(self.cfg.runtime.db_path),engine.path(self.cfg)):
                conn=sqlite3.connect(target,timeout=.1)
                try:conn.execute('BEGIN EXCLUSIVE');conn.rollback()
                finally:conn.close()
            if len(seen)==1:
                return {'payload':base.extracted(source,claims=[{'type':'step','quote':'UNTRUSTED_OLD_OUTPUT_NOT_SOURCE'}]),'usage_tokens':7}
            return {'payload':base.extracted(source,candidates),'usage_tokens':13}
        self.provider.extract=extract
        run_result=self.run_engine(max_calls=2);result=run_result['job']
        self.assertEqual((result['processed'],result['failed'],result['skipped'],result['calls'],result['usage_tokens']),(1,0,0,2,20))
        self.assertEqual((result['status'],result['error']),('completed',''))
        self.assertNotIn('_extraction_feedback',seen[0]);self.assertIn('_extraction_feedback',seen[1])
        self.assertEqual(seen[0]['question'],seen[1]['question']);self.assertEqual(seen[0]['answer'],seen[1]['answer'])
        self.assertNotIn('UNTRUSTED_OLD_OUTPUT',json.dumps(requests[1]))
        self.assertEqual(result['reserved_tokens'],sum(len(assets.dump(r).encode('utf-8'))+r['max_tokens'] for r in requests))
        self.assertEqual(engine.topics(self.cfg,{})['items'][0]['event_count'],1)
        self.assertEqual(engine._events(self.cfg,{})[0]['data']['extraction_attempts'],2)
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],1)
        self.assertEqual(self.sql('SELECT * FROM qa_pair'),before)
        from src import voc_eval
        report=voc_eval.measure(self.cfg,[{'question':before[0]['question'],'answer':before[0]['answer'],'expected_topic':'install','answer_kind':'resolved_answer','must_not_be_ready':False}],run_result)
        self.assertEqual((report['actual_attempts'],report['successful_retried_sources'],report['first_pass_complete']),(2,1,False))

    def test_permanent_retryable_failure_counts_two_attempts_one_failed_source(self):
        self.seed();calls=[]
        def extract(*args):calls.append(1);raise VocModelError('模型输出结构无效。',usage_tokens=5)
        self.provider.extract=extract
        job=self.run_engine(max_calls=2)['job']
        self.assertEqual((len(calls),job['calls'],job['failed'],job['processed'],job['usage_tokens']),(2,2,1,0,10))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)
        self.assertEqual(engine.exceptions(self.cfg,{})['total'],1)
        self.assertEqual(engine.summary(self.cfg,{})['coverage']['pending_pairs'],1)

    def test_nonretryable_and_unknown_errors_never_trigger_correction(self):
        for error in (VocModelError('网关配置被拒绝。',retryable=False,usage_tokens=4),RuntimeError('secret-provider-token')):
            with self.subTest(error=type(error).__name__):
                calls=[]
                def extract(*args):calls.append(1);raise error
                self.provider.extract=extract
                if not self.sql('SELECT * FROM qa_pair'):self.seed()
                job=self.run_engine(max_calls=2)['job']
                self.assertEqual((len(calls),job['calls'],job['failed']),(1,1,1))
                self.assertNotIn('secret-provider-token',json.dumps(job))
                self.assertNotIn('secret-provider-token',json.dumps(engine.exceptions(self.cfg,{})))

    def test_exhausted_call_budget_leaves_unfinished_correction_pending(self):
        self.seed()
        self.provider.extract=lambda *args: (_ for _ in ()).throw(VocModelError('结构错误。',usage_tokens=7))
        run_result=self.run_engine(max_calls=1);job=run_result['job']
        self.assertEqual((job['status'],job['calls'],job['processed'],job['failed'],job['skipped'],job['usage_tokens']),('partial',1,0,0,1,7))
        self.assertEqual(engine.summary(self.cfg,{})['coverage']['pending_pairs'],1)
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)
        from src.voc_eval import _successful_retries
        self.assertIsNone(_successful_retries(job))

    def test_reservation_budget_checked_before_second_network_attempt(self):
        self.seed();calls=[]
        source=engine._sources(self.cfg,{})[1];limit=output_token_limit(source)
        first=len(assets.dump(request_payload(self.cfg.llm,source,[],limit)).encode('utf-8'))+limit
        def extract(*args):calls.append(1);raise VocModelError('结构错误。',usage_tokens=9)
        self.provider.extract=extract
        job=self.run_engine(max_calls=2,max_tokens=first)['job']
        self.assertEqual((len(calls),job['calls'],job['reserved_tokens'],job['failed'],job['skipped']),(1,1,first,0,1))
        self.assertEqual(job['status'],'partial')

    def test_correction_keeps_quality_gate_and_cannot_create_false_ready(self):
        self.seed(answer='仅管理员可以在控制台页面保存设置。');calls=[]
        def extract(cfg,source,candidates,limit):
            calls.append(1)
            if len(calls)==1:return {'payload':{},'usage_tokens':3}
            return {'payload':base.extracted(source,claims=[{'type':'step','quote':'可以在控制台页面保存设置'}]),'usage_tokens':6}
        self.provider.extract=extract
        job=self.run_engine(max_calls=2)['job']
        self.assertEqual((job['processed'],job['calls'],job['usage_tokens']),(1,2,9))
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)
        self.assertEqual(engine.exceptions(self.cfg,{})['items'][0]['kind'],'incomplete_claim_coverage')

    def test_oversized_source_is_not_retried_or_truncated(self):
        self.seed(question="问"*(MAX_SOURCE_CHARS+1),answer="")
        job=self.run_engine(max_calls=2)['job']
        self.assertEqual((job['calls'],job['failed'],job['processed']),(0,1,0))
        self.assertFalse(self.provider.seen)
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_recovered_source_without_event_is_still_counted_truthfully(self):
        self.seed(question="谢谢。",answer="");calls=[]
        def extract(cfg,source,candidates,limit):
            calls.append(1)
            if len(calls)==1:raise VocModelError('输出不完整。',usage_tokens=3)
            return {'payload':{'qa_id':source['qa_id'],'excluded':True,'exclusion_reason':'礼貌结束语，没有独立咨询。','intents':[]},'usage_tokens':4}
        self.provider.extract=extract
        value=self.run_engine(max_calls=2)
        from src.voc_eval import _successful_retries
        self.assertEqual((value['job']['processed'],value['job']['calls'],_successful_retries(value['job'])),(1,2,1))
        self.assertEqual(engine.topics(self.cfg,{})['total'],0)
        self.assertEqual(engine.knowledge(self.cfg,{})['total'],0)

    def test_feedback_contains_only_controlled_field_path_and_code(self):
        self.seed();sources=[]
        def extract(cfg,source,candidates,limit):
            sources.append(source)
            if len(sources)==1:raise VocModelError('领域合同：domain.actions[0].instruction.source: PRIVATE_TOKEN_OR_CUSTOMER_TEXT',usage_tokens=4)
            return {'payload':base.extracted(source),'usage_tokens':5}
        self.provider.extract=extract
        self.run_engine(max_calls=2)
        feedback=sources[1]['_extraction_feedback']
        self.assertEqual(feedback,{'code':'reference_shape_or_alignment','field_path':'domain.actions[0].instruction.source','attempt':2})
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(feedback))
        self.assertNotIn('PRIVATE_TOKEN',json.dumps(request_payload(self.cfg.llm,sources[1],[],3000)))


if __name__=='__main__':unittest.main()
