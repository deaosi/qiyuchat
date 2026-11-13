"""本地会话工作台：静态页面、统一查询和显式本地写操作。"""

from __future__ import annotations

import hashlib
import hmac
import gzip
import json
import mimetypes
import re
import secrets
import socket
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..config import Settings, settings
from .. import voc, knowledge, gateway, voc_service, voc_assistant
from .. import voc_feishu_content, voc_feishu_sync
from ..model_protocol import ModelProtocolError
from ..local import MAX_FILE_BYTES, clean_local, import_file, parse_import, template_bytes
from ..log import get
from . import admin_keys, api, case_media, guest_evidence

log = get("web")
STATIC = Path(__file__).parent / "static"
LOGIN_PAGE = Path(__file__).with_name("login.html")
LOGIN_LANDSCAPE = Path(__file__).with_name("login-landscape.js")
PANEL_PAGE = Path(__file__).with_name("panel.html")
PANEL_SCRIPT = Path(__file__).with_name("panel.js")
PANEL_PATHS = frozenset(("/panel", "/panel.js", "/api/panel/keys"))
SESSION_AGE = 30 * 86400
MANAGED_SESSION = re.compile(r"s1\.([a-f0-9]{32})\.([0-9]{1,12})\.([a-f0-9]{32})\.([a-f0-9]{64})")
# app.get_db() 切库时会关闭旧连接；查询和写入必须持有同一把锁。
DB_LOCK = threading.RLock()
IMAGE_SLOTS = threading.BoundedSemaphore(3)
GUEST_PREFIX = "/guest/evidence/"
GUEST_IMAGE_ROUTE = re.compile(
    rf"{GUEST_PREFIX}({guest_evidence.TOKEN_PATTERN})/images/({guest_evidence.IMAGE_PATTERN})"
)
GUEST_ASSET_PREFIX = "/guest/ui/assets/"
GUEST_ASSET_NAME = re.compile(r"[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8}\.(?:js|css|woff2?)")
GUEST_ASSET_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                     ".woff2": "font/woff2", ".woff": "font/woff"}
GUEST_DATA_MARKER = b"<!--VOC_GUEST_DATA-->"
GUEST_SLOTS = threading.BoundedSemaphore(6)
GUEST_IMAGE_SLOTS = threading.BoundedSemaphore(3)
GUEST_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'")
GUEST_APP_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "font-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
GUEST_HEADERS = {"X-Robots-Tag": "noindex, nofollow, noarchive",
                 "Cross-Origin-Resource-Policy": "same-origin"}
GUEST_UNAVAILABLE = (
    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<title>链接不可用 · 语海Voc</title><main><h1>此访客链接已失效或不存在</h1>'
    '<p>请联系分享者获取新的链接。</p></main></html>'
).encode("utf-8")


def _int(qs: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int((qs.get(key) or [default])[0])
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是整数") from None


def _str(qs: dict[str, list[str]], key: str, default: str = "") -> str:
    return (qs.get(key) or [default])[0]


def _full_session(qs: dict[str, list[str]]) -> bool:
    context = qs.get("context")
    if context is None:
        return False
    if context != ["session"]:
        raise ValueError("会话上下文参数无效")
    return True


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], cfg: Settings,
                 allowed_hosts: tuple[str, ...] = (),
                 allowed_origins: tuple[str, ...] = (),
                 admin_key: str = "", gateway_env_path: str | Path | None = None):
        self.cfg = cfg
        self.gateway_env_path = gateway_env_path
        voc.startup(cfg)
        knowledge.startup(cfg)
        # 反代部署时额外放行的 Host / Origin；默认空 = 只认本机回环。
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.allowed_origins = frozenset(o.lower() for o in allowed_origins)
        # The configured permanent key alone can manage other credentials.
        self.admin_key = admin_key or ""
        self.key_store = admin_keys.KeyStore(Path(cfg.runtime.state_dir) / "voc-admin-keys.db")
        # 失败计数：{(ip): [时间戳, ...]}，用于简单限速
        self.fails: dict[str, list[float]] = {}
        self.fails_lock = threading.Lock()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "local-conversations/2.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def _send(self, body: bytes, ctype: str, status: int = 200,
              filename: str = "", extra: dict[str, str] | None = None,
              *, policy: str | None = None, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", policy or "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://ysf.nosdn.127.net; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        extra: dict[str, str] = {}
        # 大响应压一下：clusters 这类接口原始 1 MB，压完 100 KB 出头
        if len(body) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            body = gzip.compress(body, 6)
            extra["Content-Encoding"] = "gzip"
        self._send(body, "application/json; charset=utf-8", status, extra=extra)

    def _file(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_relative_to(STATIC.resolve()) or not path.is_file():
            self._json({"error": "文件不存在"}, 404)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(path.read_bytes(), ctype)

    def _host_ok(self) -> bool:
        try:
            host = urlparse("http://" + self.headers.get("Host", ""))
            if host.username or host.password or host.path or host.query or host.fragment:
                return False
            # 反代放行名单：只比 hostname，不比端口（外层可能是 443/80）
            if host.hostname and host.hostname.lower() in getattr(self.server, "allowed_hosts", ()):
                return True
            bound = self.server.server_address
            return (host.hostname in {"localhost", "127.0.0.1", "::1", str(bound[0])}
                    and (host.port or 80) == bound[1])
        except ValueError:
            return False

    def _write_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        host_hdr = self.headers.get("Host", "")
        expected = {"http://" + host_hdr, "https://" + host_hdr}
        expected |= set(getattr(self.server, "allowed_origins", ()))
        return (self._host_ok() and origin.lower() in {e.lower() for e in expected}
                and self.headers.get("X-Local-Request") == "1"
                and self.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin")

    def _cfg(self, qs: dict[str, list[str]]) -> Settings:
        sources = qs.get("source", ["local"])
        if "demo" in sources:
            raise ValueError("演示数据源已停用，请使用业务数据")
        if any(source != "local" for source in sources):
            raise ValueError("未知数据源")
        return self.server.cfg

    def _filters(self, qs: dict[str, list[str]]) -> dict:
        replied = _str(qs, "replied")
        if replied not in ("", "answered", "unanswered"):
            raise ValueError("未知回复状态")
        return {"days": _int(qs, "days", 0), "start": _str(qs, "start"),
                "end": _str(qs, "end"), "group": _str(qs, "group"),
                "staff": _str(qs, "staff"), "q": _str(qs, "q"),
                "replied": replied, "cluster": _int(qs, "cluster", 0),
                "customer": _str(qs, "customer")}

    def _voc_filters(self, qs: dict[str, list[str]]) -> dict:
        filters = self._filters(qs)
        support = _str(qs, "support_intent", "all")
        diagnosis = _str(qs, "diagnosis_status", "all")
        if len(qs.get("support_intent", [])) > 1 or support not in ("all", "configuration_guidance", "functional_anomaly", "unclear"):
            raise ValueError("未知知汇鸽咨询类型")
        if len(qs.get("diagnosis_status", [])) > 1 or diagnosis not in ("all", "reported", "pending", "config_cause_confirmed", "platform_limit_confirmed", "defect_confirmed", "not_applicable"):
            raise ValueError("未知问题诊断状态")
        if "focus_topic" in qs:
            topic = _str(qs, "focus_topic")
            if len(qs["focus_topic"]) != 1 or not re.fullmatch(r"t_[a-f0-9]{24}", topic):
                raise ValueError("问题范围无效。")
            filters["focus_topic"] = topic
        return {**filters,"support_intent":support,"diagnosis_status":diagnosis}

    # ---------- 管理员密钥鉴权 ----------
    COOKIE = "qy_admin"

    def _need_auth(self) -> bool:
        return bool(getattr(self.server, "admin_key", ""))

    def _permanent_key_ok(self, value: str) -> bool:
        if not value:
            return False
        expected = self.server.admin_key
        return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))

    def _sign_managed_session(self, payload: str) -> str:
        return hmac.new(self.server.admin_key.encode("utf-8"),
                        b"voc-managed-session\0" + payload.encode("ascii"), hashlib.sha256).hexdigest()

    def _managed_session(self, key_id: str) -> str:
        payload = f"s1.{key_id}.{int(time.time()) + SESSION_AGE}.{secrets.token_hex(16)}"
        return f"{payload}.{self._sign_managed_session(payload)}"

    def _managed_session_ok(self, value: str) -> bool:
        match = MANAGED_SESSION.fullmatch(value)
        if not match or int(match[2]) <= time.time():
            return False
        payload, _, signature = value.rpartition(".")
        return (hmac.compare_digest(self._sign_managed_session(payload), signature)
                and self.server.key_store.active_id(match[1]))

    def _cookie_value(self, name: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v.strip()
        return ""

    def _is_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _cookie_header(self, value: str) -> str:
        parts = [f"{self.COOKIE}={value}", "Path=/", "HttpOnly",
                 "SameSite=Lax", f"Max-Age={SESSION_AGE}"]
        if self._is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def _note_fail(self) -> None:
        ip = self.client_address[0]
        now = time.time()
        with self.server.fails_lock:
            keep = [t for t in self.server.fails.get(ip, []) if now - t < 600]
            keep.append(now)
            self.server.fails[ip] = keep

    def _rate_limited(self) -> bool:
        ip = self.client_address[0]
        now = time.time()
        with self.server.fails_lock:
            keep = [t for t in self.server.fails.get(ip, []) if now - t < 600]
            self.server.fails[ip] = keep
            return len(keep) >= 10

    def _redirect(self, location: str, cookie: str = "") -> None:
        body = b""
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _login_html(self, error: str = "", panel: bool = False) -> bytes:
        template = LOGIN_PAGE.read_text(encoding="utf-8")
        next_field = '      <input type="hidden" name="next" value="/panel">\n' if panel else ""
        return template.replace("<!--LOGIN_ERROR-->", escape(error)).replace("      <!--LOGIN_NEXT-->\n", next_field).encode("utf-8")

    def _send_login(self, error: str = "", status: int = 401, *, panel: bool = False) -> None:
        self._send(self._login_html(error, panel), "text/html; charset=utf-8", status)

    def _panel_gate(self, path: str) -> bool:
        if not self._need_auth():
            self._json({"error": "管理员密钥管理未启用。"}, 403)
            return False
        presented = self.headers.get("X-Admin-Key", "").strip() or self._cookie_value(self.COOKIE)
        if self._permanent_key_ok(presented):
            return True
        if presented and not self._managed_session_ok(presented):
            self._note_fail()
            if self._rate_limited():
                self._json({"error": "尝试次数过多，请 10 分钟后再试"}, 429)
                return False
        if path == "/panel":
            self._send_login("请使用恒密钥登录密钥管理。", panel=True)
        else:
            self._json({"error": "密钥管理需要使用恒密钥登录。"}, 403)
        return False

    def _gate(self, path: str, qs: dict[str, list[str]]) -> bool:
        """返回 True = 放行；False = 已经回过响应，调用方直接返回。"""
        if path in PANEL_PATHS:
            return self._panel_gate(path)
        if not self._need_auth():
            return True

        presented = self.headers.get("X-Admin-Key", "").strip()
        from_query = False
        if not presented and qs.get("key"):
            presented = (qs["key"][0] or "").strip()
            from_query = True
        from_cookie = not presented
        if from_cookie:
            presented = self._cookie_value(self.COOKIE)

        permanent = self._permanent_key_ok(presented)
        key_id = None
        if not permanent and not from_cookie and not from_query:
            key_id = self.server.key_store.identify(presented)
        if permanent or key_id or (from_cookie and self._managed_session_ok(presented)):
            if from_query:
                # 地址栏里的密钥换成 Cookie，避免留在历史记录/日志里
                self._redirect(path, self._cookie_header(presented))
                return False
            return True

        if presented:
            self._note_fail()
        if self._rate_limited():
            self._json({"error": "尝试次数过多，请 10 分钟后再试"}, 429)
            return False
        if path.startswith("/api/"):
            self._json({"error": "未登录或密钥不正确"}, 401)
        else:
            self._send_login("密钥不正确" if presented else "")
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json({"error": "请通过本机服务地址访问"}, 403)
            return
        try:
            u = urlparse(self.path)
            if u.path.startswith("/guest/"):
                suffix = u.params or u.query or u.fragment
                if u.path.startswith(GUEST_ASSET_PREFIX):
                    self._guest_asset(u.path, suffix)
                elif image := GUEST_IMAGE_ROUTE.fullmatch(u.path):
                    self._guest_image(image[1], image[2], suffix)
                else:
                    self._guest_page(u.path, suffix)
                return
            path, qs = unquote(u.path), parse_qs(u.query, max_num_fields=30)
            if path == "/login":
                self._send_login(status=200)
                return
            if path == "/login-landscape.js":
                self._send(LOGIN_LANDSCAPE.read_bytes(), "text/javascript; charset=utf-8")
                return
            if path == "/logout":
                self._redirect("/login", f"{self.COOKIE}=; Path=/; Max-Age=0; HttpOnly")
                return
            if not self._gate(path, qs):
                return
            if path == "/panel":
                self._send(PANEL_PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/panel.js":
                self._send(PANEL_SCRIPT.read_bytes(), "text/javascript; charset=utf-8")
            elif path == "/api/panel/keys":
                self._json({"keys": self.server.key_store.list()})
            elif path == "/api/detail/image":
                self._case_image(qs)
            elif path.startswith("/api/voc2/"):
                # VOC queries own their SQLite connections. A slow ranking or
                # knowledge request must not hold the shared dashboard DB lock.
                self._api(path[5:], qs)
            elif path.startswith("/api/"):
                with DB_LOCK:
                    self._api(path[5:], qs)
            else:
                self._file(STATIC / ("index.html" if path == "/" else path.lstrip("/")))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:
            log.exception("查询失败")
            self._json({"error": "本地查询失败，请重试并查看服务日志"}, 500)

    def _guest_asset(self, path: str, suffix: str) -> None:
        name = path.removeprefix(GUEST_ASSET_PREFIX)
        root = STATIC.resolve() / "guest" / "assets"
        asset = root / name if GUEST_ASSET_NAME.fullmatch(name) else None
        if suffix or asset is None or asset.resolve() != asset or not asset.is_file():
            self._send(GUEST_UNAVAILABLE, "text/html; charset=utf-8", 404,
                       policy=GUEST_POLICY, extra=GUEST_HEADERS)
            return
        self._send(asset.read_bytes(), GUEST_ASSET_TYPES[asset.suffix], policy=GUEST_APP_POLICY,
                   extra=GUEST_HEADERS, cache_control="public, max-age=31536000, immutable")

    def _guest_page(self, path: str, suffix: str) -> None:
        token = path[len(GUEST_PREFIX):] if path.startswith(GUEST_PREFIX) else ""
        body = GUEST_UNAVAILABLE
        policy = GUEST_POLICY
        status = 404
        if not suffix and re.fullmatch(guest_evidence.TOKEN_PATTERN, token):
            if GUEST_SLOTS.acquire(blocking=False):
                try:
                    payload = guest_evidence.resolve_payload(self.server.cfg, token)
                    if payload is not None:
                        body, policy = self._render_guest(payload)
                        status = 200
                finally:
                    GUEST_SLOTS.release()
            else:
                status = 503
        extra = {**GUEST_HEADERS, "Vary": "Accept-Encoding"}
        if len(body) > 1400 and re.search(r"(?:^|,)\s*gzip\s*(?:,|$)",
                                         self.headers.get("Accept-Encoding", ""), re.I):
            body = gzip.compress(body, 4)
            extra["Content-Encoding"] = "gzip"
        self._send(body, "text/html; charset=utf-8", status, policy=policy, extra=extra)

    def _guest_image(self, token: str, image_id: str, suffix: str) -> None:
        if suffix:
            self._send(b"", "text/plain; charset=utf-8", 404, policy=GUEST_POLICY, extra=GUEST_HEADERS)
            return
        if not GUEST_IMAGE_SLOTS.acquire(timeout=2):
            self._send(b"", "text/plain; charset=utf-8", 503, policy=GUEST_POLICY, extra=GUEST_HEADERS)
            return
        try:
            url = guest_evidence.resolve_image(self.server.cfg, token, image_id)
            if url is None:
                self._send(b"", "text/plain; charset=utf-8", 404, policy=GUEST_POLICY, extra=GUEST_HEADERS)
                return
            try:
                data, mime, _ = case_media.fetch_image(url)
            except case_media.ImageFetchError as exc:
                self._send(b"", "text/plain; charset=utf-8", exc.status, policy=GUEST_POLICY, extra=GUEST_HEADERS)
                return
            extension = case_media._IMAGE_FORMATS[mime]
            self._send(data, mime, policy=GUEST_POLICY, extra={
                **GUEST_HEADERS, "Content-Disposition": f'inline; filename="conversation-image.{extension}"',
            })
        finally:
            GUEST_IMAGE_SLOTS.release()

    @staticmethod
    def _render_guest(payload: dict) -> tuple[bytes, str]:
        try:
            template = (STATIC / "guest" / "index.html").read_bytes()
        except FileNotFoundError:
            return guest_evidence.render_payload(payload), GUEST_POLICY
        if template.count(GUEST_DATA_MARKER) != 1:
            return guest_evidence.render_payload(payload), GUEST_POLICY
        # Escape the characters that can change HTML parsing inside a script element.
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
        element = b'<script id="voc-guest-data" type="application/json">' + data.encode("utf-8") + b"</script>"
        return template.replace(GUEST_DATA_MARKER, element), GUEST_APP_POLICY

    def _case_image(self, qs: dict[str, list[str]]) -> None:
        urls = qs.get("url", [])
        download = qs.get("download", ["0"])
        if len(urls) != 1 or len(download) != 1 or download[0] not in ("0", "1"):
            self._json({"error": "图片参数无效。"}, 400)
            return
        if not IMAGE_SLOTS.acquire(blocking=False):
            self._json({"error": "图片加载繁忙，请重试。"}, 503)
            return
        try:
            try:
                data, mime, filename = case_media.fetch_image(urls[0])
            except case_media.ImageFetchError as exc:
                self._json({"error": str(exc)}, exc.status)
                return
            mode = "attachment" if download[0] == "1" else "inline"
            self._send(data, mime, extra={
                "Content-Disposition": f'{mode}; filename="{filename}"',
                "Cross-Origin-Resource-Policy": "same-origin",
            })
        finally:
            IMAGE_SLOTS.release()

    def _feishu_export(self, cfg: Settings, qs: dict[str, list[str]], body: bytes | None = None) -> None:
        try:
            kind = _str(qs, "kind")
            if len(qs.get("kind", [])) != 1:
                raise ValueError("请选择高频问题或知识 SOP。")
            filters = self._filters(qs)
            if body is not None:
                if not self._need_auth():
                    raise voc_feishu_sync.SyncError("开启语海Voc管理员鉴权后才能发布飞书。", 403)
                if len(body) > 1024 or not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("发布请求格式无效。")
                request = json.loads(body.decode("utf-8"))
                if not isinstance(request, dict) or set(request) != {"fingerprint"}:
                    raise ValueError("发布请求格式无效。")
                fingerprint = request["fingerprint"]
                if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                    raise ValueError("请先预览本期成果。")
            target = voc_feishu_sync.configured_target()
            destination = target.destination if target is not None else ""
            document = voc_feishu_content.build_document(cfg, filters, kind, _str(qs, "id"))
            snapshot = json.dumps([kind, destination, document], ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
            if body is None:
                self._json({**{key: value for key, value in document.items() if key != "records"},
                            "fingerprint": digest, "publish_configured": target is not None,
                            "destination": destination})
                return
            if not hmac.compare_digest(fingerprint, digest):
                raise voc_feishu_sync.SyncError("成果或发布目标已变化，请刷新预览后再发布。", 409)
            if not document["count"]:
                raise ValueError("当前范围没有可发布的成果。")
            if target is None:
                raise voc_feishu_sync.SyncError("飞书同步尚未配置。", 503)
            shared = voc_feishu_content.attach_guest_links(cfg, document, kind)
            self._json(voc_feishu_sync.publish(shared, kind, filters, target=target))
        except (voc.VocError, voc_feishu_sync.SyncError) as exc:
            self._json({"error": str(exc)}, exc.status)
        except (ValueError, RecursionError):
            self._json({"error": "飞书成果请求格式无效，请检查统计日期和类型。"}, 400)

    def _api(self, name: str, qs: dict[str, list[str]]) -> None:
        cfg = self._cfg(qs)
        if name == "voc2/feishu/preview":
            self._feishu_export(cfg, qs)
            return
        if name == "gateway/config":
            self._json(gateway.public_config(self.server.cfg))
            return
        filters = self._filters(qs)
        if name.startswith("voc2/"):
            try:
                paged = name == "voc2/topics" and ("limit" in qs or "offset" in qs)
                preview = name in ("voc2/issue-pilot", "voc2/playbooks", "voc2/complete-sops") and "first_page" in qs
                if preview and qs["first_page"] != ["1"]:
                    raise ValueError("首页参数无效。")
                if paged:
                    limit, offset = _int(qs, "limit", 20), _int(qs, "offset", 0)
                    if not 1 <= limit <= 20 or offset < 0:
                        raise ValueError("高频问题每页须为 1–20 条，起始位置须为非负整数。")
                voc_filters = self._voc_filters(qs)
                exchange_only = "exchange_only" in qs
                if exchange_only and (name != "voc2/topic" or qs["exchange_only"] != ["1"] or qs.get("workbench") != ["1"]):
                    raise ValueError("问答视图参数无效。")
                if name in ("voc2/topics", "voc2/topic") and ("workbench" in qs or "trend" in qs):
                    workbench = _str(qs, "workbench", "0")
                    trend = _str(qs, "trend", "all")
                    if len(qs.get("workbench", [])) > 1 or workbench not in ("0", "1"):
                        raise ValueError("工作台视图参数无效。")
                    if (len(qs.get("trend", [])) > 1 or trend not in voc_service.WORKBENCH_TRENDS
                            or trend != "all" and (name != "voc2/topics" or workbench != "1")):
                        raise ValueError("高频问题趋势筛选无效。")
                    projection = {"exchange_only": True} if exchange_only else {}
                    result = voc_service.get(cfg, name[5:], voc_filters, _str(qs, "id"),
                                             workbench=workbench == "1", trend=trend, **projection)
                else:
                    result = voc_service.get(cfg, name[5:], voc_filters, _str(qs, "id"))
                if paged:
                    result = {**result, "items": result["items"][offset:offset + limit]}
                elif preview and name == "voc2/issue-pilot":
                    result = {**result, "items": result["items"][:6], "first_page_total": result["total"]}
                elif preview and name == "voc2/complete-sops":
                    result = {**result, "items": result["items"][:8], "first_page_total": result["total"]}
                elif preview:
                    paths = result.get("case_paths", [])
                    first_paths = paths[:8]
                    result = {**result, "case_paths": first_paths, "items": result["items"][:8 - len(first_paths)],
                              "first_page_total": len(paths) + len(result["items"])}
                self._json(result)
            except (voc_service.ServiceError, voc.VocError) as exc:
                self._json({"error": str(exc)}, exc.status)
            return
        if name == "voc/knowledge" or name.startswith("voc/knowledge/"):
            try:
                if name == "voc/knowledge/export":
                    self._send(knowledge.export_csv(cfg, _str(qs, "status", "confirmed")), "text/csv; charset=utf-8", filename="confirmed-knowledge.csv")
                else:
                    self._json(knowledge.handle_get(cfg, name, status=_str(qs, "status", "all"), knowledge_id=_int(qs, "id", 0), topic_id=_int(qs, "topic_id", 0)))
            except voc.VocError as exc:
                self._json({"error": str(exc)}, exc.status)
            return
        if name.startswith("voc/"):
            try:
                result = voc.handle_get(cfg, name, filters, status=_str(qs, "status", "all"),
                                        topic_q=_str(qs, "topic_q"), topic_id=_int(qs, "id", 0),
                                        limit=_int(qs, "limit", 100), offset=_int(qs, "offset", 0),
                                        evidence_status=_str(qs, "evidence_status", "all"))
                self._json(result)
            except voc.VocError as exc:
                self._json({"error": str(exc)}, exc.status)
            return
        if name == "summary":
            self._json(api.build_summary(cfg, **filters))
        elif name == "trend":
            self._json(api.trend(cfg, **filters))
        elif name == "questions":
            self._json(api.questions(cfg, top=_int(qs, "top", 20), **filters))
        elif name == "clusters":
            if {"limit", "offset", "entity_q", "entity_sort", "cluster_members"} & qs.keys():
                self._json(api.cluster_directory(cfg, limit=_int(qs, "limit", 20),
                                                 offset=_int(qs, "offset", 0), entity_q=_str(qs, "entity_q"),
                                                 entity_sort=_str(qs, "entity_sort", "questions"),
                                                 cluster_members=_str(qs, "cluster_members", "all"), **filters))
            else:
                self._json(api.clusters(cfg, **filters))
        elif name in ("groups", "customers"):
            if {"limit", "offset", "entity_q", "entity_sort"} & qs.keys():
                self._json(api.directory(cfg, name, limit=_int(qs, "limit", 20),
                                         offset=_int(qs, "offset", 0), entity_q=_str(qs, "entity_q"),
                                         entity_sort=_str(qs, "entity_sort", "questions"),
                                         top=_int(qs, "top", 10), **filters))
            elif name == "groups":
                self._json(api.groups(cfg, top=_int(qs, "top", 10), **filters))
            else:
                self._json(api.customers(cfg, top=_int(qs, "top", 500), **filters))
        elif name == "pairs":
            self._json(api.pairs(cfg, limit=_int(qs, "limit", 25),
                                 offset=_int(qs, "offset", 0), **filters))
        elif name == "detail":
            data = api.detail(cfg, _int(qs, "id", 0), full_session=_full_session(qs))
            self._json(data if data else {"error": "问答不存在或已在重新清洗中移除"}, 200 if data else 404)
        elif name == "meta":
            self._json({**api.meta(cfg), "source": _str(qs, "source", "local")})
        elif name == "status":
            self._json(api.status(cfg))
        elif name == "template":
            self._send(template_bytes(), "text/csv; charset=utf-8", filename="conversation-template.csv")
        elif name == "export":
            self._send(api.export_pairs(cfg, **filters), "text/csv; charset=utf-8", filename="sanitized-qa.csv")
        else:
            self._json({"error": "接口不存在"}, 404)

    def _handle_login(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 <= length <= 8192:
            self._send_login("请求无效", 400)
            return
        data = self.rfile.read(length).decode("utf-8", "replace")
        form = parse_qs(data, max_num_fields=10)
        key = (form.get("key") or [""])[0].strip()
        panel = form.get("next") == ["/panel"]
        if not self._need_auth():
            self._redirect("/")
            return
        if self._permanent_key_ok(key):
            self._redirect("/panel" if panel else "/", self._cookie_header(key))
            return
        if key_id := self.server.key_store.identify(key):
            if panel:
                self._send_login("请使用恒密钥登录密钥管理。", 403, panel=True)
            else:
                self._redirect("/", self._cookie_header(self._managed_session(key_id)))
            return
        self._note_fail()
        if self._rate_limited():
            self._send_login("尝试次数过多，请 10 分钟后再试", 429, panel=panel)
            return
        self._send_login("密钥不正确", panel=panel)

    def _panel_write(self, data: bytes) -> None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("密钥管理请求须为 JSON。")
        try:
            body = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError):
            raise ValueError("密钥管理请求格式无效。") from None
        action = body.get("action") if isinstance(body, dict) else None
        store = self.server.key_store
        try:
            if action == "create":
                self._json(store.create(body), 201)
            elif action == "update":
                self._json(store.update(body))
            elif action == "revoke":
                self._json(store.revoke(body))
            else:
                raise ValueError("未知密钥管理操作。")
        except admin_keys.MissingKey as exc:
            self._json({"error": str(exc)}, 404)
        except admin_keys.InactiveKey as exc:
            self._json({"error": str(exc)}, 409)

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json({"error": "请通过本机服务地址访问"}, 403)
            return
        try:
            u = urlparse(self.path)
            path = unquote(u.path)
            qs = parse_qs(u.query, max_num_fields=30)
            if path == "/login":
                self._handle_login()
                return
            if not self._gate(path, qs):
                return
            if not self._write_ok():
                self.close_connection = True
                self._json({"error": "写操作仅允许看板页面同源发起，请刷新后重试"}, 403)
                return
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("不支持分块上传")
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                raise ValueError("请求长度无效") from None
            if not 0 <= length <= MAX_FILE_BYTES:
                self.close_connection = True
                self._json({"error": "上传文件最多 8 MB"}, 413)
                return
            if path == "/api/panel/keys" and length > 8192:
                self.close_connection = True
                self._json({"error": "密钥管理请求最多 8 KB。"}, 413)
                return
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("上传未完成，请重新选择文件")
            cfg = self._cfg(qs)
            if path == "/api/panel/keys":
                self._panel_write(data)
            elif path == "/api/detail/replies":
                if data:
                    raise ValueError("客服回复查询不接受请求正文")
                qa_id = _int(qs, "id", 0)
                if qa_id <= 0:
                    raise ValueError("问答 ID 须为正整数")
                full_session = _full_session(qs)
                with DB_LOCK:
                    target = api.case_reply_target(cfg, qa_id, full_session=full_session)
                result = {"status": "unavailable", "messages": []}
                if target is not None:
                    result = api.case_replies.lookup(cfg, *target)
                    # The upstream request can outlive a local import or retention
                    # pass. Recheck the original case before returning any reply.
                    with DB_LOCK:
                        if api.case_reply_target(cfg, qa_id, full_session=full_session) != target:
                            result = {"status": "unavailable", "messages": []}
                        else:
                            result["context_revision"] = api.case_context_revision(target[2])
                self._json(api.case_reply_response(result))
            elif path == "/api/voc2/feishu/publish":
                self._feishu_export(cfg, qs, data)
            elif path.startswith("/api/voc2/"):
                try:
                    if not self.headers.get("Content-Type", "").lower().startswith("application/json") or len(data)>64*1024:
                        raise voc_service.ServiceError("新整理请求须为最多 64 KB 的 JSON。")
                    try:
                        body=json.loads(data.decode("utf-8"))
                    except (ValueError,UnicodeError,RecursionError):
                        raise voc_service.ServiceError("新整理请求不是有效 JSON。") from None
                    filters=self._filters(qs)
                    if path=="/api/voc2/run":
                        if "focus_topic" in qs:
                            raise voc_service.ServiceError("单个问题范围仅用于查询，请从高频问题页整理当前数据范围。")
                        self._json(voc_service.start(cfg,filters,body))
                    elif path=="/api/voc2/assistant":
                        self._json(voc_assistant.answer(cfg,self._voc_filters(qs),body))
                    else:
                        self._json({"error":"新整理接口不存在。"},404)
                except (voc_service.ServiceError,ModelProtocolError,voc.VocError) as exc:
                    self._json({"error":str(exc)},exc.status)
                except ValueError as exc:
                    self._json({"error":str(exc)},400)
                except Exception:
                    log.warning("VOC新整理操作失败")
                    self._json({"error":"新整理服务暂时不可用，请稍后重试。"},500)
            elif path in ("/api/gateway/config", "/api/gateway/models"):
                try:
                    if _str(qs, "source", "local") != "local":
                        raise gateway.GatewayError("网关设置仅用于业务服务，请切换到业务数据")
                    if not self.headers.get("Content-Type", "").lower().startswith("application/json") or len(data) > 32 * 1024:
                        raise gateway.GatewayError("网关请求须为最多 32 KB 的 JSON")
                    try:
                        body = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeError, RecursionError):
                        raise gateway.GatewayError("网关请求不是有效 JSON") from None
                    if path.endswith("/config"):
                        with gateway.CONFIG_LOCK:
                            updated = gateway.save(self.server.cfg, body, self.server.gateway_env_path)
                            self.server.cfg = updated
                            self._json(gateway.public_config(updated))
                    else:
                        self._json(gateway.models(self.server.cfg, body))
                except gateway.GatewayError as exc:
                    self._json({"error": str(exc)}, exc.status)
                except Exception:
                    log.warning("网关设置操作失败")
                    self._json({"error": "网关设置操作失败，请稍后重试"}, 500)
            elif path.startswith("/api/voc/"):
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("VOC 请求须使用 application/json")
                if len(data) > 256 * 1024:
                    raise ValueError("VOC 请求内容过大")
                try:
                    body = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    raise ValueError("VOC 请求不是有效 JSON") from None
                try:
                    with voc_service.LAUNCH_LOCK:
                        if path in ("/api/voc/run","/api/voc/knowledge/run") and voc_service.busy(cfg):
                            raise voc.VocError("新整理任务正在运行，请完成后再启动其他模型任务。",409)
                        self._json(voc.handle_post(cfg, path[5:], self._filters(qs), body))
                except voc.VocError as exc:
                    self._json({"error": str(exc)}, exc.status)
            elif path in ("/api/import/preview", "/api/import"):
                if _str(qs, "source", "local") != "local":
                    raise ValueError("仅支持导入业务数据")
                filename = unquote(self.headers.get("X-Filename", ""))
                if path.endswith("preview"):
                    self._json(parse_import(data, filename).preview())
                    return
                if self.headers.get("X-File-Digest") != hashlib.sha256(data).hexdigest():
                    raise ValueError("文件已变化，请先重新预览")
                with DB_LOCK:
                    self._json(import_file(cfg, data, filename))
            elif path == "/api/clean":
                with DB_LOCK:
                    self._json(clean_local(cfg))
            elif path == "/api/demo":
                self._json({"error": "演示数据入口已停用"}, 410)
            else:
                self._json({"error": "接口不存在"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:
            log.exception("本地操作失败")
            self._json({"error": "本地操作失败，事务已回滚；请重试并查看服务日志"}, 500)

    def log_request(self, code="-", size="-") -> None:
        target = getattr(self, "path", "")
        path = unquote(target.partition("?")[0])
        if path.startswith("/guest/"):
            self.log_message('"%s /guest/evidence/[redacted] %s" %s %s',
                             self.command, self.request_version, str(code), str(size))
            return
        if path.endswith("/api/detail/image"):
            self.log_message('"%s /api/detail/image %s" %s %s',
                             self.command, self.request_version, str(code), str(size))
            return
        super().log_request(code, size)

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


def _free_port(host: str, port: int, tries: int = 20, strict: bool = False) -> int:
    if strict:
        tries = 1
    for p in range(port, port + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            # 和服务端一样开 SO_REUSEADDR：否则刚重启时会因为 TIME_WAIT
            # 误判端口被占，服务悄悄漂到下一个端口，反代就打空了。
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise RuntimeError(f"{port}~{port + tries - 1} 都被占用，换一个 --port")


def serve(cfg: Settings | None = None, host: str = "127.0.0.1", port: int = 8848,
          open_browser: bool = False, strict_port: bool = False) -> None:
    cfg = cfg or settings()
    real_port = _free_port(host, port, strict=strict_port)
    if real_port != port:
        log.warning("端口 %d 被占用，改用 %d", port, real_port)
    httpd = DashboardServer((host, real_port), cfg,
                            allowed_hosts=cfg.runtime.web_allowed_hosts,
                            allowed_origins=cfg.runtime.web_allowed_origins,
                            admin_key=cfg.runtime.admin_key)
    url = f"http://{host}:{real_port}/"
    print(f"\n  本地会话工作台\n  地址   {url}\n  数据库 {cfg.runtime.db_path}")
    print("  在网页导入 CSV/XLSX 并查看业务会话，模型服务由管理员配置。")
    if cfg.runtime.web_allowed_hosts:
        print(f"  反代放行 Host: {', '.join(cfg.runtime.web_allowed_hosts)}")
    print("  Ctrl+C 停止\n", flush=True)
    if cfg.runtime.admin_key:
        print("  鉴权   单管理员密钥（ADMIN_KEY 已启用）")
    elif host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("当前不是仅本机监听，且没有设置 ADMIN_KEY；请勿直接暴露到公网")
    if open_browser:
        threading.Thread(target=_open_later, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        httpd.server_close()


def _open_later(url: str) -> None:
    time.sleep(0.6)
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
