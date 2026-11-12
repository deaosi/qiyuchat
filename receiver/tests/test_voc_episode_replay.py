from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from src.config import Settings
from src.db import DB
from src import push_sync, voc_episode, voc_episode_evidence, voc_episode_replay as replay, voc_episode_topics


class TestEpisodeReplay(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'business.db'
        cfg = Settings()
        self.cfg = replace(cfg, runtime=replace(cfg.runtime, db_path=str(self.path), state_dir=folder.name,
                                                voc_domain='zhihuige'))

    def seed(self, turns):
        sid = 15429
        rows = []
        for index, (direction, text, customer, extra) in enumerate(turns, 1):
            rows.append({'id': index, 'sessionId': sid, 'from': direction, 'staffId': 9 if direction == 0 else 0,
                         'userId': 101, 'externalUserId': customer, 'mType': 1, 'status': 1,
                         'time': 1794000000000 + index*1000, 'msg': text, **extra})
        session = {'id': sid, 'startTime': rows[0]['time'], 'endTime': rows[-1]['time']+1000,
                   'staffMessageCount': sum(r['from']==0 for r in rows), 'userMessageCount': sum(r['from']==1 for r in rows)}
        messages = push_sync._project(session, rows, session, sid)
        db = DB(str(self.path))
        try:
            db._conn.executescript(push_sync.SCHEMA)
            db.upsert_sessions([session], verified_source=True)
            db.upsert_messages(str(sid), messages, verified_source=True)
            snapshot = {'projection_version': push_sync.VERSION, 'session': session, 'messages': rows}
            with db.tx() as conn:
                push_sync._extract_one(db, str(sid))
                conn.execute('INSERT INTO push_sync_state(session_id,event_key,version,status,updated_at,source_version,source_json) '
                             'VALUES (?,?,?,?,?,?,?)', (str(sid), 'test', 'v1', 'completed', 0, 'v1', json.dumps(snapshot)))
        finally:
            db.close()

    def test_replay_retains_complete_context_and_splits_verified_visitors(self):
        self.seed([
            (1, '退款场景没触发，我的联系方式是13800138000。', 'merchant-a', {}),
            (0, '请发截图。', 'merchant-a', {}),
            (1, '{"url":"https://example.invalid/private.png"}', 'merchant-a', {'mType': 2}),
            (0, '请开启退款场景并保存。', 'merchant-a', {}),
            (1, '改完以后我重新测试，已经触发了。', 'merchant-a', {}),
            (1, '京东平台的知识库怎么导入？', 'merchant-b', {}),
            (0, '已收到。', 'merchant-b', {'autoReply': 1}),
        ])
        before = self.path.read_bytes()
        segments, counts = replay.load_batch(self.cfg, {}, 1)
        self.assertEqual(counts['sessions_selected'], 1)
        self.assertEqual(len(segments), 2)
        first, second = segments
        self.assertEqual(first['session_key'], second['session_key'])
        self.assertNotEqual(first['customer_key'], second['customer_key'])
        self.assertEqual([m['role'] for m in first['messages']], ['user', 'user', 'user'])
        self.assertEqual([m['role'] for m in second['messages']], ['user'])
        serialized = json.dumps(segments, ensure_ascii=False, default=list)
        for private in ('13800138000', 'merchant-a', 'merchant-b', 'private.png', 'source:15429'):
            self.assertNotIn(private, serialized)
        staffed, _ = replay.load_batch(self.cfg, {}, 1, require_staff=True)
        self.assertEqual(staffed, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_comparison_uses_same_successful_input_and_returns_only_aggregates(self):
        self.seed([
            (1, '退款场景已开启但没有触发。', 'merchant-a', {}),
            (0, '请提供截图以便排查。', 'merchant-a', {}),
            (1, '我的退款规则优先级是第一个。', 'merchant-a', {}),
            (0, '请移除冲突规则并保存。', 'merchant-a', {}),
            (1, '我移除规则后重新测试，已经触发了。', 'merchant-a', {}),
        ])
        segments, _ = replay.load_batch(self.cfg, {}, 1)
        self.assertEqual(len(segments), 1)
        before = self.path.read_bytes()

        def extract(_cfg, messages):
            question, _, _, answer, feedback = messages
            qref = {'message_id': question['id'], 'quote': question['text']}
            aref = {'message_id': answer['id'], 'quote': answer['text']}
            fref = {'message_id': feedback['id'], 'quote': feedback['text']}
            payload = {'episodes': [{'kind': 'anomaly', 'title': '退款场景未触发', 'platform': '',
                'module': '退款场景', 'goal': '', 'symptom': '没有触发', 'message_ids': [m['id'] for m in messages],
                'question': qref, 'answers': [aref], 'actions': [{'instruction': aref, 'feedback': fref, 'outcome': 'success'}]}]}
            return {'episodes': voc_episode.validate(messages, payload), 'usage_tokens': 12}

        report = replay.compare(self.cfg, segments, extract=extract, group=voc_episode_topics.group,
                                assess=voc_episode_evidence.assess, max_calls=1)
        self.assertEqual(report['replay']['segments_completed'], 1)
        self.assertGreaterEqual(report['current']['qa'], 2)
        self.assertEqual(report['proposed']['episodes'], 1)
        self.assertEqual(report['proposed']['episodes_linking_multiple_qa'], 1)
        self.assertEqual(report['proposed']['topics'], 1)
        self.assertEqual(report['proposed']['evidence_status'], {'case_verified': 1})
        self.assertNotIn('退款', json.dumps(report, ensure_ascii=False))
        self.assertEqual(self.path.read_bytes(), before)
