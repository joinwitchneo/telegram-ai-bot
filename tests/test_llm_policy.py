import unittest

from core import llm_policy


class PolicyTest(unittest.TestCase):
    def test_command_never_calls_llm(self):
        decision = llm_policy.decide("/todo", is_command=True)
        self.assertFalse(decision.use_llm)
        self.assertEqual(decision.tier, "none")
        self.assertIn("命令", decision.reason)

    def test_rule_handled_never_calls_llm(self):
        decision = llm_policy.decide("提醒你喝水", rules_handled=True)
        self.assertFalse(decision.use_llm)
        self.assertEqual(decision.tier, "none")

    def test_rule_category_never_calls_llm(self):
        decision = llm_policy.decide("模板内容", category="rule")
        self.assertFalse(decision.use_llm)

    def test_summary_uses_cheap(self):
        decided = llm_policy.decide("会话内容", category="summary")
        self.assertTrue(decided.use_llm)
        self.assertEqual(decided.tier, "cheap")

    def test_extraction_uses_cheap(self):
        decided = llm_policy.decide("抽取", category="extraction")
        self.assertEqual(decided.tier, "cheap")

    def test_chat_uses_main(self):
        decided = llm_policy.decide("我今天在公司忙了一整天，刚到家")
        self.assertTrue(decided.use_llm)
        self.assertEqual(decided.tier, "main")
        self.assertEqual(decided.category, "chat")

    def test_filler_uses_cheap(self):
        for text in ("嗯", "哦哦", "哈哈", "6", "知道了"):
            decided = llm_policy.decide(text)
            self.assertTrue(decided.is_filler, text)
            self.assertEqual(decided.tier, "cheap", text)

    def test_long_text_is_not_filler(self):
        self.assertFalse(llm_policy.is_filler("嗯嗯我知道了你别说了我这就去"))

    def test_budget_hard_blocks_llm(self):
        decided = llm_policy.decide("你好", budget_state={"level": "hard", "ratio": 0.97})
        self.assertFalse(decided.use_llm)
        self.assertEqual(decided.tier, "none")
        self.assertIn("硬上限", decided.reason)

    def test_budget_soft_downgrades(self):
        decided = llm_policy.decide("我今天又加班了", budget_state={"level": "soft", "ratio": 0.85})
        self.assertTrue(decided.use_llm)
        self.assertEqual(decided.tier, "cheap")
        self.assertIn("降档", decided.reason)

    def test_budget_ok_keeps_main(self):
        decided = llm_policy.decide("我今天又加班了", budget_state={"level": "ok", "ratio": 0.1})
        self.assertEqual(decided.tier, "main")

    def test_tools_detected(self):
        self.assertIn("weather", llm_policy.detect_tools("明天天气怎么样"))
        self.assertIn("search", llm_policy.detect_tools("帮我查一下这个"))
        self.assertIn("reminders", llm_policy.detect_tools("提醒我明天买牛奶"))
        self.assertIn("memos", llm_policy.detect_tools("把这个密码记住"))
        self.assertEqual(llm_policy.detect_tools("我今天很累"), [])

    def test_merge_needed_for_short_message(self):
        decided = llm_policy.decide("在吗")
        self.assertTrue(decided.merge_needed)

    def test_no_merge_for_question(self):
        decided = llm_policy.decide("你觉得我该怎么做？")
        self.assertFalse(decided.merge_needed)

    def test_no_merge_for_long_message(self):
        decided = llm_policy.decide("我今天在公司忙了一整天，刚才才到家，累死了，感觉整个人都空了")
        self.assertFalse(decided.merge_needed)

    def test_attachment_note(self):
        decided = llm_policy.decide("看看这个", has_attachment=True)
        self.assertTrue(any("附件" in note for note in decided.notes))

    def test_log_string_contains_key_fields(self):
        text = llm_policy.decide("你好").as_log()
        for key in ("use_llm", "tier", "category", "merge", "tools", "budget"):
            self.assertIn(key, text)


if __name__ == "__main__":
    unittest.main()
