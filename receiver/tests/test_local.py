"""离线导入、清洗和导出回归；所有写入都在临时目录。"""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from src import app
from src.clean.rules import mask_pii
from src.config import load_settings
from src.extract.cluster import cluster_questions
from src.extract.normalize import normalize
from src.extract.qa import extract_pairs
from src.local import (MAX_FILE_BYTES, clean_local, csv_bytes, demo_settings,
                       import_file, load_demo, parse_import, parse_time, template_bytes)
from src.qiyu.inspect import InspectExporter
from src.web import api


def make_csv(headers, rows, encoding="utf-8-sig"):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows(rows)
    return out.getvalue().encode(encoding)


def make_xlsx(rows):
    data = io.BytesIO()
    body = []
    for i, row in enumerate(rows, 1):
        cells = "".join(f'<c r="{chr(65+j)}{i}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
                        for j, v in enumerate(row))
        body.append(f'<row r="{i}">{cells}</row>')
    with zipfile.ZipFile(data, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(body) + '</sheetData></worksheet>')
    return data.getvalue()


class LocalFixture(unittest.TestCase):
    def setUp(self):
        app.reset_db()
        self.temp = tempfile.TemporaryDirectory()
        cfg = load_settings()
        self.cfg = replace(cfg, runtime=replace(cfg.runtime, mock=False,
                           db_path=str(Path(self.temp.name) / "real.db")),
                           llm=replace(cfg.llm, dry_run=True), feishu=replace(cfg.feishu, dry_run=True))

    def tearDown(self):
        app.reset_db()
        self.temp.cleanup()

    def seed(self):
        return import_file(self.cfg, make_csv(
            ["会话ID", "客户ID", "客户名称", "客户群", "客服", "时间", "问题", "回答"],
            [["s1", "u1", "张同学", "售前", "小林", "2026-11-01 10:00:00", "这个多少钱", "199元"],
             ["s1", "u1", "张同学", "售前", "小林", "2026-11-01 10:01:00", "什么时候发货", ""],
             ["s2", "u2", "李同学", "售后", "小陈", "2026-11-02 10:00:00", "退款什么时候到账", "3天内"],
             ["s3", "", "", "售后", "小陈", "", "滤芯多久换一次", ""],
             ["s4", "", "", "售后", "小陈", "", "滤芯多久换一次", ""]]), "input.csv")


class TestImportParsing(unittest.TestCase):
    def test_preview_does_not_open_database(self):
        with patch("src.local.get_db", side_effect=AssertionError("must not open DB")):
            p = parse_import(template_bytes(), "template.csv").preview()
        self.assertEqual(p["sessions"], 2)
        self.assertEqual(p["estimated_pairs"], 2)
        self.assertTrue(p["can_import"])

    def test_csv_gb18030_and_bom(self):
        for encoding in ("utf-8-sig", "gb18030"):
            p = parse_import(make_csv(["问题", "回答"], [["滤芯多久换一次", "半年"]], encoding), "a.csv")
            self.assertEqual(p.preview()["estimated_pairs"], 1)

    def test_minimal_xlsx(self):
        p = parse_import(make_xlsx([["问题", "回答"], ["这个多少钱", "199元"]]), "a.xlsx")
        self.assertEqual(p.preview()["estimated_pairs"], 1)

    def test_invalid_files(self):
        cases = [(b"", "a.csv"), (b"x", "a.xls"), (b"not-xlsx", "a.xlsx"),
                 (b"PKbad", "a.xlsx"), (b"a\nb", "a.csv"), (b"a\x00b", "a.csv"),
                 (b"x" * (MAX_FILE_BYTES + 1), "a.csv")]
        for content, name in cases:
            with self.subTest(name=name, size=len(content)), self.assertRaises(ValueError):
                parse_import(content, name)

    def test_duplicate_headers_and_row_limit(self):
        for headers, rows in [(["问题", "问题"], [["多少钱", "多少"]]),
                              (["问题"], [["这个多少钱"]] * 5001)]:
            with self.assertRaises(ValueError):
                parse_import(make_csv(headers, rows), "a.csv")

    def test_errors_and_duplicates_are_visible(self):
        p = parse_import(make_csv(["问题", "时间"], [["什么时候发货", ""],
            ["", ""], ["什么时候发货", ""], ["退款多久到账", "错日期"]]), "a.csv").preview()
        self.assertEqual(p["valid_rows"], 1)
        self.assertEqual(p["duplicate_rows"], 1)
        self.assertEqual(p["skipped_rows"], 1)
        self.assertEqual(p["errors"][0]["row"], 5)

    def test_time_formats_and_unknown(self):
        self.assertEqual(parse_time(""), 0)
        self.assertEqual(parse_time("0"), 0)
        self.assertEqual(parse_time("1700000000"), 1700000000000)
        self.assertEqual(parse_time("1700000000000"), 1700000000000)
        self.assertEqual(parse_time("2026/11/07 10:30:00"), parse_time("2026-11-07T10:30:00"))
        self.assertGreater(parse_time("46000.5"), 0)
        for bad in ("abc", "2026-02-30", "0001-01-01", "2102-01-01", "123"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_time(bad)

    def test_transcript_time_and_named_staff(self):
        p = parse_import(make_csv(["会话内容", "客服"], [[
            "[2026-11-07 10:30:00] 张同学：什么时候发货\n小林：今天", "小林"]]), "a.csv")
        sample = p.preview()
        self.assertEqual(sample["estimated_pairs"], 1)
        self.assertEqual(sample["samples"][0]["answer"], "今天")
        self.assertGreater(next(iter(p.messages.values()))[0]["ts"], 0)

    def test_unrecognizable_speaker_is_reported(self):
        p = parse_import(make_csv(["会话内容"], [["小林：今天发货"]]), "a.csv").preview()
        self.assertFalse(p["can_import"])
        self.assertEqual(p["skipped_rows"], 1)

    def test_preview_masks_pii(self):
        data = make_csv(["问题", "回答"], [["请联系13812345678查询物流", "邮箱zhangsan@example.com"]])
        p = parse_import(data, "13812345678.csv").preview()
        self.assertNotIn("13812345678", str(p))
        self.assertNotIn("zhangsan@", str(p))


class TestLocalWorkflow(LocalFixture):
    def test_import_idempotent_and_no_ai_results(self):
        first = self.seed()
        ids = [r["qa_id"] for r in app.get_db(self.cfg).iter_qa_pairs()]
        second = self.seed()
        self.assertEqual(first["qa_pairs"], 5)
        self.assertEqual(second["new_sessions"], 0)
        self.assertEqual(second["new_pairs"], 0)
        self.assertEqual(ids, [r["qa_id"] for r in app.get_db(self.cfg).iter_qa_pairs()])
        self.assertEqual(app.get_db(self.cfg).stats()["clean_result"], 0)

    def test_update_removes_tail_and_updates_metadata(self):
        self.seed()
        db = app.get_db(self.cfg)
        old_id = db.iter_qa_pairs(["s1"])[0]["qa_id"]
        import_file(self.cfg, make_csv(["会话ID", "客户ID", "客户群", "客服", "问题", "回答"],
            [["s1", "new-u", "新分组", "新客服", "售后保修多久", "一年"]]), "update.csv")
        rows = db.iter_qa_pairs(["s1"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qa_id"], old_id)
        self.assertEqual(rows[0]["group_name"], "新分组")
        self.assertEqual(rows[0]["customer_id"], "new-u")
        self.assertEqual(rows[0]["staff_name"], "新客服")
        self.assertEqual(len(db.get_messages("s1")), 2)

    def test_processing_states_and_invalidation(self):
        db = app.get_db(self.cfg)
        db.upsert_sessions([{"id": "empty"}, {"id": "noise"}])
        db.upsert_messages("noise", [{"role": "user", "content": "谢谢"}])
        app.stage_extract(self.cfg)
        self.assertEqual(db.local_status()["pending"], 0)
        self.assertEqual(db.local_status()["no_question"], 1)
        self.assertEqual(db.local_status()["no_content"], 1)
        self.assertEqual(app.stage_extract(self.cfg)["sessions_scanned"], 0)
        db.upsert_messages("noise", [{"role": "user", "content": "什么时候发货"}])
        self.assertEqual(db.local_status()["pending"], 1)
        app.stage_extract(self.cfg)
        self.assertEqual(db.stats()["qa_pair"], 1)
        db.upsert_sessions([{"id": "noise", "fromGroup": "新组"}])
        self.assertEqual(db.local_status()["pending"], 1)
        app.stage_extract(self.cfg)
        self.assertEqual(db.iter_qa_pairs()[0]["group_name"], "新组")

    def test_noise_replaces_old_pairs_but_archived_pairs_survive(self):
        self.seed()
        db = app.get_db(self.cfg)
        db.upsert_messages("s1", [{"role": "user", "content": "谢谢"}])
        app.stage_extract(self.cfg)
        self.assertEqual(len(db.iter_qa_pairs(["s1"])), 0)
        db.upsert_messages("s2", [])
        clean_local(self.cfg)
        self.assertEqual(len(db.iter_qa_pairs(["s2"])), 1)
        self.assertEqual(db.local_status()["archived"], 1)

    def test_transaction_rolls_back_complete_import(self):
        with patch("src.local.stage_extract", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                self.seed()
        self.assertEqual(app.get_db(self.cfg).stats()["raw_session"], 0)
        self.assertEqual(app.get_db(self.cfg).stats()["raw_message"], 0)
        self.assertEqual(app.get_db(self.cfg).stats()["task_log"], 0)

    def test_reclean_preserves_ids_counts_and_masks(self):
        self.seed()
        before = api.pairs(self.cfg)
        clean_local(self.cfg)
        self.assertEqual(before["total"], api.pairs(self.cfg)["total"])
        self.assertEqual([r["qa_id"] for r in before["items"]],
                         [r["qa_id"] for r in api.pairs(self.cfg)["items"]])

    def test_anonymous_customers_and_unknown_dates(self):
        self.seed()
        s = api.build_summary(self.cfg)
        self.assertEqual(s["customer_count"], 4)
        self.assertEqual(s["unknown_time"], 2)
        self.assertEqual(api.pairs(self.cfg, start="2026-11-01", end="2026-11-02")["total"], 3)
        self.assertEqual([r["asked_at"] for r in api.pairs(self.cfg)["items"][-2:]], [0, 0])

    def test_filter_consistency_pagination_and_drilldown(self):
        self.seed()
        filters = {"group": "售前", "staff": "小林", "start": "2026-11-01", "end": "2026-11-01"}
        s, p, t, q = (api.build_summary(self.cfg, **filters), api.pairs(self.cfg, **filters),
                       api.trend(self.cfg, **filters), api.questions(self.cfg, **filters))
        self.assertEqual(s["qa_pair"], p["total"])
        self.assertEqual(sum(d["questions"] for d in t["series"]), p["total"])
        self.assertEqual(q["total"], p["total"])
        self.assertEqual(api.pairs(self.cfg, replied="unanswered", **filters)["total"], 1)
        self.assertEqual(api.pairs(self.cfg, q="发货", **filters)["total"], 1)
        self.assertEqual(len(api.pairs(self.cfg, limit=2, offset=2)["items"]), 2)
        customer = api.customers(self.cfg)["items"][0]
        self.assertEqual(api.pairs(self.cfg, customer=customer["customer_id"])["total"], customer["total_questions"])
        cluster = api.clusters(self.cfg)["items"][0]
        self.assertEqual(api.pairs(self.cfg, cluster=cluster["cluster_id"])["total"], cluster["size"])
        rows = list(csv.reader(io.StringIO(api.export_pairs(self.cfg, **filters).decode("utf-8-sig"))))
        self.assertEqual(len(rows) - 1, p["total"])

    def test_detail_and_missing(self):
        self.seed()
        r = api.pairs(self.cfg)["items"][0]
        d = api.detail(self.cfg, r["qa_id"])
        self.assertEqual(d["pair"]["question"], r["question"])
        self.assertTrue(d["messages"])
        self.assertIsNone(api.detail(self.cfg, 99999))

    def test_invalid_date_range_and_future_trend(self):
        for params in ({"start": "bad"}, {"start": "2026-11-02", "end": "2026-11-01"}, {"days": -1}):
            with self.assertRaises(ValueError):
                api.pairs(self.cfg, **params)
        self.assertEqual(len(api.trend(self.cfg, start="2027-01-01")["series"]), 1)

    def test_demo_is_separate_and_repeatable(self):
        self.seed()
        local_before = api.build_summary(self.cfg)["qa_pair"]
        load_demo(self.cfg)
        first = api.build_summary(demo_settings(self.cfg))
        load_demo(self.cfg)
        self.assertEqual(first["qa_pair"], api.build_summary(demo_settings(self.cfg))["qa_pair"])
        self.assertGreater(first["qa_pair"], 100)
        self.assertGreater(first["unanswered"], 0)
        self.assertEqual(api.build_summary(self.cfg)["qa_pair"], local_before)
        self.assertFalse((Path(self.temp.name) / "mock.db").exists())

    def test_stage_demo_closed_connection_regression(self):
        cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, mock=True))
        with patch("src.app.stage_push", return_value={}):
            out = app.stage_demo(cfg)
        self.assertGreater(out["stats"]["qa_pair"], 0)

    def test_local_never_calls_integrations(self):
        with patch("src.qiyu.SessionExporter.export", side_effect=AssertionError("qiyu")), \
             patch("src.qiyu.InspectExporter.export", side_effect=AssertionError("qiyu")), \
             patch("src.app.stage_clean", side_effect=AssertionError("llm")), \
             patch("src.app.stage_push", side_effect=AssertionError("feishu")):
            self.seed()
            clean_local(self.cfg)
            load_demo(self.cfg)


class TestLocalRules(unittest.TestCase):
    def test_high_frequency_identical_questions(self):
        items = [(i, normalize("这个多少钱")) for i in range(100)]
        clusters = cluster_questions(items)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["size"], 100)

    def test_cluster_is_deterministic_and_quality_not_overmerged(self):
        items = [(1, normalize("机器漏水")), (2, normalize("屏幕损坏")), (3, normalize("这个多少钱"))]
        self.assertEqual(cluster_questions(items), cluster_questions(list(reversed(items))))
        self.assertEqual(len(cluster_questions(items)), 3)

    def test_boundary_keeps_unanswered_rows_separate(self):
        messages = [{"role": "user", "content": "什么时候发货", "ts": 0},
                    {"role": "boundary", "content": ""},
                    {"role": "user", "content": "退款多久到账", "ts": 0}]
        rows = extract_pairs(messages, {"id": "s", "startTime": 1700000000000})
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["asked_at"] for r in rows], [0, 0])

    def test_noise_does_not_swallow_valid_question(self):
        rows = extract_pairs([{"role": "user", "content": "好的"},
                              {"role": "user", "content": "什么时候发货"}], {"id": "s"})
        self.assertEqual(rows[0]["question"], "什么时候发货")
        self.assertEqual(rows[0]["asked_at"], 0)

    def test_addresses_do_not_leak_details(self):
        for text in ("地址北京市朝阳区建国路88号SOHO现代城A座1801", "上海市浦东新区世纪大道100号12楼", "建国路88号1801室"):
            with self.subTest(text=text):
                masked = mask_pii(text)
                self.assertIn("详细地址已脱敏", masked)
                self.assertNotIn("88", masked)
                self.assertNotIn("1801", masked)
                self.assertNotIn("100号", masked)

    def test_csv_formula_defense_and_zero(self):
        values = ["=1+1", " +SUM(A1)", "-1", "@x", "\tformula", 0, "13812345678"]
        rows = list(csv.reader(io.StringIO(csv_bytes(["x"], [[x] for x in values]).decode("utf-8-sig"))))
        self.assertTrue(all(row[0].startswith("'") for row in rows[1:6]))
        self.assertEqual(rows[6][0], "0")
        self.assertNotIn("13812345678", rows[7][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
