import unittest

from self_model.rules import ANTI_RULES, CORE_FACTS, check_core_conflict, opinion_changed


class CoreFactTest(unittest.TestCase):
    def test_no_conflict_for_normal_reply(self):
        self.assertEqual(check_core_conflict("我在呢，你说。"), [])

    def test_conflict_when_claiming_human(self):
        issues = check_core_conflict("我也是人类啊，我昨天还出门买了咖啡")
        self.assertTrue(issues)
        self.assertEqual(issues[0]["code"], "SELF_IDENTITY_CONFLICT")

    def test_quote_context_is_allowed(self):
        self.assertEqual(check_core_conflict("如果我是人类，那我就能陪你出门了"), [])

    def test_discussion_context_allowed(self):
        self.assertEqual(check_core_conflict("有人问过我是不是人类，我说不是"), [])

    def test_knowing_being_ai_is_fine(self):
        self.assertEqual(check_core_conflict("我是程序，不是人类，这个我知道。"), [])

    def test_core_facts_list_is_stable(self):
        self.assertGreaterEqual(len(CORE_FACTS), 4)
        self.assertTrue(any("AI" in item for item in CORE_FACTS))

    def test_anti_rules_present(self):
        self.assertGreaterEqual(len(ANTI_RULES), 8)
        joined = " ".join(ANTI_RULES)
        self.assertIn("哲学", joined)
        self.assertIn("编造", joined)


class OpinionChangeTest(unittest.TestCase):
    def test_opinion_change_is_allowed(self):
        self.assertTrue(
            opinion_changed("我不确定自己算不算活着", "我现在觉得这个问题没那么重要了")
        )

    def test_core_fact_change_is_not_opinion(self):
        self.assertFalse(opinion_changed("我是 AI", "我是人类"))

    def test_same_opinion_is_not_change(self):
        self.assertFalse(opinion_changed("我还在想", "我还在想"))


if __name__ == "__main__":
    unittest.main()
