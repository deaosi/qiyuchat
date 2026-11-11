"""冒烟测试：不依赖网络、不依赖任何凭证。

    python -m tests.test_smoke
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clean.rules import build_clean_text, is_noise, mask_pii, rule_score  # noqa: E402
from src.db import DB  # noqa: E402
from src.qiyu.sign import build_signature  # noqa: E402
from src.xlsx import to_dicts  # noqa: E402


class TestSign(unittest.TestCase):
    def test_signature_deterministic(self):
        ts, sig, raw = build_signature("secret", {"start": 1, "end": 2}, ts=1700000000)
        self.assertEqual(raw, '{"start":1,"end":2}')
        self.assertEqual(ts, 1700000000)
        self.assertEqual(len(sig), 40)
        # 同参数必须同签名
        self.assertEqual(build_signature("secret", {"start": 1, "end": 2}, ts=1700000000)[1], sig)

    def test_secret_changes_signature(self):
        a = build_signature("secret", {"a": 1}, ts=1700000000)[1]
        b = build_signature("secret2", {"a": 1}, ts=1700000000)[1]
        self.assertNotEqual(a, b)


class TestMask(unittest.TestCase):
    def test_phone(self):
        self.assertEqual(mask_pii("我电话13812345678"), "我电话138****5678")

    def test_id_card(self):
        out = mask_pii("身份证110101199001011234")
        self.assertNotIn("19900101", out)

    def test_email(self):
        self.assertIn("***", mask_pii("邮箱zhangsan@example.com"))

    def test_order(self):
        self.assertIn("已脱敏", mask_pii("订单号: 2024010112345678"))


class TestRules(unittest.TestCase):
    def test_noise(self):
        self.assertTrue(is_noise({"is_valid": 0, "s_type": 0, "round_number": 3})[0])
        self.assertTrue(is_noise({"is_valid": 1, "s_type": 3, "round_number": 3})[0])
        self.assertTrue(is_noise({"is_valid": 1, "s_type": 0, "round_number": 1})[0])
        self.assertFalse(is_noise({"is_valid": 1, "s_type": 0, "round_number": 5})[0])

    def test_score_range(self):
        row = {"first_reply_cost": 5000, "avg_resp_duration": 20000,
               "round_number": 6, "evaluation": 100}
        s = rule_score(row)
        self.assertTrue(0 <= s <= 100)
        self.assertGreater(s, 60)

    def test_build_text_empty(self):
        self.assertEqual(build_clean_text({"round_number": 3}, ""), "")


class TestDb(unittest.TestCase):
    def test_upsert_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            db = DB(str(Path(d) / "t.db"))
            rows = [{"id": "1", "startTime": 1, "endTime": 2, "sType": 0,
                     "staffId": "9", "staffName": "张三", "roundNumber": 3,
                     "isValid": 1, "firstReplyCost": 1000, "avgRespDuration": 2000}]
            db.upsert_sessions(rows)
            db.upsert_sessions(rows)
            self.assertEqual(db.stats()["raw_session"], 1)

            db.upsert_messages("1", [{"role": "user", "content": "你好"},
                                     {"role": "staff", "content": "您好"}])
            self.assertIn("你好", db.get_transcript("1"))

            db.upsert_clean({"session_id": "1", "prompt_version": "v1",
                             "category": "咨询", "quality_score": 80})
            db.upsert_clean({"session_id": "1", "prompt_version": "v1",
                             "category": "退换", "quality_score": 70})
            self.assertEqual(db.stats()["clean_result"], 1)
            self.assertEqual(db.iter_clean("v1")[0]["category"], "退换")

            db.set_watermark("w", "123")
            self.assertEqual(db.get_watermark("w"), "123")
            db.close()


class TestRotate(unittest.TestCase):
    """滑动窗口裁剪：按自然日对齐，进一天出一天，问答对不受影响。"""

    def _seed(self, db, sid, days_ago, end_ms):
        db.upsert_sessions([{"id": sid, "startTime": end_ms - 60000, "endTime": end_ms,
                             "sType": 0, "staffId": "9", "staffName": "张三",
                             "roundNumber": 3, "isValid": 1}])
        db.upsert_messages(sid, [{"role": "user", "content": f"{sid}的问题", "ts": end_ms},
                                 {"role": "staff", "content": "答", "ts": end_ms}])

    def test_rotate_keeps_window(self):
        now_ms = 1700000000000
        day = 86400_000
        with tempfile.TemporaryDirectory() as d:
            db = DB(str(Path(d) / "t.db"))
            self._seed(db, "old", 100, now_ms - 100 * day)
            self._seed(db, "mid", 40, now_ms - 40 * day)
            self._seed(db, "new", 0, now_ms)
            db.upsert_qa_pairs([{"session_id": "old", "idx": 0,
                                 "question": "旧问题", "answer": "答"}])

            # 先演练：一条都不能少
            dry = db.rotate(window_days=45, now_ms=now_ms, dry_run=True)
            self.assertEqual(dry["raw_session"], 1)
            self.assertEqual(db.stats()["raw_session"], 3)

            r = db.rotate(window_days=45, now_ms=now_ms)
            self.assertEqual(r["raw_session"], 1)
            self.assertEqual(r["raw_message"], 2)
            self.assertEqual(db.stats()["raw_session"], 2)
            self.assertEqual(db.stats()["raw_message"], 4)
            # 问答对是分析资产，裁原始明细时不动
            self.assertEqual(db.stats()["qa_pair"], 1)
            db.close()

    def test_rotate_counts_orphan(self):
        """窗口外没抽过问答的会话被删 → orphan 计数暴露抽取漏跑。"""
        now_ms = 1700000000000
        day = 86400_000
        with tempfile.TemporaryDirectory() as d:
            db = DB(str(Path(d) / "t.db"))
            self._seed(db, "a", 100, now_ms - 100 * day)
            self._seed(db, "b", 99, now_ms - 99 * day)
            db.upsert_qa_pairs([{"session_id": "a", "idx": 0, "question": "q", "answer": "a"}])

            r = db.rotate(window_days=45, now_ms=now_ms)
            self.assertEqual(r["raw_session"], 2)
            self.assertEqual(r["orphan_session"], 1)
            db.close()

    def test_rotate_disabled(self):
        now_ms = 1700000000000
        with tempfile.TemporaryDirectory() as d:
            db = DB(str(Path(d) / "t.db"))
            self._seed(db, "old", 999, now_ms - 999 * 86400_000)
            r = db.rotate(window_days=0, now_ms=now_ms)
            self.assertEqual(r["raw_session"], 0)
            self.assertEqual(db.stats()["raw_session"], 1)
            db.close()

    def test_rotate_keeps_open_session(self):
        """end_ts=0 表示会话还在进行中（receiver 刚建、还没收到 SESSION_END）。

        这类会话必须按 start_ts 判断，不能被当成「最老的数据」删掉。
        """
        now_ms = 1700000000000
        day = 86400_000
        with tempfile.TemporaryDirectory() as d:
            db = DB(str(Path(d) / "t.db"))
            # 今天刚开始、endTime 还是 0
            db.upsert_sessions([{"id": "open", "startTime": now_ms, "endTime": 0,
                                 "sType": 0, "isValid": 1}])
            # 100 天前开始、也一直没结束 → 按 start_ts 判为过期
            db.upsert_sessions([{"id": "stale", "startTime": now_ms - 100 * day,
                                 "endTime": 0, "sType": 0, "isValid": 1}])
            # 两个时间戳都是 0 的来路不明会话 → 一律不删
            db.upsert_sessions([{"id": "unknown", "startTime": 0, "endTime": 0,
                                 "sType": 0, "isValid": 1}])

            r = db.rotate(window_days=45, now_ms=now_ms)
            self.assertEqual(r["raw_session"], 1)
            left = {row["session_id"] for row in db.iter_sessions()}
            self.assertEqual(left, {"open", "unknown"})
            db.close()

    def test_close_uid_session_writes_end_ts(self):
        """会话结束时必须同时写 raw_session.end_ts，否则 rotate 一直以为它没结束。"""
        with tempfile.TemporaryDirectory() as d:
            db = DB(str(Path(d) / "t.db"))
            db.upsert_sessions([{"id": "s1", "startTime": 1000, "endTime": 0,
                                 "sType": 0, "isValid": 1}])
            db.bind_uid_session("u1", "s1", ts=1000)
            db.close_uid_session("u1", 2000)
            row = db._conn.execute(
                "SELECT end_ts FROM raw_session WHERE session_id='s1'").fetchone()
            self.assertEqual(row["end_ts"], 2000)
            db.close()


class TestAllGuards(unittest.TestCase):
    """裁剪闸门：宁可少删，不可错删。"""

    def _run_all(self, pull_sessions, qa_pairs, pending, rotate_flag=True):
        from unittest import mock as um
        import src.app as A

        calls = {"rotate": 0}

        def fake_rotate(cfg=None, **kw):
            calls["rotate"] += 1
            return {"raw_message": 0, "raw_session": 0, "orphan_session": 0,
                    "window_days": 60, "grace_days": 7, "cutoff_ms": 0,
                    "size_mb_before": 0.0, "size_mb_after": 0.0}

        fake_db = um.MagicMock()
        fake_db.sessions_without_pairs.return_value = [{}] * pending

        with um.patch.object(A, "stage_pull", return_value={"sessions": pull_sessions}),              um.patch.object(A, "stage_pull_inspect", return_value={"messages": 0}),              um.patch.object(A, "stage_extract",
                             return_value={"sessions_scanned": 1, "qa_pairs": qa_pairs,
                                           "clusters": 1, "largest_cluster": 1}),              um.patch.object(A, "stage_report", return_value={"top_n": 1, "groups": []}),              um.patch.object(A, "stage_clean", return_value={"classified": 0}),              um.patch.object(A, "stage_push", return_value={"bot": False}),              um.patch.object(A, "stage_rotate", side_effect=fake_rotate),              um.patch.object(A, "get_db", return_value=fake_db):
            out = A.stage_all(rotate=rotate_flag)
        return out, calls["rotate"]

    def test_runs_when_healthy(self):
        out, n = self._run_all(pull_sessions=5, qa_pairs=10, pending=0)
        self.assertEqual(n, 1)
        self.assertNotIn("skipped", out["rotate"])

    def test_skip_when_no_sessions(self):
        out, n = self._run_all(pull_sessions=0, qa_pairs=10, pending=0)
        self.assertEqual(n, 0)
        self.assertEqual(out["rotate"], {"skipped": "no_new_sessions"})

    def test_skip_when_extract_empty_but_pending(self):
        out, n = self._run_all(pull_sessions=5, qa_pairs=0, pending=3)
        self.assertEqual(n, 0)
        self.assertEqual(out["rotate"], {"skipped": "extract_produced_nothing"})

    def test_manual_skip(self):
        out, n = self._run_all(pull_sessions=5, qa_pairs=10, pending=0, rotate_flag=False)
        self.assertEqual(n, 0)
        self.assertEqual(out["rotate"], {"skipped": "manual"})


class TestXlsx(unittest.TestCase):
    def test_to_dicts(self):
        rows = [["会话ID", "会话内容"], ["900001", "客户: 你好"]]
        out = to_dicts(rows)
        self.assertEqual(out[0]["会话ID"], "900001")


class TestMockPipeline(unittest.TestCase):
    def test_end_to_end_mock(self):
        """跑完整链路：假数据 → 清洗（LLM dry run）→ 落库。"""
        from dataclasses import replace

        from src.app import reset_db, stage_all
        from src.config import load_settings

        cfg = load_settings()
        with tempfile.TemporaryDirectory() as d:
            cfg = replace(
                cfg,
                runtime=replace(cfg.runtime, mock=True, db_path=str(Path(d) / "m.db")),
                llm=replace(cfg.llm, dry_run=True),
                feishu=replace(cfg.feishu, dry_run=True),
            )
            reset_db()
            out = stage_all(cfg, limit=50)
            self.assertGreater(out["pull"]["sessions"], 0)
            self.assertGreater(out["clean"]["classified"], 0)
            self.assertIn("bitable", out["push"])
            # 零 API 主链路：push 推的是 qa_pair，条数应等于问答对数
            from src.app import get_db
            self.assertEqual(out["push"]["bitable"]["created"], get_db(cfg).stats()["qa_pair"])
            self.assertGreater(get_db(cfg).stats()["qa_pair"], 0)
            reset_db()


class TestPushPairs(unittest.TestCase):
    """零 API 主链路：不跑 LLM 也能把问答明细推到飞书。"""

    def _cfg(self, d):
        from dataclasses import replace
        from src.config import load_settings
        cfg = load_settings()
        return replace(
            cfg,
            runtime=replace(cfg.runtime, mock=True, db_path=str(Path(d) / "p.db")),
            feishu=replace(cfg.feishu, dry_run=True),
        )

    def test_pairs_source_pushes_qa_pair(self):
        from src.app import reset_db, stage_extract, stage_pull, stage_push
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d)
            reset_db()
            stage_pull(cfg, days=30)
            r = stage_extract(cfg)
            self.assertGreater(r["qa_pairs"], 0)
            out = stage_push(cfg, to="all", source="pairs")
            self.assertEqual(out["bitable"]["created"], r["qa_pairs"])
            self.assertTrue(out["bot"])
            reset_db()

    def test_clean_source_empty_without_llm(self):
        """没跑 clean 时 clean 源应返回空，而不是假装推了东西。"""
        from src.app import reset_db, stage_extract, stage_pull, stage_push
        with tempfile.TemporaryDirectory() as d:
            cfg = self._cfg(d)
            reset_db()
            stage_pull(cfg, days=30)
            stage_extract(cfg)
            self.assertEqual(stage_push(cfg, to="bitable", source="clean"), {})
            reset_db()

    def test_pair_field_spec_matches_mapper(self):
        from src.feishu.bitable import PAIR_FIELD_SPEC, pair_to_fields
        names = [n for n, _ in PAIR_FIELD_SPEC]
        self.assertIn("问答ID", names)
        self.assertIn("问题原话", names)
        got = pair_to_fields({"qa_id": "q1", "session_id": "s1",
                              "question": "多少钱", "asked_at": 1700000000000})
        self.assertEqual(got["问答ID"], "q1")
        self.assertEqual(got["提问时间"], 1700000000000)
        self.assertNotIn("问题簇", got)  # cluster_id 为空时不写该字段


class TestFeishuCheck(unittest.TestCase):
    """飞书自检：只验证「配置缺失时的判断」，不发任何网络请求。"""

    def test_empty_config(self):
        from src.config import FeishuConfig
        from src.feishu import check_feishu

        out = check_feishu(FeishuConfig(), send=False)
        self.assertFalse(out["群机器人"]["ok"])
        self.assertFalse(out["应用身份"]["ok"])
        # token 没过时，后两项必须跳过而不是各报一次同样的错
        self.assertIn("跳过", out["多维表格"]["detail"])
        self.assertIn("跳过", out["文档"]["detail"])

    def test_webhook_configured_no_send(self):
        from src.config import FeishuConfig
        from src.feishu import check_feishu

        cfg = FeishuConfig(webhook="https://open.feishu.cn/open-apis/bot/v2/hook/xxx")
        out = check_feishu(cfg, send=False)
        self.assertTrue(out["群机器人"]["ok"])
        self.assertIn("未实际发送", out["群机器人"]["detail"])

    def test_hint_translation(self):
        from src.feishu.check import hint_for

        self.assertIn("协作者", hint_for(1254302))
        self.assertIn("重新发版", hint_for(99991672))
        self.assertIn("未收录", hint_for(12345678))


class TestAgent(unittest.TestCase):
    """Aily 智能体：只验证「没配置时的行为」和「prompt 组装」，不发请求。"""

    def test_missing_agent_id(self):
        from src.config import FeishuConfig
        from src.feishu.agent import AgentClient

        client = AgentClient(FeishuConfig(app_id="a", app_secret="b"))
        with self.assertRaises(RuntimeError) as ctx:
            client.ask("hi")
        self.assertIn("FEISHU_AGENT_ID", str(ctx.exception))

    def test_prompt_has_structure(self):
        import src.app as A
        from dataclasses import replace
        from src.config import load_settings

        with tempfile.TemporaryDirectory() as d:
            cfg = load_settings()
            cfg = replace(cfg, runtime=replace(cfg.runtime, mock=True, db_path=str(Path(d) / "prompt.db")))
            A.reset_db()
            try:
                A.stage_pull(cfg)
                A.stage_extract(cfg)
                prompt = A._agent_prompt(cfg)
                self.assertIn("全局高频问题", prompt)
                self.assertIn("请输出", prompt)
            finally:
                A.reset_db()


class TestBulkMessageRoles(unittest.TestCase):
    def test_manual_pull_distinguishes_visitor_bot_and_real_staff_in_json_and_tsv(self):
        from dataclasses import replace
        from unittest.mock import patch

        from src import app
        from src.config import load_settings

        sid = "synthetic-bulk"
        visitor = "请问这个订单怎么退？"
        answer = "请在订单页面提交售后申请。"
        messages = [
            {"from": 1, "status": 1, "mType": 1, "staffId": 9, "time": 1000, "msg": visitor},
            {"from": "0", "status": "1", "mType": "110", "staffId": "9", "time": "2000", "msg": "机器人回复"},
            {"from": "0", "status": "1", "mType": "1", "staffId": "9", "time": "3000", "autoReply": "1", "msg": "自动回复"},
            {"from": "0", "status": "1", "mType": "1", "staffId": "9", "time": "4000", "autoReply": "0", "msg": answer},
            {"from": 0, "status": 2, "mType": 1, "staffId": 9, "time": 5000, "msg": "撤回的客服消息"},
            {"from": "unknown", "status": "1", "mType": "113", "staffId": "9", "time": "6000", "msg": "系统通知"},
        ]
        for message in messages:
            message["sessionId"] = sid
        with tempfile.TemporaryDirectory() as directory:
            cfg = load_settings()
            cfg = replace(cfg, runtime=replace(cfg.runtime, mock=False, db_path=str(Path(directory) / "bulk.db")),
                          qiyu=replace(cfg.qiyu, app_key="synthetic", app_secret="synthetic"))
            app.reset_db()
            try:
                with patch("src.app.SessionExporter") as exporter:
                    exporter.return_value.export_bundle.return_value = ([{"id": sid, "startTime": 1000}], messages)
                    self.assertEqual(app.stage_pull(cfg, days=1), {"sessions": 1, "messages": 6})
                db = app.get_db(cfg)
                self.assertEqual([row["role"] for row in db.get_messages(sid)],
                                 ["user", "bot", "bot", "staff", "boundary", "boundary"])
                app.stage_extract(cfg)
                pairs = db.iter_qa_pairs([sid])
                self.assertEqual([(row["question"], row["answer"]) for row in pairs], [(visitor, answer)])
            finally:
                app.reset_db()


if __name__ == "__main__":
    unittest.main(verbosity=2)
