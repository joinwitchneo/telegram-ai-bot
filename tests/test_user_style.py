import datetime
import tempfile
import unittest
from pathlib import Path

from style import style_adapter
from style.user_style import UserStyle


def make_style(**kwargs) -> UserStyle:
    tmp = Path(tempfile.mkdtemp())
    return UserStyle(tmp / "user_style.json", **kwargs)


class EmaTest(unittest.TestCase):
    def test_first_observation_uses_alpha(self):
        style = make_style(alpha=0.1)
        profile = style.observe(1, "啊" * 100)
        self.assertAlmostEqual(profile["average_message_length"], 10.0, places=1)

    def test_ema_is_gradual_not_jumpy(self):
        style = make_style(alpha=0.1)
        for _ in range(20):
            style.observe(1, "短")
        before = style.profile(1)["average_message_length"]
        after = style.observe(1, "长" * 200)["average_message_length"]
        self.assertGreater(after, before)
        self.assertLess(after - before, 25)   # 一条长消息不会把均值拉爆


class ProfileTest(unittest.TestCase):
    def test_short_messages_raise_short_ratio(self):
        style = make_style()
        for _ in range(40):
            style.observe(1, "嗯嗯")
        profile = style.profile(1)
        self.assertGreater(profile["short_message_ratio"], 0.6)
        self.assertLess(profile["long_message_ratio"], 0.2)

    def test_long_messages_raise_long_ratio(self):
        style = make_style()
        for _ in range(40):
            style.observe(1, "今天在公司发生了一件挺复杂的事情，我跟同事聊了很久，" * 3)
        profile = style.profile(1)
        self.assertGreater(profile["long_message_ratio"], 0.6)

    def test_emoji_frequency(self):
        style = make_style()
        for _ in range(20):
            style.observe(1, "好耶😄")
        self.assertGreater(style.profile(1)["emoji_frequency"], 0.4)
        other = make_style()
        for _ in range(20):
            other.observe(1, "好的")
        self.assertLess(other.profile(1)["emoji_frequency"], 0.1)

    def test_question_frequency(self):
        style = make_style()
        for _ in range(20):
            style.observe(1, "你在干嘛？")
        self.assertGreater(style.profile(1)["question_frequency"], 0.4)

    def test_punctuation_style(self):
        rare = make_style()
        for _ in range(10):
            rare.observe(1, "在忙什么")
        self.assertEqual(rare.profile(1)["punctuation_style"], "极少标点")
        heavy = make_style()
        for _ in range(40):
            heavy.observe(1, "好。然后呢？")
        self.assertEqual(heavy.profile(1)["punctuation_style"], "标点完整")

    def test_consecutive_bursts(self):
        style = make_style()
        base = datetime.datetime(2026, 9, 13, 12, 0, 0)
        for round_index in range(10):
            start = base + datetime.timedelta(minutes=30 * round_index)
            for index in range(4):
                style.observe(1, f"第{index}条", now=start + datetime.timedelta(seconds=10 * index))
        self.assertGreater(style.profile(1)["consecutive_message_count"], 1.5)

    def test_burst_resets_after_long_gap(self):
        style = make_style()
        base = datetime.datetime(2026, 9, 13, 12, 0, 0)
        for index in range(3):
            style.observe(1, "连发", now=base + datetime.timedelta(seconds=5 * index))
        style.observe(1, "隔了很久", now=base + datetime.timedelta(hours=5))
        self.assertLessEqual(style.profile(1)["consecutive_message_count"], 1.2)

    def test_reply_interval_recorded(self):
        style = make_style()
        base = datetime.datetime(2026, 9, 13, 12, 0, 0)
        for index in range(10):
            style.observe(1, "在", now=base + datetime.timedelta(minutes=10 * index))
        self.assertGreater(style.profile(1)["reply_interval"], 0)

    def test_median_and_average(self):
        style = make_style()
        for text in ("一", "二三", "四五六", "七八九十"):
            style.observe(1, text)
        profile = style.profile(1)
        self.assertGreater(profile["median_message_length"], 0)
        self.assertGreater(profile["average_message_length"], 0)

    def test_common_words(self):
        style = make_style()
        for _ in range(5):
            style.observe(1, "python 真的挺好用的")
        self.assertIn("python", style.profile(1)["common_words"])

    def test_active_hours(self):
        style = make_style()
        style.observe(1, "早", now=datetime.datetime(2026, 9, 13, 9, 0, 0))
        self.assertIn(9, style.profile(1)["active_hours"])

    def test_ready_flag(self):
        style = make_style(min_messages=3)
        style.observe(1, "一")
        self.assertFalse(style.profile(1)["ready"])
        style.observe(1, "二")
        style.observe(1, "三")
        self.assertTrue(style.profile(1)["ready"])

    def test_chats_are_separate(self):
        style = make_style()
        for _ in range(10):
            style.observe(1, "很长很长的一句话" * 5)
        profile_two = style.profile(2)
        self.assertEqual(profile_two["messages"], 0)
        self.assertEqual(profile_two["average_message_length"], 0.0)

    def test_persistence(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "user_style.json"
        style = UserStyle(path)
        for _ in range(5):
            style.observe(7, "哈哈")
        again = UserStyle(path)
        self.assertEqual(again.profile(7)["messages"], 5)

    def test_reset(self):
        style = make_style()
        style.observe(1, "哈哈")
        style.reset(1)
        self.assertEqual(style.profile(1)["messages"], 0)

    def test_empty_text_ignored(self):
        style = make_style()
        style.observe(1, "   ")
        self.assertEqual(style.profile(1)["messages"], 0)


class StyleAdapterTest(unittest.TestCase):
    def test_guidance_needs_ready_profile(self):
        self.assertEqual(style_adapter.guidance({"ready": False}), [])
        self.assertEqual(style_adapter.render(None), "")

    def test_short_user_guidance(self):
        profile = {
            "ready": True, "average_message_length": 6.0, "short_message_ratio": 0.8,
            "long_message_ratio": 0.05, "consecutive_message_count": 1.0,
            "emoji_frequency": 0.0, "question_frequency": 0.1, "punctuation_style": "极少标点",
        }
        lines = style_adapter.guidance(profile)
        self.assertTrue(any("短" in line for line in lines))
        self.assertTrue(lines[-1] == style_adapter.MIRROR_WARNING)

    def test_burst_user_guidance(self):
        profile = {
            "ready": True, "average_message_length": 20.0, "short_message_ratio": 0.2,
            "long_message_ratio": 0.1, "consecutive_message_count": 3.0,
            "emoji_frequency": 0.1, "question_frequency": 0.1, "punctuation_style": "标点适中",
        }
        text = style_adapter.render(profile)
        self.assertIn("连发", text)

    def test_guidance_has_no_numbers(self):
        profile = {
            "ready": True, "average_message_length": 6.0, "short_message_ratio": 0.8,
            "long_message_ratio": 0.0, "consecutive_message_count": 3.0,
            "emoji_frequency": 0.5, "question_frequency": 0.2, "punctuation_style": "极少标点",
        }
        text = style_adapter.render(profile)
        self.assertFalse(any(char.isdigit() for char in text))

    def test_guard_against_mirroring(self):
        profile = {
            "ready": True, "average_message_length": 6.0, "short_message_ratio": 0.8,
            "long_message_ratio": 0.0, "consecutive_message_count": 1.0,
            "emoji_frequency": 0.0, "question_frequency": 0.0, "punctuation_style": "极少标点",
        }
        self.assertIn("别逐字模仿", style_adapter.render(profile))


if __name__ == "__main__":
    unittest.main()
