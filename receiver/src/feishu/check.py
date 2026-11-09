"""飞书联通性自检。

和 `doctor` 的区别：doctor 只看「配置填了没有」，这里真的发一次请求，
把飞书返回的错误码翻译成「该去点哪里」。

三项读操作（token / 多维表格 / 文档）永远真跑，因为它们是只读的、没有副作用。
群机器人会往群里发消息，所以只有显式 --send 才真发。
"""

from __future__ import annotations

from typing import Any

from ..config import FeishuConfig
from ..http import request
from ..log import get
from .auth import BASE, FeishuAuth
from .bitable import FIELD_SPEC

log = get("feishu.check")


# 飞书错误码 → 该去干什么。只收录真会撞到的。
HINTS: dict[int, str] = {
    10003: "app_id 格式不对（应该形如 cli_a1b2c3d4e5f6g7h8），检查有没有复制到多余空格",
    10014: "App Secret 不对，或已被重置。开发者后台「凭证与基础信息」重新复制",
    99991661: "应用凭证不对，或应用还没发布。检查 App ID / App Secret，并确认版本已审核通过",
    99991663: "tenant_access_token 无效，多半是应用被停用或密钥被重置，重新发版",
    99991672: "缺权限。到开发者后台「权限管理」勾上对应 scope，然后必须重新发版才生效",
    99991668: "应用未开通机器人能力。开发者后台「添加应用能力」→ 机器人",
    1254005: "多维表格 app_token 不对。从表格 URL /base/{app_token} 里取",
    1254040: "多维表格 table_id 不对。从表格 URL 的 table= 参数取",
    19001: "Webhook 地址里的 token 无效。机器人被删了或地址复制不全，重新复制一次",
    19021: "机器人签名校验失败，FEISHU_WEBHOOK_SECRET 和「安全设置」里的密钥不一致",
    19022: "机器人开了 IP 白名单，把服务器出口 IP 加进去（curl -4 ifconfig.me）",
    1254302: "应用没有这张表的权限。打开表格 → 右上角「...」→ 添加文档应用 → 搜索应用名 → 加为协作者并给可编辑",
    1254304: "表格不存在或已被删除",
    131006: "文档没有授权给应用。打开文档 → 右上角「...」→ 添加文档应用 → 可编辑",
    1770001: "文档 token 不对，或文档已被删除",
    1770029: "文档 block 结构不对，通常是 doc_token 指向的不是新版文档（docx）",
}


def hint_for(code: int, msg: str = "") -> str:
    return HINTS.get(code) or f"未收录的错误码 {code}：{msg}"


def _result(ok: bool, detail: str, fix: str = "") -> dict[str, Any]:
    return {"ok": ok, "detail": detail, "fix": fix}


# ---------- 出口 A：群自定义机器人 ----------

def check_webhook(cfg: FeishuConfig, send: bool = False) -> dict[str, Any]:
    if not cfg.webhook:
        return _result(False, "未配置 FEISHU_WEBHOOK",
                       "飞书群 → 设置 → 群机器人 → 添加 → 自定义机器人 → 复制 Webhook 地址")
    if not send:
        return _result(True, "已配置（未实际发送，加 --send 真发一条测试卡片）")

    body: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": "七鱼分析 · 联通性测试"},
                       "template": "blue"},
            "elements": [{"tag": "div", "text": {
                "tag": "lark_md",
                "content": "如果你看到这条消息，说明**群机器人出口已打通**。\n"
                           "日报、高频问题告警都会走这个通道。"}}],
        },
    }
    # 签名：key = f"{ts}\n{secret}"，HMAC-SHA256(msg=b"")
    if cfg.webhook_secret:
        import base64
        import hashlib
        import hmac
        import time
        ts = str(int(time.time()))
        key = f"{ts}\n{cfg.webhook_secret}"
        digest = hmac.new(key.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
        body["timestamp"] = ts
        body["sign"] = base64.b64encode(digest).decode("utf-8")

    try:
        resp = request("POST", cfg.webhook, body=body, timeout=15)
    except Exception as exc:
        return _result(False, f"请求失败: {exc}", "确认 Webhook 地址完整、服务器能出网到 open.feishu.cn")

    code = int(resp.get("code") or 0) if isinstance(resp, dict) else 0
    if code == 0:
        return _result(True, "测试卡片已发送，去群里看一眼")
    if code == 19021:
        return _result(False, "签名校验失败", "FEISHU_WEBHOOK_SECRET 和机器人「安全设置」里的密钥不一致")
    if code == 19022:
        return _result(False, "IP 不在白名单", "机器人安全设置里加了 IP 白名单，把服务器出口 IP 加进去")
    return _result(False, f"code={code} msg={resp.get('msg')}", hint_for(code, str(resp.get("msg", ""))))


# ---------- 出口 B-1：应用身份 ----------

def check_token(cfg: FeishuConfig) -> dict[str, Any]:
    if not (cfg.app_id and cfg.app_secret):
        return _result(False, "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET",
                       "open.feishu.cn/app → 创建企业自建应用 → 凭证与基础信息")
    resp = request("POST", f"{BASE}/auth/v3/tenant_access_token/internal",
                   body={"app_id": cfg.app_id, "app_secret": cfg.app_secret}, timeout=20)
    code = int(resp.get("code") or 0)
    if code != 0:
        return _result(False, f"code={code} msg={resp.get('msg')}", hint_for(code, str(resp.get("msg", ""))))
    return _result(True, f"拿到 tenant_access_token（有效期 {resp.get('expire')} 秒）")


# ---------- 出口 B-2：多维表格 ----------

def check_bitable(cfg: FeishuConfig, auth: FeishuAuth) -> dict[str, Any]:
    if not (cfg.bitable_app_token and cfg.bitable_table_id):
        return _result(False, "未配置 FEISHU_BITABLE_APP_TOKEN / FEISHU_BITABLE_TABLE_ID",
                       "新建多维表格 → 看 URL：/base/{app_token}?table={table_id}")

    path = f"/bitable/v1/apps/{cfg.bitable_app_token}/tables/{cfg.bitable_table_id}/fields?page_size=200"
    try:
        data = auth.call("GET", path)
    except RuntimeError as exc:
        text = str(exc)
        code = 0
        for tok in text.replace("=", " ").replace(",", " ").split():
            if tok.isdigit():
                code = int(tok)
                break
        return _result(False, text[:160], hint_for(code, text))

    existing = {f["field_name"] for f in data.get("items", [])}
    need = [name for name, _ in FIELD_SPEC]
    missing = [n for n in need if n not in existing]

    if not missing:
        return _result(True, f"18 个字段齐全（表内共 {len(existing)} 个字段）")
    return _result(True, f"已连通，缺 {len(missing)} 个字段：{', '.join(missing[:6])}"
                         f"{'...' if len(missing) > 6 else ''}",
                   "首次 push 时 ensure_fields 会自动补建；也可以现在手工建")


# ---------- 出口 B-3：文档 ----------

def check_doc(cfg: FeishuConfig, auth: FeishuAuth) -> dict[str, Any]:
    if not cfg.doc_token:
        return _result(True, "未配置 FEISHU_DOC_TOKEN，report/push 时会自动新建文档（需 docx:document:create）")
    try:
        data = auth.call("GET", f"/docx/v1/documents/{cfg.doc_token}")
    except RuntimeError as exc:
        text = str(exc)
        code = 0
        for tok in text.replace("=", " ").replace(",", " ").split():
            if tok.isdigit():
                code = int(tok)
                break
        return _result(False, text[:160], hint_for(code, text))
    title = (data.get("document") or {}).get("title", "")
    return _result(True, f"文档可读：{title or cfg.doc_token}")


# ---------- 汇总 ----------

def run_all(cfg: FeishuConfig, send: bool = False) -> dict[str, Any]:
    """四项全测。token 挂了，后两项直接跳过（避免刷一串同样的错）。"""
    out: dict[str, Any] = {}
    out["群机器人"] = check_webhook(cfg, send=send)

    tok = check_token(cfg)
    out["应用身份"] = tok
    if not tok["ok"]:
        skip = _result(False, "跳过（应用身份未通过）", "")
        out["多维表格"] = dict(skip)
        out["文档"] = dict(skip)
        return out

    auth = FeishuAuth(cfg.app_id, cfg.app_secret)
    out["多维表格"] = check_bitable(cfg, auth)
    out["文档"] = check_doc(cfg, auth)
    return out
