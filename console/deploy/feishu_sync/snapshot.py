"""Compute the server-side VOC export snapshot in an isolated process."""

from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deploy.feishu_sync.source import MAX_RESPONSE_BYTES


ZONE = timezone(timedelta(hours=8))
FILTERS = {"days": 7}



def clock_slot(now):
    return now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0).isoformat(timespec='minutes')

def latest_job(cfg):
    from src.voc_service import _path as job_store

    path = job_store(cfg).resolve()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='voc_service_job'").fetchone():
            return None
        row = db.execute('SELECT status,total,processed,failed,skipped,created_at,updated_at '
                         'FROM voc_service_job ORDER BY created_at DESC LIMIT 1').fetchone()
    if row is None:
        return None
    return dict(row)

def records_in_scope(cfg, scope):
    from src import voc_assets, voc_engine, voc_feishu_content as content

    names = content._private_names(cfg, scope)
    original_limit = content._LIMIT
    original_rank = voc_engine.feishu_topics
    register_keys = {}
    register_members = {}

    def rank_with_register_keys(*args, **kwargs):
        ranking = original_rank(*args, **kwargs)
        for item in ranking['items']:
            identity = voc_engine._rank_category(item)
            register_keys[item['id']] = (item['id'] if identity[0] == 'topic'
                                         else 't_' + voc_assets.digest(('voc-rank-category', identity))[:24])
            register_members[item['id']] = sorted({item['id'], *(member['id'] for member in item.get('members', []))})
        return ranking

    try:
        # This isolated interpreter builds all ranked rows and their usual guest
        # views in the same ranking pass. The web preview keeps its own cap.
        content._LIMIT = sys.maxsize
        voc_engine.feishu_topics = rank_with_register_keys
        topics, _ = content._topic_records(cfg, scope, names)
    finally:
        content._LIMIT = original_limit
        voc_engine.feishu_topics = original_rank
    for topic in topics:
        topic['register_key'] = register_keys[topic['key']]
        topic['member_keys'] = register_members[topic['key']]
    sops, _ = content._sop_records(cfg, scope, '', names)
    return {'topics': (topics, len(topics)), 'sops': (sops[:original_limit], len(sops))}

def prepare(cfg, records, scope, registry, now, request):
    from src.web import guest_evidence as guest

    documents = {}
    prepared = []
    metrics = {}
    base_url = guest.PUBLIC_ORIGIN + guest.PUBLIC_PATH
    for kind, (items, total) in records.items():
        document = {'start': scope['start'], 'end': scope['end'], 'count': len(items), 'total': total, 'records': []}
        seen = set()
        counts = {'with_case': 0, 'grouped': 0, 'reused': 0}
        previous = request['links'].get(kind, {})
        for record in items:
            key = record['key']
            if key in seen:
                raise ValueError('The ranking contains a duplicate publication key')
            seen.add(key)
            view = record['_guest_view']
            entry = {'kind': kind, 'start': scope['start'], 'end': scope['end'],
                     **{field: record[field] for field in ('title', 'summary', 'detail')}}
            payload = None
            for qa, anchor in record.get('_guest_anchors', []):
                if payload := guest._make_payload(cfg, qa, anchor, entry, view):
                    break
            if payload is None:
                payload = guest._make_payload(cfg, None, '', entry, view)
            if payload is None:
                raise RuntimeError('A ranked record could not be projected for guests')
            counts['with_case'] += bool(payload.get('cases'))
            counts['grouped'] += len(payload.get('topic_variants', [])) > 1

            url = previous.get(key, '')
            token = url.removeprefix(base_url) if isinstance(url, str) and url.startswith(base_url) else ''
            old = (registry.execute('SELECT payload,expires FROM guest_evidence WHERE token=?', (token,)).fetchone()
                   if registry is not None and re.fullmatch(guest.TOKEN_PATTERN, token) else None)
            if old and old[1] >= now + 14 * 86400 and json.loads(old[0]) == payload:
                counts['reused'] += 1
            else:
                url = ''

            exported = {field: record[field] for field in ('key', 'title', 'summary', 'detail')}
            if kind == 'topics':
                exported['consultations'] = record['consultations']
                exported['register_key'] = record['register_key']
                exported['member_keys'] = record['member_keys']
                workspace = (payload.get('topic_workspace') or {}).get('topic') or {}
                candidates = [workspace.get('question'), *(case.get('title') for case in payload.get('cases', []))]
                question = next((text.strip() for text in candidates if isinstance(text, str) and text.strip()
                                 and text.strip() != record['title'].strip() and text.strip() != '会话依据'), '')
                if question:
                    exported['representative_question'] = question
            exported['url'] = url
            if request['apply'] and not url:
                prepared.append((exported, payload))
            document['records'].append(exported)
        documents[kind] = document
        metrics[kind] = counts
    return documents, prepared, metrics

def build_snapshot(request):
    from src.config import settings
    from src.voc_service import maintenance
    from src.web import guest_evidence as guest

    started = datetime.now(ZONE)
    marker = clock_slot(started)
    cfg = settings()
    if cfg.runtime.mock:
        raise RuntimeError('Live source is not configured')
    if request.get('job_only'):
        return {'job': latest_job(cfg)}
    if maintenance(cfg):
        return {'status': 'maintenance', 'marker': marker}
    if request['probe']:
        return {'status': 'ready', 'marker': marker}
    if request['marker'] and request['marker'] != marker:
        return {'status': 'slot_changed'}

    end = started.date()
    scope = {'days': 0, 'start': (end - timedelta(days=6)).isoformat(), 'end': end.isoformat()}
    records = records_in_scope(cfg, scope)
    if not records['topics'][0]:
        return {'status': 'topics_incomplete', 'marker': marker}

    path = guest._registry(cfg).resolve()
    now = int(started.timestamp())
    if path.is_file():
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=8)) as registry:
            documents, prepared, metrics = prepare(cfg, records, scope, registry, now, request)
    else:
        documents, prepared, metrics = prepare(cfg, records, scope, None, now, request)

    if request['apply']:
        if maintenance(cfg):
            return {'status': 'maintenance', 'marker': marker}
        if clock_slot(datetime.now(ZONE)) != marker:
            return {'status': 'slot_changed'}
        for exported, payload in prepared:
            url = guest._save(cfg, payload)
            if not url:
                raise RuntimeError('Guest link could not be registered')
            exported['url'] = url
        if clock_slot(datetime.now(ZONE)) != marker:
            return {'status': 'slot_changed'}
    return {'filters': FILTERS, 'documents': documents, 'marker': marker, 'source_metrics': metrics}

def main() -> None:
    raw = sys.stdin.buffer.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Snapshot request exceeds size limit")
    request = json.loads(raw)
    if not isinstance(request, dict) or type(request.get("apply")) is not bool or type(request.get("probe")) is not bool:
        raise ValueError("Invalid publication mode")

    def allow_only_expected_io(event, arguments):
        if event == "socket.connect":
            raise PermissionError("Unexpected network access in snapshot")
        if request["apply"]:
            return
        if event == "open" and arguments[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise PermissionError("Unexpected write in preview")
        if event == "sqlite3.connect" and "mode=ro" not in str(arguments[0]):
            raise PermissionError("Unexpected SQLite write in preview")

    logging.disable(logging.CRITICAL)
    sys.addaudithook(allow_only_expected_io)
    with redirect_stdout(sys.stderr):
        snapshot = build_snapshot(request)
    result = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(result) > MAX_RESPONSE_BYTES:
        raise ValueError("Snapshot response exceeds size limit")
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    main()
