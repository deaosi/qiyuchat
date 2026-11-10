"""CPU-only Chinese semantic retrieval, with an explicit local model cache.

No dependency/model download is performed by an API call or worker. There is no
hash-vector or keyword fallback masquerading as semantic retrieval.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Protocol

from . import voc_assets as assets

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class SemanticError(RuntimeError):
    pass


class Embedder(Protocol):
    model: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class LocalBGE:
    _models: dict[tuple[str, str], object] = {}
    _lock = threading.RLock()

    def __init__(self, cache_dir: Path, model: str = DEFAULT_MODEL):
        self.model = os.environ.get("VOC_EMBED_MODEL_PATH", "").strip() or model
        self.cache_dir = cache_dir

    def availability(self) -> dict:
        installed = importlib.util.find_spec("sentence_transformers") is not None
        local = Path(self.model).is_dir() or (self.cache_dir / ("models--" + self.model.replace("/", "--"))).is_dir()
        return {"ready": installed and local, "model": self.model,
                "error": "" if installed and local else "中文语义模型尚未在本机准备完成，请先安装依赖并缓存模型。"}

    def encode(self, texts: list[str]) -> list[list[float]]:
        key = (self.model, str(self.cache_dir.resolve()))
        try:
            with self._lock:
                if key not in self._models:
                    from sentence_transformers import SentenceTransformer
                    self._models[key] = SentenceTransformer(self.model, device="cpu",
                        cache_folder=str(self.cache_dir), local_files_only=True,
                        trust_remote_code=False)
                model = self._models[key]
                values = model.encode(texts, batch_size=16, normalize_embeddings=True,
                                      show_progress_bar=False, convert_to_numpy=True)
            return [normalize(list(row)) for row in values]
        except (ImportError, OSError, RuntimeError, ValueError, TypeError):
            raise SemanticError("本机中文语义模型不可用，请检查依赖和本地模型缓存；未调用远程嵌入服务。") from None


def normalize(vector: list[float]) -> list[float]:
    if not isinstance(vector, (list, tuple)) or not vector:
        raise SemanticError("语义模型返回了无效向量。")
    try:
        values = [float(value) for value in vector]
    except (ValueError, TypeError):
        raise SemanticError("语义模型返回了无效向量。") from None
    if any(not math.isfinite(value) for value in values):
        raise SemanticError("语义模型返回了无效向量。")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        raise SemanticError("语义模型返回了空向量。")
    return [value / norm for value in values]


def vectors(path: Path, embedder: Embedder, texts: list[str]) -> list[list[float]]:
    keys = [assets.digest(text) for text in texts]
    cached: dict[str, list[float]] = {}
    with assets.connection(path) as conn:
        for key in set(keys):
            row = conn.execute("SELECT vector FROM v2_vector WHERE model=? AND text_hash=?", (embedder.model, key)).fetchone()
            if row:
                cached[key] = normalize(json.loads(row["vector"]))
    missing = list(dict.fromkeys(text for text, key in zip(texts, keys) if key not in cached))
    if missing:
        # No SQLite connection or transaction exists during local model work.
        computed = embedder.encode(missing)
        if len(computed) != len(missing):
            raise SemanticError("语义模型未返回完整向量。")
        prepared = [(embedder.model, assets.digest(text), normalize(vector)) for text, vector in zip(missing, computed)]
        if len({len(row[2]) for row in prepared}) != 1:
            raise SemanticError("语义模型返回的向量维度不一致。")
        with assets.connection(path, write=True) as conn:
            with assets.transaction(conn):
                for model, key, vector in prepared:
                    conn.execute("INSERT OR REPLACE INTO v2_vector VALUES (?,?,?)", (model, key, assets.dump(vector)))
                    cached[key] = vector
    output = [cached[key] for key in keys]
    if output and len({len(vector) for vector in output}) != 1:
        raise SemanticError("缓存向量与当前语义模型维度不一致，请更换缓存模型标识。")
    return output


def similarity(first: list[float], second: list[float]) -> float:
    if len(first) != len(second):
        raise SemanticError("语义向量维度不一致。")
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(first, second))))


def segments(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[\n。！？；!?;]", text) if part.strip()]
    return [part[start:start + 240] for part in parts for start in range(0, len(part), 240)][:24] or [text[:240]]


def retrieve(path: Path, embedder: Embedder, question: str, topics: list[dict],
             top_k: int = 5, minimum: float = 0.50) -> list[dict]:
    if not 1 <= top_k <= 10:
        raise SemanticError("语义候选数须为 1–10。")
    if not topics:
        return []
    queries = segments(question)
    docs = [topic["question"] + "\n" + topic["theme"] for topic in topics]
    encoded = vectors(path, embedder, [QUERY_PREFIX + query for query in queries] + docs)
    qvectors, dvectors = encoded[:len(queries)], encoded[len(queries):]
    scored = [(max(similarity(query, vector) for query in qvectors), topic)
              for topic, vector in zip(topics, dvectors)]
    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [{**topic, "similarity": round(score, 6)} for score, topic in scored[:top_k] if score >= minimum]
