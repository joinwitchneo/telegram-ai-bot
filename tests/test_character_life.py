import datetime
import tempfile
import unittest
from pathlib import Path

from proactive.character_life import CharacterLife


def make_life(**kwargs) -> CharacterLife:
    tmp = Path(tempfile.mkdtemp())
    return CharacterLife(tmp / "character_life.json", **kwargs)


class BasicTest(unittest.TestCase):
    def test_add_and_get(self):
        life = make_life()
        record = life.add(
            "unfinished_thought",
            "还想知道用户最后有没有决定继续学 Python",
            source="INTERNAL",
            importance=0.7,
        )
        self.assertTrue(record["id"].startswith("life_"))
        self.assertEqual(record["status"], "ACTIVE")
        self.assertEqual(life.get(record["id"])["content"], record["content"])

    def test_ids_increase(self):
        life = make_life()
        first = life.add("interest", "一")
        second = life.add("interest", "二")
        self.assertLess(first["id"], second["id"])

    def test_empty_content_rejected(self):
        life = make_life()
        with self.assertRaises(ValueError):
            life.add("interest", "   ")

    def test_unknown_type_falls_back(self):
        life = make_life()
        record = life.add("whatever", "内容")
        self.assertEqual(record["type"], "unfinished_thought")

    def test_persistence(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "life.json"
        life = CharacterLife(path)
        life.add("interest", "她最近对爬虫有点兴趣")
        again = CharacterLife(path)
        self.assertEqual(len(again.all()), 1)


class SourceTest(unittest.TestCase):
    def test_default_confidences(self):
        life = make_life()
        self.assertEqual(life.add("setting", "设定", source="CONFIRMED")["confidence"], 1.0)
        self.assertEqual(life.add("interest", "念头", source="INTERNAL")["confidence"], 0.8)
        self.assertEqual(life.add("setting", "背景", source="FICTIONAL")["confidence"], 0.6)

    def test_custom_confidence(self):
        life = make_life()
        self.assertEqual(life.add("interest", "念头", confidence=0.42)["confidence"], 0.42)

    def test_fictional_cannot_invent_real_experience(self):
        life = make_life()
        with self.assertRaises(ValueError):
            life.add("setting", "我今天下午去咖啡店买了杯咖啡", source="FICTIONAL")

    def test_fictional_allows_background_setting(self):
        life = make_life()
        record = life.add("setting", "她最近在学一点吉他", source="FICTIONAL")
        self.assertEqual(record["source"], "FICTIONAL")

    def test_internal_may_describe_thoughts(self):
        life = make_life()
        record = life.add("unfinished_thought", "她有点想知道用户考试的结果", source="INTERNAL")
        self.assertEqual(record["status"], "ACTIVE")


class LifecycleTest(unittest.TestCase):
    def test_complete_and_archive(self):
        life = make_life()
        record = life.add("unfinished_topic", "Python 学不学")
        life.complete(record["id"])
        self.assertEqual(life.get(record["id"])["status"], "COMPLETED")
        self.assertEqual(life.active(), [])
        life.archive(record["id"])
        self.assertEqual(life.get(record["id"])["status"], "ARCHIVED")

    def test_set_status_validates(self):
        life = make_life()
        record = life.add("interest", "一")
        with self.assertRaises(ValueError):
            life.set_status(record["id"], "NOPE")

    def test_expire_by_ttl(self):
        base = datetime.datetime(2026, 9, 13, 12, 0, 0)
        life = make_life(now_fn=lambda: base)
        record = life.add("unfinished_thought", "想想", ttl_hours=2)
        self.assertEqual(life.expire_due(now=base + datetime.timedelta(hours=1)), 0)
        self.assertEqual(life.expire_due(now=base + datetime.timedelta(hours=3)), 1)
        self.assertEqual(life.get(record["id"])["status"], "EXPIRED")

    def test_expire_by_explicit_time(self):
        life = make_life()
        record = life.add("important_event", "用户明天考试", expires_at="2026-09-14T12:00:00")
        life.expire_due(now=datetime.datetime(2026, 9, 15, 0, 0, 0))
        self.assertEqual(life.get(record["id"])["status"], "EXPIRED")

    def test_active_expires_automatically(self):
        base = datetime.datetime(2026, 9, 13, 12, 0, 0)
        life = make_life(now_fn=lambda: base + datetime.timedelta(hours=5))
        life.add("unfinished_thought", "想想", ttl_hours=1, now=base)
        self.assertEqual(life.active(), [])

    def test_update_fields(self):
        life = make_life()
        record = life.add("interest", "旧的")
        life.update(record["id"], content="新的", importance=0.9)
        updated = life.get(record["id"])
        self.assertEqual(updated["content"], "新的")
        self.assertEqual(updated["importance"], 0.9)

    def test_stats(self):
        life = make_life()
        life.add("interest", "一", source="INTERNAL")
        record = life.add("setting", "二", source="FICTIONAL")
        life.complete(record["id"])
        stats = life.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_status"]["ACTIVE"], 1)
        self.assertEqual(stats["by_source"]["FICTIONAL"], 1)

    def test_trim_keeps_important(self):
        life = make_life(max_entries=3)
        life.add("interest", " unimportant 1", importance=0.1)
        life.add("interest", " unimportant 2", importance=0.1)
        life.add("important_event", "重要的", importance=0.9)
        life.add("interest", " unimportant 3", importance=0.1)
        contents = [item["content"] for item in life.all()]
        self.assertLessEqual(len(contents), 3)
        self.assertIn("重要的", contents)

    def test_reset(self):
        life = make_life()
        life.add("interest", "一")
        life.reset()
        self.assertEqual(life.all(), [])


if __name__ == "__main__":
    unittest.main()
