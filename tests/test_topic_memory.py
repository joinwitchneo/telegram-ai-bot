import datetime
import json
import tempfile
import unittest
from pathlib import Path

from core.event_bus import EventBus
from memory.topic_memory import TopicMemory


class TopicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bus = EventBus()
        self.topics = TopicMemory(self.tmp / "topics.json", event_bus=self.bus)
        self.now = datetime.datetime(2026, 9, 13, 12, 0)
        self.events = []
        self.bus.subscribe("TopicUpdated", lambda e: self.events.append(e.payload))

    def test_new_topic(self):
        record, action = self.topics.upsert("Python学习", now=self.now)
        self.assertEqual(action, "created")
        self.assertEqual(record["status"], "NEW")
        self.assertTrue(record["id"].startswith("topic_"))
        self.assertTrue(record["keywords"])

    def test_repeat_mention_becomes_active_then_deep(self):
        record, _ = self.topics.upsert("Python学习", now=self.now)
        self.topics.upsert("Python学习", now=self.now)
        self.assertEqual(self.topics.get(record["id"])["status"], "ACTIVE")
        for _ in range(4):
            self.topics.upsert("Python学习", now=self.now)
        self.assertEqual(self.topics.get(record["id"])["status"], "DEEP")

    def test_cooldown_then_dormant(self):
        record, _ = self.topics.upsert("Python学习", now=self.now)
        self.topics.set_status(record["id"], "ACTIVE")
        later = self.now + datetime.timedelta(days=5)
        self.topics.decay(now=later)
        self.assertEqual(self.topics.get(record["id"])["status"], "COOLDOWN")
        much_later = self.now + datetime.timedelta(days=20)
        self.topics.decay(now=much_later)
        self.assertEqual(self.topics.get(record["id"])["status"], "DORMANT")

    def test_dormant_topic_revives(self):
        record, _ = self.topics.upsert("Python学习", now=self.now)
        self.topics.set_status(record["id"], "DORMANT")
        revived, action = self.topics.upsert("Python学习", now=self.now + datetime.timedelta(days=30))
        self.assertEqual(action, "revived")
        self.assertEqual(revived["status"], "ACTIVE")
        self.assertEqual(revived["id"], record["id"])   # 同一个话题对象

    def test_find_by_keyword(self):
        self.topics.upsert("Python学习", now=self.now)
        found = self.topics.find("最近Python学到哪了")
        self.assertIsNotNone(found)
        self.assertEqual(found["topic"], "Python学习")
        self.assertIsNone(self.topics.find("完全不相关的一句话"))

    def test_memory_link(self):
        record, _ = self.topics.upsert("搬家", memory_id="mem_000001", now=self.now)
        self.assertIn("mem_000001", record["source_memory_ids"])
        self.topics.upsert("搬家", memory_id="mem_000002", now=self.now)
        self.assertEqual(len(self.topics.get(record["id"])["source_memory_ids"]), 2)

    def test_status_validation(self):
        record, _ = self.topics.upsert("考试", now=self.now)
        with self.assertRaises(ValueError):
            self.topics.set_status(record["id"], "不存在的状态")

    def test_empty_topic_rejected(self):
        with self.assertRaises(ValueError):
            self.topics.upsert("   ")

    def test_event_published(self):
        self.topics.upsert("考试", now=self.now)
        self.assertTrue(any(item.get("action") == "created" for item in self.events))
        self.topics.upsert("考试", now=self.now)
        self.assertTrue(any(item.get("action") == "updated" for item in self.events))

    def test_stats_and_list(self):
        self.topics.upsert("Python学习", now=self.now)
        self.topics.upsert("考试", now=self.now)
        self.assertEqual(self.topics.stats()["total"], 2)
        self.assertEqual(len(self.topics.list(status="NEW")), 2)

    def test_persistence(self):
        record, _ = self.topics.upsert("Python学习", now=self.now)
        reloaded = TopicMemory(self.tmp / "topics.json")
        self.assertIsNotNone(reloaded.get(record["id"]))
        data = json.loads((self.tmp / "topics.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)


if __name__ == "__main__":
    unittest.main()
