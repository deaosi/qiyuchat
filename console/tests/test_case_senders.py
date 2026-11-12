"""Verified chat sender labels and old transcript matching."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.push_sync import VERSION
from src.web import api, case_senders
import test_case_replies as fixture


class SenderLabels(unittest.TestCase):
    def setUp(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.state_dir = state.name
        (Path(self.state_dir) / 'fuke-staff-directory.json').write_text(
            json.dumps({'FukeDemoStaff': '李示例'}), encoding='utf-8',
        )
        self.metadata = {'id': 6789031, 'fromType': 'wx_cs', 'fromSubType': '福客AI-服务群'}
        self.customer = fixture.source(1, 1, '问一下福客 AI', externalUserId='wmSyntheticCustomer9001', userName='测试客户')
        self.member = fixture.source(2, 1, '我来查一下', externalUserId='FukeDemoStaff')
        self.unknown = fixture.source(3, 1, '收到', externalUserId='AnotherAccount')
        self.staff = fixture.source(4, 0, '这里是接待回复', staffName='吴示例', userName='同一个群名称')

    def retained(self, row, *, legacy=False, seq=None):
        message = {'context_seq': row['id'] if seq is None else seq, 'role': 'user' if row['from'] == 1 else 'staff',
                   'ts': row['time'], 'content': row['msg']}
        if not legacy:
            message['source_message_id'] = f"source:{row['sessionId']}:{row['id']}"
        return message

    def test_sender_roles_use_individual_source_and_confirmed_account(self):
        rows = [self.customer, self.member, self.unknown, self.staff]
        retained = [self.retained(row) for row in rows]
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, rows, retained), [
            {'context_seq': 1, 'sender_name': '测试客户'},
            {'context_seq': 2, 'sender_kind': 'fuke_staff', 'sender_name': '李示例'},
            {'context_seq': 3, 'sender_kind': 'unidentified', 'sender_account': 'AnotherAccount'},
            {'context_seq': 4, 'sender_name': '吴示例'},
        ])
        self.assertEqual(retained[1]['role'], 'user')
        other_company = {**self.metadata, 'fromSubType': '其他公司-福客AI客户'}
        self.assertEqual(case_senders.labels(self.state_dir, other_company, rows, retained)[-1]['sender_name'], '吴示例')
        self.assertFalse(any(label.get('sender_kind') == 'fuke_staff'
                             for label in case_senders.labels(self.state_dir, other_company, rows, retained)))

    def test_exact_unique_legacy_match_and_no_wrong_current_id_fallback(self):
        rows = [self.customer, self.member]
        retained = [self.retained(row, legacy=True) for row in rows]
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, rows, retained, legacy=True)[-1],
                         {'context_seq': 2, 'sender_kind': 'fuke_staff', 'sender_name': '李示例'})
        duplicate = {**self.member, 'id': 6, 'externalUserId': 'AnotherAccount'}
        self.assertEqual(len(case_senders.labels(self.state_dir, self.metadata, [*rows, duplicate], retained, legacy=True)), 1)
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, rows, [
            {**self.retained(self.member), 'source_message_id': 'source:6789031:9'},
        ], legacy=True), [])

    def test_saved_labels_never_borrow_names_from_unrelated_or_stale_source(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE raw_session (session_id TEXT, payload TEXT)')
        db.execute('CREATE TABLE push_sync_state (session_id TEXT, status TEXT, source_json TEXT, '
                   'version TEXT, source_version TEXT, archive_reason TEXT)')
        sid = str(self.metadata['id'])
        snapshot = {'projection_version': VERSION, 'session': self.metadata, 'messages': [self.member, self.staff]}
        db.execute('INSERT INTO raw_session VALUES (?,?)', (sid, json.dumps(self.metadata)))
        db.execute("INSERT INTO push_sync_state VALUES (?,'completed',?,'v2','v2','')", (sid, json.dumps(snapshot)))
        retained = [self.retained(self.member), {**self.retained(self.staff), 'content': 'mismatch'}]
        tables = {'raw_session', 'push_sync_state'}
        self.assertEqual(case_senders.saved_labels(db, self.state_dir, sid, retained, tables),
                         {2: {'sender_kind': 'fuke_staff', 'sender_name': '李示例'}})
        db.execute("UPDATE push_sync_state SET source_version='v1'")
        self.assertEqual(case_senders.saved_labels(db, self.state_dir, sid, retained, tables),
                         {2: {'sender_kind': 'unidentified'}})

    def test_legacy_reply_lookup_enriches_senders_even_without_native_staff_messages(self):
        from dataclasses import replace

        case = fixture.CaseReplies()
        case.setUp()
        case.cfg = replace(case.cfg, runtime=replace(case.cfg.runtime, state_dir=self.state_dir))
        rows = [self.customer, self.member]
        exporter = fixture.Exporter(rows, fromSubType=self.metadata['fromSubType'])
        retained = [self.retained(row, legacy=True) for row in rows]
        self.assertEqual(api.case_reply_response(case.lookup(exporter, retained=retained)), {
            'status': 'no_reply', 'messages': [], 'senders': [
                {'context_seq': 1, 'sender_name': '测试客户'},
                {'context_seq': 2, 'sender_kind': 'fuke_staff', 'sender_name': '李示例'},
            ],
        })
