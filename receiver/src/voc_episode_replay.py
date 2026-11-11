"""Compare issue extraction with current VOC on a bounded, read-only replay."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable

from . import voc, voc_assets, voc_outcome
from .clean.rules import mask_pii
from .config import Settings, load_settings
from .extract.text import clean_visible_text
from .model_protocol import ModelProtocolError
from .voc_semantics import LocalBGE, SemanticError


_EXTERNAL_LINK = re.compile(r"https?://[^\s\u3000，。；！？]+", re.I)


def _alias(namespace: str, value: str) -> str:
    return voc_assets.digest([namespace, value])[:24]


def _segments(snapshot: dict, session_key: str, display_names: list[str]) -> list[dict]:
    groups: dict[int, dict] = {}
    for row in snapshot['messages']:
        customer, mid = row['_owner'], row.get('source_message_id')
        if not customer or customer.startswith('qiyu-session:') or not mid or row['role'] not in ('user', 'staff', 'bot'):
            continue
        key = row['_segment']
        if key not in groups:
            groups[key] = {'session_key': session_key, 'customer_key': _alias('customer', customer),
                           'messages': [], 'source_ids': set()}
        group = groups[key]
        if row['_is_text']:
            text = _EXTERNAL_LINK.sub('[链接]', mask_pii(clean_visible_text(row['content'] or '')))
            for name in display_names:
                text = text.replace(name, '[名称]')
            kind = 'text'
        else:
            text, kind = '[非文本消息]', 'other'
        if not text:
            continue
        group['messages'].append({'id': _alias('message', mid), 'role': 'bot' if row['_automated'] else row['role'],
                                  'ts': row['ts'], 'text': text, 'kind': kind})
        group['source_ids'].add(mid)
    return [group for group in groups.values() if any(m['role'] == 'user' and m['kind'] == 'text' for m in group['messages'])]


def _question_anchors(conn: sqlite3.Connection, sid: str) -> list[tuple[int, set[str]]]:
    result = []
    for row in conn.execute('SELECT qa_id,question_message_ids FROM qa_origin WHERE session_id=? AND active=1', (sid,)):
        try:
            ids = json.loads(row['question_message_ids'])
        except (TypeError, ValueError):
            continue
        if isinstance(ids, list) and ids and all(isinstance(mid, str) for mid in ids):
            result.append((row['qa_id'], set(ids)))
    return result


def load_batch(cfg: Settings, filters: dict, limit: int, *, require_staff: bool = False) -> tuple[list[dict], dict]:
    """Select complete official sessions by end date and mask their text in memory."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('回放会话数须为 1–100')
    low, high = voc._bounds(filters)
    path = Path(cfg.runtime.db_path).resolve()
    records, totals = [], Counter()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=15)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'raw_session', 'raw_message', 'qa_origin', 'push_sync_state'} <= tables:
            raise ValueError('回放需要完整七鱼会话来源账本')
        rows = conn.execute('''SELECT state.session_id,session.user_name,session.staff_name,session.from_group
            FROM push_sync_state AS state
            JOIN raw_session AS session ON state.session_id=session.session_id
            WHERE state.status='completed' AND state.source_json<>'' AND state.source_version=state.version
                AND session.end_ts>0 AND (?=0 OR session.end_ts>=?) AND (?=0 OR session.end_ts<?)
            ORDER BY session.end_ts DESC, state.session_id ASC LIMIT 1000''', (low, low, high, high))
        for row in rows:
            sid = row['session_id']
            totals['sessions_scanned'] += 1
            try:
                snapshot = voc_outcome._snapshot(conn, sid)
            except (ValueError, KeyError, TypeError, OverflowError):
                totals['unverified_sessions'] += 1
                continue
            names = sorted({name for key in ('user_name', 'staff_name', 'from_group')
                            if len(name := mask_pii((row[key] or '').strip())) >= 2}, key=lambda name: (-len(name), name))
            segments = _segments(snapshot, _alias('session', sid), names)
            if require_staff:
                segments = [segment for segment in segments if any(
                    m['role'] == 'staff' and m['kind'] == 'text' for m in segment['messages'])]
            if not segments:
                totals['sessions_without_matching_segments'] += 1
                continue
            anchors = _question_anchors(conn, sid)
            for segment in segments:
                source_ids = segment.pop('source_ids')
                segment['qa_anchors'] = {qa: {_alias('message', mid) for mid in ids}
                                         for qa, ids in anchors if ids <= source_ids}
            records.extend(segments)
            totals['sessions_selected'] += 1
            if totals['sessions_selected'] == limit:
                break
    return records, dict(totals)


def _baseline_topics(cfg: Settings, qa_ids: set[int]) -> dict[int, list[str]]:
    """Snapshot current VOC assignments before replay invokes the model."""
    if not qa_ids:
        return {}
    from . import voc_engine
    current = {qa: row for qa, row in voc_engine._sources(cfg, {}).items() if qa in qa_ids}
    rows = voc_engine._events(cfg, {}, _current=current)
    assignments = {qa: [] for qa in qa_ids}
    for row in rows:
        assignments[row['qa_id']].append(row['topic_id'])
    return assignments


def _baseline_counts(assignments: dict[int, list[str]], qa_ids: set[int]) -> dict:
    assigned = [assignments[qa] for qa in qa_ids if assignments.get(qa)]
    return {'qa': len(qa_ids), 'qa_with_topic': len(assigned),
            'topics': len({topic for topics in assigned for topic in topics}),
            'intents': sum(len(topics) for topics in assigned)}


def compare(cfg: Settings, segments: list[dict], *, extract: Callable, group: Callable,
            assess: Callable, embedder=None, rerank=None, max_calls: int = 4,
            assignments: dict[int, list[str]] | None = None) -> dict:
    """Compare only successfully replayed segments, with aggregate output."""
    from .voc_episode import EpisodeError

    if type(max_calls) is not int or not 1 <= max_calls <= 100:
        raise ValueError('回放调用数须为 1–100')
    if assignments is None:
        assignments = _baseline_topics(cfg, {qa for segment in segments[:max_calls] for qa in segment['qa_anchors']})
    episodes, qa_ids, errors, verdicts, reasons = [], set(), Counter(), Counter(), Counter()
    sessions = set()
    usage = attempted = completed = joined = 0
    for segment in segments:
        if attempted >= max_calls:
            break
        attempted += 1
        try:
            result = extract(cfg, segment['messages'])
        except EpisodeError as exc:
            errors[exc.code] += 1
            continue
        except ModelProtocolError as exc:
            errors[f'gateway_{exc.status}'] += 1
            if exc.status in (401, 403, 429):
                break
            continue
        usage += result['usage_tokens']
        completed += 1
        qa_ids.update(segment['qa_anchors'])
        sessions.add(segment['session_key'])
        for item in result['episodes']:
            episode = {**item, 'session_key': segment['session_key'], 'customer_key': segment['customer_key']}
            message_ids = set(episode['message_ids'])
            joined += sum(bool(ids & message_ids) for ids in segment['qa_anchors'].values()) >= 2
            episodes.append(episode)
            assessment = assess(episode, segment['messages'])
            verdicts[assessment['status']] += 1
            reasons.update(assessment['reason_codes'])
    topics = group(episodes, embedder=embedder, rerank=rerank)
    ranked = {eid for topic in topics for eid in topic['episode_ids']}
    sizes = Counter(topic['consultation_count'] for topic in topics)
    return {'replay': {'segments_selected': len(segments), 'segments_attempted': attempted, 'segments_completed': completed,
                       'sessions_completed': len(sessions), 'usage_tokens': usage, 'errors': dict(errors)},
            'current': _baseline_counts(assignments, qa_ids), 'proposed': {'episodes': len(episodes), 'topics': len(topics),
              'episodes_linking_multiple_qa': joined,
              'unranked_episodes': len(episodes)-len(ranked), 'topics_with_one_consultation': sizes[1],
              'evidence_status': dict(verdicts), 'evidence_reasons': dict(reasons)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='只读回放七鱼会话，对比当前VOC和多轮咨询关联')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--start', default='')
    parser.add_argument('--end', default='')
    parser.add_argument('--limit', type=int, default=12, help='最多读取的官方会话数')
    parser.add_argument('--max-calls', type=int, default=4, help='最多送入模型的会话片段数')
    parser.add_argument('--timeout', type=int, default=45, help='回放用单次模型等待秒数')
    parser.add_argument('--require-staff', action='store_true')
    parser.add_argument('--with-existing-topics', action='store_true', help='只回放当前已有主题的会话片段')
    parser.add_argument('--reranker', type=Path, help='可选的本地 BGE 重排模型目录')
    parser.add_argument('--execute', action='store_true', help='使用当前网关执行回放')
    args = parser.parse_args(argv)
    if not 5 <= args.timeout <= 180:
        parser.error('回放超时须为 5–180 秒')
    cfg = load_settings()
    filters = {'days': args.days, 'start': args.start, 'end': args.end}
    try:
        segments, source_counts = load_batch(cfg, filters, args.limit, require_staff=args.require_staff)
        assignments = None
        if args.with_existing_topics:
            assignments = _baseline_topics(cfg, {qa for segment in segments for qa in segment['qa_anchors']})
            segments = [segment for segment in segments if any(assignments[qa] for qa in segment['qa_anchors'])]
        cohort = voc_assets.digest([[s['session_key'], s['customer_key'], s['messages']] for s in segments])
        report = {'mode': 'execute' if args.execute else 'inspect', 'filters': filters, 'sources': source_counts,
                  'cohort_digest': cohort,
                  'require_existing_topics': args.with_existing_topics,
                  'segments': len(segments), 'messages': sum(len(s['messages']) for s in segments),
                  'segments_with_staff': sum(any(m['role']=='staff' and m['kind']=='text' for m in s['messages']) for s in segments)}
        if args.execute:
            from . import voc_episode, voc_episode_evidence, voc_episode_topics
            if not voc.configured(cfg):
                parser.error('回放需要已启用的模型网关')
            cfg = replace(cfg, llm=replace(cfg.llm, timeout=min(cfg.llm.timeout, args.timeout)))
            embedder = LocalBGE(Path(cfg.runtime.state_dir) / 'models')
            if not embedder.availability()['ready']:
                raise SemanticError('本地语义模型尚未就绪')
            rerank = voc_episode_topics.LocalReranker(args.reranker) if args.reranker else None
            report.update(compare(cfg, segments, extract=voc_episode.extract, group=voc_episode_topics.group,
                                  assess=voc_episode_evidence.assess, embedder=embedder, rerank=rerank,
                                  max_calls=args.max_calls, assignments=assignments))
        else:
            qa_ids = {qa for segment in segments for qa in segment['qa_anchors']}
            report['current'] = _baseline_counts(_baseline_topics(cfg, qa_ids) if assignments is None else assignments, qa_ids)
        print(json.dumps(report, ensure_ascii=False))
        return int(args.execute and not report['replay']['segments_completed'])
    except (OSError, sqlite3.Error, ValueError, SemanticError) as exc:
        # Database paths, account identifiers and model payloads are not part of the report.
        if isinstance(exc, sqlite3.Error):
            parser.error('无法读取回放数据库')
        if isinstance(exc, SemanticError):
            parser.error('回放的本地语义模型尚未就绪')
        parser.error('回放参数、配置或数据格式无效')


if __name__ == '__main__':
    raise SystemExit(main())
