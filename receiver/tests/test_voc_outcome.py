"""Conversation closure: original official synthetic messages, no model/network."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.config import Settings
from src import voc_outcome as O, push_sync as P


class TestOutcome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = Settings()
        self.cfg = replace(cfg, runtime=replace(cfg.runtime, db_path=str(root / "business.db"), state_dir=str(root)))
        self.sql("CREATE TABLE qa_pair(qa_id INTEGER PRIMARY KEY,session_id TEXT,customer_id TEXT)")
        self.sql("CREATE TABLE qa_origin(qa_id INTEGER PRIMARY KEY,session_id TEXT,question_message_ids TEXT,answer_message_ids TEXT,fingerprint TEXT,revision INTEGER,active INTEGER)")
        self.sql("CREATE TABLE raw_message(session_id TEXT,seq INTEGER,role TEXT,content TEXT,ts INTEGER,source_message_id TEXT)")
        self.sql("CREATE TABLE push_sync_state(session_id TEXT PRIMARY KEY,status TEXT,version TEXT,source_version TEXT,source_json TEXT)")

    def sql(self, query, params=()):
        conn=sqlite3.connect(self.cfg.runtime.db_path)
        try:
            result=conn.execute(query, params).fetchall()
            conn.commit()
            return result
        finally:
            conn.close()

    def seed(self, followups=(), *, qa=1, sid=None, cid="visitor-a", changes=None):
        sid = sid or 1000 + qa
        # followups: (direction,text[,external identity[,mType[,staffId]]])
        turns = [(1, "知汇鸽场景怎么开启？"), (0, "进入场景设置页面，开启开关并保存。"), *followups]
        messages = []
        for index, turn in enumerate(turns, 1):
            direction, text = turn[:2]
            messages.append({"id":index,"sessionId":sid,"from":direction,"staffId":turn[4] if len(turn)>4 else 9 if direction==0 else 0,
                "userId":101,"externalUserId":turn[2] if len(turn)>2 else cid,"mType":turn[3] if len(turn)>3 else 1,
                "status":1,"time":100000+index*1000,"msg":text})
        metadata = {"id":sid,"endTime":messages[-1]["time"]+1000,"staffMessageCount":sum(m["from"]==0 for m in messages),
                    "userMessageCount":sum(m["from"]==1 for m in messages)}
        projected = P._project(metadata, messages, metadata, sid)
        for index, message in enumerate(projected):
            self.sql("INSERT INTO raw_message VALUES(?,?,?,?,?,?)", (str(sid),index,message["role"],message["content"],message["ts"],message.get("source_message_id")))
        snapshot = {"projection_version":P.VERSION,"session":metadata,"messages":messages}
        self.sql("INSERT INTO push_sync_state VALUES(?,?,?,?,?)",(str(sid),"completed","v1","v1",json.dumps(snapshot)))
        qids, aids = [f"source:{sid}:1"], [f"source:{sid}:2"]
        self.sql("INSERT INTO qa_pair VALUES(?,?,?)",(qa,str(sid),"qiyu-external:"+cid))
        self.sql("INSERT INTO qa_origin VALUES(?,?,?,?,?,?,?)",(qa,str(sid),json.dumps(qids),json.dumps(aids),"origin-v1",1,1))
        row = {"id":f"event-{qa}","qa_id":qa,"topic_id":"topic-a","fingerprint":"unchanged-extraction",
               "source":{"_staff_verified":True,"_origin_fingerprint":["origin-v1",1],"question_message_ids":qids,"answer_message_ids":aids},
               "data":{"question":"如何开启知汇鸽场景？"}}
        if changes:
            changes(messages, metadata)
            self.sql("UPDATE push_sync_state SET source_json=? WHERE session_id=?",(json.dumps({**snapshot,"session":metadata,"messages":messages}),str(sid)))
        return row

    def test_explicit_success_has_real_role_id_time_and_business_read_only(self):
        row = self.seed([(1,"现在可以触发了，谢谢。")])
        original = Path(self.cfg.runtime.db_path).read_bytes()
        result = O.evaluate(self.cfg,[row])
        self.assertEqual((result["status"],result["acceptance"],result["effect"],result["auto_reusable"]),
                         ("accepted","resolved","customer_reported_success",True))
        evidence=result["evidence"][0]
        self.assertEqual((evidence["role"],evidence["message_id"],evidence["ts"]),("merchant","source:1001:3",103000))
        self.assertEqual(Path(self.cfg.runtime.db_path).read_bytes(), original)
        self.assertNotIn("source_identity",json.dumps(result))

    def test_understood_is_not_effect_success(self):
        row=self.seed([(1,"明白了，谢谢。")])
        result=O.evaluate(self.cfg,[row])
        self.assertTrue(result["auto_reusable"])
        self.assertEqual((result["acceptance"],result["effect"]),("understood","not_established"))

    def test_hopes_guesses_conditions_and_future_attempts_are_not_success(self):
        for index,text in enumerate(("希望这下可以了","应该可以了","那我试试应该可以了","如果恢复了就好了","等会试试","准备试一下","现在正常了吗")):
            with self.subTest(text=text):
                result=O.evaluate(self.cfg,[self.seed([(1,text)],qa=10+index)])
                self.assertFalse(result["auto_reusable"])
                self.assertEqual(result["status"],"unknown")

    def test_abandonment_cannot_accumulate_weak_acceptance(self):
        rows=[self.seed([(1,"先这样")]),self.seed([(1,"算了，不用了")],qa=2)]
        result=O.evaluate(self.cfg,rows)
        self.assertFalse(result["auto_reusable"])
        self.assertEqual(result["coverage"]["independent_weak_supports"],0)

    def test_silence_and_end_are_unknown(self):
        result=O.evaluate(self.cfg,[self.seed()])
        self.assertEqual((result["status"],result["auto_reusable"]),("unknown",False))
        self.assertEqual(result["coverage"]["complete_sources"],1)

    def test_thanks_but_failure_wins(self):
        result=O.evaluate(self.cfg,[self.seed([(1,"谢谢，但是还是不行。")])])
        self.assertEqual(result["status"],"negative")
        self.assertFalse(result["auto_reusable"])

    def test_later_failure_after_new_staff_revokes_earlier_acceptance(self):
        row=self.seed([(1,"明白了"),(0,"再检查一下开关。"),(1,"测试后还是不行。")])
        result=O.evaluate(self.cfg,[row])
        self.assertEqual(result["status"],"negative")
        self.assertFalse(result["auto_reusable"])

    def test_success_after_new_intervention_does_not_belong_to_old_method(self):
        row=self.seed([(0,"改用另一种设置。"),(1,"成功了，谢谢。")])
        result=O.evaluate(self.cfg,[row])
        self.assertEqual(result["status"],"unknown")
        self.assertFalse(result["auto_reusable"])

    def test_pure_staff_polite_closing_preserves_customer_acceptance(self):
        for index,turns in enumerate(([(1,"明白了，谢谢。"),(0,"不客气，有问题随时联系。")],
                                      [(1,"成功了，谢谢。"),(0,"好的")])):
            with self.subTest(index=index):
                result=O.evaluate(self.cfg,[self.seed(turns,qa=10+index)])
                self.assertTrue(result["auto_reusable"])
                self.assertEqual(result["status"],"accepted")

    def test_substantive_staff_operation_revokes_early_acknowledgement(self):
        row=self.seed([(1,"明白了，谢谢。"),(0,"不客气，另外请重新开启开关后保存。")])
        result=O.evaluate(self.cfg,[row])
        self.assertFalse(result["auto_reusable"])
        self.assertEqual(result["status"],"unknown")

    def test_continuing_question_and_parameter_answer_are_not_acceptance(self):
        for index,followup in enumerate(("那开关在哪？","店铺名称是合成测试店","账号是子账号")):
            with self.subTest(followup=followup):
                row=self.seed([(1,followup)],qa=10+index)
                result=O.evaluate(self.cfg,[row])
                self.assertFalse(result["auto_reusable"])
                self.assertEqual(result["status"],"unknown")

    def test_one_weak_closure_is_not_auto_two_independent_are_accepted(self):
        first=self.seed([(1,"谢谢")])
        self.assertEqual(O.evaluate(self.cfg,[first])["status"],"weak")
        second=self.seed([(1,"好的，谢谢")],qa=2,cid="visitor-b")
        result=O.evaluate(self.cfg,[first,second])
        self.assertEqual((result["status"],result["acceptance"],result["auto_reusable"]),("accepted","weak_closure",True))
        self.assertEqual(result["coverage"]["independent_weak_supports"],2)

    def test_repeated_event_does_not_inflate_weak_independence(self):
        row=self.seed([(1,"谢谢")])
        copy={**row,"id":"event-again"}
        result=O.evaluate(self.cfg,[row,copy],events=[row])
        self.assertEqual(result["coverage"]["independent_weak_supports"],1)
        self.assertFalse(result["auto_reusable"])

    def test_multiple_intents_cannot_all_receive_generic_acceptance(self):
        row=self.seed([(1,"明白了，谢谢。")])
        other={**row,"id":"other-intent","topic_id":"topic-b"}
        result=O.evaluate(self.cfg,[row],events=[row,other])
        self.assertFalse(result["auto_reusable"])
        self.assertIn("ambiguous_multiple_intents",result["reason_codes"])

    def test_source_intent_count_blocks_even_if_sibling_hidden_by_filter(self):
        row=self.seed([(1,"明白了")])
        row["data"]["source_intent_count"]=2
        self.assertFalse(O.evaluate(self.cfg,[row],events=[row])["auto_reusable"])

    def test_known_new_topic_does_not_supply_success_to_previous_topic(self):
        row=self.seed([(1,"如何配置发票场景？"),(0,"进入另一场景设置。"),(1,"成功了")])
        other={"id":"other","qa_id":2,"topic_id":"topic-b","source":{"question_message_ids":["source:1001:3"]}}
        result=O.evaluate(self.cfg,[row],events=[row,other])
        self.assertFalse(result["auto_reusable"])

    def test_other_visitor_feedback_cannot_accept_current_answer(self):
        row=self.seed([(1,"成功了，谢谢。","visitor-b")])
        self.assertFalse(O.evaluate(self.cfg,[row])["auto_reusable"])

    def test_known_same_topic_reopen_after_transition_revokes_acknowledgement(self):
        row=self.seed([(1,"明白了"),(1,"如何配置另一功能？"),(0,"进入另一功能设置。"),(1,"原来的场景还是不行")])
        other={"id":"other","qa_id":2,"topic_id":"topic-b","source":{"question_message_ids":["source:1001:4"]}}
        reopened={"id":"reopen","qa_id":3,"topic_id":"topic-a","source":{"question_message_ids":["source:1001:6"]}}
        result=O.evaluate(self.cfg,[row],events=[row,other,reopened])
        self.assertFalse(result["auto_reusable"])
        self.assertIn("same_topic_reopened_after_transition",result["reason_codes"])
        accepted=self.seed([(1,"成功了")],qa=4)
        combined=O.evaluate(self.cfg,[row,accepted],events=[row,other,reopened,accepted])
        self.assertFalse(combined["auto_reusable"])
        self.assertIn("branch_has_unclosed_followup",combined["reason_codes"])

    def test_incomplete_pruned_or_stale_ledger_never_accepts(self):
        for index,sql in enumerate(("DELETE FROM raw_message WHERE source_message_id='source:1001:3'",
                "UPDATE push_sync_state SET status='pending'","UPDATE push_sync_state SET source_version='old'",
                "UPDATE qa_origin SET active=0")):
            with self.subTest(sql=sql):
                if index:
                    self.sql("DELETE FROM qa_pair");self.sql("DELETE FROM qa_origin");self.sql("DELETE FROM raw_message");self.sql("DELETE FROM push_sync_state")
                row=self.seed([(1,"成功了")])
                self.sql(sql)
                self.assertFalse(O.evaluate(self.cfg,[row])["auto_reusable"])

    def test_visitor_direction_with_positive_staff_id_is_not_staff_reply(self):
        row=self.seed([(1,"成功了")])
        self.sql("UPDATE raw_message SET role='staff' WHERE source_message_id='source:1001:3'")
        result=O.evaluate(self.cfg,[row])
        self.assertFalse(result["auto_reusable"])

    def test_negative_in_any_cited_source_blocks_branch_not_global_truth(self):
        first=self.seed([(1,"成功了")])
        second=self.seed([(1,"还是不行")],qa=2)
        result=O.evaluate(self.cfg,[first,second])
        self.assertEqual((result["status"],result["coverage"]["negative_sources"]),("negative",1))
        self.assertFalse(result["auto_reusable"])

    def test_accepted_and_still_asking_sources_are_not_a_closed_branch(self):
        first=self.seed([(1,"成功了")])
        second=self.seed([(1,"那场景开关在哪里？")],qa=2)
        result=O.evaluate(self.cfg,[first,second])
        self.assertFalse(result["auto_reusable"])
        self.assertIn("branch_has_unclosed_followup",result["reason_codes"])

    def test_filter_hidden_negative_and_missing_citation_never_become_auto(self):
        from src.voc_engine import _with_conversation_verification
        first=self.seed([(1,"成功了")])
        second=self.seed([(1,"还是不行")],qa=2)
        item={"evidence":[{"event_id":r["id"],"source_fingerprint":r["fingerprint"]} for r in (first,second)]}
        certificate=_with_conversation_verification(self.cfg,[item],[first],events=[first,second])[0]["conversation_verification"]
        self.assertFalse(certificate["auto_reusable"])
        self.assertEqual(certificate["status"],"negative")
        missing=_with_conversation_verification(self.cfg,[item],[first],events=[first])[0]["conversation_verification"]
        self.assertFalse(missing["auto_reusable"])
        self.assertIn("incomplete_branch_citations",missing["reason_codes"])

    def test_invalid_snapshot_direction_count_fails_closed_not_api_error(self):
        row=self.seed([(1,"成功了")],changes=lambda messages,meta:meta.update(staffMessageCount=10))
        result=O.evaluate(self.cfg,[row])
        self.assertFalse(result["auto_reusable"])
        self.assertIn("incomplete_source_coverage",result["reason_codes"])

    def test_certificate_is_dynamic_without_changing_extraction_fingerprint(self):
        row=self.seed([(1,"明白了")])
        result=O.evaluate(self.cfg,[row])
        self.assertEqual(row["fingerprint"],"unchanged-extraction")
        self.sql("UPDATE push_sync_state SET status='pending'")
        updated=O.evaluate(self.cfg,[row])
        self.assertNotEqual(result["fingerprint"],updated["fingerprint"])
        self.assertEqual(row["fingerprint"],"unchanged-extraction")
        self.assertFalse(updated["auto_reusable"])


if __name__ == "__main__":
    unittest.main()
