import tempfile
import unittest
from pathlib import Path

from core.conversation import Conversation
from core.event_bus import EventBus
from core.llm_client import LLMResult
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger


class FakeClient:
    """假 LLM 客户端：记录调用并按需返回。"""

    def __init__(self, content="在的。", ok=True):
        self.calls = []
        self.content = content
        self.ok = ok

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append({"tier": tier, "category": category, "messages": messages})
        if not self.ok:
            return LLMResult(ok=False, error="boom", tier=tier)
        return LLMResult(ok=True, content=self.content, model="fake", tier=tier,
                         input_tokens=100, output_tokens=20, cached_tokens=50, cache_hit=True)


class FakeRenderer:
    def __init__(self):
        self.sent = []

    def render(self, chat_id, text, **_kwargs):
        messages = [line for line in str(text).split("\n") if line.strip()]
        self.sent.append((chat_id, messages))
        return messages


class ConversationTest(unittest.TestCase):
    def build(self, client=None, renderer=None):
        self.tmp = Path(tempfile.mkdtemp())
        usage = UsageLogger(self.tmp / "usage.json")
        self.bus = EventBus()
        timers = []

        def timer_factory(delay, fn, args):
            class FakeTimer:
                daemon = False

                def start(self):
                    timers.append((delay, fn, args))

                def cancel(self):
                    pass

            return FakeTimer()

        conversation = Conversation(
            llm_client=client or FakeClient(),
            renderer=renderer or FakeRenderer(),
            token_budget=TokenBudget(daily_token_budget=100000),
            usage_logger=usage,
            event_bus=self.bus,
            static_prefix="【系统合同】\n测试前缀",
            history_limit=4,
            debounce_single=1.0,
            debounce_multi=2.0,
            timer_factory=timer_factory,
        )
        return conversation, timers, usage

    def test_single_message_uses_short_window(self):
        conversation, timers, _ = self.build()
        conversation.handle_text(1, "在吗")
        self.assertEqual(len(timers), 1)
        self.assertEqual(timers[0][0], 1.0)

    def test_burst_extends_window_and_merges(self):
        conversation, timers, _ = self.build()
        conversation.handle_text(1, "我今天")
        conversation.handle_text(1, "真的")
        conversation.handle_text(1, "好累")
        self.assertEqual(timers[-1][0], 2.0)
        self.assertEqual(conversation.pending_count(1), 3)
        result = conversation.flush(1)
        self.assertTrue(result["processed"])
        sent_messages = conversation.llm.calls[0]["messages"]
        self.assertIn("我今天\n真的\n好累", [m["content"] for m in sent_messages])

    def test_filler_routes_to_cheap_model(self):
        client = FakeClient()
        conversation, _, _ = self.build(client=client)
        result = conversation.process(1, "嗯")
        self.assertTrue(result["ok"])
        self.assertEqual(client.calls[0]["tier"], "cheap")

    def test_chat_routes_to_main_model(self):
        client = FakeClient()
        conversation, _, _ = self.build(client=client)
        conversation.process(1, "我今天在公司忙了一整天，刚到家")
        self.assertEqual(client.calls[0]["tier"], "main")

    def test_budget_hard_skips_llm_entirely(self):
        client = FakeClient()
        conversation, _, usage = self.build(client=client)
        conversation.budget.record(100000)
        result = conversation.process(1, "你好")
        self.assertFalse(result["use_llm"])
        self.assertEqual(client.calls, [])
        self.assertEqual(usage.day()["by_category"]["rule"]["requests"], 1)

    def test_static_prefix_is_stable_across_messages(self):
        client = FakeClient()
        conversation, _, _ = self.build(client=client)
        conversation.process(1, "第一句话")
        conversation.process(1, "第二句话")
        first = client.calls[0]["messages"][0]["content"].split("【当前时间】")[0]
        second = client.calls[1]["messages"][0]["content"].split("【当前时间】")[0]
        self.assertEqual(first, second)
        self.assertIn("测试前缀", first)

    def test_history_is_appended_and_trimmed(self):
        conversation, _, _ = self.build()
        for index in range(6):
            conversation.process(1, f"第{index}句")
        history = conversation.history(1)
        self.assertLessEqual(len(history), 8)   # 4 条上限 × 用户+助手
        self.assertTrue(any(item["content"] == "第5句" for item in history if item["role"] == "user"))

    def test_llm_failure_is_reported(self):
        client = FakeClient(ok=False)
        conversation, _, _ = self.build(client=client)
        result = conversation.process(1, "你在吗？")
        self.assertTrue(result["processed"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")

    def test_events_published(self):
        conversation, _, _ = self.build()
        seen = []
        conversation.bus.subscribe("UserMessageReceived", lambda e: seen.append(e.payload.get("text")))
        conversation.bus.subscribe("ConversationEnded", lambda e: seen.append("ended"))
        conversation.handle_text(1, "嗨")
        conversation.flush(1)
        self.assertIn("嗨", seen)
        self.assertIn("ended", seen)

    def test_clear_history(self):
        conversation, _, _ = self.build()
        conversation.process(1, "记住这句")
        conversation.clear_history(1)
        self.assertEqual(conversation.history(1), [])


if __name__ == "__main__":
    unittest.main()
