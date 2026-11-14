from unittest import TestCase
from unittest.mock import patch

from deploy.feishu_sync import run


class TestDisplayLinkRename(TestCase):
    def test_uses_ranked_table_alias_after_verified_rename(self):
        url = "https://qiyu.fanwulink.cn/guest/evidence/" + "a" * 43
        old = {"text": "查看问题", "link": url}
        fields = [
            {"field_name": "语海详情", "type": 15},
            {"field_name": "点击查看详情", "type": 15},
        ]
        with (patch.object(run.sync, "_paginated", return_value=fields),
              patch.object(run.top_table, "_cli") as cli):
            self.assertEqual(run._ensure_display_field(object(), top=True), "点击查看详情")
            self.assertEqual(run._ensure_display_field(object()), "语海详情")
            cli.assert_not_called()

        known = {"topic": {"id": "recExample", "fields": {"语海详情": old}}}
        current = {"recExample": {"点击查看详情": [old]}}
        run._match_display_field(known, current, "点击查看详情")
        self.assertEqual(known["topic"]["fields"], {"点击查看详情": old})

    def test_rejects_drift_in_renamed_or_duplicate_cell(self):
        old = {"text": "查看问题", "link": "https://example.com/old"}
        for row in (
            {"点击查看详情": {**old, "text": "手动编辑"}},
            {"点击查看详情": old, "语海详情": old},
        ):
            with self.subTest(row=row):
                known = {"topic": {"id": "recExample", "fields": {"语海详情": old}}}
                with self.assertRaisesRegex(ValueError, "内容无法核对"):
                    run._match_display_field(known, {"recExample": row}, "点击查看详情")
                self.assertEqual(known["topic"]["fields"], {"语海详情": old})
