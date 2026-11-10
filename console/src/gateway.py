"""Admin gateway settings. Secrets never cross the public response boundary."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import ENV_FILE, Settings

CONFIG_LOCK = threading.RLock()
MAX_MODELS_BYTES = 1_000_000


class GatewayError(ValueError):
    """Only fixed, sanitized messages; never include a key, URL or upstream body."""
    def __init__(self, message: str, status: int = 400):
        self.status = status
        super().__init__(message)


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast and not address.is_unspecified


def normalize_base(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2048:
        raise GatewayError("网关地址须为 1–2048 字符的公共 HTTPS API 地址")
    value = value.strip()
    if any(ord(char) < 33 or char in ('\\', '"', "'") for char in value):
        raise GatewayError("网关地址包含不支持的字符")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port or 443
        if parsed.scheme.lower() != "https" or not host or not 1 <= port <= 65535:
            raise ValueError()
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise GatewayError("请填写不含账号、查询参数或片段的公共 HTTPS API 地址") from None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public_ip(host):
            raise GatewayError("网关不能使用本机、内网、保留或链路本地地址")
    elif "." not in host or host.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        raise GatewayError("网关不能使用本机或内部域名")
    authority = f"[{host}]" if ":" in host else host
    if port != 443:
        authority += ":" + str(port)
    return urlunsplit(("https", authority, parsed.path.rstrip("/"), "", ""))


def resolve_public(base_url: str) -> list[str]:
    """Reject the entire DNS answer when any address is not globally routable."""
    parsed = urlsplit(base_url)
    try:
        answers = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    except (OSError, UnicodeError):
        raise GatewayError("无法解析网关域名，请检查 API 地址") from None
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise GatewayError("网关域名必须仅解析到公共互联网地址")
    return addresses


def _key(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise GatewayError("密钥须为不含控制字符的 ASCII 文本，最多 4096 字符")
    return value.strip()


def _model(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise GatewayError("模型 ID 须为不含控制字符的 1–200 字符文本")
    return value.strip()


def _number(body: dict, name: str, fallback: int, low: int, high: int) -> int:
    value = body.get(name, fallback)
    if type(value) is not int or not low <= value <= high:
        raise GatewayError(f"{name} 须为 {low}–{high} 的整数")
    return value


def public_config(cfg: Settings) -> dict:
    llm = cfg.llm
    try:
        base = normalize_base(llm.base_url)
    except GatewayError:
        base = ""
    model = llm.model_main
    if llm.api_key:
        base = "" if llm.api_key in base else base
        model = "" if llm.api_key in model else model
    return {"base_url": base, "model": model, "key_configured": bool(llm.api_key),
            "configured": bool(base and model and llm.api_key and not llm.dry_run),
            "timeout": llm.timeout, "batch_size": llm.batch_size}


def _draft(cfg: Settings, body: object) -> tuple[str, str, bool]:
    if not isinstance(body, dict):
        raise GatewayError("网关设置须为 JSON 对象")
    base = normalize_base(body.get("base_url"))
    try:
        unchanged = base == normalize_base(cfg.llm.base_url)
    except GatewayError:
        unchanged = False
    supplied = _key(body.get("api_key", ""))
    if not unchanged and cfg.llm.api_key and not supplied:
        raise GatewayError("更换网关地址时请重新填写密钥；留空只能保留当前网关的密钥")
    return base, supplied or (cfg.llm.api_key if unchanged else ""), not unchanged


def _persist(path: Path, changes: dict[str, str]) -> None:
    # Resolve a deployment-owned .env symlink, if one is used. The API cannot
    # select this path. Keep the atomic rename inside its private target folder.
    path = path.resolve()
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        newline = "\r\n" if "\r\n" in text else "\n"
        lines, written = [], set()
        for line in text.splitlines():
            name, separator, _ = line.partition("=")
            name = name.strip()
            if separator and name in changes:
                if name not in written:
                    lines.append(name + "=" + json.dumps(changes[name], ensure_ascii=False))
                    written.add(name)
            else:
                lines.append(line)
        lines.extend(name + "=" + json.dumps(value, ensure_ascii=False) for name, value in changes.items() if name not in written)
        descriptor, temporary = tempfile.mkstemp(prefix=".gateway-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(newline.join(lines) + newline)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        temporary = None
    except (OSError, UnicodeError):
        raise GatewayError("配置保存失败，请检查服务配置目录权限", 500) from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def save(cfg: Settings, body: object, env_path: str | Path | None = None) -> Settings:
    with CONFIG_LOCK:
        base, key, changed = _draft(cfg, body)
        if not key:
            raise GatewayError("请填写网关密钥后保存")
        if set(body) - {"base_url", "api_key", "model", "timeout", "batch_size"}:
            raise GatewayError("网关设置包含不支持的字段")
        model = _model(body.get("model"))
        if key and key in model:
            raise GatewayError("模型 ID 不能包含密钥")
        timeout = _number(body, "timeout", cfg.llm.timeout, 10, 600)
        batch = _number(body, "batch_size", cfg.llm.batch_size, 1, 16)
        resolve_public(base)
        llm = replace(cfg.llm, base_url=base, api_key=key, model_main=model, model_fast=model,
                      timeout=timeout, batch_size=batch, dry_run=False, public_gateway=True,
                      extra_headers=() if changed else cfg.llm.extra_headers)
        changes = {"LLM_BASE_URL": base, "LLM_API_KEY": key, "LLM_MODEL_MAIN": model, "LLM_MODEL_FAST": model,
                   "LLM_TIMEOUT": str(timeout), "LLM_BATCH_SIZE": str(batch), "LLM_DRY_RUN": "0", "LLM_PUBLIC_GATEWAY": "1"}
        if changed:
            changes["LLM_EXTRA_HEADERS"] = ""
        _persist(Path(env_path) if env_path is not None else ENV_FILE, changes)
        return replace(cfg, llm=llm)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str, timeout: int = 10):
        super().__init__(hostname, port=port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_models(base_url: str, api_key: str) -> dict:
    """GET only /models, pinned to a validated public IP, TLS checked, no redirects."""
    base = normalize_base(base_url)
    addresses = resolve_public(base)
    parsed = urlsplit(base)
    connection = None
    try:
        connection = _PinnedHTTPSConnection(parsed.hostname, parsed.port or 443, addresses[0])
        connection.request("GET", parsed.path + "/models", headers={"Authorization": "Bearer " + api_key,
                           "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "qiyu-gateway-settings/1.0"})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise GatewayError("网关返回跳转，请填写最终 API 地址后重试", 502)
        if response.status in (401, 403):
            raise GatewayError("模型列表鉴权失败，请检查填写的密钥")
        if response.status != 200:
            raise GatewayError("网关暂时无法读取模型列表，请稍后重试", 502)
        raw = response.read(MAX_MODELS_BYTES + 1)
        if len(raw) > MAX_MODELS_BYTES:
            raise GatewayError("网关模型列表响应过大", 502)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise GatewayError("网关未返回兼容的模型列表，可手动填写模型 ID", 502)
        items, seen = [], set()
        for row in data["data"]:
            model = row.get("id") if isinstance(row, dict) else None
            if isinstance(model, str) and 1 <= len(model) <= 200 and not any(ord(c) < 32 or ord(c) == 127 for c in model) and model not in seen and not (api_key and api_key in model):
                items.append({"id": model})
                seen.add(model)
            if len(items) >= 500:
                break
        return {"items": items, "total": len(items)}
    except GatewayError:
        raise
    except Exception:
        raise GatewayError("无法安全读取网关模型列表，请检查地址、证书或稍后重试", 502) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def models(cfg: Settings, body: object) -> dict:
    with CONFIG_LOCK:
        base, key, _ = _draft(cfg, body)
        if set(body) - {"base_url", "api_key"}:
            raise GatewayError("模型列表请求包含不支持的字段")
        if not key:
            raise GatewayError("请填写密钥后读取模型列表")
    return fetch_models(base, key)


def model_request(method: str, url: str, *, body: object = None, headers: dict | None = None,
                  timeout: int = 90, retries: int = 1) -> object:
    """Controlled inference transport for UI-saved profiles, one attempt only.

    Keep the shared request signature and HttpError status contract so existing
    response_format fallback remains intact. Error bodies are reduced to a
    fixed compatibility hint; provider text and signed URLs are never retained.
    """
    from .http import HttpError
    if method.upper() != "POST":
        raise GatewayError("模型网关仅允许 POST 推理请求")
    target = normalize_base(url)
    addresses = resolve_public(target)
    parsed = urlsplit(target)
    connection = None
    try:
        connection = _PinnedHTTPSConnection(parsed.hostname, parsed.port or 443, addresses[0], timeout=timeout)
        request_headers = {"Content-Type": "application/json;charset=utf-8", "Accept": "application/json",
                           "Accept-Encoding": "identity", "User-Agent": "qiyu-gateway-inference/1.0"}
        for name, value in (headers or {}).items():
            if str(name).lower() in ("host", "connection", "content-length", "transfer-encoding", "proxy-authorization"):
                continue
            if any(ord(char) < 32 or ord(char) == 127 for char in str(name) + str(value)):
                raise GatewayError("模型网关请求头包含不支持的字符")
            request_headers[str(name)] = str(value)
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        connection.request("POST", parsed.path or "/", body=payload, headers=request_headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise GatewayError("模型网关不允许跳转，请填写最终 API 地址")
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise GatewayError("模型网关响应过大，请减小整理批次", 502)
        if not 200 <= response.status < 300:
            hint = "response_format" if response.status in (400, 422) and b"response_format" in raw else ""
            raise HttpError(response.status, hint, "")
        return json.loads(raw.decode("utf-8"))
    except (GatewayError, HttpError):
        raise
    except ssl.SSLError:
        raise GatewayError("模型网关证书校验失败，请检查 API 地址") from None
    except Exception:
        raise GatewayError("模型网关连接失败，请稍后重试", 502) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
