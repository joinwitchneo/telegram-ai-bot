"""Phase 3 集成：记忆召回 → L3 → Context，以及 Debugger 的记忆字段。"""

import tempfile
import unittest
from pathlib import Path

from core.context_debugger import ContextDebugger
from core.context_manager import ContextManager
from core.conversation import Conversation
from core.event_bus import EventBus
from core.llm_client import LLMResult
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger
from memory.memory_decay import MemoryDecay
from memory.memory_retriever import MemoryRetriever
from memory.memory_store import MemoryStore
from memory.memory_summary import MemoryExtractor
from memory.topic_memory import TopicMemory


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append({"messages": messages, "tier": tier, "category": category})
        return LLMResult(ok=True, content="嗯，你之前说过这个", model="fake", tier=tier,
                         input_tokens=900, output_tokens=60, cached_tokens=700,
                         cache_hit=True, request_id="req_mem_1")


class FakeRenderer:
    def render(self, chat_id, text, **_kwargs):
        return [line for line in str(text).split("\n") if line.strip()]


class MemoryPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bus = EventBus()
        self.store = MemoryStore(self.tmp / "memories.json")
        self.topics = TopicMemory(self.tmp / "topics.json", event_bus=self.bus)
        self.retriever = MemoryRetriever(self.store, top_k=5)
        self.decay = MemoryDecay(self.store)
        self.events = []
        self.bus.subscribe("MemoryCreated", lambda e: self.events.append(("created", e.payload)))
        self.bus.subscribe("MemoryRecalled", lambda e: self.events.append(("recalled", e.payload)))
        self.bus.subscribe("TopicUpdated", lambda e: self.events.append(("topic", e.payload)))

    def build_conversation(self, debugger=None):
        manager = ContextManager(
            contract="【系统合同】\n直接输出消息。",
            persona="【人格】\n嘴硬心软。",
            token_budget=TokenBudget(max_context_tokens=4000, daily_token_budget=100000),
            max_context_tokens=4000,
        )
        extractor = MemoryExtractor(self.store, topics=self.topics, client=None, bus=self.bus)
        conversation = Conversation(
            llm_client=FakeClient(),
            renderer=FakeRenderer(),
            context_manager=manager,
            token_budget=TokenBudget(max_context_tokens=4000, daily_token_budget=100000),
            usage_logger=UsageLogger(self.tmp / "usage.json"),
            event_bus=self.bus,
            debugger=debugger,
            memory_provider=lambda text, history: self.retriever.retrieve_blocks(text, mark_used=True),
            after_reply=lambda chat_id, text, history: extractor.process(text),
        )
        return conversation

    def test_scenario_python_then_recall(self):
        """规范里的真实验证场景：学 Python → 学爬虫 → 问"我在学什么"。"""
        conversation = self.build_conversation()

        conversation.process(1, "我最近在学Python")            # 规则建记忆
        self.assertEqual(len(self.store.list()), 1)
        self.assertTrue(any(item[0] == "created" for item in self.events))

        conversation.process(1, "我已经开始学爬虫了")           # 同话题，更新或新建
        self.assertGreaterEqual(len(self.store.list()), 1)
        self.assertGreaterEqual(self.topics.stats()["total"], 1)

        result = conversation.process(1, "你还记得我在学什么吗")
        inject = [m for m in conversation.llm.calls[-1]["messages"] if m["role"] == "system"][-1]["content"]
        self.assertIn("相关记忆", inject)
        self.assertIn("Python", inject)
        self.assertTrue(any(item[0] == "recalled" for item in self.events))
        self.assertTrue(result["context"]["tokens"]["L3"] > 0)

    def test_unrelated_message_does_not_recall(self):
        conversation = self.build_conversation()
        conversation.process(1, "我最近在学Python")
        conversation.llm.calls.clear()
        conversation.process(1, "今天中午吃了拉面")
        inject = [m for m in conversation.llm.calls[-1]["messages"] if m["role"] == "system"][-1]["content"]
        self.assertNotIn("Python", inject)

    def test_debugger_records_memory_fields(self):
        debugger = ContextDebugger(debug_dir=self.tmp / "debug", enabled=True)
        conversation = self.build_conversation(debugger=debugger)
        conversation.process(1, "我最近在学Python")
        conversation.process(1, "Python 学得怎么样了")
        record = debugger.last()
        self.assertIn("retrieved_memory_ids", record)
        self.assertIn("selected_memory_ids", record)
        self.assertIn("retrieved_memory_scores", record)
        self.assertTrue(record["retrieved_memory_ids"])
        self.assertTrue(record["selected_memory_ids"])

    def test_memory_not_recalled_is_not_counted_as_used(self):
        conversation = self.build_conversation()
        conversation.process(1, "我最近在学Python")
        conversation.process(1, "我最近在学Python爬虫")   # 触发召回
        unrelated, _ = self.store.create(type="fact", content="用户喜欢打羽毛球")
        unused = [item for item in self.store.list() if int(item.get("use_count", 0)) == 0]
        self.assertTrue(unused, "没被注入 L3 的记忆不应该计 use_count")
        self.assertEqual(self.store.get(unrelated["id"])["use_count"], 0)

    def test_topic_created_and_tracked(self):
        conversation = self.build_conversation()
        conversation.process(1, "我最近在学Python")
        conversation.process(1, "我最近还是在学Python")
        topic = self.topics.list()[0]
        self.assertIn(topic["status"], ("NEW", "ACTIVE", "DEEP"))
        self.assertTrue(any(item[0] == "topic" for item in self.events))

    def test_many_memories_do_not_break_context(self):
        for index in range(300):
            self.store.create(type="fact", content=f"用户的第{index}条测试信息，关于学习与生活")
        conversation = self.build_conversation()
        result = conversation.process(1, "学习的事情")
        self.assertTrue(result["ok"])
        self.assertLessEqual(len(self.store.list()), 300)
        self.assertLess(result["context"]["tokens"]["L3"], 2000)


if __name__ == "__main__":
    unittest.main()
