import datetime
import tempfile
import unittest
from pathlib import Path

from memory.memory_retriever import MemoryRetriever, tokenize
from memory.memory_store import MemoryStore


def make_store() -> MemoryStore:
    return MemoryStore(Path(tempfile.mkdtemp()) / "memories.json")


class TokenizeTest(unittest.TestCase):
    def test_chinese_and_english(self):
        tokens = tokenize("我在学 Python 爬虫")
        self.assertIn("python", tokens)
        self.assertIn("爬虫", tokens)

    def test_stopwords_removed(self):
        self.assertNotIn("的", tokenize("我的东西"))

    def test_empty(self):
        self.assertEqual(tokenize(""), set())


class ScoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.retriever = MemoryRetriever(self.store, top_k=5)
        self.now = datetime.datetime(2026, 9, 13, 12, 0)

    def test_keyword_drives_relevance(self):
        hit, _ = self.store.create(type="preference", content="用户正在学习Python爬虫")
        self.store.create(type="preference", content="用户喜欢喝美式咖啡")
        results = self.retriever.retrieve("我的Python学得怎么样了", now=self.now)
        self.assertTrue(results)
        self.assertEqual(results[0]["memory"]["id"], hit["id"])
        self.assertGreater(results[0]["parts"]["keyword"], 0)

    def test_importance_affects_order(self):
        low, _ = self.store.create(type="fact", content="用户偶尔打游戏", importance=0.3)
        high, _ = self.store.create(type="fact", content="用户偶尔看电影", importance=0.95)
        results = self.retriever.retrieve("随便聊聊", now=self.now, top_k=2)
        self.assertEqual(results[0]["memory"]["id"], high["id"])

    def test_recency_affects_order(self):
        fresh, _ = self.store.create(type="fact", content="用户最近在跑步")
        old, _ = self.store.create(type="fact", content="用户以前在游泳")
        self.store.update(old["id"], last_used_at="2025-01-01T00:00:00")
        self.store.update(fresh["id"], last_used_at="2026-09-12T00:00:00")
        results = self.retriever.retrieve("跑步游泳", now=self.now, top_k=2)
        scores = {item["memory"]["id"]: item["parts"]["recency"] for item in results}
        self.assertGreater(scores[fresh["id"]], scores[old["id"]])

    def test_frequency_affects_score(self):
        used, _ = self.store.create(type="fact", content="用户想学吉他")
        idle, _ = self.store.create(type="fact", content="用户想学吉他课")
        for _ in range(4):
            self.store.mark_used(used["id"])
        results = self.retriever.retrieve("吉他", now=self.now, top_k=2)
        self.assertEqual(results[0]["memory"]["id"], used["id"])

    def test_emotion_weight_included(self):
        warm, _ = self.store.create(type="shared_event", content="我们一起聊过搬家的事", emotion_weight=0.9)
        plain, _ = self.store.create(type="shared_event", content="我们一起聊过搬家计划", emotion_weight=0.0)
        results = self.retriever.retrieve("搬家", now=self.now, top_k=2)
        self.assertEqual(results[0]["memory"]["id"], warm["id"])
        self.assertGreater(results[0]["parts"]["emotion"], 0)

    def test_top_k_limit(self):
        # 造一批"相关但彼此不重复"的记忆（内容差异足够大，不会被去重合并）
        for index in range(8):
            record, _ = self.store.create(type="fact", content=f"学习相关第{index}件事：{index}号科目")
            self.store.set_temperature(record["id"], "HOT")
        results = self.retriever.retrieve("学习相关", now=self.now)
        self.assertEqual(len(results), 5)

    def test_top_k_configurable_range(self):
        for index in range(12):
            record, _ = self.store.create(type="fact", content=f"学习相关第{index}件事：{index}号科目")
            self.store.set_temperature(record["id"], "HOT")
        retriever = MemoryRetriever(self.store, top_k=8)
        self.assertEqual(len(retriever.retrieve("学习", now=self.now)), 8)
        clamped = MemoryRetriever(self.store, top_k=99)
        self.assertEqual(clamped.top_k, 8)

    def test_custom_weights_are_used(self):
        self.store.create(type="fact", content="用户学习吉他", importance=0.1)
        keyword_heavy = MemoryRetriever(self.store, weights={"keyword": 1.0, "importance": 0.0,
                                                             "recency": 0.0, "frequency": 0.0, "emotion": 0.0})
        importance_heavy = MemoryRetriever(self.store, weights={"keyword": 0.0, "importance": 1.0,
                                                               "recency": 0.0, "frequency": 0.0, "emotion": 0.0})
        first = keyword_heavy.retrieve("吉他", now=self.now)[0]["score"]
        second = importance_heavy.retrieve("吉他", now=self.now)[0]["score"]
        self.assertGreater(first, second)

    def test_archived_never_returned(self):
        record, _ = self.store.create(type="fact", content="用户喜欢钓鱼")
        self.store.archive(record["id"])
        self.assertEqual(self.retriever.retrieve("钓鱼", now=self.now), [])

    def test_superseded_never_returned(self):
        old, _ = self.store.create(type="preference", content="用户喜欢打篮球")
        self.store.create(type="preference", content="用户不喜欢打篮球了")
        results = self.retriever.retrieve("篮球", now=self.now)
        self.assertNotIn(old["id"], [item["memory"]["id"] for item in results])

    def test_cold_requires_keyword_match(self):
        record, _ = self.store.create(type="fact", content="用户喜欢摄影")
        self.store.set_temperature(record["id"], "COLD")
        self.assertEqual(self.retriever.retrieve("完全无关的话题", now=self.now), [])
        self.assertTrue(self.retriever.retrieve("摄影", now=self.now))

    def test_hot_memory_can_come_without_keyword(self):
        record, _ = self.store.create(type="fact", content="用户有一条重要信息", importance=0.95)
        self.store.set_temperature(record["id"], "HOT")
        self.assertTrue(self.retriever.retrieve("毫不相干", now=self.now))

    def test_retrieve_blocks_marks_used(self):
        record, _ = self.store.create(type="fact", content="用户正在学习Python")
        self.assertTrue(self.retriever.retrieve_blocks("Python 学得怎么样", now=self.now))
        self.assertEqual(self.store.get(record["id"])["use_count"], 1)
        self.assertTrue(self.store.get(record["id"])["last_used_at"])

    def test_only_selected_memories_count_as_used(self):
        hit, _ = self.store.create(type="fact", content="用户正在学习Python爬虫")
        miss, _ = self.store.create(type="fact", content="用户喜欢看电影")
        self.retriever.retrieve_blocks("Python 学得怎么样", now=self.now)
        self.assertEqual(self.store.get(hit["id"])["use_count"], 1)
        self.assertEqual(self.store.get(miss["id"])["use_count"], 0)

    def test_min_score_filter(self):
        self.store.create(type="fact", content="用户喜欢下棋")
        self.assertEqual(self.retriever.retrieve("下棋", now=self.now, min_score=0.99), [])


if __name__ == "__main__":
    unittest.main()
