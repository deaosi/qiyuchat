"""本地会话工作台：静态页面、统一查询和显式本地写操作。"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..config import Settings, settings
from .. import voc, knowledge
from ..local import MAX_FILE_BYTES, clean_local, demo_settings, import_file, load_demo, parse_import, template_bytes
from ..log import get
from . import api

log = get("web")
STATIC = Path(__file__).parent / "static"
# app.get_db() 切库时会关闭旧连接；查询和写入必须持有同一把锁。
DB_LOCK = threading.RLock()


def _int(qs: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int((qs.get(key) or [default])[0])
    except (TypeError, ValueError):
        raise ValueError(f"{key} 必须是整数") from None


def _str(qs: dict[str, list[str]], key: str, default: str = "") -> str:
    return (qs.get(key) or [default])[0]


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], cfg: Settings,
                 allowed_hosts: tuple[str, ...] = (),
                 allowed_origins: tuple[str, ...] = (),
                 admin_key: str = ""):
        self.cfg = cfg
        voc.startup(cfg)
        knowledge.startup(cfg)
        # 反代部署时额外放行的 Host / Origin；默认空 = 只认本机回环。
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.allowed_origins = frozenset(o.lower() for o in allowed_origins)
        # 单管理员密钥；空 = 不启用应用层鉴权
        self.admin_key = admin_key or ""
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
              filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: object, status: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

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
        source = _str(qs, "source", "local")
        if source not in ("local", "demo"):
            raise ValueError("未知数据源")
        cfg = self.server.cfg
        return demo_settings(cfg) if source == "demo" else cfg

    def _filters(self, qs: dict[str, list[str]]) -> dict:
        replied = _str(qs, "replied")
        if replied not in ("", "answered", "unanswered"):
            raise ValueError("未知回复状态")
        return {"days": _int(qs, "days", 0), "start": _str(qs, "start"),
                "end": _str(qs, "end"), "group": _str(qs, "group"),
                "staff": _str(qs, "staff"), "q": _str(qs, "q"),
                "replied": replied, "cluster": _int(qs, "cluster", 0),
                "customer": _str(qs, "customer")}

    # ---------- 单管理员密钥鉴权 ----------
    COOKIE = "qy_admin"

    def _need_auth(self) -> bool:
        return bool(getattr(self.server, "admin_key", ""))

    def _key_ok(self, value: str) -> bool:
        if not value:
            return False
        expected = self.server.admin_key
        return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))

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
                 "SameSite=Lax", "Max-Age=2592000"]
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

    def _login_html(self, error: str = "") -> bytes:
        err = f'<p class="err">{error}</p>' if error else ""
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>会话看板 · 登录</title><style>"
            ":root{color-scheme:light}"
            "body{margin:0;min-height:100vh;display:flex;align-items:center;"
            "justify-content:center;background:#f5f6f8;"
            "font:14px/1.6 -apple-system,'Microsoft YaHei',sans-serif;color:#1f2329}"
            ".card{width:320px;padding:28px 26px;background:#fff;border-radius:12px;"
            "box-shadow:0 4px 24px rgba(31,35,41,.08)}"
            "h1{margin:0 0 4px;font-size:17px;font-weight:600}"
            ".sub{margin:0 0 18px;color:#646a73;font-size:13px}"
            "input{width:100%;box-sizing:border-box;padding:10px 12px;font-size:14px;"
            "border:1px solid #d0d3d6;border-radius:8px;outline:none}"
            "input:focus{border-color:#3370ff;box-shadow:0 0 0 3px rgba(51,112,255,.12)}"
            "button{width:100%;margin-top:14px;padding:10px;font-size:14px;"
            "border:0;border-radius:8px;background:#3370ff;color:#fff;cursor:pointer}"
            "button:hover{background:#245bdb}"
            ".err{margin:12px 0 0;color:#d83931;font-size:13px}"
            "</style></head><body><main class=\"card\">"
            "<h1>会话看板</h1><p class=\"sub\">请输入管理员密钥</p>"
            '<form method="post" action="/login">'
            '<input type="password" name="key" autofocus required '
            'autocomplete="current-password" placeholder="管理员密钥">'
            "<button type=\"submit\">进入</button></form>"
            + err +
            "</main></body></html>"
        ).encode("utf-8")

    def _send_login(self, error: str = "", status: int = 401) -> None:
        self._send(self._login_html(error), "text/html; charset=utf-8", status)

    def _gate(self, path: str, qs: dict[str, list[str]]) -> bool:
        """返回 True = 放行；False = 已经回过响应，调用方直接返回。"""
        if not self._need_auth():
            return True

        presented = self.headers.get("X-Admin-Key", "").strip()
        from_query = False
        if not presented and qs.get("key"):
            presented = (qs["key"][0] or "").strip()
            from_query = True
        if not presented:
            presented = self._cookie_value(self.COOKIE)

        if self._key_ok(presented):
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
            path, qs = unquote(u.path), parse_qs(u.query, max_num_fields=30)
            if path == "/login":
                self._send_login(status=200)
                return
            if path == "/logout":
                self._redirect("/login", f"{self.COOKIE}=; Path=/; Max-Age=0; HttpOnly")
                return
            if not self._gate(path, qs):
                return
            if path.startswith("/api/"):
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

    def _api(self, name: str, qs: dict[str, list[str]]) -> None:
        cfg = self._cfg(qs)
        filters = self._filters(qs)
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
            self._json(api.clusters(cfg, **filters))
        elif name == "groups":
            self._json(api.groups(cfg, top=_int(qs, "top", 10), **filters))
        elif name == "customers":
            self._json(api.customers(cfg, top=_int(qs, "top", 500), **filters))
        elif name == "pairs":
            self._json(api.pairs(cfg, limit=_int(qs, "limit", 25),
                                 offset=_int(qs, "offset", 0), **filters))
        elif name == "detail":
            data = api.detail(cfg, _int(qs, "id", 0))
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
        key = (parse_qs(data, max_num_fields=10).get("key") or [""])[0].strip()
        if not self._need_auth():
            self._redirect("/")
            return
        if self._key_ok(key):
            self._redirect("/", self._cookie_header(key))
            return
        self._note_fail()
        if self._rate_limited():
            self._send_login("尝试次数过多，请 10 分钟后再试", 429)
            return
        self._send_login("密钥不正确")

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json({"error": "请通过本机服务地址访问"}, 403)
            return
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
        try:
            u = urlparse(self.path)
            qs = parse_qs(u.query, max_num_fields=30)
            cfg = self._cfg(qs)
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
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("上传未完成，请重新选择文件")
            if u.path.startswith("/api/voc/"):
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise ValueError("VOC 请求须使用 application/json")
                if len(data) > 256 * 1024:
                    raise ValueError("VOC 请求内容过大")
                try:
                    body = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError):
                    raise ValueError("VOC 请求不是有效 JSON") from None
                try:
                    self._json(voc.handle_post(cfg, u.path[5:], self._filters(qs), body))
                except voc.VocError as exc:
                    self._json({"error": str(exc)}, exc.status)
            elif u.path in ("/api/import/preview", "/api/import"):
                if _str(qs, "source", "local") != "local":
                    raise ValueError("演示库不接收导入，请切换到本地数据")
                filename = unquote(self.headers.get("X-Filename", ""))
                if u.path.endswith("preview"):
                    self._json(parse_import(data, filename).preview())
                    return
                if self.headers.get("X-File-Digest") != hashlib.sha256(data).hexdigest():
                    raise ValueError("文件已变化，请先重新预览")
                with DB_LOCK:
                    self._json(import_file(cfg, data, filename))
            elif u.path == "/api/clean":
                with DB_LOCK:
                    self._json(clean_local(cfg))
            elif u.path == "/api/demo":
                with DB_LOCK:
                    self._json(load_demo(self.server.cfg))
            else:
                self._json({"error": "接口不存在"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:
            log.exception("本地操作失败")
            self._json({"error": "本地操作失败，事务已回滚；请重试并查看服务日志"}, 500)

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
    print("  在网页导入 CSV/XLSX，或载入独立演示数据。不会自动连接七鱼、飞书或模型。")
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
