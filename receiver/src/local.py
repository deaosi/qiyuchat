"""本地文件导入与规则整理。这里不调用七鱼、飞书或任何模型接口。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import ParseError

from .app import get_db, stage_extract
from .clean.rules import mask_pii
from .config import Settings
from .extract import extract_pairs
from .qiyu.inspect import InspectExporter
from .xlsx import read_table

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_IMPORT_ROWS = 5000

ALIASES = {
    "session_id": ("会话ID", "session_id", "sessionId", "id"),
    "content": ("会话内容", "聊天内容", "对话内容", "sessionContent", "content"),
    "question": ("问题", "客户问题", "问题原话", "question"),
    "answer": ("回答", "客服回答", "客服回复", "answer"),
    "customer_id": ("客户ID", "用户ID", "customer_id", "userId"),
    "customer_name": ("客户名称", "客户名", "客户", "用户名称", "用户名", "customer_name", "userName"),
    "group": ("客户群", "客户群名称", "客户群/渠道", "分流组", "分流组名称", "fromGroup", "group_name"),
    "staff": ("客服", "接待客服", "客服名称", "客服姓名", "staffName", "staff_name"),
    "time": ("时间", "提问时间", "会话开始时间", "开始时间", "startTime", "asked_at", "start_ts"),
    "end": ("会话结束时间", "结束时间", "endTime", "end_ts"),
}


def _header(text: str) -> str:
    return re.sub(r"[\s_（）()\-]", "", text).casefold()


def parse_time(value: str) -> int:
    """空时间保留为 0；支持 Excel 序号、秒/毫秒和 ISO/中文常用日期。"""
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            number = float(text)
            if not number:
                return 0
            if 20000 <= number <= 80000:
                date = datetime(1899, 12, 30) + timedelta(days=number)
                ms = int(date.timestamp() * 1000)
            else:
                ms = int(number if number >= 100_000_000_000 else number * 1000)
        else:
            text = text.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
            ms = int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        if not 946684800000 <= ms <= 4133980799999:
            raise ValueError
        return ms
    except (ValueError, OverflowError, OSError):
        raise ValueError("时间无法识别，请使用 2026-11-07 10:30:00 或留空") from None


@dataclass
class ImportData:
    filename: str
    digest: str
    kind: str
    fields: dict[str, str]
    total_rows: int
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    valid_rows: int = 0
    duplicate_rows: int = 0
    unknown_time: int = 0

    def preview(self) -> dict[str, Any]:
        samples = []
        pair_count = 0
        for sid, session in self.sessions.items():
            pairs = extract_pairs(self.messages[sid], session)
            pair_count += len(pairs)
            for pair in pairs:
                if len(samples) < 4:
                    samples.append({k: mask_pii(str(pair.get(k) or "")) for k in
                                    ("question", "answer", "customer_name", "group_name")})
        return {
            "filename": mask_pii(self.filename), "digest": self.digest, "kind": self.kind,
            "fields": {k: mask_pii(v) for k, v in self.fields.items()}, "total_rows": self.total_rows,
            "valid_rows": self.valid_rows, "duplicate_rows": self.duplicate_rows,
            "skipped_rows": len(self.errors), "errors": self.errors[:30],
            "sessions": len(self.sessions), "estimated_pairs": pair_count,
            "unknown_time": self.unknown_time, "samples": samples,
            "can_import": bool(self.sessions),
        }


def parse_import(data: bytes, filename: str) -> ImportData:
    """纯解析阶段不打开数据库。相同文件内容有稳定会话标识。"""
    filename = Path(filename.replace("\\", "/")).name[:160]
    suffix = Path(filename).suffix.lower()
    if suffix not in (".csv", ".xlsx"):
        raise ValueError("只支持 .csv 和 .xlsx 文件；旧版 .xls 请另存为 .xlsx")
    if not data:
        raise ValueError("文件是空的，请选择包含会话或问答的表格")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("文件超过 8 MB，请拆分后导入")
    if suffix == ".xlsx" and not data.startswith(b"PK"):
        raise ValueError("文件不是有效的 XLSX，请从 Excel 重新保存")
    try:
        rows = read_table(data, max_rows=MAX_IMPORT_ROWS + 1)
    except (zipfile.BadZipFile, ParseError, KeyError, RuntimeError, UnicodeError):
        raise ValueError("无法读取表格，请检查文件是否损坏或加密") from None
    if not rows or len(rows) < 2:
        raise ValueError("表格没有数据行，第一行应为表头")
    if len(rows) - 1 > MAX_IMPORT_ROWS:
        raise ValueError("一次最多导入 5000 行，请拆分文件")
    headers = [str(h).strip() for h in rows[0]]
    nonempty = [_header(h) for h in headers if h]
    if len(nonempty) != len(set(nonempty)):
        raise ValueError("表头有重复列，请为每一列使用不同名称")
    lookup = {_header(h): h for h in headers if h}
    fields = {}
    for key, aliases in ALIASES.items():
        for alias in aliases:
            if _header(alias) in lookup:
                fields[key] = lookup[_header(alias)]
                break
    kind = "会话正文" if "content" in fields else "问答表"
    if "content" not in fields and "question" not in fields:
        raise ValueError("未找到“会话内容”或“问题”列，请下载模板并保留对应表头")
    result = ImportData(filename, hashlib.sha256(data).hexdigest(), kind, fields, 0)
    seen_rows: set[str] = set()
    for row_number, cells in enumerate(rows[1:], 2):
        if not any(str(c).strip() for c in cells):
            continue
        result.total_rows += 1
        row = {headers[i]: str(cells[i]).strip() if i < len(cells) else ""
               for i in range(len(headers)) if headers[i]}
        values = {key: row.get(column, "") for key, column in fields.items()}
        signature = json.dumps(values, ensure_ascii=False, sort_keys=True)
        if signature in seen_rows:
            result.duplicate_rows += 1
            continue
        try:
            text = values.get("content" if kind == "会话正文" else "question", "")
            if not text:
                raise ValueError("会话内容/问题为空，已跳过")
            start = parse_time(values.get("time", ""))
            end = parse_time(values.get("end", "")) or start
            if start and end and end < start:
                raise ValueError("结束时间早于开始时间")
            sid = values.get("session_id") or "local-" + hashlib.sha256(signature.encode()).hexdigest()[:24]
            customer = values.get("customer_id") or "anonymous-" + hashlib.sha256(sid.encode()).hexdigest()[:16]
            session = {
                "id": sid, "userId": customer, "userName": values.get("customer_name", ""),
                "fromGroup": values.get("group", ""), "staffName": values.get("staff", ""),
                "startTime": start, "endTime": end, "sType": 0, "isValid": 1,
                "fromType": "本地导入",
            }
            if kind == "会话正文":
                messages = InspectExporter.split_transcript(text, session["staffName"])
                for message in messages:
                    message["ts"] = message.get("ts") or start
                if not messages or not any(m["role"] == "user" for m in messages):
                    raise ValueError("无法识别客户消息，请使用“客户: … / 客服: …”或填写接待客服")
                result.messages[sid] = messages
            else:
                messages = [{"role": "user", "content": text, "ts": start}]
                if values.get("answer"):
                    messages.append({"role": "staff", "content": values["answer"], "ts": start})
                # 无回复的连续问答也要保持行边界，系统消息不会冒充客服回答。
                if sid in result.messages:
                    messages.insert(0, {"role": "boundary", "content": "", "ts": start})
                result.messages.setdefault(sid, []).extend(messages)
                previous = result.sessions.get(sid)
                if previous:
                    session["startTime"] = min(x for x in (previous["startTime"], start) if x) if (previous["startTime"] or start) else 0
                    session["endTime"] = max(previous["endTime"], end)
            session["roundNumber"] = max(1, len(result.messages[sid]) // 2)
            result.sessions[sid] = session
            result.valid_rows += 1
            result.unknown_time += int(not start)
            seen_rows.add(signature)
        except ValueError as exc:
            result.errors.append({"row": row_number, "reason": str(exc)})
    return result


def import_file(cfg: Settings, data: bytes, filename: str) -> dict[str, Any]:
    parsed = parse_import(data, filename)
    if not parsed.sessions:
        raise ValueError("没有可以导入的会话，请先修正预览中列出的错误")
    db = get_db(cfg)
    old_ids = {r["session_id"] for r in db.iter_sessions()}
    before = db.stats()["qa_pair"]
    with db.tx():
        db.upsert_sessions(parsed.sessions.values())
        for sid, messages in parsed.messages.items():
            db.upsert_messages(sid, messages)
        extracted = stage_extract(cfg)
        summary = {
            "filename": mask_pii(parsed.filename), "sessions": len(parsed.sessions),
            "new_sessions": len(set(parsed.sessions) - old_ids),
            "updated_sessions": len(set(parsed.sessions) & old_ids),
            "skipped_rows": len(parsed.errors), "duplicate_rows": parsed.duplicate_rows,
            "qa_pairs": db.stats()["qa_pair"], "new_pairs": db.stats()["qa_pair"] - before,
            "extract": extracted,
        }
        db.log_task("local_import", summary, parsed.digest, "completed")
    return summary


def clean_local(cfg: Settings) -> dict[str, Any]:
    db = get_db(cfg)
    with db.tx():
        result = stage_extract(cfg, rebuild=True)
        db.log_task("local_clean", result, f"clean-{time.time_ns()}", "completed")
    return result


def demo_settings(cfg: Settings) -> Settings:
    return replace(
        cfg,
        runtime=replace(cfg.runtime, db_path=str(Path(cfg.runtime.db_path).with_name("dashboard_demo.db")), mock=True),
        llm=replace(cfg.llm, dry_run=True), feishu=replace(cfg.feishu, dry_run=True),
    )


def load_demo(cfg: Settings) -> dict[str, Any]:
    """只写独立演示库，不改已有 mock.db 或真实库。重复载入不覆盖数据。"""
    from . import mock as M

    cfg = demo_settings(cfg)
    db = get_db(cfg)
    if not db.stats()["raw_session"]:
        sessions = M.make_sessions(240, days_back=28)
        with db.tx():
            for i, session in enumerate(sessions):
                messages = M.make_transcript(session["id"], session["startTime"])
                session["id"] = "demo-" + session["id"]
                session["userId"] = f"demo-u{1000 + i % 83}"
                session["userName"] = f"访客 {1000 + i % 83}"
                if session["staffName"] == "机器人":
                    session["staffName"] = "陈雨"
                if i % 9 == 0:
                    messages = [m for m in messages if m["role"] == "user"]
                if i % 17 == 0:
                    messages = [{"role": "user", "content": "谢谢", "ts": session["startTime"]},
                                {"role": "staff", "content": "不客气", "ts": session["startTime"]}]
                db.upsert_sessions([session])
                db.upsert_messages(session["id"], messages)
            stage_extract(cfg)
    elif db.local_status()["pending"]:
        stage_extract(cfg)
    return {"source": "demo", "stats": db.stats()}


def csv_bytes(headers: list[str], rows: list[list[Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        safe = []
        for value in row:
            text = mask_pii("" if value is None else str(value))
            if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
                text = "'" + text
            safe.append(text)
        writer.writerow(safe)
    return output.getvalue().encode("utf-8-sig")


def template_bytes() -> bytes:
    return csv_bytes(
        ["会话ID", "客户ID", "客户名称", "客户群", "客服", "时间", "问题", "回答"],
        [["sample-001", "customer-001", "示例客户", "售前咨询组", "客服A", "",
          "这个净水器滤芯多久换一次？", "建议每 6–8 个月更换一次。"],
         ["sample-002", "customer-002", "示例客户二", "售后一组", "客服B", "",
          "退款什么时候到账？", ""]],
    )
