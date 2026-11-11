"""Rank scoped VOC material itself, without changing its evidence/quality gates.

Semantic scores come from the existing local BGE cache. Lexical overlap helps
distinguish software objects, settings and identifiers inside one broad topic;
neither a topic match nor a rank score establishes that an answer is verified.
"""
from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
import re
import unicodedata

from .voc_semantics import Embedder, QUERY_PREFIX, similarity, vectors

_FILLER = re.compile(r"请问|麻烦|谢谢|您好|你好|如何|怎么|怎样|为什么|为何|什么|哪些|一下|这个|那个|我们|你们|是否|能否|可以|需要|知汇鸽")
_WORDS = re.compile(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*|[\u3400-\u9fff]+")


def _terms(text: str) -> set[str]:
    value = _FILLER.sub(" ", unicodedata.normalize("NFKC", text).casefold())
    result = set()
    for word in _WORDS.findall(value):
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            if len(word) == 1:
                continue
            for size in (2, 3):
                result.update(word[index:index + size] for index in range(len(word) - size + 1))
        else:
            result.add(word)
    return result


def _quotes(value: object) -> list[str]:
    if isinstance(value, dict):
        return [value['quote']] if isinstance(value.get('quote'), str) and value['quote'].strip() else []
    if isinstance(value, list):
        return [quote for item in value for quote in _quotes(item)]
    return []


def has_staff_reply(item: dict) -> bool:
    return item.get('staff_source_verified') is True and any(
        isinstance(claim, dict) and claim.get('role') == 'staff'
        and isinstance(claim.get('quote'), str) and claim['quote'].strip()
        for claim in item.get('claims', []))


def material_parts(item: dict, kind: str) -> list[str]:
    """Event answers use intent-bound human claims, never another intent's blob."""
    values = [item.get('question_quote') or item.get('question', '')]
    if kind == 'topic':
        values += [item.get('theme', ''), item.get('boundary', ''), *item.get('samples', [])]
    elif kind == 'knowledge':
        values += [item.get('answer', ''), item.get('scope', ''), *item.get('steps', []),
                   *item.get('cautions', []), *item.get('reply_patterns', []),
                   *_quotes(item.get('applicability')), *_quotes(item.get('confirmed_cause'))]
    elif kind == 'event':
        if has_staff_reply(item):
            values += [claim['quote'] for claim in item['claims']
                       if isinstance(claim, dict) and claim.get('role') == 'staff'
                       and isinstance(claim.get('quote'), str)]
        domain = item.get('domain') or {}
        for key in ('platforms', 'modules', 'prerequisites', 'operation_path', 'merchant_goal', 'expected', 'actual'):
            values += _quotes(domain.get(key))
        values += _quotes((domain.get('diagnosis') or {}).get('cause'))
        for action in domain.get('actions', []):
            if isinstance(action, dict):
                values += _quotes(action.get('instruction')) + _quotes(action.get('verification'))
    else:
        raise ValueError('Unsupported VOC retrieval material kind')
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def _chunks(parts: list[str], query_terms: set[str]) -> list[str]:
    chunks = list(dict.fromkeys(piece[start:start + 240]
                  for part in parts for piece in re.split(r'[\n。！？；!?;]', part) if piece.strip()
                  for start in range(0, len(piece), 240)))
    # Rank the full text lexically below. For bounded local embedding, include
    # the most query-specific clauses rather than blindly losing the tail.
    ordered = sorted(enumerate(chunks), key=lambda value: (-len(_terms(value[1]) & query_terms), value[0]))
    return [value for _, value in ordered[:12]]


def rank_materials(store: Path, embedder: Embedder, query: str, items: list[dict], *,
                   kind: str, limit: int | None = None, require_staff: bool = False) -> list[dict]:
    if require_staff and kind == 'event':
        items = [item for item in items if has_staff_reply(item)]
    prepared = [(item, material_parts(item, kind)) for item in items]
    prepared = [(item, parts) for item, parts in prepared if parts]
    if not prepared:
        return []
    query_terms = _terms(query)
    terms = [_terms('\n'.join(parts)) for _, parts in prepared]
    frequency = Counter(term for values in terms for term in values)
    weights = {term: 1 + math.log((len(prepared) + 1) / (frequency[term] + 1)) for term in query_terms}
    denominator = sum(weights.values()) or 1
    query_chunks = _chunks([query], query_terms) or [query[:240]]
    chunks = [_chunks(parts, query_terms) for _, parts in prepared]
    texts = [QUERY_PREFIX + text for text in query_chunks] + [text for group in chunks for text in group]
    encoded = vectors(store, embedder, texts)
    query_vectors = encoded[:len(query_chunks)]
    offset = len(query_chunks)
    ranked = []
    for (item, _parts), item_terms, group in zip(prepared, terms, chunks):
        document_vectors = encoded[offset:offset + len(group)]
        offset += len(group)
        semantic = max((similarity(a, b) for a in query_vectors for b in document_vectors), default=-1.)
        overlap = query_terms & item_terms
        lexical = sum(weights[term] for term in overlap) / denominator
        # Broad-topic membership alone is deliberately absent. Strong semantic
        # paraphrases may match without shared spelling; weaker ones need words
        # from the actual material. These are retrieval thresholds, not truth.
        if not (semantic >= .68 or semantic >= .50 and lexical >= .12
                or lexical >= .55 and semantic >= .30):
            continue
        score = .65 * max(0., semantic) + .35 * lexical
        ranked.append({**item, 'similarity': round(semantic, 6),
                       'retrieval': {'score': round(score, 6), 'semantic': round(semantic, 6),
                                     'lexical': round(lexical, 6), 'matched_terms': sorted(overlap)[:16]}})
    ranked.sort(key=lambda item: (-item['retrieval']['score'],
                                -int(kind == 'event' and has_staff_reply(item)), str(item.get('id', ''))))
    return ranked if limit is None else ranked[:limit]
