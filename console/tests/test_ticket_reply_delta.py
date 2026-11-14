import unittest
from unittest.mock import patch

from deploy.feishu_sync import ticket_reply_delta as delta
from src import voc_feishu_sync as sync


def _row(number, stamp):
    return {"record_id": f"recTest{number}", "last_modified_time": stamp,
            "fields": {delta.TIME_FIELD: stamp, "技术回复": "测试回复"}}


class TestTicketReplyDelta(unittest.TestCase):
    def test_inclusive_cutoff_reads_entire_timestamp_across_pages(self):
        pages = [
            {"items": [_row(1, 6000), _row(2, 5000), _row(3, 4000)], "has_more": True, "page_token": "first"},
            {"items": [_row(4, 4000), _row(5, 3000)], "has_more": True, "page_token": "second"},
        ]
        filters = {"conjunction": "and", "conditions": [
            {"field_name": "技术回复", "operator": "isNotEmpty", "value": []}
        ]}
        with patch.object(sync, "_call", side_effect=pages) as call:
            rows = list(delta.read(object(), since_ms=4000, until_ms=5000, field_names=["技术回复"], filters=filters))
        self.assertEqual([row["record_id"] for row in rows], ["recTest2", "recTest3", "recTest4"])
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.kwargs["params"]["page_token"], "first")
        self.assertEqual(call.call_args.kwargs["data"], {
            "field_names": ["技术回复", delta.TIME_FIELD], "automatic_fields": True,
            "sort": [{"field_name": delta.TIME_FIELD, "desc": True}], "filter": filters,
        })

    def test_rejects_invalid_timestamp_and_unstable_pagination(self):
        mismatch = _row(1, 4000)
        mismatch["fields"][delta.TIME_FIELD] = 5000
        first = {"items": [_row(1, 4000)], "has_more": True, "page_token": "first"}
        cases = {
            "mismatch": [{"items": [mismatch], "has_more": False}],
            "bad sort": [first, {"items": [_row(2, 5000)], "has_more": False}],
            "duplicate": [first, {"items": [_row(1, 4000)], "has_more": False}],
            "cursor loop": [first, {"items": [_row(2, 4000)], "has_more": True, "page_token": "first"}],
        }
        for case, pages in cases.items():
            with self.subTest(case=case), patch.object(sync, "_call", side_effect=pages):
                with self.assertRaises(sync.SyncError):
                    list(delta.read(object(), since_ms=4000, until_ms=5000, field_names=["技术回复"]))
