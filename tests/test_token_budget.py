import unittest

from core.token_budget import TokenBudget, estimate_messages, estimate_tokens, trim_blocks


class EstimateTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(estimate_tokens(""), 0)

    def test_chinese_costs_more_than_ascii(self):
        chinese = estimate_tokens("你好世界" * 10)
        ascii_text = estimate_tokens("hello world " * 4)
        self.assertGreater(chinese, ascii_text)

    def test_monotonic(self):
        self.assertLess(estimate_tokens("你好"), estimate_tokens("你好" * 50))

    def test_estimate_messages(self):
        total = estimate_messages([{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}])
        self.assertGreater(total, estimate_tokens("你好") + estimate_tokens("在"))


class TokenBudgetTest(unittest.TestCase):
    def make(self, **kwargs):
        defaults = dict(max_context_tokens=6000, max_output_tokens=400, daily_token_budget=10000,
                        soft_ratio=0.8, hard_ratio=0.95)
        defaults.update(kwargs)
        return TokenBudget(**defaults)

    def test_starts_ok(self):
        budget = self.make()
        self.assertEqual(budget.level(), TokenBudget.LEVEL_OK)
        self.assertEqual(budget.used_today(), 0)

    def test_soft_then_hard(self):
        budget = self.make()
        budget.record(8000)
        self.assertEqual(budget.level(), TokenBudget.LEVEL_SOFT)
        budget.record(1600)
        self.assertEqual(budget.level(), TokenBudget.LEVEL_HARD)

    def test_state_fields(self):
        budget = self.make()
        budget.record(5000)
        state = budget.state()
        self.assertEqual(state["used"], 5000)
        self.assertEqual(state["limit"], 10000)
        self.assertAlmostEqual(state["ratio"], 0.5, places=3)
        self.assertEqual(state["level"], TokenBudget.LEVEL_OK)
        self.assertEqual(state["max_context_tokens"], 6000)

    def test_can_call_blocks_when_hard(self):
        budget = self.make()
        budget.record(9800)
        allowed, reason = budget.can_call(100, 100)
        self.assertFalse(allowed)
        self.assertIn("硬上限", reason)

    def test_can_call_warns_when_context_too_big(self):
        budget = self.make()
        allowed, reason = budget.can_call(9000, 100)
        self.assertTrue(allowed)
        self.assertIn("裁剪", reason)

    def test_can_call_blocks_when_over_daily(self):
        budget = self.make(daily_token_budget=5000)
        budget.record(4000)
        allowed, _ = budget.can_call(3000, 400)
        self.assertFalse(allowed)


class TrimBlocksTest(unittest.TestCase):
    def blocks(self):
        return [
            {"name": "L0", "tokens": 300, "priority": 0},
            {"name": "L1", "tokens": 900, "priority": 0},
            {"name": "state", "tokens": 200, "priority": 1},
            {"name": "memory-low", "tokens": 400, "priority": 2},
            {"name": "memory-cold", "tokens": 500, "priority": 3},
            {"name": "history", "tokens": 1500, "priority": 4},
        ]

    def test_no_trim_when_within_budget(self):
        kept, dropped = trim_blocks(self.blocks(), 5000)
        self.assertEqual(len(kept), 6)
        self.assertEqual(dropped, [])

    def test_drops_high_priority_number_first(self):
        kept, dropped = trim_blocks(self.blocks(), 2400)
        names = [b["name"] for b in kept]
        self.assertIn("L0", names)
        self.assertIn("L1", names)
        self.assertEqual(dropped[0], "history")

    def test_never_drops_priority_zero(self):
        kept, dropped = trim_blocks(self.blocks(), 100)
        names = [b["name"] for b in kept]
        self.assertIn("L0", names)
        self.assertIn("L1", names)
        self.assertNotIn("L0", dropped)
        self.assertNotIn("L1", dropped)

    def test_dropped_list_matches_removed(self):
        kept, dropped = trim_blocks(self.blocks(), 2000)
        self.assertEqual(len(kept) + len(dropped), 6)


if __name__ == "__main__":
    unittest.main()
