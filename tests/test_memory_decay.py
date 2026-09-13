import datetime
import tempfile
import unittest
from pathlib import Path

from memory.memory_decay import MemoryDecay
from memory.memory_store import MemoryStore

DAY = 86400


class DecayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(self.tmp / "memories.json")
        self.decay = MemoryDecay(self.store, hot_days=3, warm_days=30, archive_days=90)
        self.now = datetime.datetime(2026, 9, 13, 12, 0)

    def make(self, content="用户喜欢喝美式", importance=0.6, used_days_ago=0.0, protected=False):
        record, _ = self.store.create(type="preference", content=content, importance=importance, protected=protected)
        moment = self.now - datetime.timedelta(days=used_days_ago)
        self.store.update(record["id"], last_used_at=moment.isoformat(timespec="seconds"))
        return record

    def test_fresh_memory_is_hot(self):
        record = self.make(used_days_ago=0.5)
        self.assertEqual(self.decay.evaluate(self.store.get(record["id"]), self.now), "HOT")

    def test_frequent_use_stays_hot(self):
        record = self.make(used_days_ago=5)
        for _ in range(3):
            self.store.mark_used(record["id"])
        self.assertEqual(self.decay.evaluate(self.store.get(record["id"]), self.now), "HOT")

    def test_important_memory_warm(self):
        record = self.make(importance=0.8, used_days_ago=20)
        self.assertEqual(self.decay.evaluate(self.store.get(record["id"]), self.now), "WARM")

    def test_low_importance_becomes_cold(self):
        record = self.make(importance=0.2, used_days_ago=60)
        self.assertEqual(self.decay.evaluate(self.store.get(record["id"]), self.now), "COLD")

    def test_old_low_importance_archived(self):
        record = self.make(importance=0.2, used_days_ago=200)
        self.assertEqual(self.decay.evaluate(self.store.get(record["id"]), self.now), "ARCHIVED")

    def test_old_but_important_not_archived(self):
        record = self.make(importance=0.9, used_days_ago=200)
        self.assertEqual(self.decay.evaluate(self.store.get(record["id"]), self.now), "WARM")

    def test_protected_never_archived(self):
        record = self.make(importance=0.1, used_days_ago=400, protected=True)
        self.assertEqual(self.decay.evaluate(self.store.get(record["id"]), self.now), "COLD")
        self.assertNotEqual(self.store.get(record["id"])["temperature"], "ARCHIVED")

    def test_apply_updates_temperature(self):
        hot = self.make(content="用户喜欢听歌", used_days_ago=1)
        old = self.make(content="用户喜欢钓鱼", importance=0.1, used_days_ago=200)
        report = self.decay.apply(now=self.now)
        self.assertEqual(self.store.get(hot["id"])["temperature"], "HOT")
        self.assertEqual(self.store.get(old["id"])["temperature"], "ARCHIVED")
        self.assertGreater(report["moved"], 0)
        self.assertGreaterEqual(report["evaluated"], 2)

    def test_temperature_does_not_change_importance(self):
        record = self.make(importance=0.7, used_days_ago=200)
        self.decay.apply(now=self.now)
        self.assertEqual(self.store.get(record["id"])["importance"], 0.7)

    def test_used_cold_memory_warms_up(self):
        record = self.make(importance=0.2, used_days_ago=60)
        self.decay.apply(now=self.now)
        self.assertEqual(self.store.get(record["id"])["temperature"], "COLD")
        self.store.mark_used(record["id"], now=self.now)
        self.assertEqual(self.store.get(record["id"])["temperature"], "WARM")

    def test_no_last_used_falls_back_to_created(self):
        record, _ = self.store.create(type="fact", content="用户是左撇子")
        self.assertIn(self.decay.evaluate(self.store.get(record["id"]), self.now), ("HOT", "WARM"))


if __name__ == "__main__":
    unittest.main()
