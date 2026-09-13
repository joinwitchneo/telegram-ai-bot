import datetime
import json
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


class DebuggerUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_log_and_read(self):
        debugger = ContextDebugger(debug_dir=self.tmp, enabled=True)
        debugger.log({"request_id": "r1", "mode": "CASUAL", "tokens": {"L0": 10}, "history_count": 3})
        self.assertEqual(len(debugger.recent()), 1)
        self.assertEqual(debugger.last()["request_id"], "r1")
        self.assertEqual(debugger.stats()["records"], 1)

    def test_recent_is_ring_buffer(self):
        debugger = ContextDebugger(debug_dir=self.tmp, keep_recent=5)
        for index in range(12):
            debugger.log({"request_id": f"r{index}"})
        self.assertEqual(len(debugger.recent(100)), 5)
        self.assertEqual(debugger.last()["request_id"], "r11")

    def test_snapshot_disabled_creates_nothing(self):
        debugger = ContextDebugger(debug_dir=self.tmp, snapshot_enabled=False)
        self.assertIsNone(debugger.snapshot("r1", {"L0": "x"}))
        self.assertEqual(list(self.tmp.rglob("*.json")), [])

    def test_snapshot_enabled_writes_file(self):
        debugger = ContextDebugger(debug_dir=self.tmp, snapshot_enabled=True)
        path = debugger.snapshot("req_abc", {"L0": "合同", "L1": "人格", "L4": [{"role": "user"}]})
        self.assertIsNotNone(path)
        day = datetime.date.today().isoformat()
        self.assertEqual(path.parent, self.tmp / day)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["L0"], "合同")
        self.assertEqual(data["L1"], "人格")

    def test_disabled_logger_still_counts_in_memory(self):
        debugger = ContextDebugger(debug_dir=self.tmp, enabled=False)
        debugger.log({"request_id": "r1"})
        self.assertEqual(debugger.stats()["records"], 1)


class FakeClient:
    def __init__(self):
        self.calls = []

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append(messages)
        return LLMResult(ok=True, content="在的\n怎么了", model="fake", tier=tier,
                         input_tokens=100, output_tokens=20, cached_tokens=60,
                         cache_hit=True, request_id="req_fake_1")


class FakeRenderer:
    def render(self, chat_id, text, **_kwargs):
        return [line for line in str(text).split("\n") if line.strip()]


class ConversationDebugTest(unittest.TestCase):
    def build(self, snapshot=False, debug=True):
        self.tmp = Path(tempfile.mkdtemp())
        debugger = ContextDebugger(debug_dir=self.tmp / "debug", enabled=debug, snapshot_enabled=snapshot)
        manager = ContextManager(
            contract="【系统合同】\n直接输出消息。",
            persona="【人格】\n嘴硬心软。",
            token_budget=TokenBudget(max_context_tokens=4000, daily_token_budget=100000),
            max_context_tokens=4000,
        )
        conversation = Conversation(
            llm_client=FakeClient(),
            renderer=FakeRenderer(),
            context_manager=manager,
            token_budget=TokenBudget(max_context_tokens=4000, daily_token_budget=100000),
            usage_logger=UsageLogger(self.tmp / "usage.json"),
            event_bus=EventBus(),
            debugger=debugger,
            debounce_single=0.5,
            debounce_multi=1.0,
        )
        return conversation, debugger

    def test_record_has_required_fields(self):
        conversation, debugger = self.build()
        result = conversation.process(1, "我今天好烦")
        record = debugger.last()
        expected = {
            "request_id", "model", "mode", "tokens", "total_context_tokens", "memory_ids",
            "topic", "emotion", "relationship", "history_count", "policy_reason", "budget_state",
        }
        self.assertTrue(expected.issubset(set(record.keys())))
        self.assertEqual(record["mode"], "EMOTIONAL")
        self.assertEqual(result["mode"], "EMOTIONAL")

    def test_token_stats_match_context(self):
        conversation, debugger = self.build()
        result = conversation.process(1, "在吗")
        record = debugger.last()
        self.assertEqual(record["tokens"], result["context"]["tokens"])
        self.assertEqual(record["total_context_tokens"], result["context"]["total_context_tokens"])
        self.assertGreater(record["tokens"]["L0"], 0)
        self.assertGreater(record["tokens"]["L1"], 0)

    def test_history_count_reported(self):
        conversation, debugger = self.build()
        for index in range(3):
            conversation.process(1, f"第{index}句")
        record = debugger.last()
        self.assertEqual(record["history_count"], 4)   # 3 轮之前累积的历史

    def test_snapshot_written_when_enabled(self):
        conversation, debugger = self.build(snapshot=True)
        conversation.process(1, "在吗")
        files = list((self.tmp / "debug").rglob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertIn("L0", data)
        self.assertIn("L1", data)
        self.assertIn("L4", data)
        self.assertIn("meta", data)

    def test_no_snapshot_when_disabled(self):
        conversation, _ = self.build(snapshot=False)
        conversation.process(1, "在吗")
        self.assertEqual(list((self.tmp / "debug").rglob("*.json")), [])

    def test_debug_data_does_not_leak_into_context(self):
        """Debugger 是旁路：记录前后送给模型的消息必须一致。"""
        conversation, debugger = self.build(snapshot=True)
        conversation.process(1, "在吗")
        first_messages = json.loads(json.dumps(conversation.llm.calls[0]))
        conversation.process(1, "在不")
        second_messages = conversation.llm.calls[1]
        self.assertNotIn("request_id", json.dumps(second_messages))
        self.assertNotIn("debug", json.dumps(second_messages))
        self.assertEqual(first_messages[0]["role"], second_messages[0]["role"])

    def test_rule_path_is_logged_too(self):
        conversation, debugger = self.build()
        conversation.budget.record(100000)
        conversation.process(1, "你好")
        record = debugger.last()
        self.assertIn("硬上限", record["policy_reason"])


if __name__ == "__main__":
    unittest.main()
