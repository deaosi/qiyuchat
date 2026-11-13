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
from src.push_sync import SCHEMA, VERSION
from src.web import case_senders, guest_evidence as guest
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
        self.assertEqual(guest.resolve_payload(self.cfg, token)["cases"], [])
        with patch.object(guest.time, "time", return_value=time.time() + 95 * 86400):
            self.assertIsNone(guest.resolve(self.cfg, token))

    def test_legacy_guest_messages_use_exact_source_senders_without_reissuing_the_link(self):
        sid = "123456"
        messages = [
            {"context_seq": 0, "role": "user", "ts": self.stamp, "content": "请帮忙确认设置"},
            {"context_seq": 1, "role": "user", "ts": self.stamp + 1000, "content": "我来检查"},
            {"context_seq": 2, "role": "staff", "ts": self.stamp + 2000, "content": "设置已核对"},
        ]
        with app.get_db(self.cfg).tx() as db:
            db.execute("INSERT INTO raw_session(session_id,user_id) VALUES(?,?)", (sid, "group-fixture"))
            qa = db.execute("INSERT INTO qa_pair(session_id,idx,customer_id,asked_at,question,answer) VALUES(?,?,?,?,?,?)",
                            (sid, 1, "group-fixture", self.stamp, "请帮忙确认设置\n我来检查", "设置已核对")).lastrowid
            db.executemany("INSERT INTO raw_message(session_id,seq,role,content,ts) VALUES(?,?,?,?,?)",
                           [(sid, m["context_seq"], m["role"], m["content"], m["ts"]) for m in messages])
        token = self._token(guest.issue_url(self.cfg, qa, entry=self.entry))
        before, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertEqual(len(before["messages"]), 2)
        self.assertEqual(before["messages"][0]["role"], "combined_transcript")

        originals = [
            {"id": seq + 1, "sessionId": int(sid), "time": message["ts"], "status": 1, "mType": 1,
             "msg": message["content"], "staffId": 8, "from": int(message["role"] == "user"), **sender}
            for seq, (message, sender) in enumerate(zip(messages, [
                {"externalUserId": "wmExampleCustomer001", "userName": "店主昵称"},
                {"externalUserId": "StaffLogin", "userName": "未接入坐席的群友昵称"},
                {"staffName": "七鱼坐席昵称", "staffAccount": "QiyuLogin"},
            ]))
        ]
        metadata = {"id": int(sid), "fromType": "wx_cs", "fromSubType": "福客AI-企业群"}
        path = case_senders.legacy_source_file(self.cfg.runtime.state_dir, sid)
        path.parent.mkdir()
        records = case_senders.legacy_source_records(metadata, originals, messages)
        path.write_text(json.dumps(records), encoding="utf-8")

        rendered, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertEqual([(row["role"], row.get("sender_name")) for row in rendered["messages"]],
                         [("user", "店主昵称"), ("group_participant", "未接入坐席的群友昵称"), ("staff", "七鱼坐席昵称")])
        self.assertEqual([row.get("sender_label") for row in rendered["messages"]], ["群成员 1", "群成员 2", None])
        self.assertTrue(rendered["messages"][0]["anchor"])
        public_page = guest.resolve(self.cfg, token).decode()
        self.assertIn("未接入坐席的群友昵称", public_page)
        for value in ("QiyuLogin", "StaffLogin", "sender_key"):
            self.assertNotIn(value, public_page)
        records["1"].pop("participant_name")
        path.write_text(json.dumps(records), encoding="utf-8")
        with_alias = guest.resolve(self.cfg, token).decode()
        self.assertIn("<strong>群成员 2</strong>", with_alias)
        self.assertNotIn("未接入坐席的群友昵称", with_alias)

    def test_controlled_context_does_not_cross_visitor_or_case_boundary(self):
        qa = self._seed_case(controlled=True)
        token = self._token(guest.issue_url(self.cfg, qa, "own-answer", entry=self.entry))
        page = guest.resolve(self.cfg, token).decode()
        self.assertIn("本次咨询的上一条", page)
        self.assertIn("这一条如何处理", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("138****5678", page)
        self.assertNotIn("<script>", page)
        case, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertEqual(case["customer_name"], "朱测试店主")
        self.assertNotIn("朱测试店主", str(case["messages"]))
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
        self.assertEqual(set(case), {"title", "category", "time", "sections", "messages", "scope", "customer_name"})
        self.assertEqual(case["customer_name"], "朱测试店主")
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

    def test_saved_nicknames_follow_existing_link_scope_and_keep_body_redaction(self):
        self.source = "6789031"
        qa = self._seed_case(controlled=True)
        ids = {i: f"source:{self.source}:{i + 1}" for i in (1, 3, 4, 5)}
        with app.get_db(self.cfg).tx() as db:
            db.executemany("UPDATE raw_message SET source_message_id=? WHERE session_id=? AND seq=?",
                           [(mid, self.source, i) for i, mid in ids.items()])
            db.execute("UPDATE qa_origin SET question_message_ids=?,answer_message_ids=? WHERE qa_id=?",
                       (json.dumps([ids[4]]), json.dumps([ids[5]]), qa))
            originals = [dict(row) for row in db.execute("SELECT * FROM raw_message WHERE source_message_id IN (?,?,?,?) ORDER BY seq",
                                                         tuple(ids.values()))]
        token = self._token(guest.issue_url(self.cfg, qa, entry=self.entry))
        original_case, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertTrue(all("sender_name" not in message for message in original_case["messages"]))

        source_names = {
            1: {"userName": "范围外的客户昵称", "externalUserId": "wmAnotherExternalContact"},
            3: {"userName": "群内来源昵称", "externalUserId": "unverified-login"},
            4: {"userName": "客户昵称 <小店> 13812345678", "externalUserId": "wmVerifiedExternalContact"},
            5: {"staffName": "七鱼昵称 <值班> https://internal.example.com/profile", "staffAccount": "staff-login"},
        }
        source_rows = [{"id": row["seq"] + 1, "sessionId": int(self.source), "status": 1, "mType": 1,
                        "time": row["ts"], "msg": row["content"], "from": 1 if row["role"] == "user" else 0,
                        "staffId": 12, **source_names[row["seq"]]} for row in originals]
        snapshot = {"projection_version": VERSION,
                    "session": {"id": int(self.source), "fromType": "wx_cs", "fromSubType": "福客AI-服务群"},
                    "messages": source_rows}
        (Path(self.temp.name) / "fuke-staff-directory.json").write_text('{"unverified-login":"内部通讯录姓名"}', encoding="utf-8")
        app.get_db(self.cfg)._conn.executescript(SCHEMA)
        with app.get_db(self.cfg).tx() as db:
            db.execute("INSERT INTO push_sync_state(session_id,event_key,version,status,source_version,source_json,updated_at) "
                       "VALUES(?,?,'v1','completed','v1',?,?)", (self.source, "fixture-event", json.dumps(snapshot), time.time()))

        case, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertEqual([message.get("sender_name") for message in case["messages"]],
                         ["群内来源昵称", "客户昵称 <小店> 138****5678", "七鱼昵称 <值班> [链接已隐藏]"])
        self.assertEqual([message.get("sender_label") for message in case["messages"]], ["群成员 1", "群成员 2", None])
        self.assertEqual([{k: v for k, v in message.items() if k not in ("sender_name", "sender_label")} for message in case["messages"]],
                         original_case["messages"])
        page = guest.resolve(self.cfg, token).decode()
        self.assertIn("<strong>客户昵称 &lt;小店&gt; 138****5678</strong>", page)
        self.assertIn("<strong>七鱼昵称 &lt;值班&gt; [链接已隐藏]</strong>", page)
        self.assertIn("<strong>群内来源昵称</strong>", page)
        for hidden in ("范围外的客户昵称", "unverified-login", "内部通讯录姓名", "staff-login",
                       "internal.example.com", "唐测试客服", "13812345678"):
            self.assertNotIn(hidden, page)

        source_rows[-1]["msg"] = "消息原文不匹配"
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE push_sync_state SET source_json=? WHERE session_id=?", (json.dumps(snapshot), self.source))
        case, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertNotIn("sender_name", case["messages"][-1])
        variant = {"topic_id": "t_" + "1" * 24,
                   "topic": {"question": "原问题", "theme": "来源昵称", "consultation_count": 6},
                   "comparison": {"current": {"start": self.entry["start"], "end": self.entry["end"],
                                               "source_pairs": 9, "processed_pairs": 8}}}
        grouped = self._token(guest.issue_url(self.cfg, qa, entry=self.entry, view={
            "topic_variants": [variant], "cases": [{"qa_id": qa, "variant": 0}],
        }))
        public = guest.resolve_payload(self.cfg, grouped)
        self.assertEqual(public["topic_variants"][0]["case_indices"], [0])
        self.assertEqual(public["topic_variants"][0]["topic_detail"]["events"][0]["customer_name"], "朱测试店主")
        self.assertEqual(public["cases"][0]["messages"][0]["sender_name"], "群内来源昵称")
        grouped_page = guest.resolve(self.cfg, grouped).decode()
        self.assertIn("<strong>群内来源昵称</strong>", grouped_page)
        for hidden in ("范围外的客户昵称", "内部通讯录姓名", "unverified-login", "staff-login", "internal.example.com"):
            self.assertNotIn(hidden, grouped_page)

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

    def test_grouped_variants_use_pinned_cases_and_local_ids(self):
        qa = self._seed_case(controlled=True)
        reply = "请检查授权并重新同步。"
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE raw_message SET content=? WHERE session_id=? AND source_message_id='own-answer'",
                       (reply, self.source))
            db.execute("UPDATE qa_pair SET answer=? WHERE qa_id=?", (reply, qa))

        def variant(index, question, count, end=None):
            return {
                "topic_id": f"t_{index:024x}",
                "topic": {"question": question, "theme": "授权", "consultation_count": count},
                "comparison": {"current": {"start": self.entry["start"], "end": end or self.entry["end"],
                                            "source_pairs": 9, "processed_pairs": 8}},
            }

        primary = {"qa_id": qa, "anchor_id": "own-question", "title": "这一条如何处理？", "variant": 0,
                   "topic_evidence": {"staff_source_verified": True, "source_anchored": True, "source_reply": reply},
                   "sections": [{"title": "上下文", "lines": ["内部地址 /login?next=/api/voc2/topics"]}]}
        second = {**primary, "variant": 2, "topic_evidence": {"staff_source_verified": True,
                                                           "source_anchored": True, "source_reply": "未出现的回复"}}
        view = {
            "topic_workspace": variant(1, "授权", 6),
            "topic_variants": [
                variant(1, "如何恢复授权？", 3),
                variant(2, "其他统计范围", 4, end="2026-11-13"),
                variant(3, "如何同步授权？", 2),
                variant(4, "如何检查授权？", 1),
            ],
            "cases": [primary, primary, {**primary, "variant": 1}, second, {**primary, "variant": False}],
        }
        token = self._token(guest.issue_url(self.cfg, qa, "own-question", entry=self.entry, view=view))
        with (
            patch.object(case_senders, "saved_source_nicknames", return_value={4: "已核验来源昵称"}),
            patch.object(case_senders, "saved_guest_groups", return_value={4: "fixture-opaque-sender-key"}),
        ):
            public = guest.resolve_payload(self.cfg, token)
        self.assertEqual(public["topic_detail"]["topic"]["consultation_count"], 6)
        self.assertEqual(len(public["cases"]), 2)
        first, second, empty = public["topic_variants"]
        self.assertEqual(first["case_indices"], [0])
        self.assertEqual(second["case_indices"], [1])
        self.assertEqual(empty["case_indices"], [])
        self.assertEqual(first["topic_detail"]["topic"]["consultation_count"], 3)
        self.assertEqual(second["topic_detail"]["topic"]["consultation_count"], 2)
        self.assertEqual(first["topic_detail"]["topic"]["id"], "guest-topic-0")
        self.assertEqual(second["topic_detail"]["topic"]["id"], "guest-topic-2")
        event, = first["topic_detail"]["events"]
        self.assertEqual(event["id"], "guest-case-0")
        self.assertEqual(event["answer"], reply)
        self.assertEqual(event["source_kind"], "source-anchored")
        self.assertEqual(second["topic_detail"]["events"][0]["answer"], "")
        self.assertEqual(second["topic_detail"]["events"][0]["qa_id"], 2)
        anchor = next(message for message in public["cases"][0]["messages"] if message.get("anchor"))
        self.assertEqual(anchor["sender_label"], "群成员 1")
        self.assertEqual(anchor["sender_name"], "已核验来源昵称")
        serialized = json.dumps(public, ensure_ascii=False)
        for private in ("own-question", "其他统计范围", "未出现的回复", "/login", "/api/voc2/topics",
                        "fixture-opaque-sender-key",
                        f"t_{1:024x}", f"t_{3:024x}"):
            self.assertNotIn(private, serialized)

        with closing(guest.sqlite3.connect(guest._registry(self.cfg))) as db, db:
            stored = json.loads(db.execute("SELECT payload FROM guest_evidence WHERE token=?", (token,)).fetchone()[0])
            stored["cases"][0]["fingerprint"] = "0" * 64
            db.execute("UPDATE guest_evidence SET payload=? WHERE token=?", (json.dumps(stored), token))
        changed = guest.resolve_payload(self.cfg, token)["topic_variants"]
        self.assertEqual(changed[0]["case_indices"], [])
        self.assertEqual(changed[0]["topic_detail"]["topic"]["consultation_count"], 3)
        self.assertEqual(changed[1]["case_indices"], [0])
        self.assertEqual(changed[1]["topic_detail"]["events"][0]["id"], "guest-case-0")
        self.assertEqual(changed[1]["topic_detail"]["events"][0]["qa_id"], 1)

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

    def test_case_customer_uses_recorded_display_name_only_and_keeps_sop_evidence_bound(self):
        qa = self._seed_case(controlled=False)
        display = "示例店铺群 13812345678"
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE qa_pair SET customer_name=?,group_name=? WHERE qa_id=?", (display, display, qa))
        token = self._token(guest.issue_url(self.cfg, qa, entry={**self.entry, "kind": "sops"}))
        payload = guest.resolve_payload(self.cfg, token)
        case, = payload["cases"]
        self.assertEqual(case["customer_name"], "示例店铺群 138****5678")
        self.assertEqual(case["scope"], "pair")
        self.assertNotIn("示例店铺群", str(case["messages"]))
        self.assertNotIn("13812345678", json.dumps(payload))
        self.assertNotIn(self.customer, json.dumps(payload))
        for name in ("匿名商家", "身份未知访客", "来源账号 1", self.customer, self.source, "source:123:456",
                     "wxid_a_test", "777123456789", "https://internal.example.com"):
            with self.subTest(name=name), app.get_db(self.cfg).tx() as db:
                db.execute("UPDATE qa_pair SET customer_name=? WHERE qa_id=?", (name, qa))
            self.assertNotIn("customer_name", guest.resolve_payload(self.cfg, token)["cases"][0])
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE qa_pair SET customer_name=?,customer_id=? WHERE qa_id=?", (display, self.visitor, qa))
        self.assertEqual(guest.resolve_payload(self.cfg, token)["cases"], [])

    def test_images_pin_exact_visible_human_attachments_on_existing_links(self):
        qa = self._seed_case(controlled=True, extended=True)
        user_url = "https://ysf.nosdn.127.net/guest-customer.png?token=private-upstream-token"
        staff_url = "https://ysf.nosdn.127.net/guest-staff.jpg"
        outside_url = "https://ysf.nosdn.127.net/guest-outside.png"
        bot_url = "https://ysf.nosdn.127.net/guest-bot.png"

        def attachment(url):
            return json.dumps({"type": "image", "url": url})

        with app.get_db(self.cfg).tx() as db:
            db.executemany("UPDATE raw_message SET content=? WHERE source_message_id=?", [
                (attachment(user_url), "preceding"), (attachment(staff_url), "later-1"),
                (attachment(outside_url), "outside-question"), (attachment(bot_url), "bot-reminder"),
            ])
        token = self._token(guest.issue_url(self.cfg, qa, "own-question", entry=self.entry))
        with closing(guest.sqlite3.connect(guest._registry(self.cfg))) as db, db:
            payload, expiry, dedupe = db.execute(
                "SELECT payload,expires,dedupe FROM guest_evidence WHERE token=?", (token,)).fetchone()
            old = json.loads(payload)
            old["cases"][0].pop("media")
            db.execute("UPDATE guest_evidence SET payload=? WHERE token=?", (json.dumps(old), token))
            db.commit()
            before = guest.resolve_payload(self.cfg, token)
            self.assertFalse(any(message.get("images") for message in before["cases"][0]["messages"]))
            upgraded = guest.build_guest_media_upgrade(self.cfg, old)
            original = old["cases"][0]
            pinned = upgraded["cases"][0]
            self.assertEqual({key: value for key, value in pinned.items() if key != "media"}, original)
            self.assertEqual(len(pinned["media"]), 2)
            db.execute("UPDATE guest_evidence SET payload=? WHERE token=?", (json.dumps(upgraded), token))
            self.assertEqual(db.execute("SELECT expires,dedupe FROM guest_evidence WHERE token=?", (token,)).fetchone(),
                             (expiry, dedupe))
        visible = guest.resolve_payload(self.cfg, token)
        images = [(message["role"], image) for message in visible["cases"][0]["messages"]
                  for image in message.get("images", [])]
        self.assertEqual([role for role, _ in images], ["user", "staff"])
        self.assertEqual([guest.resolve_image(self.cfg, token, image) for _, image in images], [user_url, staff_url])
        for url in (user_url, staff_url, outside_url, bot_url):
            self.assertNotIn(url, json.dumps(visible))
        image_id = images[0][1]
        other = self._token(guest.issue_url(self.cfg, qa, "own-question",
                                            entry={**self.entry, "title": "另一个访客条目"}))
        self.assertNotEqual(token, other)
        self.assertIsNone(guest.resolve_image(self.cfg, other, image_id))
        self.assertIsNone(guest.resolve_image(self.cfg, token, "1-" + image_id.split("-", 1)[1]))
        with patch.object(guest.time, "time", return_value=expiry):
            self.assertIsNone(guest.resolve_image(self.cfg, token, image_id))
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE raw_message SET content=? WHERE source_message_id='preceding'",
                       (attachment("https://ysf.nosdn.127.net/replacement.png"),))
        changed = guest.resolve_payload(self.cfg, token)
        self.assertEqual(len(changed["cases"]), 1, "an attachment change alone preserves the legacy text fingerprint")
        self.assertIsNone(guest.resolve_image(self.cfg, token, image_id))
        self.assertEqual([image for message in changed["cases"][0]["messages"] for image in message.get("images", [])],
                         [images[1][1]])

    def test_pair_fallback_keeps_image_placeholders_without_proxy_authority(self):
        qa = self._seed_case(controlled=False)
        url = "https://ysf.nosdn.127.net/unscoped.png"
        with app.get_db(self.cfg).tx() as db:
            db.execute("UPDATE qa_pair SET question=? WHERE qa_id=?", (json.dumps({"type": "image", "url": url}), qa))
        token = self._token(guest.issue_url(self.cfg, qa, entry=self.entry))
        case, = guest.resolve_payload(self.cfg, token)["cases"]
        self.assertEqual(case["scope"], "pair")
        self.assertEqual(case["messages"][0]["text"], "[图片]")
        self.assertFalse(any(message.get("images") for message in case["messages"]))
