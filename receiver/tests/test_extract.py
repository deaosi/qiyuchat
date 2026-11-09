"""抽取层单元测试。这一层是纯规则，所以可以断言得很死。"""

from __future__ import annotations

import unittest

from src.extract.cluster import cluster_questions, jaccard, similarity
from src.extract.concepts import concept_signature, dominant_concept
from src.extract.normalize import (
    grams,
    is_info_not_question,
    is_noise_question,
    normalize,
)
from src.extract.qa import extract_pairs


class TestNormalize(unittest.TestCase):
    def test_寒暄和语气词被去掉(self):
        self.assertEqual(normalize("你好，请问这个多少钱啊？"), "这个多少钱")
        self.assertEqual(normalize("麻烦问一下什么时候发货呢"), "什么时候发货")

    def test_数字归一(self):
        self.assertEqual(normalize("订单123456789什么时候到"), "订单0什么时候到")

    def test_全角转半角(self):
        self.assertEqual(normalize("ＡＢＣ多少钱"), "abc多少钱")

    def test_空输入(self):
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize(None or ""), "")

    def test_噪声判定(self):
        self.assertTrue(is_noise_question("好的"))
        self.assertTrue(is_noise_question("嗯嗯嗯"))
        self.assertTrue(is_noise_question("哈"))
        self.assertFalse(is_noise_question("这个多少钱"))


class TestInfoFilter(unittest.TestCase):
    def test_地址不算问题(self):
        self.assertTrue(is_info_not_question("北京市朝阳区建国路88号SOHO现代城A座1801"))

    def test_纯单号不算问题(self):
        self.assertTrue(is_info_not_question("SF1234567890123"))

    def test_带疑问词的地址仍然算问题(self):
        self.assertFalse(is_info_not_question("地址填错了能改吗"))

    def test_普通问题不算信息(self):
        self.assertFalse(is_info_not_question("这个净水器滤芯多久换一次"))


class TestConcept(unittest.TestCase):
    def test_价格概念命中多种说法(self):
        for t in ("这个多少钱", "价格是多少", "怎么卖", "有点贵"):
            self.assertIn("价格", concept_signature(t), t)

    def test_退货和退款是不同概念(self):
        self.assertIn("退货", concept_signature("我要退货"))
        self.assertIn("退款", concept_signature("什么时候退钱"))

    def test_主导概念需要占比(self):
        self.assertEqual(dominant_concept(["多少钱", "价格", "多少钱"]), "价格")
        self.assertEqual(dominant_concept(["多少钱", "发货", "退货"]), "")


class TestCluster(unittest.TestCase):
    def test_同义不同形能合并(self):
        qs = ["这个多少钱", "价格是多少", "怎么卖", "多少钱啊"]
        items = [(i, normalize(q)) for i, q in enumerate(qs)]
        cl = cluster_questions(items, threshold=0.45)
        self.assertEqual(len(cl), 1, f"应该合并成 1 簇，实际 {len(cl)}")

    def test_不同主题不会误合(self):
        qs = ["这个多少钱", "什么时候发货", "我要退货"]
        items = [(i, normalize(q)) for i, q in enumerate(qs)]
        cl = cluster_questions(items, threshold=0.45)
        self.assertEqual(len(cl), 3)

    def test_滤芯问价和滤芯换芯要分开(self):
        qs = ["滤芯多少钱", "滤芯多久换一次"]
        items = [(i, normalize(q)) for i, q in enumerate(qs)]
        cl = cluster_questions(items, threshold=0.45)
        self.assertEqual(len(cl), 2)

    def test_jaccard边界(self):
        self.assertEqual(jaccard(set(), {"a"}), 0.0)
        self.assertEqual(jaccard({"a"}, {"a"}), 1.0)

    def test_相似度加权(self):
        g = grams("多少钱")
        s1, s2 = concept_signature("多少钱"), concept_signature("价格")
        self.assertAlmostEqual(similarity(g, grams("价格"), s1, s2), 0.5, places=2)


class TestExtractPairs(unittest.TestCase):
    def setUp(self):
        self.session = {
            "session_id": "s1", "user_id": "u1", "user_name": "张三",
            "from_group": "售后一组", "staff_name": "客服A", "start_ts": 1_700_000_000_000,
        }

    def test_一问一答(self):
        msgs = [
            {"role": "user", "content": "这个多少钱", "ts": 1},
            {"role": "staff", "content": "199元", "ts": 2},
        ]
        pairs = extract_pairs(msgs, self.session)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["question"], "这个多少钱")
        self.assertEqual(pairs[0]["answer"], "199元")
        self.assertEqual(pairs[0]["customer_id"], "u1")
        self.assertEqual(pairs[0]["group_name"], "售后一组")

    def test_多段客户消息合并(self):
        msgs = [
            {"role": "user", "content": "买的洗衣机", "ts": 1},
            {"role": "user", "content": "面板是裂的，我要退货", "ts": 2},
            {"role": "staff", "content": "给您走换货流程", "ts": 3},
        ]
        pairs = extract_pairs(msgs, self.session)
        self.assertEqual(len(pairs), 1)
        self.assertIn("面板是裂的", pairs[0]["question"])

    def test_没被回答的问题也记录(self):
        msgs = [{"role": "user", "content": "到底什么时候能发货", "ts": 1}]
        pairs = extract_pairs(msgs, self.session)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["answer"], "")

    def test_地址和噪声被过滤(self):
        msgs = [
            {"role": "user", "content": "北京市朝阳区建国路88号", "ts": 1},
            {"role": "user", "content": "好的", "ts": 2},
            {"role": "user", "content": "SF1234567890", "ts": 3},
        ]
        self.assertEqual(extract_pairs(msgs, self.session), [])

    def test_两条独立问答(self):
        msgs = [
            {"role": "user", "content": "这个多少钱", "ts": 1},
            {"role": "staff", "content": "199", "ts": 2},
            {"role": "user", "content": "什么时候发货", "ts": 3},
            {"role": "staff", "content": "今天发", "ts": 4},
        ]
        pairs = extract_pairs(msgs, self.session)
        self.assertEqual(len(pairs), 2)
        self.assertEqual([p["idx"] for p in pairs], [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
