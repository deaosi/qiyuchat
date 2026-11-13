"""本机 HTTP 边界与完整导入闭环；只绑定临时回环端口。"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import re
import threading
import time
from dataclasses import replace
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from urllib.response import addinfourl

from tests.test_local import LocalFixture, make_csv
from src import app
from src.web import case_media
from src.web.server import DashboardServer


class TestWeb(LocalFixture):
    def setUp(self):
        super().setUp()
        self.server = DashboardServer(("127.0.0.1", 0), self.cfg)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def call(self, path, method="GET", body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            data = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()

    def write_headers(self, **extra):
        return {"Origin": self.origin, "X-Local-Request": "1", **extra}

    def test_static_and_traversal(self):
        for path in ("/", "/app.js", "/style.css"):
            status, _, headers = self.call(path)
            self.assertEqual(status, 200)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/../config.py", "/%2e%2e/config.py", "/..%5cconfig.py"):
            self.assertEqual(self.call(path)[0], 404)

    def test_feishu_publish_requires_a_current_preview_and_authenticated_same_origin(self):
        self.server.admin_key = "fixture-admin"
        scope = "?source=local&kind=topics&start=2026-11-01&end=2026-11-07"
        preview_path = "/api/voc2/feishu/preview" + scope
        publish_path = "/api/voc2/feishu/publish" + scope
        auth = {"X-Admin-Key": "fixture-admin"}
        write_headers = self.write_headers(**auth, **{"Content-Type": "application/json"})
        destination = "https://example.feishu.cn/base/example?table=tblFixture"
        document = {"title": "测试高频问题", "content": "<title>测试高频问题</title>", "count": 1,
                    "total": 1, "start": "2026-11-01", "end": "2026-11-07",
                    "items": [{"title": "测试问题", "summary": "3 次咨询"}],
                    "records": [{"key": "topic-fixture", "title": "测试问题"}]}
        receipt = {"url": destination, "title": document["title"], "count": 1, "reused": False}

        target = SimpleNamespace(destination=destination)
        with (
            patch("src.voc_feishu_content.build_document", return_value=document),
            patch("src.voc_feishu_sync.configured_target", return_value=target),
            patch("src.voc_feishu_content.attach_guest_links", return_value=document) as attach,
            patch("src.voc_feishu_sync.publish", return_value=receipt) as publish,
        ):
            self.assertEqual(self.call(preview_path)[0], 401)
            code, preview, _ = self.call(preview_path, headers=auth)
            self.assertEqual(code, 200)
            self.assertTrue(preview["publish_configured"])
            self.assertNotIn("records", preview)
            body = json.dumps({"fingerprint": preview["fingerprint"]}).encode()
            self.assertEqual(self.call(publish_path, "POST", body, auth)[0], 403)
            document["records"][0]["title"] = "更新的问题"
            self.assertEqual(self.call(publish_path, "POST", body, write_headers)[0], 409)
            publish.assert_not_called()
            attach.assert_not_called()
            current = self.call(preview_path, headers=auth)[1]
            body = json.dumps({"fingerprint": current["fingerprint"]}).encode()
            self.assertEqual(self.call(publish_path, "POST", body, write_headers)[:2], (200, receipt))
            publish.assert_called_once()
            attach.assert_called_once()

    def test_guest_evidence_only_opens_the_exact_public_page(self):
        self.server.admin_key = "fixture-admin"
        token = "A" * 43
        path = "/guest/evidence/" + token
        public_assets = {"index-Abc_1234.js": "text/javascript; charset=utf-8",
                         "index-5678Abc_.css": "text/css; charset=utf-8", "geist-ABCD1234.woff2": "font/woff2"}
        static = Path(self.temp.name) / "static"
        guest_assets = static / "guest" / "assets"
        guest_assets.mkdir(parents=True)
        (static / "guest" / "index.html").write_bytes(b"<main>Guest</main><!--VOC_GUEST_DATA-->")
        for name in public_assets:
            (guest_assets / name).write_bytes(name.encode())
        (guest_assets / "index-Abc_1234.js.map").write_bytes(b"source map")
        title = '</script><img src="/api/meta">&'
        payload = {"entry": {"title": title, "summary": "发布校验" * 140}, "cases": [], "total_cases": 0}
        with (
            patch("src.web.server.STATIC", static),
            patch("src.web.guest_evidence.resolve_payload",
                  side_effect=lambda cfg, key: payload if key == token else None) as resolve,
        ):
            status, body, headers = self.call(path)
            self.assertEqual(status, 200)
            script = re.fullmatch(rb'<main>Guest</main><script id="voc-guest-data" type="application/json">([^<]+)</script>', body)
            self.assertIsNotNone(script)
            self.assertEqual(json.loads(script.group(1)), payload)
            self.assertNotIn(token.encode(), body)
            compressed_status, compressed, compressed_headers = self.call(path, headers={"Accept-Encoding": "gzip"})
            self.assertEqual((compressed_status, compressed_headers.get("Content-Encoding")), (200, "gzip"))
            self.assertEqual(gzip.decompress(compressed), body)
            self.assertNotIn("Set-Cookie", headers)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertIn("noindex", headers["X-Robots-Tag"])
            policy = headers["Content-Security-Policy"]
            for rule in ("default-src 'none'", "script-src 'self'", "font-src 'self'", "img-src 'self' data:", "form-action 'none'"):
                self.assertIn(rule, policy)
            for suffix in ("?id=99", "/other", ";other"):
                self.assertEqual(self.call(path + suffix)[0], 404)
            self.assertEqual(self.call("/guest/evidence/" + "B" * 43)[0], 404)
            prefix = "/guest/ui/assets/"
            for name, mime in public_assets.items():
                code, data, asset_headers = self.call(prefix + name)
                self.assertEqual((code, data, asset_headers["Content-Type"]), (200, name.encode(), mime))
                self.assertEqual(asset_headers["Cache-Control"], "public, max-age=31536000, immutable")
            for name in ("index-Abc_1234.js.map", "%2e%2e/index.html", "index-Abc_1234.js?v=1", "index.html"):
                self.assertEqual(self.call(prefix + name)[0], 404)
            self.assertEqual(self.call("/guest/ui/index.html")[0], 404)
            self.assertEqual(resolve.call_count, 3)
        for route in ("/", "/assets/index-Abc_1234.js", "/api/detail?id=1", "/api/meta",
                      "/api/voc2/feishu/preview?kind=topics&days=7"):
            self.assertEqual(self.call(route, headers={"Referer": self.origin + path})[0], 401)

    def test_guest_image_route_uses_opaque_id_and_never_exposes_upstream(self):
        self.server.admin_key = "fixture-admin"
        token, image_id = "A" * 43, "0-" + "b" * 64
        path = f"/guest/evidence/{token}/images/{image_id}"
        upstream = "https://ysf.nosdn.127.net/private-original.png?token=private-signature"
        png = b"\x89PNG\r\n\x1a\nsynthetic"
        with (
            patch("src.web.guest_evidence.resolve_image",
                  side_effect=lambda cfg, key, media: upstream if (key, media) == (token, image_id) else None) as resolve,
            patch("src.web.case_media.fetch_image", return_value=(png, "image/png", "private-original.png")) as fetch,
        ):
            status, data, headers = self.call(path)
            self.assertEqual((status, data), (200, png))
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertEqual(headers["Content-Disposition"], 'inline; filename="conversation-image.png"')
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertNotIn("Set-Cookie", headers)
            self.assertNotIn("private-original", str(headers))
            self.assertNotIn("Location", headers)
            for rejected in (path + "?url=" + upstream, path + "/more", path.replace(image_id, "%30-" + "b" * 64),
                             path.replace(token, "B" * 43), path.replace(image_id, "1-" + "b" * 64)):
                self.assertEqual(self.call(rejected)[0], 404)
            self.assertEqual(resolve.call_count, 3)
            fetch.assert_called_once_with(upstream)
            self.assertEqual(self.call("/api/detail/image", headers={"Referer": self.origin + path})[0], 401)

    def test_host_and_origin_protection(self):
        self.assertEqual(self.call("/api/meta", headers={"Host": "evil.example"})[0], 403)
        for headers in ({}, {"Origin": "http://evil.example", "X-Local-Request": "1"},
                        {"Origin": self.origin}, self.write_headers(**{"Sec-Fetch-Site": "cross-site"})):
            self.assertEqual(self.call("/api/clean", "POST", headers=headers)[0], 403)
        self.assertEqual(self.call("/api/clean", "POST", headers=self.write_headers())[0], 200)

    def test_import_preview_confirm_export_and_reclean(self):
        data = make_csv(["会话ID", "问题", "回答"], [["one", "这个多少钱，电话13812345678", "199元"]])
        headers = self.write_headers(**{"X-Filename": quote("样本.csv")})
        code, preview, _ = self.call("/api/import/preview", "POST", data, headers)
        self.assertEqual(code, 200)
        self.assertEqual(self.call("/api/meta")[1]["total"], 0)
        self.assertEqual(self.call("/api/import", "POST", data, headers)[0], 400)
        headers["X-File-Digest"] = preview["digest"]
        self.assertEqual(self.call("/api/import", "POST", data, headers)[0], 200)
        self.assertEqual(self.call("/api/import", "POST", data, headers)[1]["new_pairs"], 0)
        pairs = self.call("/api/pairs")[1]
        self.assertEqual(pairs["total"], 1)
        self.assertNotIn("13812345678", str(pairs))
        self.assertEqual(self.call("/api/detail?id=" + str(pairs["items"][0]["qa_id"]))[0], 200)
        self.assertEqual(self.call("/api/detail?id=99999")[0], 404)
        status, raw, headers = self.call("/api/export?replied=answered")
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"13812345678", raw)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.call("/api/clean", "POST", headers=self.write_headers())[0], 200)
        self.assertEqual(self.call("/api/status")[1]["pending"], 0)
        self.assertEqual(self.call("/api/template")[0], 200)

    def test_case_reply_lookup_checks_access_and_releases_database_lock(self):
        cfg = self.server.cfg
        self.server.cfg = replace(cfg, qiyu=replace(cfg.qiyu, app_key="fixture", app_secret="fixture"))
        self.server.admin_key = "case-fixture-admin"
        db = app.get_db(self.server.cfg)
        stamp = int(time.time() * 1000) - 1000
        sid = 123456
        question = "合成在线客服问题"
        db.upsert_sessions([{"id": sid, "startTime": stamp, "endTime": stamp + 10, "fromType": "wx_cs",
                             "staffMessageCount": 0, "userMessageCount": 1}])
        db.upsert_messages(str(sid), [{"role": "user", "content": question, "ts": stamp}])
        with db.tx() as conn:
            qa_id = conn.execute("INSERT INTO qa_pair(session_id,idx,question,asked_at) VALUES (?,1,?,?)",
                                 (str(sid), question, stamp)).lastrowid
        path = f"/api/detail/replies?id={qa_id}"
        auth = {"X-Admin-Key": "case-fixture-admin"}
        write = self.write_headers(**auth)
        self.assertEqual(self.call(path, "POST", headers=self.write_headers())[0], 401)
        self.assertEqual(self.call(path, "POST", headers=auth)[0], 403)
        before_lookup = self.call(f"/api/detail?id={qa_id}", headers=auth)[1]
        self.assertTrue(before_lookup["can_lookup_replies"])
        full_path = f"/api/detail?id={qa_id}&context=session"
        self.assertEqual(self.call(full_path)[0], 401)
        self.assertEqual(self.call(full_path + "&context=session", headers=auth)[0], 400)
        self.assertEqual(self.call(f"/api/detail?id={qa_id}&context=other", headers=auth)[0], 400)
        full_session = self.call(full_path, headers=auth)[1]
        self.assertEqual(full_session["context_scope"], "source_session")
        self.assertTrue(full_session["can_lookup_replies"])
        self.assertEqual(full_session["context_revision"], before_lookup["context_revision"])
        started, finish = threading.Event(), threading.Event()
        upstream = {"status": "found", "messages": [
            {"role": "staff", "content": "合成客服消息 13812345678", "ts": stamp + 1,
             "source_message_id": f"source:{sid}:2"}]}

        def lookup(_cfg, session, pair, retained):
            self.assertEqual(session["session_id"], str(sid))
            self.assertEqual(pair["qa_id"], qa_id)
            self.assertEqual(retained[0]["content"], question)
            started.set()
            finish.wait(5)
            return upstream

        with patch("src.web.api.case_replies.lookup", side_effect=lookup), ThreadPoolExecutor() as pool:
            response = pool.submit(self.call, path, "POST", headers=write)
            try:
                self.assertTrue(started.wait(3))
                local = pool.submit(self.call, f"/api/detail?id={qa_id}", headers=auth)
                self.assertEqual(local.result(timeout=2)[1]["message_count"], 1)
            finally:
                finish.set()
            status, result, _ = response.result(timeout=3)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["context_revision"], before_lookup["context_revision"])
        self.assertNotIn("13812345678", str(result))
        self.assertEqual(result["messages"][0]["source_message_id"], f"source:{sid}:2")
        self.assertEqual(len(db.get_messages(str(sid))), 1)
        with patch("src.web.api.case_replies.lookup", return_value=upstream):
            status, explicit, _ = self.call(path + "&context=session", "POST", headers=write)
        self.assertEqual(status, 200)
        self.assertEqual(explicit["context_revision"], full_session["context_revision"])

        def archive_during_lookup(*_args):
            with db.tx() as conn:
                conn.execute("DELETE FROM raw_message WHERE session_id=?", (str(sid),))
            return upstream

        with patch("src.web.api.case_replies.lookup", side_effect=archive_during_lookup):
            self.assertEqual(self.call(path, "POST", headers=write)[1],
                             {"status": "unavailable", "messages": []})

    def test_input_errors_have_400(self):
        for path in ("/api/pairs?limit=bad", "/api/summary?start=bad", "/api/meta?source=unknown",
                     "/api/pairs?replied=resolved", "/api/trend?days=-1"):
            self.assertEqual(self.call(path)[0], 400, path)
        self.assertEqual(self.call("/api/import/preview", "POST", b"x", self.write_headers(**{"X-Filename": "a.xls"}))[0], 400)
        self.assertEqual(self.call("/api/no-such-route")[0], 404)
        self.assertEqual(self.call("/api/clean", "POST", headers=self.write_headers(**{"Content-Length": "9000000"}))[0], 413)

    def test_overlong_post_query_returns_an_http_error(self):
        query = "&".join(f"param{i}=x" for i in range(31))
        with patch("src.web.server.clean_local") as clean:
            status, response, _ = self.call("/api/clean?" + query, "POST", headers=self.write_headers())
        self.assertEqual(status, 400)
        self.assertIn("error", response)
        clean.assert_not_called()

    def test_disabled_demo_cannot_switch_database_during_parallel_local_requests(self):
        self.seed()
        demo = Path(self.cfg.runtime.db_path).with_name("dashboard_demo.db")
        sentinel = b"existing independent demo database must not be opened or changed"
        demo.write_bytes(sentinel)
        self.assertEqual(self.call("/api/demo", "POST", headers=self.write_headers())[0], 410)
        def read(i):
            source = "demo" if i % 2 else "local"
            status, data, _ = self.call("/api/summary?source=" + source)
            return status, source, data
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read, range(24)))
        for _, source, data in results:
            if source == "local":
                self.assertEqual(data["qa_pair"], 5)
            else:
                self.assertIn("已停用", data["error"])
        self.assertTrue(all(status == (200 if source == "local" else 400) for status, source, _ in results))
        self.assertEqual(demo.read_bytes(), sentinel)
        self.assertEqual(self.call("/api/import/preview?source=demo", "POST", b"x", self.write_headers())[0], 400)

    def test_all_demo_http_routes_are_disabled_without_loading_helpers_or_files(self):
        self.seed()
        demo = Path(self.cfg.runtime.db_path).with_name("dashboard_demo.db")
        before = self.call("/api/summary?source=local")[1]
        with patch("src.local.load_demo", side_effect=AssertionError("HTTP must not load demo")) as helper:
            for name in ("summary", "meta", "detail?id=1", "status", "export", "gateway/config", "voc/summary", "voc/knowledge"):
                separator = "&" if "?" in name else "?"
                self.assertEqual(self.call("/api/" + name + separator + "source=demo")[0], 400, name)
            for name in ("demo", "clean", "import/preview", "voc/run", "voc/knowledge/run", "gateway/config", "gateway/models"):
                self.assertEqual(self.call("/api/" + name + "?source=demo", "POST", b"{}", self.write_headers(**{"Content-Type": "application/json"}))[0], 400, name)
            for query in ("source=local&source=demo", "source=demo&source=local", "source=local&source=unknown"):
                self.assertEqual(self.call("/api/gateway/config?" + query)[0], 400)
                self.assertEqual(self.call("/api/clean?" + query, "POST", headers=self.write_headers())[0], 400)
            self.assertEqual(self.call("/api/demo", "POST", headers=self.write_headers())[0], 410)
            self.assertEqual(self.call("/api/demo")[0], 404)
        helper.assert_not_called()
        self.assertFalse(demo.exists())
        after = self.call("/api/summary?source=local")[1]
        self.assertEqual((after["qa_pair"], after["customer_count"]), (before["qa_pair"], before["customer_count"]))


    def test_group_directory_pages_and_matches_shared_filters(self):
        self.seed()
        path = "/api/groups?limit=1&entity_sort=unanswered&entity_q=" + quote("售")
        status, first, _ = self.call(path)
        self.assertEqual(status, 200)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["totals"], {"questions": 5, "unanswered": 3})
        self.assertEqual(first["items"][0]["group_name"], "售后")
        self.assertEqual(first["items"][0]["last_seen"], "2026-11-02")
        second = self.call(path + "&offset=1")[1]
        self.assertEqual(second["totals"], first["totals"])
        self.assertEqual(second["items"][0]["group_name"], "售前")
        legacy = self.call("/api/groups")[1]
        self.assertEqual(set(legacy), {"items"})
        self.assertNotIn("last_seen", legacy["items"][0])

        scope = "start=2026-11-01&end=2026-11-01&group=" + quote("售前") + "&staff=" + quote("小林")
        page = self.call("/api/groups?entity_sort=recent&" + scope)[1]
        unanswered = self.call("/api/pairs?replied=unanswered&" + scope)[1]
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["unanswered"], unanswered["total"])
        self.assertEqual(page["totals"]["unanswered"], unanswered["total"])
        self.assertEqual(self.call("/api/groups?entity_sort=invalid")[0], 400)

    def test_customer_directory_searches_all_public_groups_and_keeps_legacy_cap(self):
        phone = "13812345678"
        stamp = int(time.time() * 1000)
        rows = [
            (f"s{n}", 1, f"fixture-{n:03d}", "合成客户", "", "", stamp, "如何设置", "")
            for n in range(501)
        ]
        rows.extend(
            (f"alias{n}", 1, phone, "", name, "合成客服", stamp - 5000 + n, "如何设置", answer)
            for n, (name, answer) in enumerate([
                ("群 A", "按说明设置"), ("群 B", "   "), ("群 C", ""), (f"群 D {phone}", ""),
            ])
        )
        with app.get_db(self.cfg).tx() as conn:
            conn.executemany(
                "INSERT INTO qa_pair(session_id,idx,customer_id,customer_name,group_name,staff_name,"
                "asked_at,question,answer) VALUES (?,?,?,?,?,?,?,?,?)", rows,
            )
        status, result, _ = self.call("/api/customers?entity_q=" + quote("群 D"))
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["totals"], {"questions": 4, "unanswered": 3})
        item = result["items"][0]
        self.assertEqual(item["group_names"], ["群 A", "群 B", "群 C"])
        self.assertEqual(item["group_count"], 4)
        self.assertNotIn(phone, str(result))
        self.assertEqual(self.call("/api/customers?entity_q=" + quote("匿名客户 30778"))[1], result)
        self.assertEqual(self.call("/api/customers?entity_q=" + phone)[1]["total"], 0)
        self.assertEqual(self.call("/api/customers?entity_q=FIXTURE-500")[1]["total"], 1)
        scoped = self.call("/api/pairs?replied=unanswered&customer=" + quote(item["customer_id"]))[1]
        self.assertEqual(item["unanswered"], scoped["total"])

        recent = self.call("/api/customers?entity_sort=recent&limit=1")[1]
        self.assertEqual(recent["items"][0]["customer_id"], "fixture-000")
        tail = self.call("/api/customers?limit=200&offset=500")[1]
        self.assertEqual((tail["total"], tail["offset"], tail["limit"]), (502, 500, 100))
        self.assertEqual([row["customer_id"] for row in tail["items"]], ["fixture-499", "fixture-500"])
        legacy = self.call("/api/customers")[1]
        self.assertEqual(set(legacy), {"items"})
        self.assertEqual(len(legacy["items"]), 500)
        self.assertNotIn("group_names", legacy["items"][0])


    def seed_rule_clusters(self):
        stamp = int(time.mktime(time.strptime("2026-11-01 10:00", "%Y-%m-%d %H:%M")) * 1000)
        phone = "13812345678"
        long_variant = "说明" * 120 + f" {phone} 合成尾词UniqueVariant"
        rows = [
            (12, "售前", "小林", stamp + n, question, "" if n == 0 else "按说明操作")
            for n, question in enumerate([
                "绑定店铺", "绑定店铺", "新增店铺", "添加店铺", "如何绑店", "店铺接入", long_variant,
            ])
        ]
        rows.extend([
            (21, "售前", "小林", stamp + 7, "如何设置", '{"type":"image","url":"https://example.com/image.jpg"}'),
            (21, "售前", "小林", stamp + 8, "怎么设置", ""),
            (25, "售前", "小林", stamp + 2, "如何退款", "按说明操作"),
            (23, "售前", "小林", stamp + 86400000, "次日咨询", ""),
            (24, "售后", "小林", stamp + 9, "不同客户群", ""),
            (12, "售前", "小陈", stamp + 10, "不同接待客服", ""),
            (0, "售前", "小林", stamp + 11, "未归簇的咨询", ""),
        ])
        with app.get_db(self.cfg).tx() as conn:
            conn.executemany(
                "INSERT INTO qa_pair(session_id,idx,cluster_id,group_name,staff_name,"
                "asked_at,question,answer) VALUES (?,1,?,?,?,?,?,?)",
                [(f"cluster-fixture-{index}", *row) for index, row in enumerate(rows)],
            )
        return "start=2026-11-01&end=2026-11-01&group=" + quote("售前") + "&staff=" + quote("小林")

    def test_rule_cluster_directory_pages_sort_and_match_pairs(self):
        scope = self.seed_rule_clusters()
        path = "/api/clusters?" + scope
        status, first, _ = self.call(path + "&limit=1&entity_sort=questions&cluster_members=repeated")
        self.assertEqual(status, 200)
        self.assertEqual((first["total"], first["offset"], first["limit"]), (2, 0, 1))
        self.assertEqual(first["totals"], {"questions": 9, "unanswered": 3})
        self.assertEqual(first["items"][0]["cluster_id"], 12)
        self.assertEqual(first["items"][0]["share"], 63.6)
        second = self.call(path + "&limit=1&offset=1&cluster_members=repeated")[1]
        self.assertEqual(second["totals"], first["totals"])
        self.assertEqual(second["items"][0]["cluster_id"], 21)
        self.assertEqual(self.call(path + "&entity_sort=unanswered")[1]["items"][0]["cluster_id"], 21)
        self.assertEqual(self.call(path + "&entity_sort=recent")[1]["items"][0]["cluster_id"], 21)
        single = self.call(path + "&cluster_members=single")[1]
        self.assertEqual(single["total"], 1)
        self.assertEqual(single["items"][0]["cluster_id"], 25)
        self.assertEqual(single["totals"], {"questions": 1, "unanswered": 0})
        for item in [first["items"][0], second["items"][0], single["items"][0]]:
            pairs_path = f"/api/pairs?{scope}&cluster={item['cluster_id']}"
            self.assertEqual(self.call(pairs_path)[1]["total"], item["count"])
            self.assertEqual(self.call(pairs_path + "&replied=unanswered")[1]["total"], item["unanswered"])
        legacy = self.call(path + "&top=1")[1]
        self.assertEqual(set(legacy), {"items"})
        self.assertEqual([item["cluster_id"] for item in legacy["items"]], [12, 21, 25])
        self.assertEqual(self.call(path + "&cluster_members=invalid")[0], 400)
        self.assertEqual(self.call(path + "&entity_sort=invalid")[0], 400)

    def test_rule_cluster_directory_searches_masked_variants_and_exact_ids(self):
        scope = self.seed_rule_clusters()
        path = "/api/clusters?" + scope + "&entity_q="
        status, result, _ = self.call(path + "uniquevariant")
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["totals"], {"questions": 7, "unanswered": 1})
        item = result["items"][0]
        self.assertEqual(item["question"], "绑定店铺")
        self.assertEqual(item["variants"], 6)
        self.assertTrue(all("UniqueVariant" not in sample for sample in item["samples"]))
        self.assertIn("138****5678 合成尾词UniqueVariant", item["matched_sample"])
        self.assertNotIn("13812345678", str(result))
        for query in ("#12", "簇12", "问题簇 12"):
            exact = self.call(path + quote(query))[1]
            self.assertEqual([row["cluster_id"] for row in exact["items"]], [12])
            self.assertNotIn("matched_sample", exact["items"][0])
        for query in ("#1", "12", "13812345678"):
            self.assertEqual(self.call(path + quote(query))[1]["total"], 0)


    def test_case_image_auth_exact_bytes_download_and_no_database_lock(self):
        self.server.admin_key = "image-fixture-admin"
        auth = {"X-Admin-Key": "image-fixture-admin", "Cookie": "fixture=do-not-forward"}
        url = "https://ysf.nosdn.127.net/preview%20sample.png?token=private-image-signature"
        path = "/api/detail/image?url=" + quote(url, safe="")
        image = b"\x89PNG\r\n\x1a\n" + b"synthetic-png-bytes"
        entered, release = threading.Event(), threading.Event()

        def upstream(request, **options):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            response = BytesIO(image)
            response.status = 200
            response.headers = Message()
            response.headers["Content-Type"] = "image/png"
            response.headers["Content-Length"] = str(len(image))
            return response

        with patch.object(case_media._IMAGE_OPENER, "open", side_effect=upstream) as fetch, \
                patch("src.web.server.IMAGE_SLOTS", threading.BoundedSemaphore(1)), \
                patch("src.web.server.log.debug") as logs:
            self.assertEqual(self.call(path)[0], 401)
            fetch.assert_not_called()
            with ThreadPoolExecutor(max_workers=2) as workers:
                pending = workers.submit(self.call, path, headers=auth)
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertEqual(workers.submit(self.call, "/api/meta", headers=auth).result(2)[0], 200)
                    self.assertEqual(self.call(path, headers=auth)[0], 503)
                finally:
                    release.set()
                status, data, headers = pending.result(2)
            self.assertEqual((status, data), (200, image))
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertEqual(headers["Content-Disposition"], 'inline; filename="preview_sample.png"')
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertEqual(fetch.call_args.args[0].full_url, url)
            self.assertEqual(fetch.call_args.args[0].header_items(), [("Accept-encoding", "identity")])
            status, data, headers = self.call(path + "&download=1", headers=auth)
            self.assertEqual((status, data), (200, image))
            self.assertEqual(headers["Content-Disposition"], 'attachment; filename="preview_sample.png"')
            self.assertNotIn("private-image-signature", str(logs.call_args_list))

    def test_case_image_rejects_invalid_parameters_redirects_and_bad_content(self):
        path = "/api/detail/image?url="
        url = "https://ysf.nosdn.127.net/photo.png"
        valid = path + quote(url, safe="")
        image = b"\x89PNG\r\n\x1a\n" + b"synthetic-png-bytes"

        def response(content, mime="image/png", length=None, encoding=None):
            stream = BytesIO(content)
            stream.status = 200
            stream.headers = Message()
            stream.headers["Content-Type"] = mime
            if length is not None:
                stream.headers["Content-Length"] = str(length)
            if encoding is not None:
                stream.headers["Content-Encoding"] = encoding
            return stream

        with patch.object(case_media._IMAGE_OPENER, "open") as fetch:
            for bad in ("http://ysf.nosdn.127.net/photo.png", "https://ysf.nosdn.127.net.evil.test/photo.png",
                        "https://user@ysf.nosdn.127.net/photo.png", "https://ysf.nosdn.127.net:443/photo.png",
                        "https://ysf.nosdn.127.net/photo.svg", "https://ysf.nosdn.127.net/photo.png#fragment"):
                self.assertEqual(self.call(path + quote(bad, safe=""))[0], 400)
            for bad in (path, valid + "&download=bad", valid + "&url=" + quote(url, safe="")):
                self.assertEqual(self.call(bad)[0], 400)
            fetch.assert_not_called()
            mislabeled = path + quote("https://ysf.nosdn.127.net/photo.jpg", safe="") + "&download=1"
            fetch.return_value = response(image, mime="image/jpeg")
            status, data, headers = self.call(mislabeled)
            self.assertEqual((status, data), (200, image))
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertEqual(headers["Content-Disposition"], 'attachment; filename="photo.png"')
            for upstream in (response(image, mime="text/html"), response(b"<svg/>"),
                             response(image, length=len(image) + 1), response(image, encoding="gzip")):
                fetch.return_value = upstream
                self.assertEqual(self.call(valid)[0], 502)
            fetch.return_value = response(image, length=case_media._IMAGE_MAX_BYTES + 1)
            self.assertEqual(self.call(valid)[0], 413)
            with patch.object(case_media, "_IMAGE_MAX_BYTES", len(image) - 1):
                fetch.return_value = response(image)
                self.assertEqual(self.call(valid)[0], 413)
            fetch.side_effect = TimeoutError
            self.assertEqual(self.call(valid)[0], 504)

        headers = Message()
        headers["Location"] = "https://ysf.nosdn.127.net/redirected.png"
        redirected = addinfourl(BytesIO(), headers, url, 302)
        redirected.msg = "Found"
        with patch("urllib.request.HTTPSHandler.https_open", return_value=redirected) as fetch:
            self.assertEqual(self.call(valid)[0], 502)
            fetch.assert_called_once()


class TestAdminKey(LocalFixture):
    """单管理员密钥：设了 ADMIN_KEY 之后，整站必须先过密钥。"""

    KEY = "s3cret-admin-key"

    def setUp(self):
        super().setUp()
        self.server = DashboardServer(("127.0.0.1", 0), self.cfg, admin_key=self.KEY)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def call(self, path, method="GET", body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
            return response.status, raw, dict(response.getheaders())
        finally:
            conn.close()

    def test_no_key_is_rejected(self):
        status, body, _ = self.call("/")
        self.assertEqual(status, 401)
        self.assertIn("管理员密钥".encode("utf-8"), body)
        self.assertEqual(self.call("/app.js")[0], 401)
        self.assertEqual(self.call("/api/summary")[0], 401)

    def test_login_template_and_canvas_script_are_available_before_authentication(self):
        status, body, headers = self.call("/login")
        self.assertEqual(status, 200)
        self.assertIn("汇沙成海".encode("utf-8"), body)
        self.assertIn(b'method="post" action="/login"', body)
        self.assertIn(b'name="key" type="password"', body)
        self.assertIn(b'src="/login-landscape.js"', body)
        self.assertNotIn(b"<!--LOGIN_ERROR-->", body)
        self.assertNotIn(self.KEY.encode(), body)
        script_policy = next(part for part in headers["Content-Security-Policy"].split(";")
                             if part.strip().startswith("script-src "))
        self.assertEqual(script_policy.strip(), "script-src 'self'")
        status, animation, headers = self.call("/login-landscape.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn(b"requestAnimationFrame", animation)
        self.assertNotIn(b"preventDefault", animation)
        self.assertEqual(self.call("/login-landscape.js/extra")[0], 401)

    def test_query_key_sets_cookie_and_redirects(self):
        status, _, headers = self.call("/?key=" + self.KEY)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertIn("qy_admin=" + self.KEY, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("Secure", cookie)  # 本机 http 不加 Secure
        # 拿到 Cookie 之后就能正常访问
        self.assertEqual(self.call("/", headers={"Cookie": "qy_admin=" + self.KEY})[0], 200)

    def test_header_key_works(self):
        status, _, _ = self.call("/api/summary", headers={"X-Admin-Key": self.KEY})
        self.assertEqual(status, 200)

    def test_login_form(self):
        body = ("key=" + self.KEY).encode()
        status, _, headers = self.call("/login", "POST", body,
                                       {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 302)
        self.assertIn("qy_admin=" + self.KEY, headers["Set-Cookie"])

        status, raw, _ = self.call("/login", "POST", b"key=wrong",
                                   {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 401)
        self.assertIn("密钥不正确".encode("utf-8"), raw)

    def test_logout_clears_cookie(self):
        status, _, headers = self.call("/logout")
        self.assertEqual(status, 302)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_wrong_key_is_rate_limited(self):
        """连续失败会限速；但正确密钥永远放行，不能把管理员自己锁在外面。"""
        for _ in range(10):
            self.call("/login", "POST", b"key=bad",
                      {"Content-Type": "application/x-www-form-urlencoded"})
        status, _, _ = self.call("/login", "POST", b"key=bad",
                                 {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 429)
        status, _, headers = self.call("/login", "POST", b"key=" + self.KEY.encode(),
                                       {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 302)
