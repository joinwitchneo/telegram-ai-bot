"""提醒解析器：口语时间 + 提醒内容分离（纯 Python，0 token）。"""

import datetime
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tools.reminders import parse_reminder, parse_reminder_with_llm   # noqa: E402

# 2026-09-13 是周日，晚上 8 点
SUNDAY_EVENING = datetime.datetime(2026, 9, 13, 20, 0, 0)


def when(text: str) -> datetime.datetime | None:
    result = parse_reminder(text, SUNDAY_EVENING)
    return (result or {}).get("when")


def content(text: str) -> str:
    return (parse_reminder(text, SUNDAY_EVENING) or {}).get("content", "")


class ReportedBugTest(unittest.TestCase):
    """这次点名的两个 bug，必须长期锁住。"""

    def test_half_hour_and_content_not_mangled(self):
        # 旧行为：15:00 + 内容被削成"半交表"
        self.assertEqual(when("周五下午3点半交表"), datetime.datetime(2026, 9, 18, 15, 30))
        self.assertEqual(content("周五下午3点半交表"), "交表")

    def test_evening_hour_is_23_not_11(self):
        # 旧行为：算成 11:00 且内容留了个"每"
        self.assertEqual(when("每晚11点提醒睡觉"), datetime.datetime(2026, 9, 13, 23, 0))
        self.assertEqual(content("每晚11点提醒睡觉"), "睡觉")


class SameFamilyFixTest(unittest.TestCase):
    """同一段代码里一起暴露出来的同类问题。"""

    def test_period_applies_to_hour_and_minute(self):
        # 旧行为：忽略"下午"，算成 03:15
        self.assertEqual(when("周五下午3点15分交表"), datetime.datetime(2026, 9, 18, 15, 15))
        self.assertEqual(content("周五下午3点15分交表"), "交表")

    def test_chinese_hour_digits(self):
        self.assertEqual(when("下午三点开会"), datetime.datetime(2026, 9, 14, 15, 0))
        self.assertEqual(when("每天晚上11点半睡觉"), datetime.datetime(2026, 9, 13, 23, 30))
        self.assertEqual(content("下午三点开会"), "开会")

    def test_every_day_word_does_not_leak_into_content(self):
        self.assertEqual(when("每天8点吃药"), datetime.datetime(2026, 9, 14, 8, 0))
        self.assertEqual(content("每天8点吃药"), "吃药")

    def test_evening_without_clock_uses_evening_not_9am(self):
        # 旧行为："周六晚上" 被排到早上 9 点，而这是 /remind 帮助里自己的例子
        self.assertEqual(when("周六晚上买牛奶"), datetime.datetime(2026, 9, 19, 20, 0))
        self.assertEqual(content("周六晚上买牛奶"), "买牛奶")

    def test_tomorrow_evening_and_tonight_keep_their_day(self):
        self.assertEqual(when("明晚8点看电影"), datetime.datetime(2026, 9, 14, 20, 0))
        self.assertEqual(when("今晚10点提醒我睡觉"), datetime.datetime(2026, 9, 13, 22, 0))
        self.assertEqual(content("今晚10点提醒我睡觉"), "睡觉")

    def test_weekday_prefix_does_not_leak_into_content(self):
        self.assertEqual(when("下周一上午十点开会"), datetime.datetime(2026, 9, 14, 10, 0))
        self.assertEqual(content("下周一上午十点开会"), "开会")


class NoRegressionTest(unittest.TestCase):
    def test_relative_hours_and_minutes_still_work(self):
        self.assertEqual(when("2小时后提醒我喝水"), datetime.datetime(2026, 9, 13, 22, 0))
        self.assertEqual(when("30分钟后"), datetime.datetime(2026, 9, 13, 20, 30))

    def test_plain_note_without_time_is_still_none(self):
        self.assertIsNone(parse_reminder("买牛奶", SUNDAY_EVENING))
        self.assertIsNone(parse_reminder("提醒我早点睡", SUNDAY_EVENING),
                          "没写时间就不该硬塞一个时间，也不该把内容削坏")

    def test_water_repeat_path_unchanged(self):
        result = parse_reminder("每2小时喝水", SUNDAY_EVENING)
        self.assertEqual(result.get("repeat"), "water")

    def test_llm_fallback_still_parses_json(self):
        raw = '{"when": "2026-09-14 07:30", "content": "吃药", "repeat": ""}'
        result = parse_reminder_with_llm("随便一句话", lambda _prompt: raw, SUNDAY_EVENING)
        self.assertEqual(result["when"], datetime.datetime(2026, 9, 14, 7, 30))
        self.assertEqual(result["content"], "吃药")


if __name__ == "__main__":
    unittest.main()
