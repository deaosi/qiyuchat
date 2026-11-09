"""问题聚类：字符 2-gram Jaccard + 并查集。零依赖、零成本、可解释。

为什么不用 embedding：
- 客服问题句式高度重复（「这个多少钱」「什么时候发货」），2-gram 已经够
- 不需要 GPU、不需要联网、不需要花钱
- 结果可解释：两个人为什么被归到一类，能算给你看

什么时候该升级到 embedding：
- 同义不同形严重（「发货慢」vs「物流太磨叽」）
- 数据量 > 5 万条，字符匹配的召回开始掉

性能：用倒排索引先筛候选，避免 O(n²) 全比较。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from .concepts import MERGEABLE, concept_signature
from .normalize import grams


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / (len(a) + len(b) - inter)


def similarity(g1: set[str], g2: set[str], s1: frozenset[str], s2: frozenset[str],
               w_gram: float = 0.5, w_concept: float = 0.5) -> float:
    """字面相似 + 业务概念相似 的加权和。

    只用字面：抓不住「这个多少钱 / 价格是多少」这种同义不同形
    只用概念：会把「滤芯多少钱 / 机器多少钱」并成一坨
    两个一起才对。
    """
    g = jaccard(g1, g2)
    c = 0.0
    if s1 and s2:
        inter = len(s1 & s2)
        if inter and (s1 & s2 & MERGEABLE):
            c = inter / len(s1 | s2)
    return w_gram * g + w_concept * c


class _DSU:
    def __init__(self, n: int):
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]:
            self.r[ra] += 1


def cluster_questions(
    items: Iterable[tuple[int, str]],
    threshold: float = 0.45,
    max_df_ratio: float = 0.25,
) -> list[dict[str, Any]]:
    """items = [(id, 归一化问题)]，返回簇列表。

    max_df_ratio：出现频率超过这个比例的 gram 视为「太常见」，不参与候选生成，
    否则「这个」「什么」这类词会把所有问题连成一坨。
    """
    # 高频剪枝只用于“不同问法”的候选召回；相同问法必须先合并。
    identical: dict[str, list[int]] = defaultdict(list)
    for item_id, text in items:
        if text:
            identical[text].append(item_id)
    texts = sorted(identical)
    n = len(texts)
    if n == 0:
        return []

    gsets = [grams(t) for t in texts]
    sigs = [concept_signature(t) for t in texts]

    # 1) 倒排索引 + 高频 gram 剪枝
    df: Counter[str] = Counter()
    for gs in gsets:
        df.update(gs)
    # 小数据集不做剪枝 —— 否则「价格」这种全命中概念会被当成高频词丢掉，
    # 反而把本该合并的问题拆开
    max_df = n if n < 12 else max(8, int(n * max_df_ratio))

    inverted: dict[str, list[int]] = defaultdict(list)
    for pos, gs in enumerate(gsets):
        for g in gs:
            if df[g] <= max_df:
                inverted[g].append(pos)

    # 2) 候选对：字面倒排 + 概念倒排（后者补上「同义不同形」）
    dsu = _DSU(n)
    # seen 用来避免同一对在不同 gram 桶里被重复计算。
    # 但它的体积是 O(候选对)，数据量大时会吃掉几百 MB —— 超过阈值就不维护它，
    # 改用「已经同簇就跳过」的廉价判断（union 本身是幂等的，结果不变）。
    keep_seen = n <= 8000
    seen: set[tuple[int, int]] = set()

    def _try(a: int, b: int) -> None:
        if keep_seen:
            key = (a, b) if a < b else (b, a)
            if key in seen:
                return
            seen.add(key)
        if dsu.find(a) == dsu.find(b):
            return
        if similarity(gsets[a], gsets[b], sigs[a], sigs[b]) >= threshold:
            dsu.union(a, b)

    for g, plist in sorted(inverted.items()):
        if len(plist) > max_df:
            continue
        for x in range(len(plist)):
            for y in range(x + 1, len(plist)):
                _try(plist[x], plist[y])

    concept_idx: dict[str, list[int]] = defaultdict(list)
    for pos, s in enumerate(sigs):
        for c in s:
            concept_idx[c].append(pos)
    for c, plist in sorted(concept_idx.items()):
        if len(plist) > max_df:
            continue
        for x in range(len(plist)):
            for y in range(x + 1, len(plist)):
                _try(plist[x], plist[y])

    # 3) 归组
    buckets: dict[int, list[int]] = defaultdict(list)
    for pos in range(n):
        buckets[dsu.find(pos)].append(pos)

    clusters: list[dict[str, Any]] = []
    for root, positions in buckets.items():
        counts = Counter({texts[p]: len(identical[texts[p]]) for p in positions})
        rep = sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
        member_ids = sorted(i for p in positions for i in identical[texts[p]])
        clusters.append({
            "key": root,
            "rep_norm": rep[0][0],
            "size": len(member_ids),
            "ids": member_ids,
        })

    clusters.sort(key=lambda c: (-c["size"], c["rep_norm"]))
    for i, c in enumerate(clusters, 1):
        c["cluster_id"] = i
    return clusters
