"""七鱼「消息与事件接收url」接收端。

七鱼只会往这个地址推 **OpenApi 渠道（消息接口）** 下的事件，而且只推客服侧消息。
网页 / 微信 / 飞书 / 企微这些原生渠道的会话不会推 —— 这是官方文档里反复写的硬限制。

官方契约（`bHm7bBp7RY` 事件 / `bHltvOolwe` 消息接口）：

1. 七鱼 POST `https://QIYU_MSG_URL?eventType=XXX&time=<UTC秒>&checksum=<hex>`，
   `Content-Type: application/json;charset=utf-8`。
2. `checksum = SHA1(appSecret + MD5(body) + time)`，小写 hex。
3. **10 秒内必须返回空字符串**。超时或返回非空 → 七鱼判定失败并重发。
4. MSG 用 `msgId` 去重；其余事件用 `eventType + uid + time` 去重。

事件类型：
    SESSION_START   会话开始（带 sessionId，是 uid→sessionId 的唯一来源）
    SESSION_END     会话结束（带 closeReason 0~49）
    MSG             客服（人工/机器人）发出的消息，带正文
    EVA_INVITATION  客服主动邀评
    USER_JOIN_QUEUE 进入排队
    QUEUE_TIMEOUT   排队超时

因为 MSG 只带 `uid` 不带 `sessionId`，必须先靠 SESSION_START 把映射记下来，
否则后续所有消息都归不了档。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .db import DB
from .log import get

log = get("receiver")

EVENT_MSG = "MSG"
EVENT_SESSION_START = "SESSION_START"
EVENT_SESSION_END = "SESSION_END"
EVENT_EVA_INVITATION = "EVA_INVITATION"
EVENT_USER_JOIN_QUEUE = "USER_JOIN_QUEUE"
EVENT_QUEUE_TIMEOUT = "QUEUE_TIMEOUT"

KNOWN_EVENTS = {
    EVENT_MSG,
    EVENT_SESSION_START,
    EVENT_SESSION_END,
    EVENT_EVA_INVITATION,
    EVENT_USER_JOIN_QUEUE,
    EVENT_QUEUE_TIMEOUT,
}

# 客服侧消息统一用这个 role，和本地导入 / 质检导出保持一致
ROLE_STAFF = "staff"

ACK_BODY = ""          # 成功必须回空字符串，这是官方契约
MAX_BODY = 2 * 1024 * 1024

# 七鱼只推「客服侧」消息。OpenApi 渠道下客户发的话是我们自己转进七鱼的，
# 所以那一半必须由我们自己镜像一份进来，否则永远凑不成问答对。
CUSTOMER_PATH = "/qiyu/customer"


def compute_checksum(app_secret: str, raw: bytes, ts: str) -> str:
    """七鱼侧校验用：SHA1(appSecret + MD5(body) + time)，小写 hex。"""
    md5 = hashlib.md5(raw or b"").hexdigest()
    return hashlib.sha1(f"{app_secret}{md5}{ts}".encode("utf-8")).hexdigest()


def _pick(payload: dict[str, Any], *names: str, default: Any = "") -> Any:
    """字段名容错：官方文档里 uid / sessionId 的大小写在不同事件里不完全一致。"""
    for n in names:
        if n in payload and payload[n] not in (None, ""):
            return payload[n]
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for n in names:
        v = lowered.get(n.lower())
        if v not in (None, ""):
            return v
    return default


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


class EventRouter:
    """把一条推送翻译成数据库动作。与 HTTP 层解耦，方便单测。"""

    def __init__(self, db: DB, app_secret: str = "", verify: bool = True,
                 auto_session: bool = True) -> None:
        self.db = db
        self.app_secret = app_secret
        self.verify = verify
        self.auto_session = auto_session

    # ---------- 入口 ----------

    def handle(self, query: dict[str, list[str]], raw: bytes) -> tuple[int, str]:
        """返回 (HTTP 状态码, 响应体)。响应体为空串表示 ack 成功。"""
        event_type = (query.get("eventType") or [""])[0].strip()
        ts = (query.get("time") or [""])[0].strip()
        checksum = (query.get("checksum") or [""])[0].strip()

        if not event_type:
            # 不是事件推送（可能是探活），直接 ack，不制造重发
            return 200, ACK_BODY

        if self.verify:
            if not self.app_secret:
                log.error("RECEIVER_VERIFY=1 但没配 AppSecret，拒绝全部推送")
                return 401, "receiver secret missing"
            expect = compute_checksum(self.app_secret, raw, ts)
            if checksum.lower() != expect.lower():
                log.warning("checksum 校验失败 event=%s time=%s got=%s expect=%s",
                            event_type, ts, checksum[:8], expect[:8])
                return 401, "bad checksum"

        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                payload = {"_raw": payload}
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning("事件体不是合法 JSON event=%s err=%s", event_type, e)
            return 400, "bad json"

        try:
            self._dispatch(event_type, ts, payload)
        except Exception as e:  # 数据库异常 → 让七鱼重发，避免丢消息
            log.exception("处理事件失败 event=%s: %s", event_type, e)
            return 500, "internal error"

        return 200, ACK_BODY

    # ---------- 分发 ----------

    def _dispatch(self, event_type: str, ts: str, payload: dict[str, Any]) -> None:
        uid = str(_pick(payload, "uid", "userId", "userid"))
        session_id = str(_pick(payload, "sessionId", "sessionid", "sid"))

        if event_type == EVENT_SESSION_START:
            self._on_session_start(uid, session_id, ts, payload)
            return

        if event_type == EVENT_SESSION_END:
            self._on_session_end(uid, session_id, ts, payload)
            return

        if event_type == EVENT_MSG:
            self._on_msg(uid, session_id, ts, payload)
            return

        # 其余事件只记账
        key = f"{event_type}:{uid}:{ts}"
        first = self.db.record_push(key, event_type, uid, session_id, _int(ts),
                                    json.dumps(payload, ensure_ascii=False))
        log.info("事件 %s uid=%s 首次=%s", event_type, uid or "-", first)

    def _on_session_start(self, uid: str, session_id: str, ts: str,
                          payload: dict[str, Any]) -> None:
        if not session_id:
            session_id = f"live-{uid}" if uid else ""
        if not session_id:
            log.warning("SESSION_START 既没有 sessionId 也没有 uid，丢弃")
            return

        key = f"{EVENT_SESSION_START}:{uid}:{ts}"
        if not self.db.record_push(key, EVENT_SESSION_START, uid, session_id,
                                   _int(ts), json.dumps(payload, ensure_ascii=False)):
            log.info("SESSION_START 重复推送，跳过 uid=%s", uid)
            return

        staff_id = str(_pick(payload, "staffId", "staffid"))
        staff_name = str(_pick(payload, "staffName", "staffname"))
        started = _int(_pick(payload, "timeStamp", "timestamp", "startTime")) or _int(ts) * 1000

        # 注意：upsert_sessions 吃的是七鱼原始字段名（id / startTime / staffId ...），
        # 不是 DB 列名 —— 直接传 snake_case 会静默写成一堆空值。
        self.db.upsert_sessions([{
            "id": session_id,
            "startTime": started,
            "endTime": 0,
            "sType": 0,
            "staffId": staff_id,
            "staffName": staff_name,
            "userId": uid,
            "userName": str(_pick(payload, "userName", "username")),
            "fromType": "OpenApi",
            "isValid": 1,
            "_source": "push",
        }])
        self.db.bind_uid_session(uid, session_id, staff_id, staff_name, started)
        log.info("会话开始 uid=%s session=%s staff=%s", uid or "-", session_id, staff_name or "-")

    def _on_session_end(self, uid: str, session_id: str, ts: str,
                        payload: dict[str, Any]) -> None:
        key = f"{EVENT_SESSION_END}:{uid}:{ts}"
        if not self.db.record_push(key, EVENT_SESSION_END, uid, session_id,
                                   _int(ts), json.dumps(payload, ensure_ascii=False)):
            return
        ended = _int(_pick(payload, "timeStamp", "timestamp", "endTime")) or _int(ts) * 1000
        self.db.close_uid_session(uid, ended)
        reason = _pick(payload, "closeReason", "closereason", default="")
        log.info("会话结束 uid=%s reason=%s", uid or "-", reason)

    def _on_msg(self, uid: str, session_id: str, ts: str, payload: dict[str, Any]) -> None:
        msg_id = str(_pick(payload, "msgId", "msgid"))
        content = str(_pick(payload, "content", "msg", "text"))
        msg_type = str(_pick(payload, "msgType", "msgtype", default="TEXT"))
        stamp = _int(_pick(payload, "timeStamp", "timestamp")) or _int(ts) * 1000

        if not session_id:
            session_id = self.db.current_session_for_uid(uid)
        if not session_id and self.auto_session and uid:
            session_id = f"live-{uid}"
            self.db.bind_uid_session(uid, session_id, ts=stamp)
            log.warning("MSG 早于 SESSION_START，临时归入 %s（收到会话开始后会自动改绑）", session_id)
        if not session_id:
            log.warning("MSG 无法定位会话（无 sessionId、无 uid），丢弃 msgId=%s", msg_id)
            return

        key = f"{EVENT_MSG}:{msg_id}" if msg_id else f"{EVENT_MSG}:{uid}:{stamp}:{hash(content)}"
        if not self.db.record_push(key, EVENT_MSG, uid, session_id, stamp,
                                   json.dumps(payload, ensure_ascii=False)):
            log.info("MSG 重复推送，跳过 msgId=%s", msg_id or "-")
            return

        if not content:
            log.info("MSG 无正文（可能是非文本消息 msgType=%s），只记账", msg_type)
            return

        self.db.append_message(session_id, ROLE_STAFF, content, stamp)
        log.info("MSG uid=%s session=%s len=%d msgType=%s", uid or "-", session_id,
                 len(content), msg_type)


    # ---------- 客户侧消息（不是七鱼推的，是我们自己镜像进来的） ----------

    def ingest_customer(self, raw: bytes, token: str = "") -> tuple[int, str]:
        """把 OpenApi 渠道里客户说的话补进来。

        body: {"uid": "...", "content": "...", "msgId": "...", "timeStamp": 毫秒}
        """
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, "bad json"
        if not isinstance(payload, dict):
            return 400, "bad json"

        uid = str(_pick(payload, "uid", "userId"))
        content = str(_pick(payload, "content", "text"))
        msg_id = str(_pick(payload, "msgId", "msgid"))
        stamp = _int(_pick(payload, "timeStamp", "timestamp")) or int(time.time() * 1000)

        if self.verify and self.app_secret and token != self.app_secret:
            return 401, "bad token"

        if not uid or not content:
            return 400, "uid and content required"

        session_id = str(_pick(payload, "sessionId", "sessionid"))             or self.db.current_session_for_uid(uid)
        if not session_id and self.auto_session:
            session_id = f"live-{uid}"
            self.db.bind_uid_session(uid, session_id, ts=stamp)
        if not session_id:
            return 400, "no session for uid"

        key = f"CUSTOMER:{msg_id}" if msg_id else f"CUSTOMER:{uid}:{stamp}:{hash(content)}"
        if not self.db.record_push(key, "CUSTOMER", uid, session_id, stamp,
                                   json.dumps(payload, ensure_ascii=False)):
            return 200, ACK_BODY
        self.db.append_message(session_id, "user", content, stamp)
        log.info("CUSTOMER uid=%s session=%s len=%d", uid, session_id, len(content))
        return 200, ACK_BODY


class _Handler(BaseHTTPRequestHandler):
    server_version = "qiyu-receiver/1.0"
    protocol_version = "HTTP/1.1"

    router: EventRouter          # 由 serve() 注入

    # ---------- 路由 ----------

    def do_GET(self) -> None:                       # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in ("/health", "/healthz"):
            self._send(200, "ok")
            return
        self._send(404, "not found")

    def do_POST(self) -> None:                      # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        event_path = self.server.event_path.rstrip("/")     # type: ignore[attr-defined]
        customer_path = self.server.customer_path.rstrip("/")  # type: ignore[attr-defined]

        if path not in (event_path, customer_path):
            self._send(404, "not found")
            return

        raw = self._read_body()
        if raw is None:
            self._send(413, "payload too large")
            return

        try:
            if path == customer_path:
                status, body = self.router.ingest_customer(
                    raw, token=self.headers.get("X-Receiver-Token") or "")
            else:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query,
                                              keep_blank_values=True)
                status, body = self.router.handle(query, raw)
        except Exception as e:                       # 兜底：绝不让异常把连接挂死
            log.exception("receiver 处理异常: %s", e)
            status, body = 500, "internal error"
        self._send(status, body)

    # ---------- 工具 ----------

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:   # 屏蔽默认 access log
        log.debug("http %s - %s", self.address_string(), fmt % args)


def serve(db: DB, host: str = "127.0.0.1", port: int = 8860, path: str = "/qiyu/event",
          app_secret: str = "", verify: bool = True, auto_session: bool = True,
          ready: threading.Event | None = None) -> ThreadingHTTPServer:
    """启动接收端并**立即返回**（后台线程跑），便于脚本里继续做别的事。"""
    router = EventRouter(db, app_secret=app_secret, verify=verify, auto_session=auto_session)
    handler = type("BoundHandler", (_Handler,), {"router": router})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.event_path = path                       # type: ignore[attr-defined]
    httpd.customer_path = CUSTOMER_PATH           # type: ignore[attr-defined]

    t = threading.Thread(target=httpd.serve_forever, name="qiyu-receiver", daemon=True)
    t.start()
    if ready is not None:
        ready.set()
    log.info("接收端已启动 http://%s:%d%s（验签=%s，自动建会话=%s）",
             host, port, path, "开" if verify else "关", "开" if auto_session else "关")
    log.info("客户侧镜像入口 http://%s:%d%s（我们自己的转发器往这里补客户消息）",
             host, port, CUSTOMER_PATH)
    return httpd


def serve_forever(db: DB, host: str = "127.0.0.1", port: int = 8860, path: str = "/qiyu/event",
                  app_secret: str = "", verify: bool = True, auto_session: bool = True) -> None:
    """阻塞式运行，给 `python run.py receiver` 用。"""
    httpd = serve(db, host, port, path, app_secret, verify, auto_session)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("收到中断，关闭接收端")
        httpd.shutdown()
        httpd.server_close()
