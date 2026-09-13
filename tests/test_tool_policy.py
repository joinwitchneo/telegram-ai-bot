import unittest

from tools.bootstrap import build_registry
from tools.core.policy import ToolPolicy


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry(city="香港")
        self.policy = ToolPolicy(self.registry)

    def test_plain_chat_goes_to_main_llm(self):
        decision = self.policy.decide("我今天在公司忙了一整天")
        self.assertEqual(decision.level, "L2")
        self.assertEqual(decision.tool, "")
        self.assertFalse(decision.is_direct)

    def test_time_question_is_python_only(self):
        decision = self.policy.decide("现在几点了")
        self.assertEqual(decision.tool, "clock")
        self.assertEqual(decision.level, "L0")
        self.assertTrue(decision.is_direct)

    def test_weekday_question(self):
        self.assertEqual(self.policy.decide("今天星期几").tool, "clock")

    def test_game_request(self):
        decision = self.policy.decide("陪我玩个游戏，掷个骰子")
        self.assertEqual(decision.tool, "game")
        self.assertTrue(decision.is_direct)

    def test_weather_needs_llm_after(self):
        decision = self.policy.decide("明天天气怎么样")
        self.assertEqual(decision.tool, "weather")
        self.assertTrue(decision.use_llm_after)
        self.assertFalse(decision.is_direct)

    def test_search_request(self):
        self.assertEqual(self.policy.decide("帮我查一下 Python 的 GIL").tool, "web_search")

    def test_url_goes_to_web_reader(self):
        decision = self.policy.decide("你看看这个 https://example.com/article")
        self.assertEqual(decision.tool, "web_reader")
        self.assertEqual(decision.args["url"], "https://example.com/article")

    def test_location_request(self):
        self.assertEqual(self.policy.decide("这个地方在哪里").tool, "location")

    def test_empty_message(self):
        self.assertEqual(self.policy.decide("   ").level, "L2")

    def test_disabled_policy(self):
        policy = ToolPolicy(self.registry, enabled=False)
        self.assertEqual(policy.decide("现在几点").level, "L2")

    def test_url_takes_priority_over_keywords(self):
        decision = self.policy.decide("搜索 https://example.com")
        self.assertEqual(decision.tool, "web_reader")

    def test_perception_decision(self):
        self.assertEqual(self.policy.decide_perception("image").tool, "image")
        self.assertEqual(self.policy.decide_perception("voice").tool, "audio")
        self.assertEqual(self.policy.decide_perception("video").tool, "video")
        self.assertEqual(self.policy.decide_perception("document").tool, "document")
        self.assertEqual(self.policy.decide_perception("nothing").level, "L2")

    def test_decision_log_text(self):
        text = self.policy.decide("现在几点").as_log()
        self.assertIn("level=L0", text)
        self.assertIn("clock", text)


if __name__ == "__main__":
    unittest.main()
