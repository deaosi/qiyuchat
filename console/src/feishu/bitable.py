"""多维表格写入：以「会话ID」为唯一键做 upsert。

不做 upsert 就会重跑重复 —— 这是这类流水线最常见的坑。
实现方式：先分页拉出已有 record_id 建索引，再分 create / update 两批提交。
"""

from __future__ import annotations

from typing import Any

from ..config import FeishuConfig
from ..log import get
from .auth import FeishuAuth

log = get("feishu.bitable")

# 字段类型：1 文本 / 2 数字 / 3 单选 / 5 日期 / 7 复选框 / 15 超链接
FIELD_SPEC: list[tuple[str, int]] = [
    ("会话ID", 1),
    ("开始时间", 5),
    ("渠道", 1),
    ("客服", 1),
    ("分流组", 1),
    ("一级分类", 3),
    ("二级分类", 1),
    ("情绪", 3),
    ("风险", 3),
    ("是否解决", 7),
    ("客户诉求", 1),
    ("处理结果", 1),
    ("产品", 1),
    ("需跟进", 7),
    ("一句话", 1),
    ("质量分", 2),
    ("置信度", 2),
    ("提示词版本", 1),
]

# 问答明细表的字段（推 qa_pair，零 API 模式下的主力表）
PAIR_FIELD_SPEC: list[tuple[str, int]] = [
    ("问答ID", 1),
    ("会话ID", 1),
    ("客户ID", 1),
    ("客户", 1),
    ("客户群", 1),
    ("客服", 1),
    ("提问时间", 5),
    ("问题原话", 1),
    ("客服回答", 1),
    ("问题簇", 2),
]


class BitableWriter:
    def __init__(self, cfg: FeishuConfig, auth: FeishuAuth | None = None,
                 field_spec: list[tuple[str, int]] | None = None,
                 key_field: str = "会话ID",
                 mapper=None):
        self.cfg = cfg
        self.auth = auth or FeishuAuth(cfg.app_id, cfg.app_secret)
        self.field_spec = field_spec or FIELD_SPEC
        self.key_field = key_field
        self.mapper = mapper or _to_fields
        self._base = f"/bitable/v1/apps/{cfg.bitable_app_token}/tables/{cfg.bitable_table_id}"

    # ---------- 结构 ----------

    def ensure_fields(self) -> list[str]:
        """确保表里有需要的字段，缺的补上。返回新建的字段名。"""
        data = self.auth.call("GET", f"{self._base}/fields?page_size=200")
        existing = {f["field_name"] for f in data.get("items", [])}
        created = []
        for name, ftype in self.field_spec:
            if name in existing:
                continue
            self.auth.call("POST", f"{self._base}/fields",
                           body={"field_name": name, "type": ftype})
            created.append(name)
        if created:
            log.info("多维表格补建字段: %s", created)
        return created

    # ---------- 读已有 ----------

    def _existing_index(self) -> dict[str, str]:
        """返回 {会话ID: record_id}"""
        idx: dict[str, str] = {}
        page_token = ""
        while True:
            q = f"/records?page_size=500"
            if page_token:
                q += f"&page_token={page_token}"
            data = self.auth.call("GET", f"{self._base}{q}")
            for item in data.get("items", []):
                fields = item.get("fields", {})
                k = _as_text(fields.get(self.key_field))
                if k:
                    idx[k] = item["record_id"]
            if not data.get("has_more"):
                break
            page_token = data.get("page_token", "")
            if not page_token:
                break
        return idx

    # ---------- 写 ----------

    def upsert(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        if not rows:
            return {"created": 0, "updated": 0}

        if self.cfg.dry_run or not self.cfg.bitable_app_token:
            log.info("[DRY RUN] 多维表格 upsert %d 条（未实际写入）", len(rows))
            return {"created": len(rows), "updated": 0}

        self.ensure_fields()
        idx = self._existing_index()

        to_create, to_update = [], []
        for r in rows:
            fields = self.mapper(r)
            sid = str(r.get(self.key_field, ""))
            if sid in idx:
                to_update.append({"record_id": idx[sid], "fields": fields})
            else:
                to_create.append({"fields": fields})

        for chunk in _chunks(to_create, 500):
            self.auth.call("POST", f"{self._base}/records/batch_create", body={"records": chunk})
        for chunk in _chunks(to_update, 500):
            self.auth.call("POST", f"{self._base}/records/batch_update", body={"records": chunk})

        log.info("多维表格写入：新增 %d，更新 %d", len(to_create), len(to_update))
        return {"created": len(to_create), "updated": len(to_update)}


# ---------- 辅助 ----------

def _to_fields(r: dict[str, Any]) -> dict[str, Any]:
    def txt(v: Any) -> str:
        return "" if v is None else str(v)

    fields: dict[str, Any] = {
        "会话ID": txt(r.get("session_id")),
        "渠道": txt(r.get("from_type")),
        "客服": txt(r.get("staff_name")),
        "分流组": txt(r.get("from_group")),
        "一级分类": txt(r.get("category")),
        "二级分类": txt(r.get("sub_category")),
        "情绪": txt(r.get("emotion")),
        "风险": txt(r.get("risk")),
        "是否解决": bool(int(r.get("resolved") or 0)),
        "客户诉求": txt(r.get("demand")),
        "处理结果": txt(r.get("result")),
        "产品": txt(r.get("product")),
        "需跟进": bool(int(r.get("followup") or 0)),
        "一句话": txt(r.get("one_line")),
        "质量分": int(r.get("quality_score") or 0),
        "置信度": float(r.get("confidence") or 0),
        "提示词版本": txt(r.get("prompt_version")),
    }
    start = r.get("start_ts")
    if start:
        fields["开始时间"] = int(start)
    return fields


def pair_to_fields(r: dict[str, Any]) -> dict[str, Any]:
    """qa_pair → 多维表格字段。"""
    def txt(v: Any) -> str:
        return "" if v is None else str(v)

    fields: dict[str, Any] = {
        "问答ID": txt(r.get("qa_id")),
        "会话ID": txt(r.get("session_id")),
        "客户ID": txt(r.get("customer_id")),
        "客户": txt(r.get("customer_name")),
        "客户群": txt(r.get("group_name")),
        "客服": txt(r.get("staff_name")),
        "问题原话": txt(r.get("question")),
        "客服回答": txt(r.get("answer")),
    }
    asked = r.get("asked_at")
    if asked:
        fields["提问时间"] = int(asked)
    cid = r.get("cluster_id")
    if cid:
        fields["问题簇"] = int(cid)
    return fields


def _as_text(v: Any) -> str:
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            return str(first.get("text", ""))
        return str(first)
    if isinstance(v, dict):
        return str(v.get("text", ""))
    return str(v or "")


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
