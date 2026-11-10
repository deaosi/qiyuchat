"""Gateway settings: synthetic secrets, private .env fixtures, no real requests."""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from src import config, gateway as G, voc, voc_model
from src.http import HttpError
from src.web.server import DB_LOCK, DashboardServer
from tests.test_local import LocalFixture

PUBLIC_IP = "93.184.216.34"


class GatewayFixture(LocalFixture):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, base_url="https://gateway.example/v1",
                           api_key="synthetic-existing-key", model_main="old-model", model_fast="old-fast",
                           timeout=90, batch_size=2, dry_run=False, thinking="disabled", json_mode=False,
                           extra_headers=(("X-Tenant", "old-provider-private-value"),)))
        self.env = Path(self.temp.name) / "gateway.env"
        self.env.write_text("# Keep unrelated service settings\nADMIN_KEY=synthetic-admin-keep\nQIYU_APP_SECRET=synthetic-qiyu-keep\nLLM_THINKING=disabled\nLLM_JSON_MODE=0\n", encoding="utf-8")

    def body(self, **changes):
        return {"base_url": self.cfg.llm.base_url, "model": "new-model", "api_key": "", **changes}


class TestGatewaySettings(GatewayFixture):
    def test_public_response_never_contains_old_secret_or_hidden_headers(self):
        response = G.public_config(self.cfg)
        self.assertTrue(response["key_configured"] and response["configured"])
        self.assertNotIn(self.cfg.llm.api_key, json.dumps(response))
        self.assertNotIn("old-provider-private-value", json.dumps(response))
        embedded = replace(self.cfg, llm=replace(self.cfg.llm, base_url="https://user:synthetic-existing-key@gateway.example/v1", model_main=self.cfg.llm.api_key))
        self.assertNotIn(self.cfg.llm.api_key, json.dumps(G.public_config(embedded)))

    def test_save_keeps_key_and_unsubmitted_fields_and_updates_only_llm_snapshot(self):
        with patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]):
            updated = G.save(self.cfg, self.body(), self.env)
        self.assertEqual(updated.llm.api_key, self.cfg.llm.api_key)
        self.assertTrue(updated.llm.public_gateway)
        self.assertFalse(self.cfg.llm.public_gateway)
        self.assertEqual((updated.llm.model_main, updated.llm.model_fast), ("new-model", "new-model"))
        self.assertEqual((updated.llm.thinking, updated.llm.json_mode, updated.llm.extra_headers),
                         ("disabled", False, self.cfg.llm.extra_headers))
        self.assertIs(updated.runtime, self.cfg.runtime)
        self.assertIs(updated.qiyu, self.cfg.qiyu)
        self.assertEqual(self.cfg.llm.model_main, "old-model")
        saved = self.env.read_text(encoding="utf-8")
        for original in ("ADMIN_KEY=synthetic-admin-keep", "QIYU_APP_SECRET=synthetic-qiyu-keep", "LLM_THINKING=disabled", "LLM_JSON_MODE=0"):
            self.assertIn(original, saved)

    def test_changed_url_blank_key_rejected_before_dns_write_or_request(self):
        before = self.env.read_bytes()
        with patch("src.gateway.resolve_public") as dns, patch("src.gateway.fetch_models") as fetch:
            for operation, body in ((G.save, self.body(base_url="https://other.example/v1")),
                                    (G.models, {"base_url": "https://other.example/v1", "api_key": ""})):
                with self.assertRaises(G.GatewayError):
                    operation(self.cfg, body, self.env) if operation is G.save else operation(self.cfg, body)
        dns.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(self.env.read_bytes(), before)

    def test_new_key_targets_new_gateway_and_clears_old_private_headers(self):
        with patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]):
            updated = G.save(self.cfg, self.body(base_url="https://other.example/v1/", api_key="synthetic-new-key"), self.env)
        self.assertEqual((updated.llm.base_url, updated.llm.api_key, updated.llm.extra_headers),
                         ("https://other.example/v1", "synthetic-new-key", ()))
        with patch("src.gateway.fetch_models", return_value={"items": [], "total": 0}) as fetch:
            G.models(self.cfg, {"base_url": "https://other.example/v1", "api_key": "synthetic-new-key"})
        fetch.assert_called_once_with("https://other.example/v1", "synthetic-new-key")

    def test_models_same_canonical_base_may_keep_old_key_without_returning_it(self):
        with patch("src.gateway.fetch_models", return_value={"items": [{"id": "new-model"}], "total": 1}) as fetch:
            result = G.models(self.cfg, {"base_url": "https://GATEWAY.example:443/v1/", "api_key": ""})
        fetch.assert_called_once_with(self.cfg.llm.base_url, self.cfg.llm.api_key)
        self.assertNotIn(self.cfg.llm.api_key, json.dumps(result))

    def test_env_values_roundtrip_escaped_key_and_future_process_settings(self):
        key = 'synthetic-"quoted"-\\-key-$literal'
        with patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]):
            updated = G.save(self.cfg, self.body(api_key=key, timeout=120, batch_size=4), self.env)
        with patch.dict(os.environ, {}, clear=True):
            config._load_env_file(self.env)
            future = config.load_settings()
        self.assertEqual((future.llm.api_key, future.llm.model_main, future.llm.timeout, future.llm.batch_size),
                         (key, updated.llm.model_main, 120, 4))
        self.assertFalse(future.llm.dry_run)
        self.assertTrue(future.llm.public_gateway)
        self.assertEqual(future.runtime.admin_key, "synthetic-admin-keep")

    def test_atomic_failure_keeps_old_env_and_removes_temporary_secret_file(self):
        before = self.env.read_bytes()
        with (patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]),
              patch("src.gateway.os.replace", side_effect=OSError("synthetic-secret-path"))):
            with self.assertRaises(G.GatewayError) as caught:
                G.save(self.cfg, self.body(api_key="new-key"), self.env)
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIn("synthetic-secret-path", str(caught.exception))
        self.assertEqual(self.env.read_bytes(), before)
        self.assertFalse(list(self.env.parent.glob(".gateway-*")))

    def test_validation_rejects_private_urls_credentials_queries_and_control_input(self):
        for url in ("http://gateway.example/v1", "https://127.0.0.1/v1", "https://[::1]/v1",
                    "https://169.254.169.254/v1", "https://100.64.0.1/v1", "https://224.0.0.1/v1",
                    "https://localhost/v1", "https://metadata.internal/v1", "https://2130706433/v1",
                    "https://user:secret@gateway.example/v1", "https://gateway.example/v1?key=secret",
                    "https://gateway.example/v1#fragment", "https://gateway.example/\npath"):
            with self.subTest(url=url), self.assertRaises(G.GatewayError):
                G.normalize_base(url)
        for changes in ({"model": "bad\x7fmodel"}, {"api_key": "bad\r\nheader"}, {"timeout": True},
                        {"timeout": 601}, {"batch_size": 17}, {"extra": "not-allowed"}):
            with patch("src.gateway.resolve_public") as dns:
                with self.assertRaises(G.GatewayError):
                    G.save(self.cfg, self.body(**changes), self.env)
            dns.assert_not_called()
        no_key = replace(self.cfg, llm=replace(self.cfg.llm, api_key=""))
        with self.assertRaises(G.GatewayError):
            G.save(no_key, self.body(), self.env)

    def test_mixed_private_dns_answer_is_rejected_before_connection(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("src.gateway.socket.getaddrinfo", return_value=answers), patch("src.gateway._PinnedHTTPSConnection") as connect:
            with self.assertRaises(G.GatewayError):
                G.fetch_models(self.cfg.llm.base_url, self.cfg.llm.api_key)
        connect.assert_not_called()

    def test_models_request_is_pinned_get_and_returns_only_safe_ids(self):
        body = {"data": [{"id": "model-A", "private": "hidden"}, {"id": "model-A"},
                         {"id": self.cfg.llm.api_key}, {"id": "bad\x7fmodel"}, {"id": "model-B"}]}
        response = Mock(status=200)
        response.read.return_value = json.dumps(body).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        with (patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]),
              patch("src.gateway._PinnedHTTPSConnection", return_value=connection) as factory):
            result = G.fetch_models(self.cfg.llm.base_url, self.cfg.llm.api_key)
        factory.assert_called_once_with("gateway.example", 443, PUBLIC_IP)
        self.assertEqual(connection.request.call_args.args, ("GET", "/v1/models"))
        self.assertEqual(result, {"items": [{"id": "model-A"}, {"id": "model-B"}], "total": 2})
        self.assertNotIn(self.cfg.llm.api_key, json.dumps(result))

    def test_redirect_error_body_malformed_and_large_response_never_leak_secret(self):
        for status, raw in ((302, b"secret-location"), (401, b"synthetic-existing-key"),
                            (500, b"upstream-secret"), (200, b"not-json synthetic-existing-key"),
                            (200, b"x" * (G.MAX_MODELS_BYTES + 1))):
            response = Mock(status=status)
            response.read.return_value = raw
            connection = Mock()
            connection.getresponse.return_value = response
            with (patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]),
                  patch("src.gateway._PinnedHTTPSConnection", return_value=connection)):
                with self.assertRaises(G.GatewayError) as caught:
                    G.fetch_models(self.cfg.llm.base_url, self.cfg.llm.api_key)
            self.assertNotIn(self.cfg.llm.api_key, str(caught.exception))
            self.assertNotIn("upstream-secret", str(caught.exception))
            self.assertEqual(connection.request.call_count, 1)
            self.assertNotEqual(caught.exception.status, 401)

    def test_tls_uses_pinned_ip_and_original_hostname_for_certificate_checks(self):
        connection = G._PinnedHTTPSConnection("gateway.example", 443, PUBLIC_IP)
        context, raw = Mock(), Mock()
        connection._context = context
        with patch("src.gateway.socket.create_connection", return_value=raw) as create:
            connection.connect()
        create.assert_called_once_with((PUBLIC_IP, 443), timeout=10)
        context.wrap_socket.assert_called_once_with(raw, server_hostname="gateway.example")

    def test_public_inference_uses_pinned_post_and_retains_provider_parameters(self):
        llm = replace(self.cfg.llm, public_gateway=True)
        response = Mock(status=200)
        response.read.return_value = b'{"choices": []}'
        connection = Mock()
        connection.getresponse.return_value = response
        with (patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]),
              patch("src.gateway._PinnedHTTPSConnection", return_value=connection) as factory,
              patch("src.voc_model.request") as legacy):
            result = voc_model._gateway(llm, {"model": "selected-model", "messages": []})
        self.assertEqual(result, {"choices": []})
        legacy.assert_not_called()
        factory.assert_called_once_with("gateway.example", 443, PUBLIC_IP, timeout=90)
        request = connection.request.call_args
        self.assertEqual(request.args, ("POST", "/v1/chat/completions"))
        sent = json.loads(request.kwargs["body"])
        self.assertEqual(sent["thinking"], {"type": "disabled"})
        self.assertEqual(request.kwargs["headers"]["X-Tenant"], "old-provider-private-value")

    def test_public_inference_policy_failures_are_nonretryable_and_never_redirect(self):
        llm = replace(self.cfg.llm, public_gateway=True)
        response = Mock(status=302)
        connection = Mock()
        connection.getresponse.return_value = response
        with (patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]),
              patch("src.gateway._PinnedHTTPSConnection", return_value=connection) as factory):
            with self.assertRaises(voc_model.VocModelError) as caught:
                voc_model._gateway(llm, {"messages": []})
        self.assertFalse(caught.exception.retryable)
        self.assertFalse(voc._can_split_batch(caught.exception))
        self.assertEqual((factory.call_count, connection.request.call_count), (1, 1))
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("src.gateway.socket.getaddrinfo", return_value=answers), patch("src.gateway._PinnedHTTPSConnection") as connect:
            with self.assertRaises(voc_model.VocModelError) as caught:
                voc_model._gateway(llm, {"messages": []})
        self.assertFalse(caught.exception.retryable)
        connect.assert_not_called()

    def test_public_inference_json_mode_fallback_keeps_only_safe_error_hint(self):
        llm = replace(self.cfg.llm, public_gateway=True)
        bad, good = Mock(status=400), Mock(status=200)
        bad.read.return_value = b'response_format unsupported synthetic-existing-key'
        good.read.return_value = b'{"choices": []}'
        first, second = Mock(), Mock()
        first.getresponse.return_value = bad
        second.getresponse.return_value = good
        payload = {"messages": [], "response_format": {"type": "json_object"}}
        with (patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]),
              patch("src.gateway._PinnedHTTPSConnection", side_effect=[first, second])):
            self.assertEqual(voc_model._gateway(llm, payload), {"choices": []})
        self.assertIn("response_format", json.loads(first.request.call_args.kwargs["body"]))
        self.assertNotIn("response_format", json.loads(second.request.call_args.kwargs["body"]))
        self.assertIn("response_format", payload)
        unauthorized = Mock(status=401)
        unauthorized.read.return_value = b'provider echoed synthetic-existing-key'
        first.getresponse.return_value = unauthorized
        with (patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]),
              patch("src.gateway._PinnedHTTPSConnection", return_value=first)):
            with self.assertRaises(HttpError) as caught:
                G.model_request("POST", self.cfg.llm.base_url + "/chat/completions", body={}, headers={})
        self.assertEqual((caught.exception.status, caught.exception.body, caught.exception.url), (401, "", ""))
        self.assertNotIn(self.cfg.llm.api_key, str(caught.exception))

    def test_legacy_local_gateway_keeps_existing_transport(self):
        llm = replace(self.cfg.llm, base_url="http://127.0.0.1:11434/v1", public_gateway=False)
        with patch("src.voc_model.request", return_value={"choices": []}) as legacy, patch("src.voc_model.public_request") as controlled:
            self.assertEqual(voc_model._gateway(llm, {"messages": []}), {"choices": []})
        legacy.assert_called_once()
        controlled.assert_not_called()


class TestGatewayHttp(GatewayFixture):
    ADMIN = "synthetic-admin-session"

    def setUp(self):
        super().setUp()
        self.server = DashboardServer(("127.0.0.1", 0), self.cfg, admin_key=self.ADMIN, gateway_env_path=self.env)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def call(self, name="config", body=None, authenticated=True, same_origin=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Cookie"] = "qy_admin=" + self.ADMIN
        if same_origin:
            headers.update({"Origin": self.origin, "X-Local-Request": "1"})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request("POST" if body is not None else "GET", "/api/gateway/" + name,
                               body=json.dumps(body).encode() if body is not None else b"", headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_original_cookie_and_origin_gate_protect_gateway_routes(self):
        self.assertEqual(self.call(authenticated=False)[0], 401)
        self.assertEqual(self.call(body=self.body(), authenticated=False)[0], 401)
        self.assertEqual(self.call(body=self.body(), same_origin=False)[0], 403)
        code, data = self.call()
        self.assertEqual(code, 200)
        self.assertNotIn(self.cfg.llm.api_key, json.dumps(data))

    def test_save_updates_following_requests_but_preserves_captured_job_settings(self):
        self.seed()
        snapshot = self.server.cfg
        job = voc.start_job(snapshot, {}, {"limit": 1}, launch=False)["job"]
        with patch("src.gateway.resolve_public", return_value=[PUBLIC_IP]):
            code, data = self.call(body=self.body(timeout=100, batch_size=3))
        self.assertEqual((code, data["model"], self.call()[1]["model"]), (200, "new-model", "new-model"))
        self.assertEqual((snapshot.llm.model_main, snapshot.llm.timeout), ("old-model", 90))
        self.assertEqual((self.server.cfg.runtime, self.server.cfg.qiyu), (snapshot.runtime, snapshot.qiyu))
        observed = []
        def classify(llm, pairs, topics):
            observed.append((llm.model_main, llm.api_key, llm.timeout))
            return {"results": [{"qa_id": p["qa_id"], "excluded": True, "reason": "合成测试排除", "intents": []} for p in pairs], "usage_tokens": 1}
        with patch("src.voc.analyze_batch", side_effect=classify):
            voc._worker(snapshot, job["id"])
        self.assertEqual(observed, [("old-model", snapshot.llm.api_key, 90)])
        following = voc.start_job(self.server.cfg, {}, {"limit": 1}, launch=False)["job"]
        self.assertEqual(following["model"], "new-model")

    def test_models_network_has_no_global_db_lock_and_rejects_old_key_for_new_target(self):
        probes = []
        def fetch(base, key):
            def probe():
                acquired = DB_LOCK.acquire(timeout=.2)
                probes.append(acquired)
                if acquired:
                    DB_LOCK.release()
            thread = threading.Thread(target=probe, daemon=True)
            thread.start()
            thread.join(1)
            return {"items": [{"id": "listed-model"}], "total": 1}
        with patch("src.gateway.fetch_models", side_effect=fetch) as mock:
            self.assertEqual(self.call("models", {"base_url": self.cfg.llm.base_url, "api_key": ""})[0], 200)
            self.assertEqual(self.call("models", {"base_url": "https://other.example/v1", "api_key": ""})[0], 400)
        self.assertEqual(probes, [True])
        self.assertEqual(mock.call_count, 1)

    def test_unexpected_failure_never_echoes_or_logs_secret(self):
        with (patch("src.gateway.fetch_models", side_effect=RuntimeError("synthetic-existing-key provider-body")),
              self.assertLogs("web", level="WARNING") as logs):
            code, data = self.call("models", {"base_url": self.cfg.llm.base_url, "api_key": ""})
        self.assertEqual(code, 500)
        self.assertNotIn(self.cfg.llm.api_key, json.dumps(data) + str(logs.output))
        self.assertNotIn("provider-body", json.dumps(data) + str(logs.output))
