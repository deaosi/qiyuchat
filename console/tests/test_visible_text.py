"""Media transport fields must never masquerade as customer questions."""
from __future__ import annotations

import csv
import io
import json
import unittest

from src import app
from src.extract.normalize import normalize
from src.extract.qa import extract_pairs
from src.extract.text import clean_visible_text
from src.qiyu.inspect import InspectExporter
from src.web import api
from tests.test_local import LocalFixture


def media(**extra):
    value = {"ext": "jpg", "size": 0, "thumb": "n", "origin": "n", "w": 0,
             "name": 'sample-{photo}-"2".jpg', "h": 0,
             "url": "https://example.test/photo.jpg", "md5": "n"}
    value.update(extra)
    return json.dumps(value, ensure_ascii=False)


class TestVisibleText(unittest.TestCase):
    def test_mixed_text_and_multiple_attachments(self):
        text = f'RC159是4根滤芯 {media()}\n这个型号怎么安装？{media(ext="gif")}'
        self.assertEqual(clean_visible_text(text), 'RC159是4根滤芯\n这个型号怎么安装？')

    def test_pure_media_and_technical_envelope(self):
        self.assertEqual(clean_visible_text(media()), '')
        self.assertEqual(clean_visible_text(json.dumps({"type": 1, "attach": json.loads(media())})), '')
        self.assertEqual(clean_visible_text(json.dumps(media())), '')
        self.assertEqual(clean_visible_text(media(ext='mp4', dur=12, expire=0, tt='n')), '')

    def test_caption_and_rich_text_blocks(self):
        self.assertEqual(clean_visible_text(media(caption='请看这个安装位置')), '请看这个安装位置')
        blocks = [{"type": "text", "text": "这个型号能用吗？"}, json.loads(media())]
        self.assertEqual(clean_visible_text(json.dumps(blocks, ensure_ascii=False)), '这个型号能用吗？')

    def test_ordinary_json_links_braces_and_emoji_survive(self):
        examples = ['参数是 {"size":"M","color":"blue"}，哪个合适？',
                    '{"url":"https://example.test/product","name":"型号A","size":"M"}',
                    '接口返回 {"price":199,"stock":2} 是什么意思？',
                    '参考链接 https://example.test/help?size=M&color=blue',
                    '型号{A/B}怎么选？ 😊',
                    '请解释 {"type":"file","name":"config.json","error":"上传失败怎么办"}',
                    '{"type":"text","text":"型号A","error":"安装失败怎么办"}',
                    '{"question":"请看附件","attachment":' + media() + '}']
        for text in examples:
            self.assertEqual(clean_visible_text(text), text)

    def test_only_exact_transport_placeholders_are_hidden(self):
        self.assertEqual(clean_visible_text('[非文本消息 mType=2]'), '')
        text = '这个提示 [非文本消息 mType=2] 是什么意思？'
        self.assertEqual(clean_visible_text(text), text)

    def test_multiline_reordered_uppercase_and_quoted_filename(self):
        payload = json.dumps(dict(reversed(list(json.loads(media(ext='JPG')).items()))), indent=2)
        self.assertEqual(clean_visible_text('请问\n' + payload + '\n怎么安装？'), '请问\n怎么安装？')

    def test_malformed_payload_does_not_consume_following_text(self):
        text = '这段损坏了 {"ext":"jpg", "url":"broken"\n后面的咨询文字必须保留'
        self.assertEqual(clean_visible_text(text), text)
        text = '{' * 20000 + '保留咨询' + '}' * 20000
        self.assertEqual(clean_visible_text(text), text)

    def test_normalization_never_indexes_media_keys(self):
        self.assertEqual(normalize('这个型号怎么安装？' + media()), normalize('这个型号怎么安装？'))
        self.assertEqual(normalize(media()), '')

    def test_transcript_json_colons_are_not_speakers(self):
        messages = InspectExporter.split_transcript('客户: 这个型号怎么安装？\n' + media() + '\n客服: 按说明安装')
        self.assertEqual([m['role'] for m in messages], ['user', 'staff'])
        pairs = extract_pairs(messages, {"id": "s1"})
        self.assertEqual(pairs[0]['question'], '这个型号怎么安装？')
        self.assertEqual(pairs[0]['answer'], '按说明安装')

    def test_staff_image_preserves_question_turn_boundary(self):
        messages = [{'role': 'user', 'content': '第一个型号怎么安装？'},
                    {'role': 'staff', 'content': media()},
                    {'role': 'user', 'content': '另一个型号多少钱？'},
                    {'role': 'staff', 'content': '199元'}]
        pairs = extract_pairs(messages, {"id": "s1"})
        self.assertEqual([p['answer'] for p in pairs], ['', '199元'])
        self.assertEqual([p['question'] for p in pairs], ['第一个型号怎么安装？', '另一个型号多少钱？'])

    def test_staff_image_followed_by_text_still_keeps_answer(self):
        messages = [{'role': 'user', 'content': '这个型号怎么安装？'},
                    {'role': 'staff', 'content': media()},
                    {'role': 'staff', 'content': '先固定底座，再安装滤芯' + media()}]
        pairs = extract_pairs(messages, {"id": "s1"})
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]['answer'], '先固定底座，再安装滤芯')


class TestHistoricalProjection(LocalFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.db = app.get_db(self.cfg)
        self.raw = [dict(row) for row in self.db.iter_qa_pairs()]
        # Simulate legacy derived rows, not newly extracted clean data.
        first = self.raw[0]
        second = self.raw[1]
        first['question'] = '这个型号怎么安装？' + media()
        first['answer'] = '按说明固定底座' + media()
        first['q_norm'] = '这个型号怎么安装extjpgsize0url'
        second['question'] = media()
        second['q_norm'] = 'extjpgsize0url'
        self.db.upsert_qa_pairs([first, second])
        self.before = [dict(row) for row in self.db.iter_qa_pairs()]

    def test_all_views_share_the_readable_scope_without_db_mutation(self):
        rows = api.pairs(self.cfg)
        self.assertEqual(rows['total'], len(self.before) - 1)
        summary = api.build_summary(self.cfg)
        self.assertEqual(summary['qa_pair'], rows['total'])
        self.assertEqual(api.questions(self.cfg)['total'], rows['total'])
        self.assertEqual(api.meta(self.cfg)['total'], rows['total'])
        self.assertEqual(api.status(self.cfg)['total_pairs'], rows['total'])
        self.assertEqual(api.status(self.cfg)['stored_pairs'], len(self.before))
        for record in rows['items']:
            self.assertNotIn('"ext"', record['question'])
            self.assertNotIn('"url"', record['answer'])
            self.assertNotIn('extjpg', record['q_norm'])
        exported = api.export_pairs(self.cfg).decode('utf-8-sig')
        self.assertNotIn('"ext"', exported)
        self.assertNotIn('extjpg', exported)
        self.assertEqual(len(list(csv.reader(io.StringIO(exported)))) - 1, rows['total'])
        self.assertEqual([dict(row) for row in self.db.iter_qa_pairs()], self.before)

    def test_pure_attachment_detail_and_metadata_search_are_hidden(self):
        dirty = next(row for row in self.before if row['question'] == media())
        self.assertIsNone(api.detail(self.cfg, dirty['qa_id']))
        self.assertEqual(api.pairs(self.cfg, q='example.test')['total'], 0)
        self.assertGreater(api.pairs(self.cfg, q='安装')['total'], 0)

    def test_context_strips_media_without_changing_raw_messages(self):
        row = next(row for row in self.before if '怎么安装' in row['question'])
        messages = [{'role': 'user', 'content': '这个型号怎么安装？' + media(), 'ts': 1},
                    {'role': 'staff', 'content': media(), 'ts': 2},
                    {'role': 'staff', 'content': '按说明固定底座', 'ts': 3}]
        self.db.upsert_messages(row['session_id'], messages)
        raw_before = self.db.get_messages(row['session_id'])
        detail = api.detail(self.cfg, row['qa_id'])
        self.assertEqual([m['content'] for m in detail['messages']], ['这个型号怎么安装？', '按说明固定底座'])
        self.assertEqual(detail['pair']['q_norm'], normalize('这个型号怎么安装？'))
        self.assertEqual(self.db.get_messages(row['session_id']), raw_before)


if __name__ == '__main__':
    unittest.main()
