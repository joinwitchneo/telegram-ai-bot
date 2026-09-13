"""Phase 4 集成：Event Bus 解耦、防循环、L2 真注入、状态事件写记忆。"""

import datetime
import re
import tempfile
import unittest
from pathlib import Path

from core.context_manager import ContextManager
from core.conversation import Conversation
from core.event_bus import Event, EventBus
from core.llm_client import LLMResult
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger
from emotion.emotion_engine import EmotionEngine
from memory.memory_store import MemoryStore
from relationship.relationship_engine import RelationshipEngine


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append({"messages": messages, "tier": tier, "category": category})
        return LLMResult(ok=True, content="嗯，我在", model="fake", tier=tier,
                         input_tokens=1000, output_tokens=40, cached_tokens=800,
                         cache_hit=True, request_id="req_p4")


class FakeRenderer:
    def render(self, chat_id, text, **_kwargs):
        return [line for line in str(text).split("\n") if line.strip()]


class WireTest(unittest.TestCase):
    """两个引擎订阅同一批事件、互相之间没有引用。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bus = EventBus()
        self.emotion = EmotionEngine(self.tmp / "emotion_state.json", bus=self.bus)
        self.relationship = RelationshipEngine(self.tmp / "relationship_state.json", bus=self.bus)
        self.events = []
        self.bus.subscribe("EmotionChanged", lambda e: self.events.append(("emotion", e.payload)))
        self.bus.subscribe("RelationshipChanged", lambda e: self.events.append(("relationship", e.payload)))

    def test_both_engines_react_to_same_event(self):
        joy_before = self.emotion.value("joy")
        heat_before = self.relationship.value("interaction_heat")
        self.bus.publish("UserMessageReceived", text="谢谢你，你真厉害")
        self.assertGreater(self.emotion.value("joy"), joy_before)
        self.assertGreater(self.relationship.value("interaction_heat"), heat_before)
        kinds = {item[0] for item in self.events}
        self.assertEqual(kinds, {"emotion", "relationship"})

    def test_no_feedback_loop_on_emotion_changed(self):
        """EmotionChanged 不能再次触发情绪或关系更新。"""
        self.bus.publish("UserMessageReceived", text="在吗")
        emotion_before = self.emotion.emotions()
        relationship_before = self.relationship.dimensions()
        events_before = len(self.events)
        self.bus.publish(Event(name="EmotionChanged", payload={"dimensions": ["joy"], "deltas": {"joy": 0.5}}))
        self.assertEqual(self.emotion.emotions(), emotion_before)
        self.assertEqual(self.relationship.dimensions(), relationship_before)
        # 只收到我们自己手动发的那一条，没有产生任何连锁事件
        self.assertEqual(len(self.events), events_before + 1)
        self.assertEqual(self.events[-1][0], "emotion")

    def test_no_feedback_loop_on_relationship_changed(self):
        self.bus.publish("UserMessageReceived", text="在吗")
        emotion_before = self.emotion.emotions()
        relationship_before = self.relationship.dimensions()
        events_before = len(self.events)
        self.bus.publish(
            Event(name="RelationshipChanged", payload={"dimensions": ["trust"], "deltas": {"trust": 0.3}})
        )
        self.assertEqual(self.emotion.emotions(), emotion_before)
        self.assertEqual(self.relationship.dimensions(), relationship_before)
        self.assertEqual(len(self.events), events_before + 1)
        self.assertEqual(self.events[-1][0], "relationship")

    def test_memory_events_do_not_trigger_state_updates(self):
        """MemoryCreated 不在白名单里，不应该改变状态。"""
        self.bus.publish("UserMessageReceived", text="在吗")
        emotion_before = self.emotion.emotions()
        relationship_before = self.relationship.dimensions()
        self.bus.publish("MemoryCreated", memory_id="mem_1", type="fact")
        self.assertEqual(self.emotion.emotions(), emotion_before)
        self.assertEqual(self.relationship.dimensions(), relationship_before)

    def test_long_absence_only_on_return(self):
        self.bus.publish("UserReturned", gap_hours=24)
        self.assertGreater(self.emotion.value("joy"), self.emotion.baseline["joy"])
        self.assertGreater(
            self.relationship.value("interaction_heat"), self.relationship.baseline["interaction_heat"]
        )


class L2InjectionTest(unittest.TestCase):
    def build(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bus = EventBus()
        self.emotion = EmotionEngine(self.tmp / "emotion_state.json", bus=self.bus)
        self.relationship = RelationshipEngine(self.tmp / "relationship_state.json", bus=self.bus)
        self.bus.publish("UserMessageReceived", text="谢谢你陪我说这些，真的挺开心的")

        def state_provider():
            return {
                "emotion": self.emotion.describe(),
                "relationship": self.relationship.describe(),
                "behavior": "；".join(self.emotion.behavior_hints() + self.relationship.behavior_hints())[:160],
            }

        manager = ContextManager(
            contract="【系统合同】\n直接输出消息。",
            persona="【人格】\n嘴硬心软。",
            token_budget=TokenBudget(max_context_tokens=4000, daily_token_budget=100000),
            max_context_tokens=4000,
        )
        return Conversation(
            llm_client=FakeClient(),
            renderer=FakeRenderer(),
            context_manager=manager,
            token_budget=TokenBudget(max_context_tokens=4000, daily_token_budget=100000),
            usage_logger=UsageLogger(self.tmp / "usage.json"),
            event_bus=self.bus,
            state_provider=state_provider,
        )

    def test_l2_reaches_final_messages(self):
        conversation = self.build()
        result = conversation.process(1, "你今天帮我解决那个问题，真的挺厉害的")
        dynamic = conversation.llm.calls[-1]["messages"][1]["content"]
        self.assertIn("当前状态", dynamic)
        self.assertIn("关系", dynamic)
        self.assertGreater(result["context"]["tokens"]["L2"], 0)

    def test_l2_has_no_raw_numbers(self):
        conversation = self.build()
        conversation.process(1, "在吗")
        dynamic = conversation.llm.calls[-1]["messages"][1]["content"]
        l2_part = dynamic.split("【当前模式】")[0]
        self.assertFalse(re.search(r"\d\.\d", l2_part), l2_part)
        for name in ("joy", "anger", "intimacy", "trust"):
            self.assertNotIn(name, l2_part)

    def test_l2_stays_small(self):
        conversation = self.build()
        result = conversation.process(1, "在吗")
        self.assertLess(result["context"]["tokens"]["L2"], 200)   # 几十 token 级别

    def test_behavior_line_present(self):
        conversation = self.build()
        conversation.process(1, "谢谢你，你真厉害")
        dynamic = conversation.llm.calls[-1]["messages"][1]["content"]
        self.assertTrue("行为影响" in dynamic or "当前状态" in dynamic)


class StateEventMemoryTest(unittest.TestCase):
    """只有幅度够大、且过了冷却的状态变化才写 Memory（防 Memory 爆炸）。"""

    class StubBot:
        def __init__(self, store, bus, min_delta=0.15, cooldown=3600):
            self.memory_store = store
            self.bus = bus
            self.emotion = EmotionEngine(Path(tempfile.mkdtemp()) / "e.json")
            self.relationship = RelationshipEngine(Path(tempfile.mkdtemp()) / "r.json")
            self._event_memory_at = 0.0
            self._event_memory_min_delta = min_delta
            self._event_memory_cooldown = cooldown
            self.logging = None

    def setUp(self) -> None:
        from bot import Bot

        self.handler = Bot._on_state_changed
        self.tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(self.tmp / "memories.json")
        self.bus = EventBus()
        self.stub = self.StubBot(self.store, self.bus)

    def test_significant_relationship_change_writes_memory(self):
        event = Event(name="RelationshipChanged", payload={"deltas": {"trust": 0.4}})
        self.handler(self.stub, event)
        self.assertEqual(len(self.store.list(type="relationship_event")), 1)

    def test_small_change_ignored(self):
        event = Event(name="EmotionChanged", payload={"deltas": {"joy": 0.02}})
        self.handler(self.stub, event)
        self.assertEqual(len(self.store.list()), 0)

    def test_cooldown_prevents_spam(self):
        for _ in range(5):
            self.handler(self.stub, Event(name="EmotionChanged", payload={"deltas": {"joy": 0.5}}))
        self.assertLessEqual(len(self.store.list(type="emotion_event")), 1)

    def test_writes_emotion_event_type(self):
        self.handler(self.stub, Event(name="EmotionChanged", payload={"deltas": {"anger": 0.3}}))
        items = self.store.list(type="emotion_event")
        self.assertEqual(len(items), 1)
        self.assertIn("情绪变化", items[0]["content"])


if __name__ == "__main__":
    unittest.main()
