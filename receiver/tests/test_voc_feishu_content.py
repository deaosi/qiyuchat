"""Feishu previews and records use current projections and omit raw evidence."""

from datetime import datetime, timedelta
from unittest.mock import patch
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
        ranking = {"items": topics, "comparison": {"current": {"source_pairs": 50, "processed_pairs": 40}}}
        with patch.object(export.voc_engine, "topics", return_value=ranking) as read:
            result = export.build_document(self.cfg, filters, "topics")
        read.assert_called_once_with(self.cfg, {**filters, "days": 0}, workbench=True)
        self.assertEqual((result["count"], result["total"]), (20, 21))
        first = result["records"][0]
        self.assertEqual(first["key"], topic_id(0))
        self.assertEqual(first["summary"], "40 次咨询 · 配置与使用")
        self.assertIn("已整理 40 / 50 条问答", first["detail"])
        self.assertEqual(first["url"],
                         f"https://qiyu.fanwulink.cn/#questions?topic={topic_id(0)}&start=2026-11-01&end=2026-11-07")
        root = fromstring(f"<doc>{result['content']}</doc>")
        self.assertEqual(root[0].tag, "title")
        self.assertEqual(root.findtext("h2"), "[名称]和[名称]查看 <规则> & 配置")
        self.assertEqual(root.find(".//a").attrib["href"], first["url"])
        self.assertEqual(result["items"][0], {"title": first["title"], "summary": first["summary"]})
        for private in ("现场客服阿兰", "小周店主", "示例内部群", "当前客户群限定", "当前客服限定", "无关会话原话"):
            self.assertNotIn(private, str(result))

    def test_sops_publish_validated_steps_without_failed_attempts_or_chat(self):
        def sop(number, status, result=None):
            return {"id": sop_id(number), "topic_id": topic_id(1), "qa_id": 1234,
                    "title": "客户原话不要外发", "problem": {"quote": "客户的详细对话"},
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
        self.assertIn("客户反馈按该流程处理后复测成功", result["records"][0]["detail"])
        self.assertIn("效果待验证", result["records"][1]["summary"])
        self.assertIn("商品ID [已脱敏]", result["records"][0]["detail"])
        self.assertIn("核对场景开关", result["records"][0]["detail"])
        for private in ("ABCD1234", "当前客服限定", "example.invalid", "1234", "raw-response-id", "客户复测原话",
                        "客户原话不要外发", "客户的详细对话", "此前失败操作", "被拦截的操作"):
            self.assertNotIn(private, str(result))

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
