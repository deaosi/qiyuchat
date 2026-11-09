"""本机 HTTP 边界与完整导入闭环；只绑定临时回环端口。"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from tests.test_local import LocalFixture, make_csv
from src import app
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

    def test_input_errors_have_400(self):
        for path in ("/api/pairs?limit=bad", "/api/summary?start=bad", "/api/meta?source=unknown",
                     "/api/pairs?replied=resolved", "/api/trend?days=-1"):
            self.assertEqual(self.call(path)[0], 400, path)
        self.assertEqual(self.call("/api/import/preview", "POST", b"x", self.write_headers(**{"X-Filename": "a.xls"}))[0], 400)
        self.assertEqual(self.call("/api/no-such-route")[0], 404)
        self.assertEqual(self.call("/api/clean", "POST", headers=self.write_headers(**{"Content-Length": "9000000"}))[0], 413)

    def test_sources_are_serialized_during_parallel_requests(self):
        self.seed()
        self.assertEqual(self.call("/api/demo", "POST", headers=self.write_headers())[0], 200)
        def read(i):
            source = "demo" if i % 2 else "local"
            status, data, _ = self.call("/api/summary?source=" + source)
            return status, source, data
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(read, range(24)))
        self.assertTrue(all(status == 200 for status, _, _ in results))
        for _, source, data in results:
            self.assertEqual(data["qa_pair"] == 5, source == "local")
        self.assertEqual(self.call("/api/import/preview?source=demo", "POST", b"x", self.write_headers())[0], 400)


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
