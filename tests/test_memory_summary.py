import json
import tempfile
import unittest
from pathlib import Path

from core.event_bus import EventBus
from core.llm_client import LLMResult
from core.token_budget import TokenBudget
from memory.memory_store import MemoryStore
from memory.memory_summary import MemoryExtractor, MemorySummarizer
from memory.topic_memory import TopicMemory


class FakeClient:
    def __init__(self, payload: str, ok: bool = True):
        self.payload = payload
        self.ok = ok
        self.calls = []

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append({"tier": tier, "category": category})
        if not self.ok:
            return LLMResult(ok=False, error="boom", tier=tier)
        return LLMResult(ok=True, content=self.payload, model="cheap-model", tier=tier,
                         input_tokens=500, output_tokens=100)


def make_env():
    tmp = Path(tempfile.mkdtemp())
    store = MemoryStore(tmp / "memories.json")
    topics = TopicMemory(tmp / "topics.json", event_bus=EventBus())
    return store, topics


class ExtractorRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.topics = make_env()
        self.extractor = MemoryExtractor(self.store, topics=self.topics)

    def test_preference_rule(self):
        items = self.extractor.rule_extract("我特别喜欢喝美式咖啡")
        self.assertTrue(items)
        self.assertEqual(items[0]["type"], "preference")
        self.assertIn("美式", items[0]["content"])

    def test_negative_preference(self):
        items = self.extractor.rule_extract("我不喜欢太吵的地方")
        self.assertEqual(items[0]["type"], "preference")
        self.assertIn("不喜欢", items[0]["content"])

    def test_habit_rule(self):
        items = self.extractor.rule_extract("我最近天天熬夜到两点")
        self.assertEqual(items[0]["type"], "habit")

    def test_agreement_is_protected(self):
        items = self.extractor.rule_extract("记住我下周三要体检")
        self.assertEqual(items[0]["type"], "agreement")
        self.assertTrue(items[0]["protected"])

    def test_major_event_is_protected(self):
        items = self.extractor.rule_extract("我明天要面试")
        self.assertTrue(items[0]["protected"])
        self.assertGreaterEqual(items[0]["importance"], 0.8)

    def test_chitchat_extracts_nothing(self):
        for text in ("哈哈哈哈", "嗯嗯", "今天天气不错啊", "在吗"):
            self.assertEqual(self.extractor.rule_extract(text), [])

    def test_process_writes_memory_and_topic(self):
        written = self.extractor.process("我最近在学Python")
        self.assertTrue(written)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.topics.stats()["total"], 1)

    def test_rule_takes_priority_over_model(self):
        client = FakeClient("[]")
        extractor = MemoryExtractor(self.store, topics=self.topics, client=client)
        extractor.process("我喜欢弹吉他")
        self.assertEqual(client.calls, [])      # 规则命中就不该调用模型

    def test_model_path_only_for_substantive_text(self):
        client = FakeClient(json.dumps([{"type": "fact", "content": "用户在准备考研", "importance": 0.7}]))
        extractor = MemoryExtractor(self.store, topics=self.topics, client=client)
        extractor.process("嗯")
        self.assertEqual(client.calls, [])
        # 这条不命中任何规则（没有"我最近/我喜欢"这类固定搭配），才会走模型路径
        extractor.process("关于以后的方向，我有些说不清楚的感觉，也一直没跟别人讲过这件事")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["category"], "extraction")

    def test_model_uses_cheap_tier(self):
        client = FakeClient(json.dumps([{"type": "fact", "content": "用户在准备考试", "importance": 0.7}]))
        extractor = MemoryExtractor(self.store, topics=self.topics, client=client, budget=TokenBudget())
        extractor.process("关于以后的发展方向，我心里一直有个想法，但还没跟任何人说过")
        self.assertEqual(client.calls[0]["tier"], "cheap")

    def test_bad_model_output_is_ignored(self):
        client = FakeClient("这不是 JSON")
        extractor = MemoryExtractor(self.store, topics=self.topics, client=client, budget=TokenBudget())
        extractor.process("关于以后的方向，我有些说不清楚的感觉，也一直没跟别人讲过这件事")
        self.assertEqual(len(self.store.list()), 0)


class SummarizerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.topics = make_env()
        self.events = []
        self.bus = EventBus()
        self.bus.subscribe("MemoryCreated", lambda e: self.events.append(e.payload))

    def payload(self):
        return json.dumps(
            {
                "summary": "聊了学习和作息",
                "new_facts": ["用户是大学生"],
                "preferences": ["用户喜欢安静"],
                "shared_events": ["用户下周有考试"],
                "topics": ["学习", "作息"],
                "relationship_changes": [],
                "emotion_events": ["用户最近压力大"],
            },
            ensure_ascii=False,
        )

    def test_should_run_thresholds(self):
        summarizer = MemorySummarizer(self.store, min_messages=30, token_threshold=1500)
        self.assertFalse(summarizer.should_run(message_count=10, token_count=100))
        self.assertTrue(summarizer.should_run(message_count=30, token_count=100))
        self.assertTrue(summarizer.should_run(message_count=12, token_count=2000))

    def test_run_writes_memories_and_topics(self):
        client = FakeClient(self.payload())
        summarizer = MemorySummarizer(self.store, topics=self.topics, client=client, bus=self.bus)
        history = [{"role": "user", "content": "我最近压力挺大的"}, {"role": "assistant", "content": "怎么了"}]
        report = summarizer.run(history)
        self.assertTrue(report["ran"])
        contents = [item["content"] for item in self.store.list()]
        self.assertIn("用户是大学生", contents)
        self.assertIn("用户喜欢安静", contents)
        self.assertEqual(summarizer.calls, 1)
        self.assertGreaterEqual(self.topics.stats()["total"], 2)
        self.assertTrue(self.events)

    def test_summary_uses_cheap_model(self):
        client = FakeClient(self.payload())
        summarizer = MemorySummarizer(self.store, topics=self.topics, client=client, budget=TokenBudget())
        summarizer.run([{"role": "user", "content": "聊点事"}])
        self.assertEqual(client.calls[0]["tier"], "cheap")
        self.assertEqual(client.calls[0]["category"], "summary")

    def test_relationship_event_protected(self):
        payload = json.dumps({"relationship_changes": ["用户第一次跟我吵架"], "summary": "", "new_facts": [],
                              "preferences": [], "shared_events": [], "topics": [], "emotion_events": []},
                             ensure_ascii=False)
        client = FakeClient(payload)
        summarizer = MemorySummarizer(self.store, topics=self.topics, client=client)
        summarizer.run([{"role": "user", "content": "x"}])
        item = self.store.list(type="relationship_event")[0]
        self.assertTrue(item["protected"])

    def test_malformed_json_is_safe(self):
        client = FakeClient("...不是 JSON...")
        summarizer = MemorySummarizer(self.store, topics=self.topics, client=client)
        report = summarizer.run([{"role": "user", "content": "x"}])
        self.assertTrue(report["ran"])
        self.assertEqual(report["data"]["summary"], "")
        self.assertEqual(len(self.store.list()), 0)

    def test_llm_failure_is_reported(self):
        client = FakeClient("", ok=False)
        summarizer = MemorySummarizer(self.store, topics=self.topics, client=client)
        report = summarizer.run([{"role": "user", "content": "x"}])
        self.assertFalse(report["ran"])

    def test_empty_history_skipped(self):
        client = FakeClient(self.payload())
        summarizer = MemorySummarizer(self.store, topics=self.topics, client=client)
        self.assertFalse(summarizer.run([])["ran"])
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
