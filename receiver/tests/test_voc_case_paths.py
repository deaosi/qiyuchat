"""Case-level handling requires an exact current official reply and does not infer its outcome."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from src import push_sync, voc_assets, voc_case_paths, voc_engine
from src.config import Settings


class TestCasePaths(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "business.db"
        settings = Settings()
        self.cfg = replace(settings, runtime=replace(settings.runtime, db_path=str(self.path)))
        with voc_assets.connection(self.path, write=True) as conn:
            conn.executescript("""
                CREATE TABLE qa_pair(qa_id INTEGER PRIMARY KEY,session_id TEXT,customer_id TEXT);
                CREATE TABLE qa_origin(qa_id INTEGER PRIMARY KEY,session_id TEXT,question_message_ids TEXT,answer_message_ids TEXT,fingerprint TEXT,revision INTEGER,active INTEGER);
                CREATE TABLE raw_message(session_id TEXT,seq INTEGER,role TEXT,content TEXT,ts INTEGER,source_message_id TEXT);
                CREATE TABLE push_sync_state(session_id TEXT PRIMARY KEY,status TEXT,version TEXT,source_version TEXT,source_json TEXT);
            """)

    def seed(self, questions, answers, *, intervening=(), subsequent=()):
        with voc_assets.connection(self.path, write=True) as conn:
            qa = conn.execute("SELECT count(*) FROM qa_pair").fetchone()[0] + 1
            sid = qa + 1000
            turns = [(1, text, {}) for text in questions] + list(intervening) + [(0, text, {}) for text in answers] + list(subsequent)
            messages = [{"id": index, "sessionId": sid, "from": direction, "staffId": 9 if direction == 0 else 0,
                         "userId": 101, "externalUserId": "visitor-a", "mType": 1, "status": 1,
                         "time": 100000 + index * 1000, "msg": quote, **extra}
                        for index, (direction, quote, extra) in enumerate(turns, 1)]
            metadata = {"id": sid, "endTime": messages[-1]["time"] + 1000,
                        "staffMessageCount": sum(message["from"] == 0 for message in messages),
                        "userMessageCount": sum(message["from"] == 1 for message in messages)}
            projected = push_sync._project(metadata, messages, metadata, sid)
            conn.executemany("INSERT INTO raw_message VALUES(?,?,?,?,?,?)", (
                (str(sid), index, row["role"], row["content"], row["ts"], row.get("source_message_id"))
                for index, row in enumerate(projected)))
            snapshot = {"projection_version": push_sync.VERSION, "session": metadata, "messages": messages}
            conn.execute("INSERT INTO push_sync_state VALUES(?,?,?,?,?)", (str(sid), "completed", "v1", "v1", json.dumps(snapshot)))
            qids = [f"source:{sid}:{index}" for index in range(1, len(questions) + 1)]
            first_answer = len(questions) + len(intervening) + 1
            aids = [f"source:{sid}:{index}" for index in range(first_answer, first_answer + len(answers))]
            conn.execute("INSERT INTO qa_pair VALUES(?,?,?)", (qa, str(sid), "qiyu-external:visitor-a"))
            conn.execute("INSERT INTO qa_origin VALUES(?,?,?,?,?,?,?)",
                         (qa, str(sid), json.dumps(qids), json.dumps(aids), "origin-v1", 1, 1))
            question, question_parts = voc_engine._anchored_text(conn, str(sid), qids, "user")
            answer, answer_parts = voc_engine._anchored_text(conn, str(sid), aids, "staff")
        return {qa: {"question": question, "answer": answer, "question_message_ids": qids, "answer_message_ids": aids,
                     "_question_parts": question_parts, "_answer_parts": answer_parts, "_staff_verified": True,
                     "_origin_fingerprint": ["origin-v1", 1], "customer_name": "样例店"}}

    def test_extracts_exact_pending_action_and_desired_criterion(self):
        sources = self.seed(["参考聊天记录 说了稍等 但是没有待办 拼多多样例店的", "这个看下，稍等的一定要有待办"],
                            ["他这个是参考聊天记录的 我这边配置为知识加入待办"])
        before = self.path.read_bytes()
        [case] = voc_case_paths.build(self.cfg, sources)
        self.assertEqual((case["platform"], case["outcome"]), ("拼多多", "not_recorded"))
        self.assertEqual(case["problem"], {"qa_id": 1, "message_id": "source:1001:1",
                         "quote": "参考聊天记录 说了稍等 但是没有待办 拼多多[名称]的", "ts": 101000})
        self.assertEqual(case["criterion"], {"qa_id": 1, "message_id": "source:1001:2",
                         "quote": "稍等的一定要有待办", "ts": 102000})
        [step] = case["steps"]
        self.assertEqual((step["actor"], step["state"], step["action"]), ("staff", "proposed", "配置为知识加入待办"))
        self.assertEqual(step["evidence"], {"qa_id": 1, "message_id": "source:1001:3",
                         "quote": "他这个是参考聊天记录的 我这边配置为知识加入待办", "ts": 103000})
        self.assertEqual(self.path.read_bytes(), before)

    def test_reports_staff_intent_and_execution_claims_separately(self):
        sources = self.seed(["拼多多改地址的待办怎么处理？"], ["改地址这个进入待办了我这边先取消一下待办辛苦观察一下效果"])
        [case] = voc_case_paths.build(self.cfg, sources)
        [step] = case["steps"]
        self.assertEqual((step["state"], step["action"]), ("proposed", "取消一下待办"))
        self.assertEqual(case["outcome"], "not_recorded")

        sources = self.seed(["拼多多商品同步报错"], ["我这边已经重新同步商品"], subsequent=[(1, "现在可以了", {})])
        [case] = voc_case_paths.build(self.cfg, sources)
        self.assertEqual((case["steps"][0]["state"], case["outcome"]), ("reported", "not_recorded"))

    def test_new_exact_target_supports_merchant_instruction(self):
        sources = self.seed(["拼多多的会员到期积分提醒怎么开启？"], ["请开启会员到期积分提醒，然后点击保存。"])
        [case] = voc_case_paths.build(self.cfg, sources)
        step, save = case["steps"]
        self.assertEqual((step["actor"], step["state"], step["action"]), ("merchant", "instructed", "开启会员到期积分提醒"))
        self.assertIn(step["action"], step["evidence"]["quote"])
        self.assertEqual((save["action"], save["evidence"]), ("点击保存", step["evidence"]))

    def test_no_status_vague_checks_unproven_conditions_or_risky_instructions(self):
        for answer in ("同步成功了老师", "同步商品成功了老师", "进入待办了", "您让我重新同步商品", "联系技术",
                       "看一下商品同步状态", "再刷新一下", "请开启这个功能",
                       "如果是子账号，请重新同步商品", "请删除商品同步设置", "请导出客户资料"):
            with self.subTest(answer=answer):
                sources = self.seed(["拼多多商品同步失败，没有待办，怎么开启这个功能？"], [answer],
                                    subsequent=[(1, "是主账号 同步的", {})])
                self.assertEqual(voc_case_paths.build(self.cfg, sources), [])

    def test_requires_one_problem_target_and_no_other_visitor_or_unbound_turn(self):
        ambiguous = self.seed(["商品同步失败", "商品同步报错"], ["请重新同步商品。"])
        unrelated = self.seed(["商品同步失败"], ["请修改欢迎语。"])
        mixed = self.seed(["商品同步失败"], ["请重新同步商品。"], intervening=[(1, "另一个客户咨询", {"externalUserId": "visitor-b"})])
        unbound = self.seed(["商品同步失败"], ["请重新同步商品。"], intervening=[(1, "另一件事也要问一下", {})])
        auto = self.seed(["商品同步失败"], ["请重新同步商品。"], intervening=[(0, "已为您转接", {"autoReply": 1})])
        self.assertEqual(voc_case_paths.build(self.cfg, ambiguous | unrelated | mixed | unbound | auto), [])

    def test_current_origin_roles_and_snapshot_must_match(self):
        sources = self.seed(["商品同步失败"], ["请重新同步商品。"])
        self.assertEqual(len(voc_case_paths.build(self.cfg, sources)), 1)
        bad_offset = {1: {**sources[1], "_answer_parts": [{**sources[1]["_answer_parts"][0], "start": 1}]}}
        self.assertEqual(voc_case_paths.build(self.cfg, bad_offset), [])
        with voc_assets.connection(self.path, write=True) as conn:
            conn.execute("UPDATE qa_origin SET revision=2")
        self.assertEqual(voc_case_paths.build(self.cfg, sources), [])
        with voc_assets.connection(self.path, write=True) as conn:
            conn.execute("UPDATE qa_origin SET revision=1")
            conn.execute("UPDATE push_sync_state SET version='v2'")
        self.assertEqual(voc_case_paths.build(self.cfg, sources), [])
