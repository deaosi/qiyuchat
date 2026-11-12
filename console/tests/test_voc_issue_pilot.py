"""Product issue grouping from exact, scope-compatible VOC evidence."""
import unittest

from src import voc_issue_pilot


def row(qa_id, question, *, session=None, merchant="", anchored=True, span=None, domain=None, answer="", topic=None):
    start, end = span or (0, len(question))
    return {"id": f"event-{qa_id}-{start}", "qa_id": qa_id, "topic_id": topic or f"old-{qa_id}",
            "source": {"qa_id": qa_id, "session_id": session or f"s{qa_id}", "customer_id": merchant or "unknown",
                       "question": question, "answer": answer, "asked_at": qa_id * 1000, "_known_merchant": merchant,
                       "_staff_verified": bool(answer),
                       "_question_parts": [{"message_id": f"q{qa_id}", "start": 0, "end": len(question)}] if anchored else [],
                       "_answer_parts": [{"message_id": f"a{qa_id}", "start": 0, "end": len(answer)}] if answer and anchored else []},
            "data": {"question_span": {"start": start, "end": end, "quote": question[start:end], "alignment": "exact"},
                     "domain": domain or {}}}


def ref(text, quote, side="question"):
    start = text.index(quote)
    return {"source": side, "quote": quote, "start": start, "end": start + len(quote), "alignment": "exact"}


class IssueGroupingTests(unittest.TestCase):
    def test_compatible_replies_dedupe_old_topics_and_keep_membership(self):
        rows = [row(1, "抖音下单后跟单催付没有触发。", session="one", merchant="merchant-a"),
                row(2, "抖音跟单催付拍下后没命中。", session="one", merchant="merchant-a"),
                row(3, "抖音下单后跟单催付没触发。", session="two", merchant="merchant-a"),
                row(4, "抖音下单后跟单催付没有触发。", anchored=False)]
        result = voc_issue_pilot.build(rows)
        self.assertEqual(result["coverage"], {"source_events": 4, "matched_consultations": 3,
                                             "classified_consultations": 3, "needs_context": 0})
        [card] = result["items"]
        self.assertEqual(card["title"], "跟单催付：下单后未触发")
        self.assertEqual(card["impact"], {"consultations": 3, "sessions": 3, "merchants": 1, "unidentified_sessions": 1})
        self.assertEqual(card["scope_status"], "scoped")
        self.assertEqual({case["qa_id"] for case in card["_members"]}, {1, 2, 3, 4})
        self.assertEqual(len(card["cases"]), 3)
        self.assertEqual(card["cases"][-1]["message_id"], "")
        self.assertEqual(voc_issue_pilot.build(list(reversed(rows)))["items"], [card])

    def test_conflicting_platform_module_trigger_and_symptom_do_not_merge(self):
        questions = ["抖音下单后跟单催付没有触发。", "淘宝下单后跟单催付没有触发。",
                     "抖音付款后跟单催付没有触发。", "抖音下单后跟单催付没有回复。",
                     "抖音下单后跟单催付回复内容不对。", "下单后跟单催付没有触发。",
                     "抖音下单后自动回复场景没有触发。"]
        result = voc_issue_pilot.build([row(index, text, topic="same-old") for index, text in enumerate(questions, 1)])
        self.assertEqual(result["total"], len(questions))
        self.assertTrue(all(card["impact"]["consultations"] == 1 for card in result["items"]))
        self.assertEqual({card["scope_status"] for card in result["items"]}, {"scoped", "needs_context"})

    def test_reply_cases_with_uncertain_scope_and_explicit_setup(self):
        questions = ["有时候回复不了，想知道是提示词的问题还是表格检索不完全导致的",
                     "拼多多的AI也不回复了", "退货回复没有触发", "设了问答，测试怎么没回复",
                     "一直不回复粉丝", "如果抖音机器人没回复怎么办？", "抖音和淘宝的机器人没回复"]
        result = voc_issue_pilot.build([row(index, text) for index, text in enumerate(questions, 1)])
        cards = {card["cases"][0]["qa_id"]: card for card in result["items"]}
        self.assertEqual(set(cards), {1, 2, 3, 4})
        self.assertEqual(cards[1]["title"], "回复异常：间歇性没有回复")
        self.assertEqual(cards[1]["module"], "")
        self.assertEqual(cards[2]["platform"], "拼多多")
        self.assertEqual(cards[2]["module"], "机器人接待")
        self.assertEqual(cards[3]["module"], "退货场景")
        self.assertEqual(cards[4]["module"], "问答设置")
        self.assertEqual(cards[4]["condition"], "测试时 · 已配置")
        self.assertEqual(result["coverage"]["needs_context"], 2)

    def test_open_discovery_uses_grounded_goal_without_generic_old_category(self):
        first = "请教一下，怎么导出历史会话？"
        second = "我还想问，怎么导出历史会话？"
        common_goal = "导出历史会话"
        rows = [row(index, text, domain={"primary_intent": "configuration_guidance", "merchant_goal": ref(text, common_goal)})
                for index, text in enumerate((first, second), 1)]
        rows.extend([row(3, "同步商品一直报错。"), row(4, "微信小店支持批量绑定店铺吗？"),
                     row(5, "怎么查看店铺子账号权限？"), row(6, "抖音跟单场景怎么设置触发条件？")])
        result = voc_issue_pilot.build(rows)
        self.assertEqual(result["total"], 5)
        card = result["items"][0]
        self.assertEqual((card["title"], card["family"], card["impact"]["consultations"]), ("会话导出", "configuration", 2))
        self.assertEqual({item["family"] for item in result["items"]}, {"configuration", "anomaly", "platform_limit", "scenario_config"})

        novel = "复制辅助诊断面板样式"
        texts = (f"麻烦问一下，怎么{novel}？", f"你好，我想问如何{novel}？")
        unseen = [row(index, text, domain={"merchant_goal": ref(text, novel), "modules": [ref(text, "辅助诊断面板")]})
                  for index, text in enumerate(texts, 1)]
        [new_card] = voc_issue_pilot.build(unseen)["items"]
        self.assertEqual((new_card["title"], new_card["module"], new_card["impact"]["consultations"]), (novel, "辅助诊断面板", 2))

    def test_rejects_noise_and_does_not_leak_ids_or_relabel_history_as_no_reply(self):
        questions = ["方便远程吗", "回复不对", "谢谢", "123456789012", "不支持定制贴纸", "一直不回复粉丝",
                     "重新登录后看不到历史消息，需查哪些未回复", "能不能导出话术？",
                     "@运营同事 123456789012 商品同步报错 10001", "商品同步报错 10002"]
        result = voc_issue_pilot.build([row(index, text) for index, text in enumerate(questions, 1)])
        cards = {card["cases"][0]["qa_id"]: card for card in result["items"]}
        self.assertEqual(set(cards), {7, 8, 9, 10})
        self.assertEqual((cards[7]["title"], cards[7]["family"]), ("历史消息缺失", "anomaly"))
        self.assertEqual((cards[8]["title"], cards[8]["family"]), ("话术导出", "platform_limit"))
        self.assertNotIn("123456789012", cards[9]["cases"][0]["quote"])
        self.assertNotIn("运营同事", cards[9]["cases"][0]["quote"])
        self.assertIn("123456789012", cards[9]["_members"][0]["quote"])
        self.assertNotEqual(cards[9]["id"], cards[10]["id"])

    def test_capability_and_suggested_actions_are_not_incidents(self):
        prompts = ("已经配置好的话术能不能全部导出", "先更新抓取 如果无法抓取到的话再手动哈",
                   "没有掉线之前的消息，怎么看历史记录？", "人工已经回复完了怎么还是发兜底话术呢",
                   "您这边有哪些不需要AI回复的场景？这边帮您看下怎么限制一下。",
                   "我这里导入的知识都匹配不了场景怎么办", "请问是否有尝试登录平台主账号同步过了呢")
        result = voc_issue_pilot.build([row(index, text, domain={"primary_intent": "functional_anomaly"})
                                        for index, text in enumerate(prompts, 1)])
        cards = {card["cases"][0]["qa_id"]: card for card in result["items"]}
        self.assertEqual(set(cards), {1, 3, 4, 6})
        self.assertEqual((cards[1]["title"], cards[1]["family"]), ("话术导出", "platform_limit"))
        self.assertEqual(cards[3]["title"], "历史消息缺失")
        self.assertEqual(cards[4]["title"], "人工回复后仍发送兜底话术")
        self.assertEqual((cards[6]["title"], cards[6]["family"]), ("导入的知识无法匹配场景", "anomaly"))

    def test_do_not_promote_tentative_or_unanchored_staff_and_mixed_causes(self):
        question = "抖音下单后跟单催付未触发。"
        confirmed = "已确认是场景开关未启用导致的。"
        tentative = "可能是场景开关未启用导致的。"
        investigated = "已复测，场景还是没有触发。"

        def diagnosed(qa, answer, *, anchored=True, status="config_cause_confirmed"):
            origin = ref(answer, answer, "answer")
            return row(qa, question, anchored=anchored, answer=answer,
                       domain={"diagnosis": {"status": status, "cause": origin, "evidence": [origin]}})

        single = voc_issue_pilot.build([diagnosed(1, confirmed)])["items"][0]
        self.assertEqual(single["evidence_status"], "cause_confirmed")
        pending = voc_issue_pilot.build([diagnosed(1, tentative), diagnosed(2, confirmed, anchored=False)])["items"][0]
        self.assertEqual(pending["evidence_status"], "reported")
        mixed = voc_issue_pilot.build([diagnosed(1, confirmed), diagnosed(2, investigated, status="pending")])["items"][0]
        self.assertEqual(mixed["evidence_status"], "staff_investigated")


if __name__ == "__main__":
    unittest.main()
