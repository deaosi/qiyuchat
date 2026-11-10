"""配置层：读取 .env / 环境变量，全局单例。

设计原则：
- 零第三方依赖，只用标准库
- 所有外部依赖（七鱼/LLM/飞书）都留空即可跑 doctor，不阻塞本地验证
- 敏感值只从环境变量读，不写死在代码里
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，支持 # 注释和引号。不覆盖已存在的环境变量。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            try:
                decoded = json.loads(val)
                val = decoded if isinstance(decoded, str) else val.strip('"')
            except ValueError:
                val = val.strip('"')
        else:
            val = val.strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file(ENV_FILE)


def _s(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


def _kv_headers(key: str) -> tuple[tuple[str, str], ...]:
    """解析网关自定义请求头。

    支持两种写法：JSON 对象 {"X-Api-Version": "2024-01"}，或 k=v;k=v。
    解析失败按空处理，不让配置错误把服务拖挂。
    """
    raw = os.environ.get(key, "").strip()
    if not raw:
        return ()
    items: list[tuple[str, str]] = []
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        if isinstance(obj, dict):
            items = [(str(k), str(v)) for k, v in obj.items()]
    else:
        for part in raw.split(";"):
            k, _, v = part.partition("=")
            if k.strip():
                items.append((k.strip(), v.strip()))
    return tuple(items)


def _b(key: str, default: bool = False) -> bool:
    return _s(key, "1" if default else "0").lower() in ("1", "true", "yes", "y", "on")


def _thinking_mode() -> str:
    value = _s("LLM_THINKING").lower()
    if value not in ("", "enabled", "disabled"):
        raise ValueError("LLM_THINKING 仅支持留空、enabled 或 disabled")
    return value


def _t(key: str) -> tuple[str, ...]:
    """逗号分隔的字符串列表；空值返回空元组。"""
    raw = os.environ.get(key, "")
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class QiyuConfig:
    app_key: str = ""
    app_secret: str = ""
    base_url: str = "https://qiyukf.com"
    timeout: int = 30

    # 单次拉取时间窗（天）。官方上限：会话记录 5 天 / 质检导出 2 天
    session_window_days: int = 5
    inspect_window_days: int = 2

    # 轮询质检任务
    poll_interval: int = 10
    poll_max_times: int = 60


@dataclass(frozen=True)
class LLMConfig:
    """OpenAI 兼容接口。DeepSeek / Qwen / GLM / vLLM / Ollama 都是这个协议。"""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model_fast: str = "deepseek-chat"       # 分类用
    model_main: str = "deepseek-chat"       # 摘要用
    model_hard: str = ""                    # 难例升级，留空则复用 main
    timeout: int = 120
    temperature: float = 0.1
    batch_size: int = 10                    # 分类批处理条数
    max_input_chars: int = 6000             # 单条会话截断
    concurrency: int = 4
    dry_run: bool = True                    # True = 不真调模型，返回占位结果
    # 自有网关常用：额外请求头（如 X-Api-Version、X-Tenant），JSON 对象字符串
    extra_headers: tuple[tuple[str, str], ...] = ()
    # 网关不支持 response_format 时置 0，改由提示词约束 JSON
    json_mode: bool = True
    # Optional provider extension; VOC/knowledge only. Empty keeps generic gateways compatible.
    thinking: str = ""
    # Admin UI profiles use public HTTPS with pinned DNS and no redirects.
    # Legacy local/Ollama configurations retain their existing transport.
    public_gateway: bool = False


@dataclass(frozen=True)
class FeishuConfig:
    # 出口 A：群自定义机器人（零权限，最快见效）
    webhook: str = ""
    webhook_secret: str = ""

    # 出口 B：应用身份（建文档 / 写多维表格 / 发消息）
    app_id: str = ""
    app_secret: str = ""
    bitable_app_token: str = ""
    bitable_table_id: str = ""
    doc_token: str = ""                     # 目标文档，留空则新建
    doc_owner: str = ""                     # 新建文档后授权给谁（邮箱 / oc_群id / ou_用户id）
    agent_id: str = ""                      # Aily 自定义智能体 id（agent_xxxx），从智能体编辑页地址栏取

    dry_run: bool = True


@dataclass(frozen=True)
class ReceiverConfig:
    """七鱼「消息与事件接收url」的本地接收端。

    七鱼只会往这个地址推 OpenApi 渠道（消息接口）的事件：
    MSG / SESSION_START / SESSION_END / EVA_INVITATION / USER_JOIN_QUEUE / QUEUE_TIMEOUT。
    """

    host: str = "127.0.0.1"          # 默认只听本机，生产用 Caddy/Nginx 反代进来
    port: int = 8860
    path: str = "/qiyu/event"        # 对外路径，必须和后台填的 url 一致
    verify: bool = True              # 生产必须开：校验 checksum
    secret: str = ""                 # 留空则回落到 QIYU_APP_SECRET


@dataclass(frozen=True)
class RuntimeConfig:
    db_path: str = str(ROOT / "data" / "qiyu.db")
    log_dir: str = str(ROOT / "logs")
    state_dir: str = str(ROOT / "data")
    mock: bool = False                      # True = 用内置假数据跑通全链路
    log_level: str = "INFO"
    retention_days: int = 60                # 原始明细保留窗口（自然日），0=不裁
    # 反代场景：允许哪些 Host 访问看板（逗号分隔的域名）。
    # 空 = 只允许本机回环地址（默认行为，最安全）。
    web_allowed_hosts: tuple[str, ...] = ()
    # 反代场景：允许哪些 Origin 发起写操作（逗号分隔，如 https://qiyu.example.com）
    web_allowed_origins: tuple[str, ...] = ()
    # 单管理员密钥：设置后整个看板必须凭这把钥匙访问（空 = 不做应用层鉴权，只认本机回环）
    admin_key: str = ""


@dataclass(frozen=True)
class Settings:
    qiyu: QiyuConfig = field(default_factory=QiyuConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    receiver: ReceiverConfig = field(default_factory=ReceiverConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)


def load_settings() -> Settings:
    return Settings(
        qiyu=QiyuConfig(
            app_key=_s("QIYU_APP_KEY"),
            app_secret=_s("QIYU_APP_SECRET"),
            base_url=_s("QIYU_BASE_URL", "https://qiyukf.com"),
            timeout=_i("QIYU_TIMEOUT", 30),
            session_window_days=_i("QIYU_SESSION_WINDOW_DAYS", 5),
            inspect_window_days=_i("QIYU_INSPECT_WINDOW_DAYS", 2),
            poll_interval=_i("QIYU_POLL_INTERVAL", 10),
            poll_max_times=_i("QIYU_POLL_MAX_TIMES", 60),
        ),
        llm=LLMConfig(
            base_url=_s("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            api_key=_s("LLM_API_KEY"),
            model_fast=_s("LLM_MODEL_FAST", "deepseek-chat"),
            model_main=_s("LLM_MODEL_MAIN", "deepseek-chat"),
            model_hard=_s("LLM_MODEL_HARD"),
            timeout=_i("LLM_TIMEOUT", 120),
            temperature=float(_s("LLM_TEMPERATURE", "0.1") or 0.1),
            batch_size=_i("LLM_BATCH_SIZE", 10),
            max_input_chars=_i("LLM_MAX_INPUT_CHARS", 6000),
            concurrency=_i("LLM_CONCURRENCY", 4),
            dry_run=_b("LLM_DRY_RUN", True),
            extra_headers=_kv_headers("LLM_EXTRA_HEADERS"),
            json_mode=_b("LLM_JSON_MODE", True),
            thinking=_thinking_mode(),
            public_gateway=_b("LLM_PUBLIC_GATEWAY", False),
        ),
        feishu=FeishuConfig(
            webhook=_s("FEISHU_WEBHOOK"),
            webhook_secret=_s("FEISHU_WEBHOOK_SECRET"),
            app_id=_s("FEISHU_APP_ID"),
            app_secret=_s("FEISHU_APP_SECRET"),
            bitable_app_token=_s("FEISHU_BITABLE_APP_TOKEN"),
            bitable_table_id=_s("FEISHU_BITABLE_TABLE_ID"),
            doc_token=_s("FEISHU_DOC_TOKEN"),
            doc_owner=_s("FEISHU_DOC_OWNER"),
            agent_id=_s("FEISHU_AGENT_ID"),
            dry_run=_b("FEISHU_DRY_RUN", True),
        ),
        receiver=ReceiverConfig(
            host=_s("RECEIVER_HOST", "127.0.0.1"),
            port=_i("RECEIVER_PORT", 8860),
            path=_s("RECEIVER_PATH", "/qiyu/event"),
            verify=_b("RECEIVER_VERIFY", True),
            secret=_s("RECEIVER_SECRET") or _s("QIYU_APP_SECRET"),
        ),
        runtime=RuntimeConfig(
            db_path=_s("DB_PATH", str(ROOT / "data" / "qiyu.db")),
            log_dir=_s("LOG_DIR", str(ROOT / "logs")),
            state_dir=_s("STATE_DIR", str(ROOT / "data")),
            mock=_b("MOCK", False),
            log_level=_s("LOG_LEVEL", "INFO").upper(),
            retention_days=_i("RETENTION_DAYS", 60),
            web_allowed_hosts=_t("WEB_ALLOWED_HOSTS"),
            web_allowed_origins=_t("WEB_ALLOWED_ORIGINS"),
            admin_key=_s("ADMIN_KEY"),
        ),
    )


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
