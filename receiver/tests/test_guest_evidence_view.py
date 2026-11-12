"""Check the read-only guest detail markup and its projection boundary."""

from html.parser import HTMLParser
from unittest import TestCase

from src.web.guest_evidence_view import render_page


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class TestGuestEvidenceView(TestCase):
    def test_complete_taxonomy_and_ordered_chat_are_rendered(self):
        entry = {
            "kind": "topics", "title": "订单配置 <script>", "summary": "12 次咨询",
            "start": "2026-11-06", "end": "2026-11-12",
            "metrics": [{"label": "咨询", "value": "12 次"}],
            "taxonomy": [{"primary": "商品与订单", "secondary": f"子类 {index:02d}", "count": index}
                         for index in range(12)],
            "sections": [{"title": "处理办法", "lines": ["先核对平台配置。"]}],
        }
        cases = [
            {"title": "关联案例", "category": "配置与使用", "scope": "context",
             "sections": [{"title": "实际触发结果", "lines": ["规则未命中"]}],
             "messages": [{"role": "user", "time": "2026-11-08 11:30:00", "text": "客户问候\n<iframe src=x>"},
                          {"role": "staff", "time": "2026-11-08 11:31:00", "text": "客服说明"}]},
            {"title": "另一条已分享案例", "scope": "pair", "messages": [{"role": "user", "text": "来源问答"}]},
        ]
        page = render_page(entry, cases, total_cases=5).decode()
        for text in ("一级分类", "二级分类", "咨询次数", "处理办法", "实际触发结果", "展示 2 / 共 5 条", "来源问答"):
            self.assertIn(text, page)
        for index in range(12):
            self.assertIn(f"子类 {index:02d}", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;iframe src=x&gt;", page)
        self.assertLess(page.index("客户问候"), page.index("客服说明"))
        tags = _Tags()
        tags.feed(page)
        self.assertFalse({"a", "script", "iframe", "img", "form", "link"} & {tag for tag, _ in tags.tags})
        self.assertEqual([attrs.get("class") for tag, attrs in tags.tags if tag == "li"],
                         ["chat-row customer", "chat-row staff", "chat-row customer"])
        self.assertEqual([("open" in attrs) for tag, attrs in tags.tags if tag == "details"], [True, False])

    def test_legacy_sop_detail_preserves_process_and_result(self):
        entry = {
            "kind": "sops", "title": "历史 SOP",
            "detail": "适用条件\n场景已启用\n处理步骤\n1. 核对客服配置\n复测结果\n已触发",
            "start": "2026-11-08", "end": "2026-11-08",
        }
        page = render_page(entry, []).decode()
        for text in ("适用条件", "场景已启用", "处理步骤", "核对客服配置", "复测结果", "已触发"):
            self.assertIn(text, page)
        self.assertIn('class="steps section-body"', page)
        self.assertNotIn("1. 核对客服配置", page)
        self.assertIn("暂无可公开查看的会话上下文", page)
