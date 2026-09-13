import tempfile
import unittest
from pathlib import Path

from core.context_manager import ContextManager
from core.conversation import Conversation
from core.event_bus import EventBus
from core.llm_client import LLMResult
from core.response_planner import ResponsePlanner
from core.response_validator import ResponseValidator
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger
from personality.consistency_checker import ConsistencyChecker
from style.message_renderer import MessageRenderer, RenderLimits
from tools.bootstrap import build_registry
from tools.core.cost import CostTracker
from tools.core.executor import ToolExecutor
from tools.core.policy import ToolPolicy
from tools.perception.router import PerceptionRequest, PerceptionRouter


class FakeClient:
    def __init__(self, content="这张图我看到了一只猫。", ok=True):
        self.content = content
        self.ok = ok
        self.calls = []

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append({"category": category, "messages": messages})
        if not self.ok:
            return LLMResult(ok=False, error="boom", tier=tier)
        return LLMResult(ok=True, content=self.content, model="fake", tier=tier,
                         input_tokens=100, output_tokens=20, cached_tokens=50)


def build_conversation(ok=True):
    tmp = Path(tempfile.mkdtemp())
    usage = UsageLogger(tmp / "usage.json")
    limits = RenderLimits()
    sent = []
    renderer = MessageRenderer(
        send=lambda chat, text: sent.append(text), typing=None, limits=limits, sleep=lambda s: None
    )
    client = FakeClient(ok=ok)
    conversation = Conversation(
        llm_client=client,
        renderer=renderer,
        context_manager=ContextManager(contract="【系统合同】\n测试", persona="【人格】\n测试"),
        token_budget=TokenBudget(daily_token_budget=100000),
        usage_logger=usage,
        event_bus=EventBus(),
        planner=ResponsePlanner(),
        validator=ResponseValidator(limits=limits),
        checker=ConsistencyChecker(),
        signal_provider=lambda: {"emotion": {"fatigue": 0.3, "joy": 0.5},
                                 "relationship": {"interaction_heat": 0.5, "intimacy": 0.2}},
        dry_run=False,
    )
    return conversation, client, sent, tmp


class LocalToolNoLlmTest(unittest.TestCase):
    """L0 工具必须 0 token。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cost = CostTracker(self.tmp / "cost.json")
        self.registry = build_registry(city="香港")
        self.executor = ToolExecutor(self.registry, cost=self.cost)
        self.policy = ToolPolicy(self.registry)

    def test_clock_costs_nothing(self):
        decision = self.policy.decide("现在几点了")
        result = self.executor.run(decision.tool, text="现在几点了", chat_id=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.llm_tokens, 0)
        self.assertEqual(self.cost.day()["llm_tokens"], 0)

    def test_game_costs_nothing(self):
        decision = self.policy.decide("掷个骰子")
        result = self.executor.run(decision.tool, text="掷个骰子", chat_id=1)
        self.assertTrue(result.ok)
        self.assertEqual(self.cost.day()["llm_tokens"], 0)

    def test_registry_marks_paid_only_for_llm(self):
        report = self.registry.cost_report()
        self.assertEqual(report["by_mode"]["PAID_API"], ["main_llm"])
        self.assertIn("clock", report["by_mode"]["LOCAL"])
        self.assertIn("weather", report["by_mode"]["FREE_NETWORK"])


class PerceptionPipelineTest(unittest.TestCase):
    def test_perceive_replies_and_records_history(self):
        conversation, client, sent, _ = build_conversation()
        result = conversation.perceive(
            1, source_type="image", summary="用户发来一张 800×600 的png图片；画面内容：一只猫趴在沙发上"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(sent, ["这张图我看到了一只猫。"])
        self.assertEqual(client.calls[0]["category"], "chat")
        contents = [message["content"] for message in client.calls[0]["messages"]]
        self.assertTrue(any("本地感知结果" in text for text in contents))
        self.assertTrue(any("别解释你是怎么看到的" in text for text in contents))
        history = conversation.history(1)
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertEqual(history[0]["role"], "user")

    def test_perception_never_silent(self):
        """就算规划器想沉默，用户发来的东西也必须有回应。"""
        conversation, client, sent, _ = build_conversation()
        conversation.process(1, "我今天在公司忙了一整天，刚到家")
        conversation.process(1, "嗯")
        result = conversation.perceive(1, source_type="voice", summary="用户发来一段 14 秒的语音，说的是：在吗")
        self.assertTrue(sent)
        self.assertTrue(result["sent"])

    def test_llm_failure_is_contained(self):
        conversation, _, sent, _ = build_conversation(ok=False)
        result = conversation.perceive(1, source_type="document", summary="文件内容：会议纪要")
        self.assertFalse(result["ok"])
        self.assertEqual(sent, [])

    def test_tone_hint_in_context(self):
        conversation, client, _, _ = build_conversation()
        conversation.perceive(1, source_type="image", summary="用户发来一张图片；画面内容：一只猫")
        contents = "\n".join(message["content"] for message in client.calls[0]["messages"])
        self.assertIn("回复倾向", contents)

    def test_router_degradation_reaches_context(self):
        conversation, client, _, tmp = build_conversation()
        router = PerceptionRouter(vision=None, use_ocr=False)
        path = tmp / "a.txt"
        path.write_text("随便写点东西", encoding="utf-8")
        result = router.perceive(PerceptionRequest(source_type="image", path=str(path)))
        summary = PerceptionRouter.describe_for_context(result)
        conversation.perceive(1, source_type="image", summary=summary)
        contents = "\n".join(message["content"] for message in client.calls[0]["messages"])
        self.assertIn("别假装", contents)


if __name__ == "__main__":
    unittest.main()
