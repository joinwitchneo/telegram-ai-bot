import datetime
import json
import tempfile
import unittest
from pathlib import Path

from memory.memory_store import MemoryStore, looks_like_change, similarity


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(self.tmp / "memories.json")

    # ── CRUD ───────────────────────────────────────────────────────
    def test_create_and_get(self):
        record, action = self.store.create(type="preference", content="用户喜欢喝美式")
        self.assertEqual(action, "created")
        self.assertTrue(record["id"].startswith("mem_"))
        self.assertEqual(self.store.get(record["id"])["content"], "用户喜欢喝美式")

    def test_ids_are_stable_and_unique(self):
        first, _ = self.store.create(type="fact", content="用户是学生")
        second, _ = self.store.create(type="fact", content="用户住在这里")
        self.assertNotEqual(first["id"], second["id"])
        self.store.update(first["id"], content="用户是大学生")
        self.assertEqual(self.store.get(first["id"])["content"], "用户是大学生")
        self.assertEqual(len(self.store.list()), 2)

    def test_update(self):
        record, _ = self.store.create(type="fact", content="用户喜欢跑步")
        updated = self.store.update(record["id"], importance=0.9)
        self.assertEqual(updated["importance"], 0.9)
        self.assertIsNone(self.store.update("mem_999999", importance=0.1))

    def test_delete_is_soft(self):
        record, _ = self.store.create(type="fact", content="用户养了一只猫")
        self.assertTrue(self.store.delete(record["id"]))
        self.assertNotIn(record["id"], [item["id"] for item in self.store.list()])
        self.assertIsNotNone(self.store.get(record["id"]))          # 记录仍在
        self.assertTrue(self.store.get(record["id"])["deleted"])

    def test_purge_removes(self):
        record, _ = self.store.create(type="fact", content="临时信息")
        self.assertTrue(self.store.purge(record["id"]))
        self.assertIsNone(self.store.get(record["id"]))

    def test_mark_used(self):
        record, _ = self.store.create(type="fact", content="用户喜欢篮球")
        self.store.mark_used(record["id"])
        self.store.mark_used(record["id"])
        item = self.store.get(record["id"])
        self.assertEqual(item["use_count"], 2)
        self.assertTrue(item["last_used_at"])

    def test_protect_and_archive(self):
        record, _ = self.store.create(type="fact", content="用户生日在三月")
        self.store.protect(record["id"])
        self.assertTrue(self.store.get(record["id"])["protected"])
        self.store.archive(record["id"])
        self.assertEqual(self.store.get(record["id"])["temperature"], "ARCHIVED")

    def test_list_filters(self):
        self.store.create(type="preference", content="用户喜欢猫")
        kept, _ = self.store.create(type="fact", content="用户是设计师")
        self.store.archive(kept["id"])
        self.assertEqual(len(self.store.list(temperature="WARM")), 1)
        self.assertEqual(len(self.store.list(include_inactive=True)), 2)

    def test_empty_content_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create(type="fact", content="   ")

    def test_unknown_type_falls_back(self):
        record, _ = self.store.create(type="nonsense", content="随便一条")
        self.assertEqual(record["type"], "fact")

    def test_persistence(self):
        record, _ = self.store.create(type="fact", content="用户喜欢深夜写代码")
        reloaded = MemoryStore(self.tmp / "memories.json")
        self.assertIsNotNone(reloaded.get(record["id"]))
        data = json.loads((self.tmp / "memories.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)

    def test_stats(self):
        first, _ = self.store.create(type="fact", content="用户喜欢咖啡")
        self.store.create(type="preference", content="用户喜欢清静")
        self.store.protect(first["id"])
        stats = self.store.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["protected"], 1)


class DedupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(self.tmp / "memories.json")

    def test_similar_content_updates_instead_of_creating(self):
        first, action1 = self.store.create(type="preference", content="用户正在学习Python")
        second, action2 = self.store.create(type="preference", content="用户正在学习Python语言")
        self.assertEqual(action1, "created")
        self.assertEqual(action2, "updated")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_duplicate_does_not_inflate_count(self):
        for text in ("用户正在学习Python", "用户最近在学习Python", "用户正在学习Python，"):
            self.store.create(type="preference", content=text)
        self.assertEqual(len(self.store.list()), 1)

    def test_different_facts_create_new(self):
        self.store.create(type="preference", content="用户喜欢喝美式咖啡")
        self.store.create(type="preference", content="用户喜欢打游戏")
        self.assertEqual(len(self.store.list()), 2)

    def test_similarity_helper(self):
        self.assertGreater(similarity("用户喜欢喝美式", "用户喜欢喝美式咖啡"), 0.5)
        self.assertLess(similarity("用户喜欢喝美式", "今天天气不错"), 0.2)


class ConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(self.tmp / "memories.json")

    def test_change_marker_supersedes_old(self):
        old, _ = self.store.create(type="habit", content="用户喜欢晚上学习")
        new, action = self.store.create(type="habit", content="用户现在改成早上学习了")
        self.assertEqual(action, "superseded")
        stored_old = self.store.get(old["id"])
        self.assertEqual(stored_old["superseded_by"], new["id"])
        active = [item["content"] for item in self.store.list()]
        self.assertIn("用户现在改成早上学习了", active)
        self.assertNotIn("用户喜欢晚上学习", active)

    def test_negation_supersedes_old(self):
        old, _ = self.store.create(type="preference", content="用户喜欢喝咖啡")
        new, action = self.store.create(type="preference", content="用户不喜欢喝咖啡")
        self.assertEqual(action, "superseded")
        self.assertEqual(self.store.get(old["id"])["superseded_by"], new["id"])

    def test_old_record_is_kept_not_deleted(self):
        old, _ = self.store.create(type="preference", content="用户喜欢早起")
        self.store.create(type="preference", content="用户不喜欢早起了")
        self.assertIsNotNone(self.store.get(old["id"]))

    def test_change_helper(self):
        self.assertTrue(looks_like_change("用户现在改成早上学习了", "用户喜欢晚上学习"))
        self.assertFalse(looks_like_change("用户喜欢喝美式", "用户喜欢喝美式咖啡"))

    def test_superseded_memory_excluded_from_list(self):
        old, _ = self.store.create(type="preference", content="用户喜欢苹果")
        self.store.create(type="preference", content="用户不喜欢苹果了")
        ids = [item["id"] for item in self.store.list()]
        self.assertNotIn(old["id"], ids)
        self.assertEqual(len(self.store.list(include_inactive=True)), 2)


if __name__ == "__main__":
    unittest.main()
