"""Focused tests for persisted, source-bound guest evidence capabilities."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import re
import time
from unittest.mock import patch

from src import app
from src.extract.identity import encode_identity
from src.web import guest_evidence as guest
from tests.test_local import LocalFixture


class TestGuestEvidence(LocalFixture):
    entry = {"kind": "topics", "title": "已同步的咨询 <标签>", "summary": "6 次咨询",
             "detail": "这是完整的已同步内容。\n第二段保留。\nhttps://internal.example.com/private",
             "start": "2026-11-06", "end": "2026-11-12"}
    customer = "qiyu-external:client-fixture"
    visitor = "qiyu-external:another-client"
    source = "hidden-session-fixture"
    stamp = 1794448800000

    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, state_dir=self.temp.name))

    def _seed_case(self, *, controlled, extended=False):
        qa_text = "这一条如何处理？"
        answer_text = "按设置说明处理。"
        rows = [
            ("user", "其他客户的原文。", "outside-question"),
            ("user", qa_text, "own-question"),
            ("staff", answer_text, "own-answer"),
        ]
        if controlled:
            rows = [
                ("source_identity", encode_identity(self.source, self.visitor, "另一位访客"), None),
                rows[0],
                ("source_identity", encode_identity(self.source, self.customer, "朱测试店主"), None),
                ("user", "本次咨询的上一条", "preceding"),
                rows[1],
                ("staff", "朱测试店主请看 <script>alert(1)</script>，私有链接 https://secrets.example.com/a，电话 13812345678。", "own-answer"),
                ("source_identity", encode_identity(self.source, self.customer, "朱测试店主"), None),
                ("user", "同一客户下一次无关咨询", "another-topic"),
                ("source_identity", encode_identity(self.source, self.visitor, "另一位访客"), None),
                ("staff", "另一位访客的私人回复", "outside-answer"),
            ]
        if controlled and extended:
            rows[6:6] = [("staff", f"完整会话第 {i} 条后续回复", f"later-{i}") for i in range(1, 14)]
            rows[6:6] = [("bot", "机器人发送了操作提醒。", "bot-reminder")]
        with app.get_db(self.cfg).tx() as db:
            db.execute("INSERT INTO raw_session(session_id,staff_name,user_id,user_name,from_group) VALUES(?,?,?,?,?)",
                       (self.source, "唐测试客服", self.customer, "朱测试店主", "内部测试群"))
            qa = db.execute("INSERT INTO qa_pair(session_id,idx,customer_id,customer_name,group_name,staff_name,"
                            "asked_at,question,answer,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (self.source, 1, self.customer, "朱测试店主", "内部测试群", "唐测试客服",
                             self.stamp, qa_text, answer_text, self.stamp)).lastrowid
            db.executemany("INSERT INTO raw_message(session_id,seq,role,content,ts,source_message_id) VALUES(?,?,?,?,?,?)",
                           [(self.source, i, role, text, self.stamp + i * 1000, msg_id)
                            for i, (role, text, msg_id) in enumerate(rows)])
            if controlled:
                db.execute("INSERT INTO qa_origin VALUES(?,?,?,?,?,?,1,1)",
                           (qa, "fixture-evidence-key", self.source, json.dumps(["own-question"]),
                            json.dumps(["own-answer"]), "fixture-fingerprint"))
        return qa

    def _token(self, url):
        self.assertTrue(url.startswith("https://qiyu.fanwulink.cn/guest/evidence/"))
        token = url.rsplit("/", 1)[-1]
        self.assertTrue(re.fullmatch(guest.TOKEN_PATTERN, token))
        return token

    def test_content_only_stays_opaque_stable_and_expires(self):
        registry = Path(self.temp.name) / "voc-guest-evidence.db"
        self.assertIsNone(guest.resolve(self.cfg, "A" * 43))
        self.assertFalse(registry.exists(), "a guest GET must never create a registry")
        self.assertEqual(guest.issue_url(self.cfg, 99999, entry=self.entry), "")
        token = self._token(guest.issue_url(self.cfg, None, entry=self.entry))
        self.assertEqual(token, self._token(guest.issue_url(self.cfg, None, entry=self.entry)))
        altered = ("B" if token[0] == "A" else "A") + token[1:]
        self.assertIsNone(guest.resolve(self.cfg, altered))
        page = guest.resolve(self.cfg, token).decode()
        self.assertIn("完整的已同步内容", page)
        self.assertIn("第二段保留", page)
        self.assertIn("&lt;标签&gt;", page)
        self.assertIn("暂无可公开查看的会话上下文", page)
        self.assertNotIn("internal.example.com", page)
        self.assertNotIn("href=", page)
        self.assertNotIn("<script", page)
        with patch.object(guest.time, "time", return_value=time.time() + 95 * 86400):
            self.assertIsNone(guest.resolve(self.cfg, token))

    def test_controlled_context_does_not_cross_visitor_or_case_boundary(self):
        qa = self._seed_case(controlled=True)
        token = self._token(guest.issue_url(self.cfg, qa, "own-answer", entry=self.entry))
        page = guest.resolve(self.cfg, token).decode()
        self.assertIn("本次咨询的上一条", page)
        self.assertIn("这一条如何处理", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("138****5678", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("朱测试店主", page)
        self.assertNotIn("另一位访客", page)
        self.assertNotIn("同一客户下一次无关咨询", page)
        self.assertNotIn("hidden-session-fixture", page)
        self.assertNotIn("own-answer", page)
        self.assertNotIn("secrets.example.com", page)
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE qa_pair SET question=?,customer_id=? WHERE qa_id=?",
                       ("另一个案例不能覆盖旧链接", self.visitor, qa))
        changed = guest.resolve(self.cfg, token).decode()
        self.assertIn("完整的已同步内容", changed)
        self.assertIn("暂无可公开查看的会话上下文", changed)
        self.assertNotIn("另一个案例不能覆盖旧链接", changed)
        self.assertNotIn("本次咨询的上一条", changed)

    def test_existing_guest_token_upgrades_to_rich_scoped_detail(self):
        qa = self._seed_case(controlled=True, extended=True)
        token = self._token(guest.issue_url(self.cfg, qa, entry=self.entry))
        source = guest._case(self.cfg, qa, "")
        previous = {"v": 1, "entry": guest._entry(self.entry, None),
                    "case": {"qa": qa, "anchor": "", "fingerprint": source[0]}}
        with closing(guest.sqlite3.connect(guest._registry(self.cfg))) as db, db:
            db.execute("UPDATE guest_evidence SET payload=? WHERE token=?", (json.dumps(previous), token))
        self.assertNotIn("第 13 条后续回复", guest.resolve(self.cfg, token).decode())
        view = {
            "taxonomy": [{"primary": "配置与使用", "secondary": "图片回复", "count": 1},
                         {"primary": "配置与使用", "secondary": "场景命中", "count": 1}],
            "metrics": [{"label": "涉及商家", "value": 0}],
            "sections": [{"title": "统计口径", "lines": ["统计范围沿用原发布。"]}],
            "cases": [{"qa_id": qa, "anchor_id": "own-question", "title": "一个范围内的案例",
                       "category": "已核验", "sections": [{"title": "诊断依据", "lines": ["原句经核对。"]}]}],
            "total_cases": 1,
        }
        self.assertEqual(guest.upgrade_url(self.cfg, token, entry={**self.entry, "title": "另一个成果"}, view=view), "")
        self.assertEqual(self._token(guest.upgrade_url(self.cfg, token, entry=self.entry, view=view)), token)
        payload = guest.resolve_payload(self.cfg, token)
        self.assertEqual(set(payload), {"entry", "cases", "total_cases", "topic_detail"})
        self.assertEqual(payload["total_cases"], 1)
        case, = payload["cases"]
        self.assertEqual(set(case), {"title", "category", "time", "sections", "messages", "scope"})
        self.assertEqual(case["scope"], "context")
        question, = [message for message in case["messages"] if message.get("anchor")]
        self.assertEqual(question, {"role": "user", "time": "2026-11-12 10:00:04",
                                    "text": "这一条如何处理？", "anchor": True, "id": "guest-message-1"})
        event, = payload["topic_detail"]["events"]
        self.assertEqual(event["id"], "guest-case-0")
        self.assertEqual(event["workbench"]["question_message_id"], question["id"])
        projection = json.dumps(payload, ensure_ascii=False)
        for private in (token, self.customer, self.visitor, self.source, "own-question", "own-answer"):
            self.assertNotIn(private, projection)
        page = guest.render_payload(payload).decode()
        self.assertEqual(page, guest.resolve(self.cfg, token).decode())
        for text in ("一级分类", "二级分类", "图片回复", "场景命中", "诊断依据", "涉及商家",
                     "第 13 条后续回复", "机器人发送了操作提醒"):
            self.assertIn(text, page)
        self.assertNotIn("同一客户下一次无关咨询", page)
        self.assertNotIn("另一位访客的私人回复", page)
        self.assertNotIn("href=", page)
        self.assertNotIn(token, page)

    def test_workspace_upgrade_only_enriches_existing_bound_cases(self):
        qa = self._seed_case(controlled=True)
        url = guest.issue_url(self.cfg, qa, "own-question", entry=self.entry)
        token = self._token(url)
        registry = guest._registry(self.cfg)
        with closing(guest.sqlite3.connect(registry)) as db, db:
            saved = db.execute("SELECT payload FROM guest_evidence WHERE token=?", (token,)).fetchone()[0]
            old = json.loads(saved)
            period = {"start": self.entry["start"], "end": self.entry["end"], "source_pairs": 9, "processed_pairs": 8}
            previous = {"start": "2026-10-30", "end": "2026-11-05", "source_pairs": 9, "processed_pairs": 6}
            fact = {"source": "question", "role": "merchant", "quote": "这一条如何处理？", "alignment": "exact",
                    "source_refs": [{"message_id": "own-question"}]}
            topic_id = "t_" + "1" * 24
            proof = {"keys": [[qa, guest._pinned_anchor("own-question")]], "level": "history",
                     "kind": "configuration_guide", "question": "操作确认", "answer": "请看", "scope": fact["quote"],
                     "steps": ["请看"], "applicability": [fact]}
            view = {
                "topic_workspace": {
                    "topic_id": topic_id,
                    "topic": {"question": self.entry["title"], "theme": f"已发布主题 {topic_id}", "consultation_count": 6,
                              "trend": {"status": "rising", "previous_consultation_count": 3, "delta": 3}},
                    "comparison": {"current": period, "previous": previous, "comparable": True,
                                   "episode": {"consultations": 3, "sessions": 2}},
                    "knowledge": [proof, {**proof, "question": "无依据不得公开", "answer": "其他范围的回复"},
                                  {**proof, "question": "未授权案例不得公开", "keys": [[qa + 1, ""]]}],
                },
                "cases": [
                    {"qa_id": qa, "anchor_id": "own-question",
                     "topic_evidence": {"workbench": {"commerce_settings": [fact, {**fact, "alignment": "unverified",
                                                                                    "quote": "未经校验不得呈现"},
                                                                            {**fact, "quote": "其他案例不得呈现"}]}}},
                    {"qa_id": qa + 1, "anchor_id": "unpublished", "topic_evidence": {"claims": ["不可公开的条目"]}},
                ],
            }
            self.assertIsNone(guest.build_topic_workspace_upgrade(self.cfg, old, entry={**self.entry, "title": "其他主题"}, view=view))
            upgraded = guest.build_topic_workspace_upgrade(self.cfg, old, entry=self.entry, view=view)
            self.assertEqual(upgraded["entry"], old["entry"])
            only_case, = upgraded["cases"]
            self.assertEqual({key: value for key, value in only_case.items() if key != "topic_evidence"}, old["cases"][0])
            db.execute("UPDATE guest_evidence SET payload=? WHERE token=?", (json.dumps(upgraded), token))
        payload = guest.resolve_payload(self.cfg, token)
        detail = payload["topic_detail"]
        self.assertEqual(detail["topic"]["trend"]["delta"], 3)
        self.assertEqual(detail["workbench"]["comparison"]["previous"], previous)
        self.assertEqual(detail["workbench"]["comparison"]["episode"], {"consultations": 3, "sessions": 2})
        event, = detail["events"]
        ref, = event["workbench"]["commerce_settings"]
        self.assertEqual(ref["source_refs"][0]["message_id"], "guest-message-1")
        knowledge, = detail["workbench"]["knowledge"]
        self.assertEqual(knowledge["reuse_level"], "history")
        self.assertEqual(knowledge["evidence"][0]["id"], event["id"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for private in ("own-question", topic_id, "不可公开的条目", "未经校验不得呈现", "其他案例不得呈现",
                        "无依据不得公开", "未授权案例不得公开"):
            self.assertNotIn(private, serialized)
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE qa_pair SET answer=? WHERE qa_id=?", ("改变来源证据", qa))
        stale = guest.resolve_payload(self.cfg, token)
        self.assertEqual(stale["cases"], [])
        self.assertEqual(stale["topic_detail"]["events"], [])
        self.assertEqual(stale["topic_detail"]["workbench"]["knowledge"], [])

    def test_legacy_only_shows_exact_pair_and_archived_source_is_unavailable(self):
        qa = self._seed_case(controlled=False)
        token = self._token(guest.issue_url(self.cfg, qa, entry=self.entry))
        case, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertEqual(case["scope"], "pair")
        self.assertTrue(case["messages"][0]["anchor"])
        self.assertNotIn("anchor", case["messages"][1])
        page = guest.resolve(self.cfg, token).decode()
        self.assertIn("这一条如何处理", page)
        self.assertIn("按设置说明处理", page)
        self.assertNotIn("其他客户的原文", page)
        with app.get_db(self.cfg).tx() as db:
            db.execute("DELETE FROM raw_message WHERE session_id=?", (self.source,))
        self.assertEqual(guest.issue_url(self.cfg, qa, entry=self.entry), "")
        archived = guest.resolve(self.cfg, token).decode()
        self.assertIn("完整的已同步内容", archived)
        self.assertNotIn("这一条如何处理", archived)
