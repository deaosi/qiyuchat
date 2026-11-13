"""Regression checks for scoped, model-safe assistant statistics."""

from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

from src import app, push_sync, voc_engine
from src.voc_assistant_metrics import read_metric
from src.voc_topic_insights import ZONE
from tests.test_local import LocalFixture


class TestAssistantMetrics(LocalFixture):
    def setUp(self):
        super().setUp()
        self.first_day = int(datetime(2026, 11, 1, 0, 5, tzinfo=ZONE).timestamp() * 1000)
        self.last_day = int(datetime(2026, 11, 3, 0, 5, tzinfo=ZONE).timestamp() * 1000)
        self.group = "店铺测试群"
        self.staff = "示例客服陈"
        self.customer_name = "测试商家张 13812345678"
        with app.get_db(self.cfg).tx() as db:
            db.executemany(
                "INSERT INTO qa_pair(session_id,idx,customer_id,customer_name,group_name,staff_name,asked_at,question,answer,cluster_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    ("13811110000", 1, "customer-test-a", self.customer_name, self.group, self.staff,
                     self.first_day, "怎么设置场景", "先看场景开关", 1),
                    ("13811110000", 2, "customer-test-a", self.customer_name, self.group, self.staff,
                     self.first_day, "场景没触发", "", 1),
                    ("13822220000", 1, "customer-test-a", self.customer_name, self.group, self.staff,
                     self.first_day, "怎么保存配置", "点击保存", 1),
                    ("other-session", 1, "customer-test-b", "另一测试商家", "另一个群", "示例客服宋",
                     self.last_day, "怎么升级", "", 2),
                    ("unknown-session", 1, "qiyu-session:private-source", "qiyu-session:private-source", "", "",
                     0, "无日期的问题", "", 0),
                ],
            )

    def metric(self, name, filters=None, limit=10):
        return read_metric(self.cfg, filters or {}, name, limit, engine=voc_engine)

    def test_overview_respects_full_source_filters_and_exact_session_identity(self):
        display, citations, model = self.metric("overview", {
            "start": "2026-11-01", "end": "2026-11-01", "group": self.group,
            "staff": self.staff, "customer": "customer-test-a", "cluster": 1,
        })
        self.assertEqual((citations, model), ([], display))
        self.assertIn("3 条来源问答，来自 2 个会话", display)
        self.assertIn("有可见答复 2 条，未记录可见答复 1 条", display)
        refined, _, _ = self.metric("overview", {"q": "没触发", "replied": "unanswered"})
        self.assertIn("1 条来源问答", refined)
        self.assertIn("未记录可见答复 1 条", refined)

    def test_first_response_counts_distinct_exact_sessions_and_excludes_missing_values(self):
        database = app.get_db(self.cfg)
        database._conn.executescript(push_sync.SCHEMA)
        with database.tx() as db:
            db.executemany("INSERT INTO raw_session(session_id,first_reply_cost,avg_resp_duration) VALUES (?,?,?)", [
                ("13811110000", 1200, 20000), ("13822220000", 2800, 30000),
                ("other-session", 0, 60000),
            ])
            db.executemany(
                "INSERT INTO push_sync_state(session_id,event_key,version,status,attempts,updated_at,source_version,source_json)"
                " VALUES (?,'event','v2','completed',1,1,?,'{}')",
                [("13811110000", "v2"), ("13822220000", "v1"), ("other-session", "v2")],
            )

        display, citations, model = self.metric("first_response", {"start": "2026-11-01", "end": "2026-11-03"})
        self.assertEqual((citations, model), ([], display))
        self.assertIn("客服平均首响为 2.00 秒", display)
        self.assertIn("3 个来源会话中，2 个有有效首响记录", display)
        self.assertIn("1 个来自当前七鱼同步，1 个为其他留存记录", display)
        self.assertIn("1 个无有效首响记录，未计入平均值", display)
        self.assertNotIn("13811110000", display)
        self.assertNotIn("13822220000", display)

        scoped, _, _ = self.metric("first_response", {"group": self.group, "staff": self.staff, "q": "保存"})
        self.assertIn("客服平均首响为 2.80 秒", scoped)
        self.assertIn("1 个来源会话中，1 个有有效首响记录", scoped)
        no_sample, _, _ = self.metric("first_response", {"staff": "示例客服宋"})
        self.assertIn("有 1 个来源会话，均无有效的客服首响记录", no_sample)

    def test_first_response_uses_legacy_metadata_without_a_sync_table(self):
        with app.get_db(self.cfg).tx() as db:
            db.execute("INSERT INTO raw_session(session_id,first_reply_cost) VALUES ('13811110000',3500)")
        display, _, _ = self.metric("first_response", {"group": self.group})
        self.assertIn("客服平均首响为 3.50 秒", display)
        self.assertIn("2 个来源会话中，1 个有有效首响记录", display)
        self.assertIn("0 个来自当前七鱼同步，1 个为其他留存记录", display)

    def test_daily_peak_uses_full_date_range_even_if_outside_displayed_days(self):
        display, citations, model = self.metric("daily_trend", {"start": "2026-11-01", "end": "2026-11-03"}, limit=2)
        self.assertEqual((citations, model), ([], display))
        self.assertIn("共有 4 条可按日统计的来源问答", display)
        self.assertIn("单日最高 3 条，最早出现在 2026-11-01", display)
        self.assertIn("2026-11-02：0 条\n2026-11-03：1 条", display)
        self.assertNotIn("2026-11-01：3 条", display)
        all_time, _, _ = self.metric("daily_trend", limit=1)
        self.assertIn("时间未知 1 条", all_time)

    def test_rankings_use_full_denominator_and_withhold_names_from_model(self):
        for metric, display_label, model_label in (
            ("group_breakdown", self.group, "客户群 1（已脱敏）"),
            ("staff_breakdown", self.staff, "客服 1（已脱敏）"),
            ("customer_breakdown", "测试商家张", "客户 1（已脱敏）"),
        ):
            with self.subTest(metric=metric):
                display, citations, model = self.metric(metric, limit=1)
                self.assertEqual(citations, [])
                self.assertIn(display_label, display)
                self.assertNotIn(display_label, model)
                self.assertIn(model_label, model)
                self.assertIn("3 条问答，占 60.0%，未记录可见答复 1 条", display)
                self.assertIn("3 条问答，占 60.0%，未记录可见答复 1 条", model)
                self.assertNotIn("13812345678", display)
                self.assertNotIn("customer-test-a", display)
        all_customers, _, _ = self.metric("customer_breakdown")
        self.assertNotIn("private-source", all_customers)
        self.assertIn("匿名客户", all_customers)

    def test_topic_and_diagnosis_stats_use_only_existing_scoped_projection(self):
        cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, voc_domain="zhihuige"))
        source = {
            "answer": "", "customer_id": "private-customer", "group_name": "甲群", "staff_name": "示例客服",
            "asked_at": self.last_day, "session_id": "private-session",
        }
        engine = Mock()
        engine._sources.return_value = {7: source, 8: {**source, "group_name": "不在主题内"}}
        engine._topic_view.return_value = [{"qa_id": 7}, {"qa_id": 7}, {"qa_id": 9}]
        filters = {"focus_topic": "t_" + "a" * 24, "support_intent": "functional_anomaly",
                   "diagnosis_status": "pending", "start": "2026-11-03", "end": "2026-11-03", "group": "甲群"}
        display, _, model = read_metric(cfg, filters, "group_breakdown", 10, engine=engine)
        self.assertIn("当前主题或分类中，共有 1 条来源问答", display)
        self.assertIn("甲群", display)
        self.assertNotIn("不在主题内", display)
        self.assertNotIn("甲群", model)
        engine._sources.assert_called_once_with(cfg, filters, time_bounds=(self.last_day - 300000, self.last_day - 300000 + 86400000))
        engine._topic_view.assert_called_once_with(cfg, filters, engine._sources.return_value)
        engine._topic_view.return_value = []
        empty, _, _ = read_metric(cfg, filters, "overview", 10, engine=engine)
        self.assertIn("暂无匹配", empty)

    def test_unknown_classification_and_invalid_budget_cannot_fall_back_to_unfiltered_data(self):
        cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, voc_domain=""))
        engine = Mock()
        text, _, _ = read_metric(cfg, {"support_intent": "functional_anomaly"}, "overview", 5, engine=engine)
        self.assertIn("无法按所选支持类型", text)
        self.assertEqual(engine.method_calls, [])
        with self.assertRaises(ValueError):
            self.metric("daily_trend", limit=11)
