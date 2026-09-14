"""铁律：指令由 Python 直接回答，不调用模型。

这里只测"/remind 的时间兜底"这一处真实存在的模型入口——
它必须是配置开关（默认关），关掉时 /remind 完全不碰模型。
"""

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import bot as bot_module                       # noqa: E402
from tools.reminders import ReminderStore      # noqa: E402


class FakeConfig:
    def __init__(self, values: dict) -> None:
        self.values = dict(values)

    def get(self, name, default=""):
        return str(self.values.get(name, default))

    def get_int(self, name, default=0):
        try:
            return int(self.values.get(name, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, name, default=0.0):
        try:
            return float(self.values.get(name, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, name, default=False):
        return str(self.values.get(name, default)).lower() in ("1", "true", "yes", "on")


def make_bot(config_values: dict):
    """只装配 cmd_remind 需要的字段，不走 Bot.__init__（那会联网校验模型）。"""
    bot = object.__new__(bot_module.Bot)
    bot.config = FakeConfig(config_values)
    bot.reminders = ReminderStore(Path(tempfile.mkdtemp()) / "reminders.json")
    bot.sent: list = []
    bot.send_message = lambda chat_id, text: bot.sent.append((chat_id, text))
    bot.cheap_calls: list = []

    def fake_cheap(prompt):
        bot.cheap_calls.append(prompt)
        when = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        return json.dumps({"when": when, "content": "吃药", "repeat": ""}, ensure_ascii=False)

    bot._ask_cheap = fake_cheap
    return bot


class RemindCommandTest(unittest.TestCase):
    def test_pure_python_path_handles_normal_sentences(self):
        bot = make_bot({"REMIND_LLM_PARSE": "false"})
        bot.cmd_remind(1, "明天早上八点吃药")
        self.assertEqual(bot.cheap_calls, [], "能解析的句子不该碰模型")
        self.assertEqual(len(bot.reminders.all()), 1)
        self.assertIn("记下了", bot.sent[0][1])
        self.assertNotIn("REMIND_LLM_PARSE", bot.sent[0][1], "解析成功时不该出现兜底提示")

    def test_llm_fallback_is_off_by_default(self):
        bot = make_bot({"REMIND_LLM_PARSE": "false"})
        bot.cmd_remind(1, "周末有空的时候记得吃药")
        self.assertEqual(bot.cheap_calls, [], "默认关掉兜底，指令必须 0 token")
        self.assertIn("没定时间", bot.sent[0][1])
        self.assertIn("REMIND_LLM_PARSE", bot.sent[0][1], "解析不出时要说清楚怎么开启兜底")

    def test_no_time_words_gives_plain_reminder_without_hint(self):
        bot = make_bot({"REMIND_LLM_PARSE": "false"})
        bot.cmd_remind(1, "买牛奶")
        self.assertEqual(bot.cheap_calls, [])
        self.assertIn("没定时间", bot.sent[0][1])
        self.assertNotIn("REMIND_LLM_PARSE", bot.sent[0][1], "本来就没提时间的句子不该提示")

    def test_llm_fallback_can_be_enabled_by_config(self):
        bot = make_bot({"REMIND_LLM_PARSE": "true"})
        bot.cmd_remind(1, "周末有空的时候记得吃药")
        self.assertEqual(len(bot.cheap_calls), 1, "显式打开时才允许调用便宜模型")
        self.assertIn("吃药", bot.sent[0][1])

    def test_reported_bug_is_fixed_through_the_command(self):
        """点名修的那个 bug，从指令这一层再验一次。"""
        bot = make_bot({"REMIND_LLM_PARSE": "false"})
        bot.cmd_remind(1, "周五下午3点半交表")
        self.assertEqual(bot.cheap_calls, [], "纯 Python 就该解析出来")
        item = bot.reminders.all()[0]
        self.assertTrue(item["due"].endswith("T15:30:00"), item["due"])
        self.assertEqual(item["content"], "交表")


if __name__ == "__main__":
    unittest.main()
