"""Full stored case context with synthetic data; no network/model calls."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.db import DB
from src.extract.identity import SOURCE_IDENTITY_ROLE, encode_identity
from src.web import api


class CaseContext(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = DB(str(Path(self.temp.name) / 'synthetic.db'))
        self.addCleanup(self.db.close)
        active = patch.object(api, 'get_db', return_value=self.db)
        active.start(); self.addCleanup(active.stop)
        self.cfg = object()

    def pair(self, sid='session-local', customer='legacy-customer', index=0):
        cursor = self.db._conn.execute("INSERT INTO qa_pair(session_id,idx,customer_id,customer_name,question,answer,asked_at) VALUES (?,?,?,?,?,?,?)",
            (sid, index, customer, '合成客户', '如何配置当前功能？', '请检查权限。', 0))
        self.db._conn.commit()
        return cursor.lastrowid

    def messages(self, rows, sid='session-local'):
        self.db.upsert_messages(sid, rows)

    def detail(self, identity):
        return api.detail(self.cfg, identity)

    def control(self, customer, sid='session-local'):
        return {'role': SOURCE_IDENTITY_ROLE, 'content': encode_identity(sid, customer, '合成访客'), 'ts': 1}

    def test_full_session_is_not_truncated_and_uses_unmasked_original_lookup(self):
        sid = 'session-13812345678'
        identity = self.pair(sid=sid)
        rows = [{'role': 'user' if i % 2 == 0 else 'staff', 'content': '合成正文-' + str(i), 'ts': i + 1} for i in range(240)]
        rows[-1]['content'] += '很长的末尾内容。' * 600
        self.messages(rows, sid=sid)
        self.messages([{'role': 'user', 'content': '另一个会话不应出现', 'ts': 1}], sid='other-session')
        result = self.detail(identity)
        self.assertEqual(result['message_count'], len(rows))
        self.assertEqual(result['messages'][-1]['content'], rows[-1]['content'])
        self.assertEqual([m['role'] for m in result['messages']], [m['role'] for m in rows])
        self.assertNotEqual(result['pair']['session_id'], sid)
        self.assertEqual(result['context_scope'], 'legacy_session')
        self.assertEqual(result['availability'], 'available')
        self.assertNotIn('另一个会话不应出现', str(result))

    def test_timestamps_are_milliseconds_with_explicit_shanghai_seconds(self):
        identity = self.pair()
        stamp = int(datetime(2026, 11, 11, 0, 0, 45, tzinfo=timezone.utc).timestamp() * 1000)
        self.messages([{'role': 'user', 'content': '合成问题', 'ts': stamp},
                       {'role': 'staff', 'content': '时间缺失的合成回复', 'ts': 0}])
        with patch.object(api, '_time', return_value='old-host-local-minute-format'):
            result = self.detail(identity)
        self.assertEqual(result['time_zone'], 'Asia/Shanghai')
        self.assertEqual(result['messages'][0]['ts'], stamp)
        self.assertEqual(result['messages'][0]['time'], '2026-11-11 08:00:45')
        self.assertEqual(result['messages'][1]['time'], '时间未知')
        self.assertEqual(result['messages'][1]['ts'], 0)

    def test_same_customer_segments_retained_without_other_visitors_or_controls(self):
        customer_a, customer_b = 'qiyu-external:visitor-a', 'qiyu-external:visitor-b'
        identity = self.pair(customer=customer_a)
        self.messages([self.control(customer_a), {'role': 'user', 'content': '甲的问题', 'ts': 1},
                       {'role': 'staff', 'content': '甲的回复', 'ts': 2}, self.control(customer_b),
                       {'role': 'user', 'content': '乙的问题', 'ts': 3}, {'role': 'staff', 'content': '乙的回复', 'ts': 4},
                       self.control(customer_a), {'role': 'user', 'content': '甲的后续', 'ts': 5},
                       {'role': 'staff', 'content': '甲的后续回复', 'ts': 6},
                       {'role': 'boundary', 'content': '隐藏的内部边界', 'ts': 7}])
        result = self.detail(identity)
        self.assertEqual([m['content'] for m in result['messages']], ['甲的问题', '甲的回复', '甲的后续', '甲的后续回复'])
        self.assertEqual(result['context_scope'], 'session_customer')
        self.assertNotIn(SOURCE_IDENTITY_ROLE, str(result['messages']))
        self.assertNotIn('隐藏的内部边界', str(result))

    def test_unresolved_controlled_customer_is_empty_not_archived_or_joined(self):
        identity = self.pair(customer='qiyu-session:session-local')
        self.messages([self.control('qiyu-external:visitor-a'), {'role': 'user', 'content': '不可归属的记录', 'ts': 1}])
        result = self.detail(identity)
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['availability'], 'empty')
        self.assertFalse(result['archived'])

    def test_partial_retention_is_distinct_from_all_messages_archived(self):
        identity = self.pair()
        self.messages([{'role': 'staff', 'content': '仅保留末尾回复', 'ts': 1}])
        self.db._conn.execute('CREATE TABLE push_sync_state(session_id TEXT PRIMARY KEY,status TEXT,archive_reason TEXT)')
        self.db._conn.execute("INSERT INTO push_sync_state VALUES ('session-local','archived','retention')")
        self.db._conn.commit()
        partial = self.detail(identity)
        self.assertEqual(partial['availability'], 'partial')
        self.assertFalse(partial['archived'])
        self.assertEqual(partial['message_count'], 1)
        self.db._conn.execute('DELETE FROM raw_message')
        self.db._conn.commit()
        archived = self.detail(identity)
        self.assertEqual(archived['availability'], 'archived')
        self.assertTrue(archived['archived'])
        self.assertEqual(archived['message_count'], 0)

    def test_recognized_media_keeps_position_without_showing_metadata_json(self):
        identity = self.pair()
        image = {'ext': 'jpg', 'size': 1, 'name': 'private.jpg', 'url': 'https://example.invalid/private.jpg'}
        attachment = {'type': 'file', 'url': 'https://example.invalid/private.pdf', 'name': 'private.pdf'}
        ordinary_json = '{"scenario":"customer-written-value"}'
        self.messages([{'role': 'user', 'content': json.dumps(image), 'ts': 1},
                       {'role': 'staff', 'content': json.dumps(attachment), 'ts': 2},
                       {'role': 'user', 'content': ordinary_json, 'ts': 3}])
        result = self.detail(identity)
        self.assertEqual([m['content'] for m in result['messages']], ['[图片]', '[附件]', ordinary_json])
        self.assertEqual([m['ts'] for m in result['messages']], [1, 2, 3])
        self.assertNotIn('example.invalid', str(result))
        self.assertNotIn('private.jpg', str(result))

    def test_only_exact_role_correct_origin_ids_are_highlighted(self):
        identity = self.pair()
        self.messages([{'role': 'user', 'content': '锚点问题', 'ts': 1, 'source_message_id': 'q1'},
                       {'role': 'staff', 'content': '锚点人工回复', 'ts': 2, 'source_message_id': 'a1'},
                       {'role': 'bot', 'content': '机器人消息', 'ts': 3, 'source_message_id': 'bot1'},
                       {'role': 'user', 'content': '后续问题', 'ts': 4}])
        self.db._conn.execute('INSERT INTO qa_origin(qa_id,evidence_key,session_id,question_message_ids,answer_message_ids,fingerprint) VALUES (?,?,?,?,?,?)',
            (identity, 'synthetic-origin', 'session-local', '["q1"]', '["a1","bot1"]', 'synthetic'))
        self.db._conn.commit()
        messages = self.detail(identity)['messages']
        self.assertEqual([m['is_question'] for m in messages], [True, False, False, False])
        self.assertEqual([m['is_answer'] for m in messages], [False, True, False, False])
        self.assertEqual(messages[0]['source_message_id'], 'q1')
        other = self.pair(index=1)
        self.assertTrue(all(not m['is_question'] and not m['is_answer'] for m in self.detail(other)['messages']))

    def test_blank_local_messages_do_not_get_invented_media_or_archive_status(self):
        identity = self.pair()
        self.messages([{'role': 'user', 'content': '  ', 'ts': 1}, {'role': 'boundary', 'content': 'hidden', 'ts': 2}])
        result = self.detail(identity)
        self.assertEqual(result['message_count'], 0)
        self.assertEqual(result['availability'], 'empty')
        self.assertFalse(result['archived'])
        self.assertIsNone(self.detail(999999))


if __name__ == '__main__':
    unittest.main()
