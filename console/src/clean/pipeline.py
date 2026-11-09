"""清洗流水线：规则 → 分类 → 摘要 → 落库。

设计要点：
- 幂等：结果按 (session_id, prompt_version) upsert，重跑不会重复
- 跳过已处理：只取还没清洗过的会话
- 没有正文的会话不硬编摘要，只出规则分并标记，避免模型瞎编
- 难例（risk=high 或 confidence 低）自动升级到 3 档模型
"""

from __future__ import annotations

from typing import Any

from ..config import LLMConfig
from ..db import DB
from ..log import get
from . import prompts as P
from .llm import LLMClient
from .rules import build_clean_text, is_noise, rule_score

log = get("pipeline")


class CleanPipeline:
    def __init__(self, db: DB, llm_cfg: LLMConfig):
        self.db = db
        self.cfg = llm_cfg
        self.llm = LLMClient(llm_cfg)

    def run(self, limit: int = 200, prompt_version: str = P.PROMPT_VERSION) -> dict[str, int]:
        rows = self.db.iter_sessions(limit=limit, only_uncleaned=prompt_version)
        stats = {"candidates": len(rows), "noise": 0, "no_text": 0,
                 "classified": 0, "escalated": 0, "written": 0}

        if not rows:
            log.info("没有待清洗的会话")
            return stats

        # 1) 规则层：剔除噪声 + 组装文本
        work: list[tuple[Any, str]] = []
        for r in rows:
            noise, reason = is_noise(r)
            if noise:
                self._write_skip(r, reason, prompt_version)
                stats["noise"] += 1
                continue

            sid = str(r["session_id"])
            transcript = self.db.get_transcript(sid)
            text = build_clean_text(r, transcript, self.cfg.max_input_chars)
            if not text:
                self._write_skip(r, "无正文（质检未覆盖）", prompt_version)
                stats["no_text"] += 1
                continue
            work.append((r, text))

        if not work:
            log.info("规则层过滤后无可清洗会话：噪声 %d，无正文 %d",
                     stats["noise"], stats["no_text"])
            return stats

        log.info("规则层过滤后剩 %d 条进入模型", len(work))

        # 2) 分类（便宜模型，批处理）
        texts = [t for _, t in work]
        cats = self.llm.classify(texts)

        # 3) 摘要（主力模型，并发）
        summaries = self.llm.map(self.llm.summarize, texts)

        # 4) 难例升级
        for i, (r, text) in enumerate(work):
            c = cats[i] or {}
            s = summaries[i] or {}
            if c.get("risk") == "high" or float(s.get("confidence") or 1) < 0.6:
                better = self.llm.escalate(text)
                if better:
                    s = {**s, **better}
                    stats["escalated"] += 1
            summaries[i] = s

        # 5) 落库
        for i, (r, text) in enumerate(work):
            c = cats[i] or {}
            s = summaries[i] or {}
            sid = str(r["session_id"])
            self.db.upsert_clean({
                "session_id": sid,
                "prompt_version": prompt_version,
                "model": self.cfg.model_main,
                "category": c.get("category", ""),
                "sub_category": c.get("sub", ""),
                "emotion": c.get("emotion", ""),
                "resolved": 1 if c.get("resolved") else 0,
                "risk": c.get("risk", ""),
                "demand": s.get("demand", ""),
                "result": s.get("result", ""),
                "product": s.get("product", ""),
                "followup": 1 if s.get("followup") else 0,
                "one_line": s.get("one_line", ""),
                "quality_score": rule_score(r),
                "confidence": float(c.get("confidence") or s.get("confidence") or 0),
                "payload": {"classify": c, "summary": s},
            })
            stats["classified"] += 1
            stats["written"] += 1

        log.info("清洗完成：%s", stats)
        return stats

    def _write_skip(self, row: Any, reason: str, prompt_version: str) -> None:
        """被规则层剔除的会话也落一行，标记原因，防止下次重复处理。"""
        self.db.upsert_clean({
            "session_id": str(row["session_id"]),
            "prompt_version": prompt_version,
            "model": "",
            "category": "噪声" if "无正文" not in reason else "无正文",
            "sub_category": "",
            "emotion": "",
            "resolved": 0,
            "risk": "",
            "demand": "",
            "result": "",
            "product": "",
            "followup": 0,
            "one_line": reason,
            "quality_score": rule_score(row),
            "confidence": 0.0,
            "payload": {"skip_reason": reason},
        })
