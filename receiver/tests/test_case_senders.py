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

    def test_group_member_names_require_matched_sender_metadata(self):
        group = fixture.source(5, 1, '@吴示例 可以帮我看吗', externalUserId='', userName='群成员原昵称')
        looks_like_staff = fixture.source(6, 1, '我来处理', externalUserId='OutsideAccount', userName='吴示例')
        account_label = fixture.source(7, 1, '收到', externalUserId='AccountOnly', userName='AccountOnly')
        automatic = {**self.staff, 'id': 8, 'autoReply': True}
        rows = [group, looks_like_staff, account_label, automatic]
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, rows, [self.retained(row) for row in rows]), [
            {'context_seq': 5, 'sender_kind': 'group_participant', 'sender_name': '群成员原昵称'},
            {'context_seq': 6, 'sender_kind': 'group_participant', 'sender_name': '吴示例'},
            {'context_seq': 7, 'sender_kind': 'unidentified', 'sender_account': 'AccountOnly'},
        ])

    def test_source_accounts_use_repeatable_aliases_except_the_session_default(self):
        metadata = {**self.metadata, 'userId': 500}
        first = fixture.source(5, 1, '第一条发言', externalUserId='', userId=501)
        follow = fixture.source(6, 1, '第二条发言', externalUserId='', userId=501)
        another = fixture.source(7, 1, '其他发言', externalUserId='', userId=502)
        default = fixture.source(8, 1, '无法确认发言', externalUserId='', userId=500)
        rows = [first, follow, another, default]
        retained = [self.retained(row, legacy=True) for row in rows]
        expected = [
            {'context_seq': 5, 'sender_kind': 'unidentified', 'sender_name': '来源账号 1'},
            {'context_seq': 6, 'sender_kind': 'unidentified', 'sender_name': '来源账号 1'},
            {'context_seq': 7, 'sender_kind': 'unidentified', 'sender_name': '来源账号 2'},
            {'context_seq': 8, 'sender_kind': 'unidentified'},
        ]
        self.assertEqual(case_senders.labels(self.state_dir, metadata, rows, retained, legacy=True), expected)
        sid = str(metadata['id'])
        records = case_senders.legacy_source_records(metadata, rows, retained)
        path = case_senders.legacy_source_file(self.state_dir, sid)
        path.parent.mkdir()
        path.write_text(json.dumps(records), encoding='utf-8')
        self.assertEqual(case_senders.saved_legacy_labels(self.state_dir, sid, retained, admin=True),
                         {label['context_seq']: {key: value for key, value in label.items() if key != 'context_seq'}
                          for label in expected})
        self.assertTrue(all(label == {'sender_kind': 'unidentified'}
                            for label in case_senders.saved_legacy_labels(self.state_dir, sid, retained).values()))
        self.assertEqual(set(case_senders.saved_guest_groups(None, self.state_dir, sid, retained, set())), {5, 6, 7})

    def test_legacy_transport_whitespace_match_is_unique_on_both_sides(self):
        retained = [{**self.retained(self.customer, legacy=True), 'content': ' ' + self.customer['msg']}]
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, [self.customer], retained, legacy=True),
                         [{'context_seq': 1, 'sender_name': '测试客户'}])
        duplicate = {**self.customer, 'id': 6, 'msg': self.customer['msg'] + ' ', 'userName': '不能借用的昵称'}
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, [self.customer, duplicate], retained, legacy=True), [])
        second_local = {**retained[0], 'context_seq': 7, 'content': retained[0]['content'] + ' '}
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, [self.customer],
                                             [*retained, second_local], legacy=True), [])
        self.assertEqual(case_senders.labels(self.state_dir, self.metadata, [self.customer],
                                             [{**retained[0], 'source_message_id': 'source:6789031:1'}], legacy=True), [])

    def test_legacy_sidecar_enriches_only_admin_and_groups_exact_sender_accounts(self):
        group = fixture.source(5, 1, '这边也有同样问题', externalUserId='OtherGroupMember', userName='访客原昵称')
        follow_up = fixture.source(6, 1, '第二条消息', externalUserId='OtherGroupMember', userName='访客原昵称')
        namesake = fixture.source(7, 1, '我也遇到了', externalUserId='', userName='访客原昵称')
        rows = [self.customer, self.member, self.staff, group, follow_up, namesake]
        retained = [self.retained(row, legacy=True) for row in rows]
        sid = str(self.metadata['id'])
        records = case_senders.legacy_source_records(self.metadata, rows, retained)
        path = case_senders.legacy_source_file(self.state_dir, sid)
        path.parent.mkdir()
        path.write_text(json.dumps(records), encoding='utf-8')

        self.assertEqual(case_senders.saved_legacy_labels(self.state_dir, sid, retained), {
            1: {'sender_name': '测试客户'}, 2: {'sender_kind': 'unidentified'}, 4: {'sender_name': '吴示例'},
            5: {'sender_kind': 'unidentified'}, 6: {'sender_kind': 'unidentified'}, 7: {'sender_kind': 'unidentified'},
        })
        authenticated = case_senders.saved_legacy_labels(self.state_dir, sid, retained, admin=True)
        self.assertEqual(authenticated[2], {'sender_kind': 'fuke_staff', 'sender_name': '李示例'})
        self.assertEqual(authenticated[5], {'sender_kind': 'group_participant', 'sender_name': '访客原昵称'})
        self.assertEqual(authenticated[7], authenticated[5])
        groups = case_senders.saved_guest_groups(None, self.state_dir, sid, retained, set())
        self.assertEqual(set(groups), {1, 2, 5, 6, 7})
        self.assertEqual(groups[5], groups[6])
        self.assertNotEqual(groups[5], groups[7])
        self.assertNotIn('OtherGroupMember', str(groups))
        altered = [{**message, 'content': '改过的消息'} if message['context_seq'] == 5 else message for message in retained]
        self.assertNotIn(5, case_senders.saved_legacy_labels(self.state_dir, sid, altered, admin=True))
        self.assertNotIn(5, case_senders.saved_guest_groups(None, self.state_dir, sid, altered, set()))

        for record in records.values():
            for field in ('sender_key', 'participant_name', 'participant_account'):
                record.pop(field, None)
        path.write_text(json.dumps(records), encoding='utf-8')
        self.assertEqual(case_senders.saved_legacy_labels(self.state_dir, sid, retained, admin=True)[5],
                         {'sender_kind': 'unidentified'})
        self.assertEqual(case_senders.saved_guest_groups(None, self.state_dir, sid, retained, set()), {})

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
        self.assertEqual(set(case_senders.saved_guest_groups(db, self.state_dir, sid, retained, tables)), {2})
        db.execute("UPDATE push_sync_state SET source_version='v1'")
        self.assertEqual(case_senders.saved_labels(db, self.state_dir, sid, retained, tables),
                         {2: {'sender_kind': 'unidentified'}})
        self.assertEqual(case_senders.saved_guest_groups(db, self.state_dir, sid, retained, tables), {})

    def test_legacy_reply_lookup_enriches_senders_even_without_native_staff_messages(self):
        from dataclasses import replace

        case = fixture.CaseReplies()
        case.setUp()
        case.cfg = replace(case.cfg, runtime=replace(case.cfg.runtime, state_dir=self.state_dir))
        group = fixture.source(5, 1, '我也问一下', externalUserId='', userName='未接七鱼群成员')
        rows = [self.customer, self.member, group]
        exporter = fixture.Exporter(rows, fromSubType=self.metadata['fromSubType'])
        retained = [self.retained(row, legacy=True) for row in rows]
        self.assertEqual(api.case_reply_response(case.lookup(exporter, retained=retained)), {
            'status': 'no_reply', 'messages': [], 'senders': [
                {'context_seq': 1, 'sender_name': '测试客户'},
                {'context_seq': 2, 'sender_kind': 'fuke_staff', 'sender_name': '李示例'},
                {'context_seq': 5, 'sender_kind': 'group_participant', 'sender_name': '未接七鱼群成员'},
            ],
        })
