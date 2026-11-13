"""Feishu previews and records use current projections and omit raw evidence."""

from datetime import datetime, timedelta
from unittest.mock import call, patch
from xml.etree.ElementTree import fromstring

from src import app, voc, voc_feishu_content as export, voc_topic_insights
from tests.test_local import LocalFixture


def topic_id(number: int) -> str:
    return f"t_{number:024x}"


def sop_id(number: int) -> str:
    return f"sop_{number:024x}"


class TestFeishuContent(LocalFixture):
    scope = {"start": "2026-11-01", "end": "2026-11-07"}

    def test_topic_ranking_retains_coverage_and_caps_both_outputs(self):
        with app.get_db(self.cfg).tx() as conn:
            conn.execute("INSERT INTO raw_session(session_id,staff_name,user_name,from_group) VALUES(?,?,?,?)",
                         ("fixture", "现场客服阿兰", "小周店主", "示例内部群"))
        topics = [
            {"id": topic_id(index), "question": "小周店主和现场客服阿兰查看 <规则> & 配置",
             "consultation_count": 40 - index, "primary_intent": "configuration_guidance",
             "samples": ["无关会话原话"], "trend": {"status": "stable"}}
            for index in range(21)
        ]
        filters = {**self.scope, "group": "当前客户群限定", "staff": "当前客服限定", "days": 7}
        ranking = {"items": topics, "comparison": {"current": {"source_pairs": 50, "processed_pairs": 40}},
                   "guest_anchors": {topic_id(0): [(1001, "private-source-anchor")]},
                   "guest_rows": {topic["id"]: [] for topic in topics},
                   "guest_topics": {topic["id"]: topic for topic in topics}}
        with patch.object(export.voc_engine, "feishu_topics", return_value=ranking) as read, \
                patch.object(export.guest_evidence, "issue_url") as issue:
            result = export.build_document(self.cfg, filters, "topics")
        read.assert_called_once_with(self.cfg, {**filters, "days": 0}, grouped=True)
        issue.assert_not_called()
        self.assertEqual((result["count"], result["total"]), (20, 21))
        first = result["records"][0]
        self.assertEqual(first["key"], topic_id(0))
        self.assertEqual(first["summary"], "40 次咨询 · 配置与使用")
        self.assertIn("已整理 40 / 50 条问答", first["detail"])
        self.assertEqual(first["url"], "")
        self.assertEqual(first["_guest_anchors"], [(1001, "private-source-anchor")])
        root = fromstring(f"<doc>{result['content']}</doc>")
        self.assertEqual(root[0].tag, "title")
        self.assertEqual(root.findtext("h2"), "[名称]和[名称]查看 <规则> & 配置")
        self.assertIsNone(root.find(".//a"))
        self.assertNotIn("private-source-anchor", result["content"])
        self.assertEqual(result["items"][0], {"title": first["title"], "summary": first["summary"]})
        for private in ("现场客服阿兰", "小周店主", "示例内部群", "当前客户群限定", "当前客服限定", "无关会话原话"):
            self.assertNotIn(private, str(result))

    def test_grouped_topic_keeps_member_counts_and_allocates_cases_across_members(self):
        members = [
            {"id": topic_id(1), "question": "机器人没回复", "consultation_count": 3},
            {"id": topic_id(2), "question": "机器人答错了", "consultation_count": 1},
        ]
        group = {**members[0], "category_key": topic_id(30), "theme": "回复异常", "members": members}

        def row(topic, qa):
            return {"topic_id": topic["id"], "data": {},
                    "source": {"qa_id": qa, "customer_id": "customer", "session_id": str(1 if qa == 4 else qa)}}

        rows = {members[0]["id"]: [row(members[0], qa) for qa in (1, 2, 3)],
                members[1]["id"]: [row(members[1], 4)]}
        ranking = {"items": [group], "comparison": {"current": {"source_pairs": 4, "processed_pairs": 4}},
                   "guest_anchors": {members[0]["id"]: [(1, "original-anchor")]},
                   "guest_rows": rows, "guest_topics": {item["id"]: item for item in members}}
        with patch.object(export.voc_engine, "feishu_topics", return_value=ranking), \
                patch.object(export.voc_topic_insights, "gaps", return_value=[]), \
                patch.object(export, "_case_projection", side_effect=lambda row, _, **kwargs: row["source"]):
            document = export.build_document(self.cfg, self.scope, "topics")

        record = document["records"][0]
        self.assertEqual(record["key"], members[0]["id"])
        self.assertEqual(record["_guest_anchors"], [(1, "original-anchor")])
        self.assertEqual((record["title"], record["consultations"]), ("回复异常", 3))
        self.assertIn("机器人答错了", record["detail"])
        view = record["_guest_view"]
        self.assertEqual([(item["topic"]["question"], item["topic"]["consultation_count"])
                          for item in view["topic_variants"]], [("机器人没回复", 3), ("机器人答错了", 1)])
        self.assertEqual([(case["variant"], case["qa_id"]) for case in view["cases"]], [(0, 1), (1, 4), (0, 2), (0, 3)])

    def test_sops_publish_validated_steps_without_failed_attempts_or_chat(self):
        def sop(number, status, result=None):
            return {"id": sop_id(number), "topic_id": topic_id(1), "qa_id": 1234,
                    "title": "客户原话不要外发", "problem": {"quote": "客户的详细对话", "message_id": "sop-source-anchor"},
                    "kind": "anomaly", "status": status, "result": result,
                    "conditions": [{"quote": "当前客服限定操作管理员账号，参考 https://example.invalid/help"}],
                    "steps": [{"phase": "action", "outcome": "failed", "evidence": {"quote": "此前失败操作"}},
                              {"phase": "action", "outcome": "blocked", "evidence": {"quote": "被拦截的操作"}},
                              {"phase": "diagnosis", "outcome": "unconfirmed", "evidence": {"quote": "请核对场景开关"}},
                              {"phase": "retest", "outcome": "unconfirmed", "evidence": {"quote": "使用商品ID ABCD1234 复测"}}]}

        verified = sop(1, "verified", {"quote": "客户复测原话", "message_id": "raw-response-id"})
        guidance = sop(2, "guidance")
        incomplete = sop(3, "incomplete")
        bad_verified = sop(4, "verified")
        filters = {**self.scope, "staff": "当前客服限定"}
        topics = {topic_id(1): {"question": "退款场景未触发"}}
        with patch.object(export.voc_engine, "_topic_definitions", return_value=topics), \
                patch.object(export.voc_engine, "sops") as read:
            read.return_value = {"items": [verified, guidance, incomplete, bad_verified]}
            result = export.build_document(self.cfg, filters, "sops")
            read.assert_called_once_with(self.cfg, {**filters, "days": 0}, "")
            read.reset_mock()
            read.return_value = {"item": guidance}
            selected = export.build_document(self.cfg, filters, "sops", guidance["id"])
            read.assert_called_once_with(self.cfg, {**filters, "days": 0}, guidance["id"])
        self.assertEqual([row["key"] for row in result["records"]], [verified["id"], guidance["id"]])
        self.assertEqual(selected["count"], 1)
        self.assertEqual(selected["records"][0]["_guest_anchors"], [(1234, "sop-source-anchor")])
        self.assertEqual(selected["records"][0]["url"], "")
        view = result["records"][0]["_guest_view"]
        sections = {section["title"]: section["lines"] for section in view["sections"]}
        self.assertEqual(sections["问题现象"], ["客户的详细对话"])
        self.assertIn("尝试未奏效", sections["处理流程"][0])
        self.assertEqual(sections["复测结果"], ["客户复测原话"])
        self.assertEqual(view["cases"][0]["anchor_id"], "sop-source-anchor")
        self.assertIn("客户反馈按该流程处理后复测成功", result["records"][0]["detail"])
        self.assertIn("效果待验证", result["records"][1]["summary"])
        self.assertIn("商品ID [已脱敏]", result["records"][0]["detail"])
        self.assertIn("核对场景开关", result["records"][0]["detail"])
        public_preview = {key: value for key, value in result.items() if key != "records"}
        for private in ("ABCD1234", "当前客服限定", "example.invalid", "1234", "raw-response-id", "sop-source-anchor",
                        "客户复测原话", "客户原话不要外发", "客户的详细对话", "此前失败操作", "被拦截的操作"):
            self.assertNotIn(private, str(public_preview))

    def test_exact_topic_candidates_prioritize_anchored_verified_replies(self):
        def row(number, topic, anchor, verified, asked_at, start=1):
            return {"topic_id": topic_id(topic), "data": {"question_span": {"start": 1, "end": 2}},
                    "source": {"qa_id": number, "asked_at": asked_at, "answer": "回复" if verified else "",
                               "_staff_verified": verified,
                               "_question_parts": [{"message_id": anchor, "start": start, "end": start + 1}]}}

        evidence = [
            row(3, 1, "later-unanswered", False, 500), row(1, 1, "exact-reply", True, 100),
            row(2, 1, "different-span", True, 1000, 4), row(4, 2, "other-topic", True, 200),
            row(1, 1, "exact-reply", True, 100),
        ]
        with patch.object(export.voc_engine, "_workbench_data", return_value=(evidence, [], set(), {})) as read, \
                patch.object(export.voc_engine, "_topic_items", return_value=[]):
            anchors = export.select_topic_anchors(self.cfg, self.scope, {topic_id(1)})
        read.assert_called_once_with(self.cfg, {**self.scope, "days": 0})
        self.assertEqual(anchors, {topic_id(1): [(1, "exact-reply"), (3, "later-unanswered"), (2, "")]})

    def test_guest_view_includes_all_recorded_levels_and_only_same_topic_cases(self):
        target, outside = topic_id(1), topic_id(2)

        def row(qa_id, topic, theme, question, *, verified=False, session=None):
            anchor = f"msg-{qa_id}"
            answer = "已核对配置" if verified else ""
            return {"id": f"event-{qa_id}", "topic_id": topic,
                    "data": {"theme": theme, "question_span": {"quote": question, "start": 0, "end": len(question)},
                             "claims": [{"quote": answer, "start": 0, "end": len(answer)}] if answer else [],
                             "domain": {"primary_intent": "configuration_guidance",
                                        "diagnosis": {"status": "not_applicable"}}},
                    "source": {"qa_id": qa_id, "session_id": session or f"session-{qa_id}", "customer_id": "fixture-shop",
                               "question": question, "answer": answer, "asked_at": qa_id * 1000, "_staff_verified": verified,
                               "_question_parts": [{"message_id": anchor, "start": 0, "end": len(question)}]}}

        evidence = [
            row(2, target, "知识管理／跨店同步", "怎么同步表格"),
            row(3, target, "知识管理 → 导入知识", "怎么导入", verified=True, session="shared"),
            row(4, target, "知识管理 → 导入知识", "追问同一咨询", session="shared"),
            row(5, outside, "另一大类 / 另一子类", "无关内容", verified=True),
        ]
        topic = {"id": target, "theme": "知识管理 / 跨店同步", "question": "知识管理标准问题",
                 "consultation_count": 2, "session_count": 2, "primary_intent": "configuration_guidance",
                 "boundary": "仅包含当前发布主题"}
        comparison = {"current": {"source_pairs": 10, "processed_pairs": 8}, "previous": None, "comparable": False}
        with (patch.object(export.voc_engine, "_workbench_data", return_value=(evidence, [], set(), comparison)) as read,
              patch.object(export.voc_engine, "_topic_items", return_value=[topic])):
            views = export.topic_guest_views(self.cfg, {**self.scope, "staff": "发布时客服限定"}, {target})
        read.assert_called_once_with(self.cfg, {**self.scope, "days": 0, "staff": "发布时客服限定"})
        self.assertEqual(set(views), {target})
        view = views[target]
        self.assertEqual(view["taxonomy"], [
            {"primary": "知识管理", "secondary": "跨店同步", "count": 1},
            {"primary": "知识管理", "secondary": "导入知识", "count": 1},
        ])
        self.assertEqual(view["total_cases"], 2)
        self.assertEqual([case["qa_id"] for case in view["cases"]], [3, 2])
        self.assertEqual(view["cases"][0]["anchor_id"], "msg-3")
        self.assertIn("已核对配置", str(view["cases"][0]["sections"]))
        self.assertIn("诊断状态", str(view["sections"]))
        for unexpected in ("无关内容", "另一子类", "发布时客服限定", "fixture-shop"):
            self.assertNotIn(unexpected, str(view))

    def test_publish_issues_scoped_links_and_falls_back_to_content_only(self):
        record = {"key": topic_id(1), "title": "可分享问题", "summary": "5 次咨询", "detail": "发布正文",
                  "_guest_anchors": [(101, "unavailable-anchor"), (102, "other-anchor")],
                  "_guest_view": {"taxonomy": [{"primary": "一级", "secondary": "二级"}]}, "url": ""}
        other = {**record, "key": topic_id(2), "title": "无会话的问题", "_guest_anchors": []}
        doc = {**self.scope, "records": [record, other]}
        first = "https://qiyu.fanwulink.cn/guest/example-one"
        second = "https://qiyu.fanwulink.cn/guest/example-two"
        entry = {**self.scope, "kind": "topics", "title": record["title"], "summary": record["summary"],
                 "detail": record["detail"]}
        with patch.object(export.guest_evidence, "issue_url", side_effect=["", first, second]) as issue:
            result = export.attach_guest_links(self.cfg, doc, "topics")
        self.assertEqual(issue.call_args_list, [
            call(self.cfg, 101, "unavailable-anchor", entry=entry, view=record["_guest_view"]),
            call(self.cfg, 102, "other-anchor", entry=entry, view=record["_guest_view"]),
            call(self.cfg, None, "", entry={**entry, "title": other["title"]}, view=other["_guest_view"]),
        ])
        self.assertEqual([record["url"] for record in result["records"]], [first, second])
        self.assertTrue(all(not {"_guest_anchors", "_guest_view"} & record.keys() for record in result["records"]))
        self.assertEqual(doc["records"][0]["url"], "")
        with patch.object(export.guest_evidence, "issue_url", return_value=""), self.assertRaises(voc.VocError):
            export.attach_guest_links(self.cfg, doc, "topics")

    def test_rejects_open_or_malformed_ranges_and_returns_empty_preview(self):
        for filters in ({}, {"start": "2026-11-01"}, {"start": "20261101", "end": "2026-11-07"},
                        {"start": "2026-11-07", "end": "2026-11-01"}):
            with self.subTest(filters=filters), self.assertRaises(voc.VocError):
                export.build_document(self.cfg, filters, "sops")
        with self.assertRaises(voc.VocError):
            export.build_document(self.cfg, self.scope, "sops", "not-a-sop-id")
        with patch.object(export.voc_engine, "sops", return_value={"items": []}) as read:
            result = export.build_document(self.cfg, {"days": 7}, "sops")
        end = datetime.now(voc_topic_insights.ZONE).date()
        self.assertEqual(result["start"], (end - timedelta(days=6)).isoformat())
        self.assertEqual(result["end"], end.isoformat())
        self.assertEqual((result["count"], result["total"], result["items"], result["records"]), (0, 0, [], []))
        read.assert_called_once_with(self.cfg, {"days": 0, "start": result["start"], "end": result["end"]}, "")
        self.assertIn("当前范围暂无可导出的条目", result["content"])
