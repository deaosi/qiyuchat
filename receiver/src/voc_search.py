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
from .voc_topic_insights import _PLATFORM_NAMES

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


def staff_claims(item: dict) -> list[str]:
    if item.get('staff_source_verified') is not True:
        return []
    return [claim['quote'] for claim in item.get('claims', [])
            if isinstance(claim, dict) and claim.get('role') == 'staff'
            and isinstance(claim.get('quote'), str) and claim['quote'].strip()]


def verified_pair_reply(item: dict) -> str:
    """Read the exact staff answer in a source-anchored pair without intent excerpts."""
    answer, ids = item.get('answer'), item.get('answer_message_ids')
    if (item.get('staff_source_verified') is True and item.get('source_kind') == 'source-anchored'
            and not item.get('claims') and isinstance(answer, str) and isinstance(ids, list)
            and ids and all(isinstance(mid, str) and mid.strip() for mid in ids)):
        return answer.strip()
    return ''


def has_staff_reply(item: dict) -> bool:
    return bool(staff_claims(item) or verified_pair_reply(item))


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
        # The full paired reply may include other intents. It can be shown
        # after matching the question, but its text cannot drive relevance.
        values += staff_claims(item)
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


STAFF_SYMPTOMS = {
    '机器人不回复': re.compile(
        r'(?:机器人|AI|智能客服|福客)[^。！？\n]{0,12}(?:(?:没(?:有)?|未|不|无法)(?:正常|自动)?(?:回复|回应|应答|接待)|不理)',
        re.I),
    '场景未触发': re.compile(
        r'场景[^。！？\n]{0,16}(?:没(?:有)?|未|无法|未能|不)(?:正常)?(?:触发|命中|生效)'
        r'|(?:没有|没|未|无法)(?:触发|命中)[^。！？\n]{0,12}场景'),
}

_LOCAL_FILLER = re.compile(
    r"请帮我|能不能|想知道|告诉我|给我看看|帮我|查一下|查找|查看|查询|了解|"
    r"有哪些|有什么|看看|上面|之前|目前|当前|范围|关于|相关|对应|具体|"
    r"真实(?:会话|案例|记录)?|排查依据|原始依据|客服原话|人工客服|问题|情况|案例|"
    r"数据|统计|平均|排行|咨询量|最近\s*[0-9]+\s*天|有没有|(?:的|吗)(?=\W|$)"
)
_LOCAL_GENERIC_WORDS = ('客户', '客服', '会话', '咨询', '回复', '处理', '记录', '功能', '操作', '设置', '系统', '语海voc')
_LOCAL_GENERIC_TERMS = _terms(' '.join(_LOCAL_GENERIC_WORDS))
_LOCAL_GENERIC_RUN = re.compile(r'(?:' + '|'.join((*_LOCAL_GENERIC_WORDS, '和', '与', '及', '或', '的')) + r')+')
_EXTRA_PLATFORM_ALIASES = {'抖音': ('抖店',), '淘宝': ('千牛',)}
_LOCAL_PLATFORMS = {name: (*aliases, *_EXTRA_PLATFORM_ALIASES.get(name, ())) for name, aliases in _PLATFORM_NAMES}
_LOCAL_SYMPTOM_VARIANTS = {
    '机器人不回复': re.compile(
        r'(?:机器人|AI|智能客服|福客)(?:一直|有时|有时候|偶尔|突然|仍然|还是)?'
        r'(?:没(?:有)?|未|不|无法)(?:正常|自动)?(?:回复|回应|应答|接待|回(?=\W|$))',
        re.I),
    '场景未触发': re.compile(
        r'场景(?:一直|有时|有时候|偶尔|突然|仍然|还是)?'
        r'(?:没(?:有)?|未|无法|未能|不)(?:正常)?(?:触发|命中|生效)'),
}


def _explicit_platforms(text: str) -> set[str]:
    return {name for name, aliases in _LOCAL_PLATFORMS.items() if any(alias in text for alias in aliases)}


def _normalize_local(text: str) -> str:
    text = unicodedata.normalize('NFKC', text)
    for label, pattern in _LOCAL_SYMPTOM_VARIANTS.items():
        text = pattern.sub(label, text)
    for platform, aliases in _LOCAL_PLATFORMS.items():
        for alias in aliases:
            text = text.replace(alias, platform)
    return text


def _local_terms(query: str) -> set[str]:
    subject = _LOCAL_FILLER.sub(' ', _normalize_local(query)).casefold()
    words = [word for word in _WORDS.findall(subject) if not _LOCAL_GENERIC_RUN.fullmatch(word)]
    return _terms(' '.join(words)) - _LOCAL_GENERIC_TERMS


def has_local_evidence_terms(query: str) -> bool:
    return len(_local_terms(query)) >= 3


def _merchant_quotes(refs: object) -> list[str]:
    refs = refs if isinstance(refs, list) else [refs]
    return [ref['quote'] for ref in refs
            if isinstance(ref, dict) and ref.get('source') == 'question' and ref.get('alignment') == 'exact'
            and isinstance(ref.get('quote'), str) and ref['quote'].strip()]


def rank_local_evidence(query: str, items: list[dict], limit: int) -> list[dict]:
    """Find directly related source text for use when model planning is unavailable."""
    terms = _local_terms(query)
    if len(terms) < 3:
        return []
    platform_filter = _explicit_platforms(query)
    symptoms = [label for label in _LOCAL_SYMPTOM_VARIANTS if label in _normalize_local(query)]
    matches = []
    for item in items:
        domain = item.get('domain') or {}
        question = item.get('question_quote') or item.get('question', '')
        evidence = list(dict.fromkeys([question, *_merchant_quotes(domain.get('merchant_goal')),
                                      *_merchant_quotes(domain.get('expected')), *_merchant_quotes(domain.get('actual'))]))
        platforms = _explicit_platforms(' '.join(evidence + _merchant_quotes(domain.get('platforms'))))
        if platform_filter and platform_filter != platforms:
            continue
        scores = []
        # Require enough shared wording in one quote. A reply or broad topic
        # title may mention the same words while addressing a different issue.
        for part in evidence:
            part = _normalize_local(part)
            if not all(label in part for label in symptoms):
                continue
            overlap = terms & _terms(part)
            longer = sum(len(term) >= 3 for term in overlap)
            if len(overlap) >= 3 and len(overlap) * 2 >= len(terms) and longer:
                scores.append((len(overlap), longer))
        if scores:
            matches.append((max(scores), has_staff_reply(item), item))
    matches.sort(key=lambda match: (-match[0][0], -match[0][1], -match[1],
                                    -(match[2].get('asked_at') or 0), match[2]['id']))
    return [item for _, _, item in matches[:limit]]


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
