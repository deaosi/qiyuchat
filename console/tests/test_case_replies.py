"""Exact official reply lookup with synthetic source snapshots."""
from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

from src.config import QiyuConfig, Settings
from src.web import api, case_replies as C


SID = 6789031
NOW = int(time.time() * 1000)
QUESTION = '店铺权限应该在哪里设置？'
ANSWER = '请打开店铺设置中的权限管理。'
CUSTOMER = 'qiyu-external:visitor-a'


def source(mid=1, direction=1, text=QUESTION, **changes):
    return {'id': mid, 'sessionId': SID, 'from': direction, 'mType': 1, 'status': 1,
            'time': NOW - 1000 + mid, 'msg': text, 'staffId': 9, 'userId': 101,
            'externalUserId': 'visitor-a', **changes}


def metadata(rows, **changes):
    return {'id': SID, 'startTime': NOW - 3000, 'endTime': NOW - 100, 'fromType': 'wx_cs',
            'staffMessageCount': sum(row['from'] == 0 for row in rows),
            'userMessageCount': sum(row['from'] == 1 for row in rows), **changes}


class Exporter:
    def __init__(self, rows, **changes):
        self.rows = rows
        self.meta = metadata(rows, **changes)
        self.calls = []

    def export_one(self, sid):
        self.calls.append(('metadata', sid))
        return self.meta

    def export_messages(self, sid):
        self.calls.append(('messages', sid))
        return self.rows


class CaseReplies(unittest.TestCase):
    def setUp(self):
        self.cfg = Settings(qiyu=QiyuConfig(app_key='synthetic', app_secret='synthetic'))
        self.question = source()
        self.staff = source(2, 0, ANSWER)
        self.pair = {'session_id': str(SID), 'customer_id': CUSTOMER, 'question': QUESTION,
                     'asked_at': self.question['time']}
        # Historical bulk metadata can encode integer fields as strings.
        original = metadata([self.question], id=str(SID), staffMessageCount='0', userMessageCount='1')
        self.session = {'session_id': str(SID), 'payload': json.dumps(original),
                        'start_ts': NOW - 3000, 'end_ts': NOW - 100}
        self.retained = [{'role': 'user', 'content': QUESTION, 'ts': self.question['time'],
                          'source_message_id': f'source:{SID}:1'}]

    def lookup(self, exporter, **changes):
        args = {'cfg': self.cfg, 'session': self.session, 'pair': self.pair, 'retained': self.retained, **changes}
        with patch.object(C, '_NEXT_LOOKUP', 0.0):
            return C.lookup(**args, exporter=exporter)

    def test_exact_session_lookup_returns_only_supplemental_verified_staff_in_time_order(self):
        media = source(3, 0, '{"type":"image","url":"https://example.invalid/photo.jpg"}', mType=2)
        exporter = Exporter([media, self.staff, self.question])
        result = self.lookup(exporter)
        self.assertEqual(exporter.calls, [('metadata', SID), ('messages', SID)])
        self.assertEqual(result, {'status': 'found', 'messages': [
            {'role': 'staff', 'content': ANSWER, 'ts': self.staff['time'], 'source_message_id': f'source:{SID}:2'},
            {'role': 'staff', 'content': '[图片]', 'ts': media['time'], 'source_message_id': f'source:{SID}:3'},
        ]})

    def test_official_staff_image_and_caption_reach_public_timeline_with_no_metadata(self):
        image_url = 'https://ysf.nosdn.127.net/synthetic-installation.jpg'
        raw = json.dumps({'type': 'image', 'url': image_url, 'name': 'internal-name.jpg', 'caption': ANSWER})
        media = source(2, 0, raw, mType=2)
        lookup = self.lookup(Exporter([self.question, media]))
        self.assertEqual(lookup['status'], 'found')
        public = api.case_reply_response(lookup)
        self.assertEqual(public['messages'][0]['content'], ANSWER)
        self.assertEqual(public['messages'][0]['images'], [{'url': image_url}])
        self.assertEqual(public['messages'][0]['ts'], media['time'])
        self.assertNotIn('internal-name.jpg', str(public))

    def test_visitor_positive_staff_id_and_automatic_system_or_withdrawn_messages_are_not_replies(self):
        for rows in ([self.question], [self.question, source(2, 0, '自动回复', autoReply=1),
                     source(3, 0, '机器人回复', mType=110), source(4, 0, '系统通知', mType=113),
                     source(5, 0, '撤回的文本', status=2), source(6, 0, '无人工身份', staffId=0)]):
            with self.subTest(message_count=len(rows)):
                self.assertEqual(self.lookup(Exporter(rows)), {'status': 'no_reply', 'messages': []})

    def test_official_metadata_message_mismatch_or_incomplete_counts_are_unavailable(self):
        for exporter in (Exporter([self.question], id=SID + 1), Exporter([self.question], fromType='another-channel'),
                         Exporter([self.question], staffMessageCount=1),
                         Exporter([self.question, source(2, 0, ANSWER, sessionId=SID + 1)])):
            with self.subTest(call_count=len(exporter.rows)):
                self.assertEqual(self.lookup(exporter), {'status': 'unavailable', 'messages': []})

    def test_archived_imported_unrecognized_and_demo_sources_never_call_official_api(self):
        invalid_sessions = [{**self.session, 'end_ts': 1000}, {**self.session, 'session_id': f'live-{SID}'},
                            {**self.session, 'payload': json.dumps(metadata([], fromType='本地导入'))},
                            {**self.session, 'payload': json.dumps(metadata([], _source='push'))},
                            {**self.session, 'payload': json.dumps({'id': SID, 'fromType': 'wx_cs'})}]
        exporter = Exporter([self.question, self.staff])
        for session in invalid_sessions:
            self.assertFalse(C.eligible(self.cfg, session))
            self.assertEqual(self.lookup(exporter, session=session)['status'], 'unavailable')
        cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, mock=True))
        self.assertFalse(C.eligible(cfg, self.session))
        self.assertEqual(self.lookup(exporter, cfg=cfg)['status'], 'unavailable')
        self.assertEqual(exporter.calls, [])
        self.assertTrue(C.eligible(self.cfg, self.session))

    def test_anchored_lookup_excludes_other_customers_and_explicit_recipient_conflicts(self):
        other = {'userId': 202, 'externalUserId': 'visitor-b'}
        exporter = Exporter([self.question, self.staff, source(3, 0, '不同收件人', **other),
                             source(4, 1, '另一个人的问题', **other), source(5, 0, '另一个人的回复', **other)])
        result = self.lookup(exporter)
        self.assertEqual(result['status'], 'found')
        self.assertEqual([row['content'] for row in result['messages']], [ANSWER])
        changed_anchor = [{**self.retained[0], 'ts': self.question['time'] + 1}]
        self.assertEqual(self.lookup(exporter, retained=changed_anchor)['status'], 'unmatched')

    def test_legacy_requires_exact_time_and_question_with_one_confirmed_visitor(self):
        pair = {**self.pair, 'customer_id': '101'}
        retained = [{'role': 'user', 'content': QUESTION, 'ts': self.question['time']}]
        exporter = Exporter([self.question, self.staff])
        self.assertEqual(self.lookup(exporter, pair=pair, retained=retained)['status'], 'found')
        self.assertEqual(self.lookup(exporter, pair={**pair, 'asked_at': pair['asked_at'] + 1}, retained=retained)['status'], 'unmatched')
        self.assertEqual(self.lookup(exporter, pair={**pair, 'question': '权限怎么设置'}, retained=retained)['status'], 'unmatched')
        other = source(3, 1, '另一个人的问题', userId=202, externalUserId='visitor-b')
        self.assertEqual(self.lookup(Exporter([self.question, self.staff, other]), pair=pair, retained=retained)['status'], 'unmatched')
        anonymous = source(3, 1, '未知访客的问题', userId=0, externalUserId='')
        self.assertEqual(self.lookup(Exporter([self.question, self.staff, anonymous]), pair=pair, retained=retained)['status'], 'unmatched')

    def test_unknown_boundary_cannot_carry_reply_ownership_and_existing_reply_is_not_duplicated(self):
        boundary = source(2, 0, '未知边界', mType=113)
        self.assertEqual(self.lookup(Exporter([self.question, boundary, source(3, 0, ANSWER)]))['status'], 'unmatched')
        retained = self.retained + [{'role': 'staff', 'content': ANSWER, 'ts': self.staff['time']}]
        self.assertEqual(self.lookup(Exporter([self.question, self.staff]), retained=retained)['messages'], [])

    def test_nonblocking_lookup_gate_and_fixed_error_status(self):
        exporter = Exporter([self.question, self.staff])
        with C._GATE:
            self.assertEqual(self.lookup(exporter), {'status': 'busy', 'messages': []})
        self.assertEqual(exporter.calls, [])
        with patch.object(exporter, 'export_one', side_effect=RuntimeError('sensitive signed URL')):
            self.assertEqual(self.lookup(exporter), {'status': 'unavailable', 'messages': []})
        with patch.object(C, 'SessionExporter', return_value=exporter) as factory:
            self.assertEqual(self.lookup(None)['status'], 'found')
        args, kwargs = factory.call_args
        self.assertEqual(args[0].timeout, 5)
        self.assertEqual(kwargs, {'retries': 1})


if __name__ == '__main__':
    unittest.main()
