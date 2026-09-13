import datetime
import unittest

from core.response_planner import (
    PAUSE_RANGES,
    ResponsePlanner,
    has_negative_emotion,
    is_low_value,
    is_question,
    is_request,
)


def signals(**overrides) -> dict:
    base = {
        "emotion": {
            "joy": 0.45, "interest": 0.5, "affection": 0.35, "trust": 0.4, "calm": 0.55,
            "excitement": 0.28, "sadness": 0.12, "anger": 0.08, "anxiety": 0.15,
            "loneliness": 0.22, "embarrassment": 0.08, "fatigue": 0.3,
        },
        "relationship": {
            "familiarity": 0.05, "trust": 0.3, "intimacy": 0.05,
            "interaction_heat": 0.2, "shared_experience": 0.02,
        },
    }
    for group, values in overrides.items():
        base.setdefault(group, {}).update(values)
    return base


class DetectionTest(unittest.TestCase):
    def test_question_detection(self):
        self.assertTrue(is_question("在吗"))
        self.assertTrue(is_question("你觉得我该学吗？"))
        self.assertFalse(is_question("今天天气不错"))

    def test_request_detection(self):
        self.assertTrue(is_request("帮我查一下明天的天气"))
        self.assertFalse(is_request("我今天挺累的"))

    def test_negative_detection(self):
        self.assertTrue(has_negative_emotion("我今天真的烦死了"))
        self.assertFalse(has_negative_emotion("今天挺顺利的"))

    def test_low_value_detection(self):
        self.assertTrue(is_low_value("嗯"))
        self.assertTrue(is_low_value("哈哈哈"))
        self.assertFalse(is_low_value("我今天在公司忙了一整天"))


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.planner = ResponsePlanner()

    # ── 基础形态 ────────────────────────────────────────────────────
    def test_normal_chat_replies(self):
        plan = self.planner.plan("我今天在公司忙了一整天，刚到家", mode="CASUAL", signals=signals())
        self.assertTrue(plan.should_reply)
        self.assertIn(plan.length, ("SHORT", "NORMAL", "LONG"))
        self.assertLessEqual(plan.message_count, 4)

    def test_question_must_reply(self):
        plan = self.planner.plan("在吗", mode="CASUAL", signals=signals(), consecutive_replies=2)
        self.assertTrue(plan.should_reply)
        self.assertTrue(plan.must_reply)
        self.assertEqual(plan.reason, "hard_rule")
        self.assertNotEqual(plan.length, "ULTRA_SHORT")

    def test_negative_emotion_is_gentle_and_must_reply(self):
        plan = self.planner.plan("我今天考试考砸了，特别难受", mode="EMOTIONAL", signals=signals())
        self.assertTrue(plan.must_reply)
        self.assertEqual(plan.tone, "GENTLE")
        self.assertIn(plan.length, ("SHORT", "NORMAL"))

    def test_tool_request_never_stickers(self):
        plan = self.planner.plan("帮我查一下明天天气", mode="TASK", signals=signals())
        self.assertTrue(plan.tool_needed)
        self.assertFalse(plan.sticker_allowed)

    def test_deep_talk_gets_room(self):
        plan = self.planner.plan(
            "我最近一直在想，要不要把Python继续学下去，因为总觉得没方向，你怎么看",
            mode="DEEP",
            signals=signals(),
        )
        self.assertIn(plan.length, ("NORMAL", "LONG"))
        self.assertGreaterEqual(plan.message_count, 1)

    def test_short_style_downgrades_length(self):
        style = {"average_message_length": 8.0, "short_message_ratio": 0.8, "consecutive_message_count": 1.0}
        long_text = "我最近在想一个挺复杂的事情，" + "说来话长，" * 8
        with_style = self.planner.plan(long_text, mode="DEEP", signals=signals(), style=style)
        without = self.planner.plan(long_text, mode="DEEP", signals=signals())
        self.assertLessEqual(
            ["ULTRA_SHORT", "SHORT", "NORMAL", "LONG"].index(with_style.length),
            ["ULTRA_SHORT", "SHORT", "NORMAL", "LONG"].index(without.length),
        )

    def test_pause_range_matches_length(self):
        for mode, text in (("CASUAL", "嗯？"), ("DEEP", "我最近在想一个很复杂的问题，想听听你的看法")):
            plan = self.planner.plan(text, mode=mode, signals=signals())
            self.assertEqual(plan.pause, PAUSE_RANGES[plan.length])
            self.assertGreaterEqual(plan.pause[0], 0.4)
            self.assertLessEqual(plan.pause[1], 4.5)

    # ── 不回复 ──────────────────────────────────────────────────────
    def test_low_value_first_message_still_replies(self):
        plan = self.planner.plan("嗯", mode="CASUAL", signals=signals(), consecutive_replies=0)
        self.assertTrue(plan.should_reply)
        self.assertEqual(plan.reason, "first_message_always_reply")

    def test_low_value_during_chat_can_stay_silent(self):
        plan = self.planner.plan("嗯", mode="CASUAL", signals=signals(), consecutive_replies=2)
        self.assertFalse(plan.should_reply)
        self.assertEqual(plan.reason, "low_value_saturated")
        self.assertEqual(plan.message_count, 0)

    def test_silence_has_cooldown(self):
        now = datetime.datetime(2026, 9, 13, 12, 0, 0)
        planner = ResponsePlanner(now_fn=lambda: now)
        first = planner.plan("嗯", mode="CASUAL", signals=signals(), consecutive_replies=2, now=now)
        second = planner.plan("哦", mode="CASUAL", signals=signals(), consecutive_replies=2, now=now)
        self.assertFalse(first.should_reply)
        self.assertTrue(second.should_reply)
        self.assertEqual(second.reason, "silence_cooldown")

    def test_silence_disabled_by_config(self):
        planner = ResponsePlanner(allow_silence=False)
        plan = planner.plan("嗯", mode="CASUAL", signals=signals(), consecutive_replies=3)
        self.assertTrue(plan.should_reply)

    def test_long_gap_always_replies(self):
        plan = self.planner.plan("嗯", mode="CASUAL", signals=signals(), consecutive_replies=3, gap_minutes=45)
        self.assertTrue(plan.should_reply)

    def test_duplicate_message_gets_lower_score(self):
        high, _, _ = self.planner.score("我今天在公司忙了一整天", mode="CASUAL", signals=signals())
        low, _, _ = self.planner.score(
            "我今天在公司忙了一整天", mode="CASUAL", signals=signals(),
            last_user_text="我今天在公司忙了一整天",
        )
        self.assertLess(low, high)

    # ── 可解释性与确定性 ────────────────────────────────────────────
    def test_score_parts_are_explainable(self):
        score, parts, hard = self.planner.score("你觉得我该不该学爬虫", mode="CASUAL", signals=signals())
        self.assertIn("base", parts)
        self.assertIn("question", parts)
        self.assertTrue(hard["question"])
        self.assertGreater(score, 0.5)

    def test_relationship_signals_raise_score(self):
        cold, _, _ = self.planner.score("我今天在公司忙了一整天", mode="CASUAL", signals=signals())
        warm, _, _ = self.planner.score(
            "我今天在公司忙了一整天",
            mode="CASUAL",
            signals=signals(relationship={"interaction_heat": 0.8, "intimacy": 0.5}),
        )
        self.assertGreater(warm, cold)

    def test_her_fatigue_shortens(self):
        plan = self.planner.plan(
            "那你今天都干嘛了呀", mode="CASUAL", signals=signals(emotion={"fatigue": 0.9})
        )
        self.assertEqual(plan.tone, "QUIET")

    def test_happy_state_is_playful(self):
        plan = self.planner.plan(
            "哈哈哈哈我今天真的笑死了", mode="CASUAL", signals=signals(emotion={"joy": 0.75})
        )
        self.assertEqual(plan.tone, "PLAYFUL")

    def test_no_randomness(self):
        first = self.planner.plan("你今天在忙什么", mode="CASUAL", signals=signals())
        second = self.planner.plan("你今天在忙什么", mode="CASUAL", signals=signals())
        self.assertEqual(first.to_plan_dict(), second.to_plan_dict())

    def test_message_count_never_exceeds_limit(self):
        planner = ResponsePlanner(max_messages=2)
        plan = planner.plan("我最近在想一个很复杂的问题，" + "说很久，" * 20, mode="DEEP", signals=signals())
        self.assertLessEqual(plan.message_count, 2)

    def test_plan_dict_shape(self):
        plan = self.planner.plan("你在干嘛", mode="CASUAL", signals=signals())
        data = plan.to_plan_dict()
        for key in ("should_reply", "reply_mode", "message_count", "length", "tone", "split", "pause", "sticker"):
            self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
