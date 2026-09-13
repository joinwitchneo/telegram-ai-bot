import json
import tempfile
import unittest
from pathlib import Path

from core.usage_logger import UsageLogger


class UsageLoggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.logger = UsageLogger(self.tmp / "usage.json")

    def test_record_accumulates(self):
        self.logger.record(category="chat", model="m", input_tokens=1000, output_tokens=100, cached_tokens=800)
        self.logger.record(category="chat", model="m", input_tokens=500, output_tokens=50, cached_tokens=0)
        day = self.logger.day()
        self.assertEqual(day["requests"], 2)
        self.assertEqual(day["input_tokens"], 1500)
        self.assertEqual(day["output_tokens"], 150)
        self.assertEqual(day["cached_tokens"], 800)
        self.assertEqual(day["cache_hit"], 1)
        self.assertEqual(day["cache_miss"], 1)
        self.assertEqual(day["by_category"]["chat"]["requests"], 2)

    def test_categories_separated(self):
        self.logger.record(category="chat", input_tokens=100)
        self.logger.record(category="summary", input_tokens=200, output_tokens=50)
        self.logger.record(category="proactive", input_tokens=300, fallback_from="strong")
        day = self.logger.day()
        self.assertEqual(day["by_category"]["summary"]["input_tokens"], 200)
        self.assertEqual(day["by_category"]["proactive"]["requests"], 1)
        self.assertEqual(day["fallbacks"], 1)

    def test_retry_counted(self):
        self.logger.record(category="chat", retry=True)
        self.assertEqual(self.logger.day()["retries"], 1)

    def test_summary_averages_and_hit_rate(self):
        self.logger.record(category="chat", input_tokens=1000, output_tokens=100, cached_tokens=500)
        self.logger.record(category="chat", input_tokens=1000, output_tokens=200, cached_tokens=0)
        summary = self.logger.summary(days=7)
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["avg_input_tokens"], 1000)
        self.assertEqual(summary["avg_output_tokens"], 150)
        self.assertEqual(summary["avg_cached_tokens"], 250)
        self.assertAlmostEqual(summary["cache_hit_rate"], 0.5, places=3)

    def test_recent_limit(self):
        for index in range(5):
            self.logger.record(category="chat", input_tokens=index)
        self.assertEqual(len(self.logger.recent(3)), 3)

    def test_record_local_does_not_add_tokens(self):
        self.logger.record_local("rule", 500, note="命令")
        day = self.logger.day()
        self.assertEqual(day["input_tokens"], 0)
        self.assertEqual(day["by_category"]["rule"]["requests"], 1)

    def test_persistence(self):
        self.logger.record(category="chat", input_tokens=42)
        reloaded = UsageLogger(self.tmp / "usage.json")
        self.assertEqual(reloaded.day()["input_tokens"], 42)

    def test_disabled_logger_records_nothing(self):
        quiet = UsageLogger(self.tmp / "quiet.json", enabled=False)
        self.assertEqual(quiet.record(category="chat", input_tokens=100), {})
        self.assertEqual(quiet.day()["requests"], 0)

    def test_unknown_category_falls_back_to_chat(self):
        self.logger.record(category="nonsense", input_tokens=10)
        self.assertIn("chat", self.logger.day()["by_category"])

    def test_file_is_valid_json(self):
        self.logger.record(category="chat", input_tokens=1)
        data = json.loads((self.tmp / "usage.json").read_text(encoding="utf-8"))
        self.assertIn("days", data)
        self.assertIn("recent", data)


if __name__ == "__main__":
    unittest.main()
