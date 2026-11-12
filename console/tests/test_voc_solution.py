from dataclasses import replace
import unittest
from unittest.mock import Mock

from src.config import Settings
from src.model_protocol import ModelProtocolError
from src.voc_solution import run


def message(identity, role, stamp, text):
    return {"id": identity, "role": role, "ts": stamp, "text": text, "kind": "text"}


def claim(kind, text, *messages, **metadata):
    return {"kind": kind, "text": text, "evidence": [{"message_id": msg["id"], "quote": msg["text"]} for msg in messages], **metadata}


def response(data, usage=20):
    return {"data": data, "usage_tokens": usage}


class TestVocSolution(unittest.TestCase):
    def setUp(self):
        base = Settings()
        self.cfg = replace(base, llm=replace(base.llm, model_main="configured-test-model"))
        self.problem = message("question", "user", 1000, "拼多多接待里出现稍等，开了保留AI回复，但没有生成待办。")
        self.diagnosis = message("diagnosis", "staff", 2000, "保留AI回复先别关，这条接待没有命中知识。")
        self.action = message("staff-action", "staff", 3000, "未命中知识时，我这边先配置为通用知识加入待办。")
        self.goal = message("goal", "user", 4000, "相同对话里出现稍等一定要有待办。")
        self.messages = [self.problem, self.diagnosis, self.action, self.goal]
        self.question_claim = claim("problem", "拼多多接待出现“稍等”后未生成待办。", self.problem)
        self.action_claim = claim("action", "未命中知识时，将此类接待配置为通用知识加入待办。", self.action,
                                  actor="staff", state="proposed")

    def extract(self, messages, sections, verdict=None, *, title="稍等对话没有生成待办"):
        complete = Mock(side_effect=[response({"title": title, "sections": sections}, 37), response(verdict, 19)])
        return run(self.cfg, messages, {"question"}, complete=complete), complete

    def test_synthesizes_context_with_provider_neutral_model_and_drops_unentailed_claim(self):
        result, complete = self.extract(self.messages, [
            self.question_claim, claim("diagnosis", "客服确认该次接待没有命中知识。", self.diagnosis),
            claim("decision", "继续保留 AI 回复。", self.diagnosis), self.action_claim,
            claim("verification", "相同对话出现“稍等”时应产生待办。", self.goal, mode="expected"),
            claim("diagnosis", "未命中是因为商家关闭了拼多多的消息权限。", self.diagnosis),
        ], {"title_supported": True, "supported": [0, 1, 2, 3, 4]})
        self.assertEqual(result["usage_tokens"], 56)
        solution = result["solution"]
        self.assertEqual(solution["title"], "稍等对话没有生成待办")
        self.assertEqual([section["kind"] for section in solution["sections"]], ["problem", "diagnosis", "decision", "action", "verification"])
        action = solution["sections"][3]
        self.assertEqual(action["text"], "客服提出：未命中知识时，将此类接待配置为通用知识加入待办。")
        self.assertEqual(action["evidence"], [{"message_id": "staff-action", "quote": self.action["text"], "ts": 3000}])
        self.assertEqual(set(solution), {"title", "sections"})
        self.assertEqual(complete.call_count, 2)
        self.assertTrue(all(call.args[0] == self.cfg.llm for call in complete.call_args_list))
        review = complete.call_args_list[1].args[2]
        self.assertEqual(review["sections"][3], [3, "action", action["text"], [2]])
        self.assertEqual(review["messages"], [["U", self.problem["text"]], ["S", self.diagnosis["text"]],
                                               ["S", self.action["text"]], ["U", self.goal["text"]]])
        self.assertEqual(review["anchors"], [0])

    def test_source_guard_rejects_missing_staff_boundary_or_unknown_anchor_before_model(self):
        complete = Mock()
        for messages, anchors in (([self.problem], {"question"}), (self.messages, {"diagnosis"}),
                                  (self.messages, {"missing"}), ([self.problem, {**self.action, "automated": True}], {"question"}),
                                  ([self.problem, {**self.action, "role": "boundary"}], {"question"})):
            with self.subTest(anchors=anchors):
                self.assertEqual(run(self.cfg, messages, anchors, complete=complete), {"solution": None, "usage_tokens": 0})
        complete.assert_not_called()

    def test_wrong_role_order_unanchored_problem_and_fabricated_quotes_cannot_reach_reviewer(self):
        prior = message("prior", "staff", 900, "请开启待办。")
        tied = message("tie", "staff", 1000, "请开启待办。")
        for problem, action in (
            (self.question_claim, claim("action", "客户确认开启。", self.goal)),
            (self.question_claim, claim("action", "开启待办。", prior)),
            (self.question_claim, claim("action", "开启待办。", tied)),
            (self.question_claim, claim("action", "开启待办。", {**self.action, "text": "杜撰原话"})),
            (claim("problem", "客户问设置。", self.goal), self.action_claim),
            (self.question_claim, {**self.action_claim, "state": "reported"}),
            (self.question_claim, {**self.action_claim, "actor": "merchant", "state": "reported"}),
        ):
            with self.subTest(problem=problem, action=action):
                result, complete = self.extract([prior, self.problem, tied, *self.messages[1:]], [problem, action])
                self.assertEqual(result, {"solution": None, "usage_tokens": 37})
                self.assertEqual(complete.call_count, 1)

    def test_complete_original_turn_retains_conditions_and_refuses_repeated_substring(self):
        condition = message("condition", "staff", 3000, "仅主账号可调整待办。请打开待办设置。再次打开待办设置。")
        repeated = claim("action", "打开待办设置。", {**condition, "text": "打开待办设置"})
        valid = claim("action", "仅主账号可调整待办设置。", {**condition, "text": "请打开待办设置"})
        result, complete = self.extract([self.problem, condition], [self.question_claim, repeated, valid],
                                        {"title_supported": True, "supported": [0, 1]})
        self.assertEqual(len(complete.call_args_list[1].args[2]["sections"]), 2)
        self.assertEqual(result["solution"]["sections"][1]["evidence"][0]["quote"], condition["text"])

    def test_expected_result_can_be_kept_but_courtesy_cannot_be_success(self):
        thanks = message("thanks", "user", 5000, "好的，谢谢。")
        result, complete = self.extract([*self.messages, thanks], [
            self.question_claim, self.action_claim,
            claim("verification", "相同对话出现稍等时应产生待办。", self.goal, mode="expected"),
            claim("verification", "客户已经确认问题解决。", thanks, mode="observed"),
            claim("verification", "实测复测成功。", self.goal, mode="expected"),
        ], {"title_supported": True, "supported": [0, 1, 2]})
        self.assertEqual(len(complete.call_args_list[1].args[2]["sections"]), 3)
        self.assertEqual(result["solution"]["sections"][-1]["evidence"][0]["message_id"], "goal")

    def test_explicit_retest_can_be_reported_until_later_evidence_conflicts(self):
        problem = message("question", "user", 1000, "退款场景已保存，但买家申请退款时没有触发。")
        issue = claim("problem", "买家申请退款后场景未触发。", problem)
        instruction = message("instruction", "staff", 2000, "请调整退款场景的触发条件并保存。")
        performed = message("performed", "user", 3000, "我调整退款场景的触发条件并保存了，重新测试退款场景已经触发。")
        failed = message("failed", "user", 4000, "同一条测试还是没有触发。")
        action = claim("action", "调整退款场景触发条件并保存。", instruction, actor="merchant", state="instructed")
        observed = claim("verification", "客户反馈调整并保存后复测，退款场景已触发。", performed, mode="observed")
        for later, accepted in (([], [0, 1, 2]), ([failed], [0, 1])):
            with self.subTest(later=later):
                result, _ = self.extract([problem, instruction, performed, *later], [issue, action, observed],
                                         {"title_supported": False, "supported": accepted}, title="没有依据的结论")
                self.assertEqual(result["solution"]["title"], issue["text"])
                self.assertEqual(len(result["solution"]["sections"]), len(accepted))

    def test_required_claim_needs_independent_approval_and_invalid_verdict_fails_closed(self):
        terminal, _ = self.extract(self.messages, [self.question_claim, self.action_claim], {"title_supported": True, "supported": []})
        self.assertEqual(terminal, {"solution": None, "usage_tokens": 56})
        for verdict in ({"title_supported": True, "supported": [False, 1]},
                        {"title_supported": True, "supported": [0, 1, 1]}, {"title_supported": "true", "supported": [0, 1]}):
            with self.subTest(verdict=verdict):
                result, _ = self.extract(self.messages, [self.question_claim, self.action_claim], verdict)
                self.assertEqual(result, {"solution": None, "usage_tokens": 56, "retry": True})

    def test_partial_keeps_only_reviewed_sections_without_a_solution_or_title(self):
        for supported, expected in (([0, 2], ["problem", "diagnosis"]), ([1], ["action"])):
            with self.subTest(supported=supported):
                result, _ = self.extract(self.messages, [self.question_claim, self.action_claim,
                    claim("diagnosis", "客服判断没有命中知识。", self.diagnosis)],
                    {"title_supported": True, "supported": supported}, title="不能作为完整方案的标题")
                self.assertIsNone(result["solution"])
                self.assertEqual(result["usage_tokens"], 56)
                self.assertNotIn("retry", result)
                self.assertEqual(set(result["partial"]), {"sections"})
                self.assertEqual([section["kind"] for section in result["partial"]["sections"]], expected)
                self.assertTrue(all(section["evidence"][0]["ts"] > 0 for section in result["partial"]["sections"]))

    def test_gateway_error_preserves_status_and_usage_from_completed_draft(self):
        complete = Mock(side_effect=[response({"sections": [self.question_claim, self.action_claim]}, 37), ModelProtocolError("模型超时", 504)])
        with self.assertRaises(ModelProtocolError) as raised:
            run(self.cfg, self.messages, {"question"}, complete=complete)
        self.assertEqual((raised.exception.status, raised.exception.usage_tokens), (504, 37))

    def test_review_retries_a_bound_checkpoint_without_generating_the_draft_again(self):
        saved = []
        source_quote = "配置为通用知识加入待办"
        action = {**self.action_claim, "evidence": [{"message_id": "staff-action", "quote": source_quote}], "unknown": "omit"}
        goal = claim("verification", "相同对话出现稍等时应产生待办。", self.goal, mode="expected")
        data = {"title": "稍等没有生成待办", "sections": [self.question_claim, action, goal], "unknown": "omit"}
        first = Mock(side_effect=[response(data, 37), ModelProtocolError("模型超时", 504)])
        with self.assertRaises(ModelProtocolError):
            run(self.cfg, self.messages, {"question"}, complete=first, checkpoint=saved.append)
        [draft] = saved
        self.assertEqual(set(draft), {"title", "sections"})
        self.assertEqual(draft["sections"][1], self.action_claim)
        self.assertEqual(draft["sections"][2], goal)

        retry = Mock(return_value=response({"title_supported": True, "supported": [0, 1, 2]}, 19))
        result = run(self.cfg, self.messages, {"question"}, complete=retry, draft=draft, checkpoint=saved.append)
        self.assertEqual(len(saved), 1)
        self.assertEqual(result["usage_tokens"], 19)
        self.assertEqual(result["solution"]["sections"][1]["text"], f"客服提出：{self.action_claim['text']}")
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args, first.call_args_list[1])

        stale = Mock()
        changed = [self.problem, self.diagnosis, {**self.action, "text": "没有配置修改。"}, self.goal]
        self.assertEqual(run(self.cfg, changed, {"question"}, complete=stale, draft=draft), {"solution": None, "usage_tokens": 0})
        stale.assert_not_called()
