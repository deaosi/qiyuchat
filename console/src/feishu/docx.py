"""飞书文档写入。

两种模式：
- 覆盖写：先清空目标文档的 block，再写入新内容（保证每次都是最新快照）
- 新建：doc_token 留空时新建一篇

block_type 参考：1 页面 / 2 文本 / 3 一级标题 / 4 二级标题 / 12 无序列表
"""

from __future__ import annotations

from typing import Any

from ..config import FeishuConfig
from ..log import get
from .auth import FeishuAuth

log = get("feishu.docx")


def _text_block(content: str) -> dict[str, Any]:
    return {"block_type": 2, "text": {"elements": [{"text_run": {"content": content}}]}}


def _h1(content: str) -> dict[str, Any]:
    return {"block_type": 3, "heading1": {"elements": [{"text_run": {"content": content}}]}}


def _h2(content: str) -> dict[str, Any]:
    return {"block_type": 4, "heading2": {"elements": [{"text_run": {"content": content}}]}}


def _bullet(content: str) -> dict[str, Any]:
    return {"block_type": 12, "bullet": {"elements": [{"text_run": {"content": content}}]}}


class DocxWriter:
    def __init__(self, cfg: FeishuConfig, auth: FeishuAuth | None = None):
        self.cfg = cfg
        self.auth = auth or FeishuAuth(cfg.app_id, cfg.app_secret)

    # ---------- 文档 ----------

    def create(self, title: str) -> str:
        data = self.auth.call("POST", "/docx/v1/documents", body={"title": title})
        return data["document"]["document_id"]

    def clear(self, doc_id: str) -> None:
        """清空文档所有子 block（保留根 block）。"""
        page_token = ""
        while True:
            q = f"/docx/v1/documents/{doc_id}/blocks?page_size=500"
            if page_token:
                q += f"&page_token={page_token}"
            data = self.auth.call("GET", q)
            items = data.get("items", [])
            children = [b["block_id"] for b in items if b.get("block_id") != doc_id]
            if children:
                self.auth.call(
                    "DELETE",
                    f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete",
                    body={"start_index": 0, "end_index": len(children)},
                )
            if not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
            if not page_token:
                break

    def append(self, doc_id: str, blocks: list[dict[str, Any]]) -> None:
        for chunk in [blocks[i:i + 50] for i in range(0, len(blocks), 50)]:
            self.auth.call(
                "POST",
                f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                body={"children": chunk, "index": -1},
            )

    # ---------- 业务 ----------

    def grant(self, doc_id: str, member: str = "", member_type: str = "") -> bool:
        """把文档授权出去。

        应用建的文档默认只有应用自己可见，人在飞书里搜不到。
        必须显式加一个协作者（或授权给群），否则「推送成功」但没人看得见。
        需要 drive:drive 权限。
        """
        member = (member or self.cfg.doc_owner or "").strip()
        if not member:
            log.warning("未配置 FEISHU_DOC_OWNER，文档只属于应用，人在飞书里看不到")
            return False
        if not member_type:
            if "@" in member:
                member_type = "email"
            elif member.startswith("oc_"):
                member_type = "openchat"
            elif member.startswith("ou_"):
                member_type = "openid"
            else:
                member_type = "email"
        try:
            self.auth.call(
                "POST",
                f"/drive/v1/permissions/{doc_id}/members?type=docx",
                body={"member_type": member_type, "member_id": member,
                      "perm": "full_access"},
            )
        except RuntimeError as exc:
            log.warning("文档授权失败（文档已建好，可手工加协作者）: %s", exc)
            return False
        log.info("文档已授权给 %s (%s)", member, member_type)
        return True

    def write_report(self, title: str, sections: list[tuple[str, list[str]]],
                     doc_token: str = "") -> str:
        """sections = [(小标题, [正文行...])]，返回 document_id（或 dry run 时的占位）。"""
        if self.cfg.dry_run or not self.cfg.app_id:
            log.info("[DRY RUN] 文档写入《%s》，%d 个章节", title, len(sections))
            return "dry-run-doc-id"

        created = not (doc_token or self.cfg.doc_token)
        doc_id = doc_token or self.cfg.doc_token or self.create(title)
        if created:
            self.grant(doc_id)
        try:
            self.clear(doc_id)
        except Exception as e:                      # noqa: BLE001
            log.warning("清空文档失败（首次写入可忽略）: %s", e)

        blocks: list[dict[str, Any]] = [_h1(title)]
        for name, lines in sections:
            blocks.append(_h2(name))
            for ln in lines:
                blocks.append(_text_block(ln) if not ln.startswith("- ") else _bullet(ln[2:]))
        self.append(doc_id, blocks)
        log.info("文档写入完成 document_id=%s", doc_id)
        return doc_id
