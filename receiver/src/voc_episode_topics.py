"""Group issue episodes for VOC replay, with optional local semantic models.

Topic identity uses a stable episode anchor and is reproducible for a fixed
cohort. A persisted catalog is needed to preserve IDs across changing cohorts.
Similarity is evidence for matching, never evidence for a knowledge answer.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from pathlib import Path
import re
import unicodedata
from typing import Callable

from .extract.cluster import jaccard
from .voc_assets import digest
from .voc_search import _terms
from .voc_semantics import Embedder, SemanticError, normalize, similarity


_MISSING = {"未知", "不明确", "未提及", "未提供", "不适用", "unknown", "n/a"}
_PLATFORM_NAMES = {"抖音小店": "抖店", "抖音电商": "抖店", "抖音": "抖店", "京东商城": "京东"}
_ERROR_CODE = re.compile(r"(?:错误码|报错码|异常码|\berror\s+code|\bhttp)[\s:：-]*([a-z0-9][a-z0-9_-]{1,19})", re.I)
_SYMPTOM_PATTERNS = {
    "missing": re.compile(r"(?:未|没有?|不)(?:能|会)?(?:触发|回复|响应|生效|同步|命中|显示)|无法(?:触发|回复|同步)"),
    "repeated": re.compile(r"重复(?:触发|回复|响应|同步|命中|执行|显示)|(?:多次|两次)(?:触发|回复|响应|执行)"),
    "delayed": re.compile(r"(?:触发|回复|响应|同步|生效|显示)(?:慢|有延迟|延迟)|延迟(?:触发|回复|响应|同步|生效|显示)"),
    "wrong": re.compile(r"误触发|错触发|答错|(?:触发|回复|同步|显示)(?:错了|错误)"),
}
_CONFIG_ACTIONS = {
    "enable": re.compile(r"启用|开启|开通"),
    "disable": re.compile(r"禁用|关闭|关停|停用"),
    "add": re.compile(r"新增|添加|创建"),
    "remove": re.compile(r"删除|移除"),
    "import": re.compile(r"导入"),
    "export": re.compile(r"导出"),
}
_OPPOSITE_ACTIONS = ({"enable", "disable"}, {"add", "remove"}, {"import", "export"})
_GENERIC_GOALS = {"配置", "设置", "查看", "修改", "启用", "开启", "同步", "排查", "解决", "处理"}
_EXACT_MATCH = 2.
_RERANK_CANDIDATES = 12
_AMBIGUITY_MARGIN = .05

Reranker = Callable[[list[tuple[str, str]]], list[float]]


class LocalReranker:
    """CPU cross-encoder using an explicitly provided local model directory."""

    def __init__(self, model_directory: str | Path):
        self.model_directory = Path(model_directory).expanduser().resolve()
        if not str(model_directory).strip() or not self.model_directory.is_dir():
            raise ValueError("VOC 重排模型必须指向已经准备好的本机模型目录。")
        self._encoder = None

    def __call__(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        try:
            from sentence_transformers import CrossEncoder
            from torch.nn import Sigmoid

            if self._encoder is None:
                self._encoder = CrossEncoder(str(self.model_directory), device="cpu", max_length=512,
                                             local_files_only=True, trust_remote_code=False, token=False)
            if self._encoder.num_labels != 1:
                raise ValueError("Expected a single relevance score per pair.")
            scores = self._encoder.predict(pairs, activation_fn=Sigmoid(), batch_size=8,
                                           show_progress_bar=False, convert_to_numpy=True)
            return [float(score) for score in scores]
        except (ImportError, OSError, RuntimeError, ValueError, TypeError):
            raise SemanticError("本机 VOC 重排模型不可用，请检查模型目录和 CPU 依赖。") from None


def _key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip().casefold()
    if normalized in _MISSING:
        return ""
    return re.sub(r"[^\w\u3400-\u9fff]", "", normalized)


@dataclass(frozen=True)
class _Issue:
    episode: dict
    title: str
    module: str
    platform: str
    goal: str
    symptom: str
    quote: str
    text: str
    terms: set[str]
    codes: set[str]
    distinctions: set[str]

    @property
    def kind(self) -> str:
        return self.episode["kind"]


def _issue(episode: dict) -> _Issue:
    kind = episode["kind"]
    goal, symptom = episode["goal"], episode["symptom"]
    title = episode["title"]
    quote = episode["question"]["quote"]
    platform = _key(episode["platform"])
    text = "\n".join(dict.fromkeys(part for part in (title, goal, symptom) if part.strip()))
    patterns, signal = (_SYMPTOM_PATTERNS, symptom) if kind == "anomaly" else (_CONFIG_ACTIONS, goal)
    return _Issue(episode, _key(title), _key(episode["module"]), _PLATFORM_NAMES.get(platform, platform),
                  _key(goal), _key(symptom), _key(quote), text, _terms(text + "\n" + quote),
                  set(_ERROR_CODE.findall((text + "\n" + quote).casefold())),
                  {name for name, pattern in patterns.items() if pattern.search(signal)})


def _conflict(first: set[str], second: set[str]) -> bool:
    return bool(first and second and first.isdisjoint(second))


def _exact(first: _Issue, second: _Issue) -> bool:
    same_goal = first.goal == second.goal
    same_symptom = first.symptom == second.symptom
    same_title = first.title == second.title
    specific_goal = first.goal and first.goal not in _GENERIC_GOALS
    same_core = bool(first.module and first.module == second.module and specific_goal and same_goal
                     and (first.kind == "configuration" or first.symptom and same_symptom))
    if first.platform and second.platform and first.platform != second.platform:
        return same_core
    return same_core or bool(same_title and same_goal and same_symptom and first.quote and first.quote == second.quote)


def _candidate(first: _Issue, second: _Issue, semantic: float) -> tuple[bool, float] | None:
    if (first.kind != second.kind or (first.module and second.module and first.module != second.module)
            or _conflict(first.codes, second.codes)):
        return None
    if first.kind == "anomaly" and _conflict(first.distinctions, second.distinctions):
        return None
    if first.kind == "configuration" and any(_conflict(first.distinctions & axis, second.distinctions & axis)
                                              for axis in _OPPOSITE_ACTIONS):
        return None
    if _exact(first, second):
        return True, 0.
    if (not first.module or first.module != second.module or not (first.goal or first.symptom)
            or not (second.goal or second.symptom)):
        return None
    if first.platform and second.platform and first.platform != second.platform:
        return None
    lexical = jaccard(first.terms, second.terms)
    if lexical < .28 and semantic < .60:
        return None
    return False, lexical


def _evidence_text(issue: _Issue) -> str:
    return f"{issue.episode['module']}\n{issue.text}\n{issue.episode['question']['quote']}"


def _scores(issues: list[_Issue], embedder: Embedder | None, rerank: Reranker | None) -> dict[tuple[int, int], float]:
    vectors = []
    if embedder is not None:
        vectors = [normalize(vector) for vector in embedder.encode([item.text for item in issues])]
        if len(vectors) != len(issues) or len({len(vector) for vector in vectors}) != 1:
            raise ValueError("VOC 主题召回的嵌入向量不完整或维度不一致。")
    strong = {}
    candidates: dict[int, list[tuple[float, tuple[int, int]]]] = defaultdict(list)
    for i, first in enumerate(issues):
        for j in range(i + 1, len(issues)):
            second = issues[j]
            semantic = similarity(vectors[i], vectors[j]) if vectors else 0.
            result = _candidate(first, second, semantic)
            if result is None:
                continue
            exact, lexical = result
            pair = i, j
            if exact:
                strong[pair] = _EXACT_MATCH
            elif rerank is not None:
                score = .65 * max(0., semantic) + .35 * lexical if vectors else lexical
                candidates[i].append((score, pair))
                candidates[j].append((score, pair))
            elif (not vectors and lexical >= .74 or vectors and
                  (semantic >= .90 and lexical >= .43 or semantic >= .70 and lexical >= .74)):
                strong[pair] = .65 * semantic + .35 * lexical if vectors else lexical
    if candidates and rerank is not None:
        pairs = sorted({pair for options in candidates.values()
                        for _, pair in sorted(options, key=lambda option: (-option[0], option[1]))[:_RERANK_CANDIDATES]})
        scores = [float(score) for score in rerank([(_evidence_text(issues[i]), _evidence_text(issues[j])) for i, j in pairs])]
        if len(scores) != len(pairs) or any(not math.isfinite(score) or not 0. <= score <= 1. for score in scores):
            raise ValueError("VOC 主题重排须为每个候选返回一个 0 到 1 之间的分数。")
        strong.update((pair, score) for pair, score in zip(pairs, scores) if score >= .88)
    return strong


def _pair_key(i: int, j: int) -> tuple[int, int]:
    return (i, j) if i < j else (j, i)


def _ambiguous(edges: dict[tuple[int, int], float]) -> set[int]:
    neighbors: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for (i, j), score in edges.items():
        neighbors[i].append((j, score))
        neighbors[j].append((i, score))
    uncertain = set()
    for node, options in neighbors.items():
        best = max(score for _, score in options)
        close = [other for other, score in options if best - score <= _AMBIGUITY_MARGIN]
        if any(_pair_key(i, j) not in edges for pos, i in enumerate(close) for j in close[pos + 1:]):
            uncertain.add(node)
    return uncertain


def group(episodes: list[dict], *, embedder: Embedder | None = None,
          rerank: Reranker | None = None) -> list[dict]:
    """Rank reproducible topics in one replay cohort.

    Optional reranking receives pairs of source-backed issue descriptions and
    returns one relevance score in [0, 1] per pair. Thresholds are conservative
    replay defaults for comparison, pending calibration on labeled cases.
    Unclear episodes and ties between incompatible candidates remain unassigned.
    """
    ids = [episode["id"] for episode in episodes]
    if len(set(ids)) != len(ids):
        raise ValueError("VOC 咨询事件编号重复。")
    issues = [_issue(item) for item in sorted(episodes, key=lambda episode: episode["id"])
              if item["kind"] in ("configuration", "anomaly") and _key(item["title"])]
    if not issues:
        return []
    strong = _scores(issues, embedder, rerank)
    uncertain = _ambiguous(strong)
    groups = {i: {i} for i in range(len(issues)) if i not in uncertain}
    owner = {i: i for i in groups}
    for (i, j), _ in sorted(strong.items(), key=lambda item: (-item[1], item[0])):
        if i not in owner or j not in owner or owner[i] == owner[j]:
            continue
        first, second = sorted((owner[i], owner[j]))
        if all(_pair_key(a, b) in strong for a in groups[first] for b in groups[second]):
            moved = groups.pop(second)
            groups[first].update(moved)
            owner.update((node, first) for node in moved)
    result = []
    for anchor, members in groups.items():
        episode = issues[anchor].episode
        rows = [issues[pos].episode for pos in sorted(members)]
        result.append({"id": "topic_" + digest(["episode-topic-v1", episode["id"]])[:24],
                       "title": episode["title"], "kind": episode["kind"],
                       "episode_ids": [row["id"] for row in rows],
                       "consultation_count": len({(row["session_key"], row["customer_key"]) for row in rows}),
                       "customer_count": len({row["customer_key"] for row in rows})})
    return sorted(result, key=lambda item: (-item["consultation_count"], -item["customer_count"], item["id"]))
