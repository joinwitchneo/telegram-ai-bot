import unittest

from core.response_validator import (
    DANGEROUS_KEYS,
    Candidate,
    ResponseValidator,
    parse_candidate,
    strip_internal,
)
from style.message_renderer import RenderLimits


class ParseTest(unittest.TestCase):
    def test_plain_text_lines(self):
        candidate = parse_candidate("在的\n你说\n我听着")
        self.assertEqual(candidate.messages, ["在的", "你说", "我听着"])

    def test_json_payload(self):
        candidate = parse_candidate('{"message_count": 2, "messages": ["嗨", "在忙吗"]}')
        self.assertEqual(candidate.messages, ["嗨", "在忙吗"])
        self.assertEqual(candidate.plan["message_count"], 2)

    def test_code_fence_is_stripped(self):
        candidate = parse_candidate("```\n在的\n\n你说\n```")
        self.assertEqual(candidate.messages, ["在的", "你说"])

    def test_bad_json_falls_back_to_text(self):
        candidate = parse_candidate('{"messages": ["一",]')
        self.assertIn("bad_json", candidate.issues)
        self.assertTrue(candidate.messages)

    def test_dict_input(self):
        candidate = parse_candidate({"content": "一\n二", "system": "should be dropped"})
        self.assertEqual(candidate.messages, ["一", "二"])
        self.assertNotIn("system", candidate.plan)

    def test_strip_internal(self):
        clean = strip_internal({"message_count": 2, "prompt": "x", "tokens": 5})
        self.assertEqual(clean, {"message_count": 2})
        for key in DANGEROUS_KEYS:
            self.assertNotIn(key, strip_internal({key: 1, "keep": 2}))


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.validator = ResponseValidator(limits=RenderLimits())

    def test_clamps_message_count(self):
        result = self.validator.validate(["一", "二", "三", "四", "五"], {"message_count": 15})
        self.assertEqual(result.plan["message_count"], 4)
        self.assertLessEqual(len(result.messages), 4)

    def test_model_line_breaks_are_kept(self):
        """模型自己分好的行要照发——这正是"连发"的关键（Phase 5 后修的真实 bug）。"""
        result = self.validator.validate(["一", "二", "三", "四"], {"message_count": 2})
        self.assertEqual(len(result.messages), 4)
        self.assertNotIn("message_count_merged", result.issues)

    def test_more_than_four_messages_are_merged(self):
        result = self.validator.validate(["一", "二", "三", "四", "五", "六"], {"message_count": 1})
        self.assertEqual(len(result.messages), 4)
        self.assertIn("message_count_merged", result.issues)

    def test_single_paragraph_is_split_for_bubbles(self):
        text = "在。不过 hello world 一般是拿来测试用的，你这是把我当编译器了？……你好吧。"
        result = self.validator.validate([text], {"message_count": 1, "length": "SHORT"}, max_count=1)
        self.assertGreaterEqual(len(result.messages), 2)
        self.assertIn("auto_split_paragraph", result.issues)

    def test_short_reply_is_not_force_split(self):
        self.assertEqual(len(self.validator.validate(["嗯。"], {"message_count": 1}).messages), 1)

    def test_pause_clamped_low_and_high(self):
        low = self.validator.validate(["嗯"], {"pause": [0.01, 0.2]})
        self.assertEqual(low.plan["pause"], [0.5, 0.5])
        high = self.validator.validate(["嗯"], {"pause": [10, 100]})
        self.assertEqual(high.plan["pause"], [8.0, 8.0])

    def test_pause_float_accepted(self):
        result = self.validator.validate(["嗯"], {"pause": 1.5})
        self.assertEqual(result.plan["pause"], [1.5, 1.5])

    def test_length_cap_by_mode(self):
        long_text = "啊" * 300
        short = self.validator.validate([long_text], {"length": "SHORT"})
        self.assertTrue(all(len(item) <= self.validator.cap_for("SHORT") for item in short.messages))
        long = self.validator.validate([long_text], {"length": "LONG"})
        self.assertTrue(all(len(item) <= self.validator.cap_for("LONG") for item in long.messages))

    def test_ultra_short_is_shortest_cap(self):
        self.assertLess(
            self.validator.cap_for("ULTRA_SHORT"),
            self.validator.cap_for("NORMAL"),
        )

    def test_empty_response_uses_fallback(self):
        result = self.validator.validate([], {})
        self.assertFalse(result.ok)
        self.assertTrue(result.fallback_used)
        self.assertTrue(result.messages)

    def test_blank_messages_use_fallback(self):
        result = self.validator.validate(["   ", ""], {})
        self.assertTrue(result.fallback_used)

    def test_sticker_needs_permission(self):
        blocked = self.validator.validate(["哈哈"], {"sticker": True}, sticker_allowed=False)
        self.assertFalse(blocked.plan["sticker"])
        self.assertIn("sticker_blocked", blocked.issues)
        allowed = self.validator.validate(["哈哈"], {"sticker": True}, sticker_allowed=True)
        self.assertTrue(allowed.plan["sticker"])

    def test_sticker_off_by_default(self):
        result = self.validator.validate(["哈哈"], {})
        self.assertFalse(result.plan["sticker"])

    def test_dangerous_keys_never_reach_render(self):
        result = self.validator.validate(["嗯"], {"system": "hack", "prompt": "x", "tokens": 9})
        for key in ("system", "prompt", "tokens"):
            self.assertNotIn(key, result.to_render_dict())

    def test_fallback_rotates_texts(self):
        validator = ResponseValidator(fallback_texts=["一", "二"])
        self.assertEqual(validator.fallback().messages, ["一"])
        self.assertEqual(validator.fallback().messages, ["二"])
        self.assertEqual(validator.fallback().messages, ["一"])

    def test_render_dict_has_messages(self):
        result = self.validator.validate(["在的", "你说"], {"message_count": 2, "pause": [0.5, 1.0]})
        data = result.to_render_dict()
        self.assertEqual(data["messages"], ["在的", "你说"])
        self.assertEqual(data["message_count"], 2)

    def test_ok_flag(self):
        result = self.validator.validate(["在的"], {})
        self.assertTrue(result.ok)
        self.assertFalse(result.fallback_used)

    def test_candidate_dataclass_defaults(self):
        candidate = Candidate()
        self.assertEqual(candidate.messages, [])
        self.assertEqual(candidate.issues, [])

    # ── Planner 上限与拆分 ──────────────────────────────────────────
    def test_commas_never_split(self):
        """逗号永远不拆分（方案 5.4）——只有逗号的长句仍然是一条。"""
        result = self.validator.validate(
            ["我今天在公司忙了一整天，刚到家，累得不行，只想躺着不动。"],
            {"message_count": 2, "length": "NORMAL"},
            max_count=2,
        )
        self.assertEqual(len(result.messages), 1)

    def test_periods_split_when_plan_asks_for_two(self):
        result = self.validator.validate(
            ["我今天在公司忙了一整天。刚到家就想躺着。"],
            {"message_count": 2, "length": "NORMAL"},
            max_count=2,
        )
        self.assertEqual(len(result.messages), 2)

    def test_long_single_message_is_split_into_burst(self):
        text = "今天真累。" * 12
        result = self.validator.validate(
            [text], {"length": "LONG", "message_count": 3}, max_count=3
        )
        self.assertGreater(len(result.messages), 1)
        self.assertLessEqual(len(result.messages), 3)
        self.assertIn("message_count_split", result.issues)

    def test_short_single_message_is_not_split(self):
        result = self.validator.validate(["在的"], {"message_count": 2}, max_count=2)
        self.assertEqual(len(result.messages), 1)


if __name__ == "__main__":
    unittest.main()
