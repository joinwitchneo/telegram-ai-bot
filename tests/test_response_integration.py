import tempfile
import unittest
from pathlib import Path

from core.conversation import Conversation
from core.event_bus import EventBus
from core.llm_client import LLMResult
from core.response_planner import ResponsePlanner
from core.response_stats import ResponseStats
from core.response_validator import ResponseValidator
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger
from personality.consistency_checker import ConsistencyChecker
from style.message_renderer import MessageRenderer, RenderLimits


class FakeClient:
    """按顺序吐出预设内容，并记录每次调用。"""

    def __init__(self, contents=("在的。",), ok=True):
        self.contents = list(contents) or [""]
        self.calls = []
        self.ok = ok

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append({"tier": tier, "category": category, "messages": messages})
        if not self.ok:
            return LLMResult(ok=False, error="boom", tier=tier, request_id="req_x")
        content = self.contents[min(len(self.calls) - 1, len(self.contents) - 1)]
        return LLMResult(
            ok=True, content=content, model="fake", tier=tier,
            input_tokens=100, output_tokens=20, cached_tokens=10, cache_hit=False,
            request_id=f"req_{len(self.calls)}",
        )


def signals(fatigue=0.3, joy=0.45, intimacy=0.05, heat=0.2) -> dict:
    return {
        "emotion": {"fatigue": fatigue, "joy": joy, "anger": 0.08, "sadness": 0.12, "anxiety": 0.15,
                    "interest": 0.5, "excitement": 0.28},
        "relationship": {"intimacy": intimacy, "interaction_heat": heat, "familiarity": 0.1,
                         "trust": 0.3, "shared_experience": 0.02},
    }


class PipelineTest(unittest.TestCase):
    def build(self, contents=("在的。",), *, ok=True, dry_run=True, send=None, state=None, signal=None):
        self.tmp = Path(tempfile.mkdtemp())
        usage = UsageLogger(self.tmp / "usage.json")
        stats = ResponseStats(self.tmp / "response_stats.json")
        self.sent = []
        renderer = MessageRenderer(
            send=send or (lambda chat, text: self.sent.append(text)),
            typing=None,
            limits=RenderLimits(),
            sleep=lambda seconds: None,
        )
        planner = ResponsePlanner()
        validator = ResponseValidator(limits=renderer.limits)
        checker = ConsistencyChecker()
        conversation = Conversation(
            llm_client=FakeClient(contents, ok=ok),
            renderer=renderer,
            context_manager=None,
            token_budget=TokenBudget(daily_token_budget=100000),
            usage_logger=usage,
            event_bus=EventBus(),
            planner=planner,
            validator=validator,
            checker=checker,
            state_provider=(lambda: state) if state else None,
            signal_provider=(lambda: signal) if signal else None,
            response_stats=stats,
            dry_run=dry_run,
            static_prefix="【系统合同】\n测试前缀",
            history_limit=8,
            debounce_single=1.0,
            debounce_multi=2.0,
            timer_factory=lambda delay, fn, args: _NullTimer(),
        )
        return conversation, stats

    # ── 完整链路 ────────────────────────────────────────────────────
    def test_plan_drives_reply_shape(self):
        conversation, _ = self.build(contents=("在的\n你说",))
        result = conversation.process(1, "在吗")
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent"], ["在的", "你说"])
        self.assertEqual(result["plan"]["reply_mode"], "CASUAL")
        self.assertIn(result["plan"]["length"], ("SHORT", "NORMAL", "LONG"))
        self.assertFalse(result["fallback_used"])

    def test_tone_hint_reaches_final_messages(self):
        conversation, _ = self.build(contents=("嗯。",))
        conversation.process(1, "在吗")
        dynamic = conversation.llm.calls[0]["messages"][1]["content"]
        self.assertIn("回复倾向", dynamic)

    def test_fatigue_makes_her_quiet(self):
        conversation, _ = self.build(contents=("嗯。",), signal=signals(fatigue=0.9))
        conversation.process(1, "在吗")
        dynamic = conversation.llm.calls[0]["messages"][1]["content"]
        self.assertIn("累", dynamic)

    def test_style_reference_reaches_final_messages(self):
        conversation, _ = self.build(contents=("嗯。",))
        conversation.style_provider = lambda chat_id: {"style": "对方习惯发很短的消息，别长篇大论"}
        conversation.process(1, "在吗")
        dynamic = conversation.llm.calls[0]["messages"][1]["content"]
        self.assertIn("对方风格", dynamic)

    # ── 计划优先于模型 ──────────────────────────────────────────────
    def test_model_cannot_inflate_message_count(self):
        conversation, _ = self.build(contents=('{"message_count": 9, "messages": ["一","二","三","四"]}',))
        result = conversation.process(1, "在吗")
        self.assertLessEqual(len(result["sent"]), 4)
        self.assertLessEqual(result["plan"]["message_count"], 4)

    def test_model_cannot_open_sticker(self):
        conversation, _ = self.build(contents=('{"sticker": true, "messages": ["哈哈"]}',))
        result = conversation.process(1, "我今天在公司忙了一整天，刚到家")
        self.assertFalse(result["plan"]["sticker"])

    # ── AI 味重试 ───────────────────────────────────────────────────
    def test_retry_once_then_good(self):
        conversation, _ = self.build(contents=("感谢你的分享，我很高兴能够帮助你。", "在的"))
        result = conversation.process(1, "在吗")
        self.assertTrue(result["retry_used"])
        self.assertEqual([call["category"] for call in conversation.llm.calls], ["chat", "retry"])
        self.assertEqual(result["sent"], ["在的"])
        self.assertFalse(result["fallback_used"])

    def test_retry_then_still_bad_uses_fallback(self):
        bad = "感谢你的分享，我很高兴能够帮助你。"
        conversation, _ = self.build(contents=(bad, bad))
        result = conversation.process(1, "在吗")
        self.assertTrue(result["retry_used"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(len(conversation.llm.calls), 2)      # 绝不会超过两次
        self.assertEqual(result["sent"], ["等下，我刚刚有点卡。"])

    def test_good_reply_never_retries(self):
        conversation, _ = self.build(contents=("在的。",))
        result = conversation.process(1, "在吗")
        self.assertFalse(result["retry_used"])
        self.assertEqual(len(conversation.llm.calls), 1)

    # ── 兜底 ────────────────────────────────────────────────────────
    def test_llm_failure_sends_fallback_not_silence(self):
        conversation, _ = self.build(ok=False)
        result = conversation.process(1, "在吗")
        self.assertFalse(result["ok"])
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["sent"], ["等下，我刚刚有点卡。"])

    def test_dev_environment_falls_back_from_llm_failure(self):
        # 开发环境故意返回"内测中"这类话时，不该被当成正常回复发出去
        conversation, _ = self.build(ok=False)
        result = conversation.process(1, "你在干嘛")
        self.assertTrue(result["sent"])

    # ── 不回复 ──────────────────────────────────────────────────────
    def test_low_value_after_reply_can_stay_silent(self):
        conversation, stats = self.build(contents=("在的",))
        conversation.process(1, "你在干嘛")
        calls_before = len(conversation.llm.calls)
        result = conversation.process(1, "嗯")
        self.assertTrue(result["skipped"])
        self.assertEqual(len(conversation.llm.calls), calls_before)
        self.assertEqual(result["sent"], [])
        self.assertEqual(stats.day()["silences"], 1)

    def test_silence_never_happens_on_first_message(self):
        conversation, _ = self.build(contents=("在的",))
        result = conversation.process(1, "嗯")
        self.assertFalse(result.get("skipped"))
        self.assertEqual(len(conversation.llm.calls), 1)

    # ── 分条发送被打断 ──────────────────────────────────────────────
    def test_new_message_interrupts_remaining_messages(self):
        conversation, _ = self.build(contents=("一\n二\n三",), dry_run=False)
        sent = []

        def send(chat_id, text):
            sent.append(text)
            if len(sent) == 1:
                conversation.handle_text(chat_id, "我又想到一句")

        conversation.renderer.send = send
        long_text = "我最近一直在想一个挺复杂的事情，要不要继续做下去，感觉有点累，你怎么看"
        result = conversation.process(1, long_text)
        self.assertEqual(sent, ["一"])
        self.assertEqual(result["sent"], ["一"])

    # ── 统计 ────────────────────────────────────────────────────────
    def test_stats_recorded(self):
        conversation, stats = self.build(contents=("在的",))
        conversation.process(1, "在吗")
        day = stats.day()
        self.assertEqual(day["replies"], 1)
        self.assertIn("SHORT", day["by_length"])
        self.assertGreaterEqual(day["messages_total"], 1)

    def test_result_reports_issues_list(self):
        conversation, _ = self.build(contents=("在的",))
        result = conversation.process(1, "在吗")
        self.assertIsInstance(result["issues"], list)

    def test_long_plan_splits_single_paragraph_into_burst(self):
        long_reply = "今天真累。" * 15
        conversation, _ = self.build(contents=(long_reply,))
        text = "我最近一直在想一个挺复杂的事情，要不要继续做下去，感觉有点累，你怎么看"
        result = conversation.process(1, text)
        self.assertGreater(len(result["sent"]), 1)
        self.assertLessEqual(len(result["sent"]), 4)
        self.assertTrue(result["plan"]["split"])

    def test_length_hint_tells_model_how_short(self):
        conversation, _ = self.build(contents=("嗯。",))
        conversation.process(1, "在吗")
        dynamic = conversation.llm.calls[0]["messages"][1]["content"]
        self.assertIn("短", dynamic)


class _NullTimer:
    daemon = False

    def start(self):
        pass

    def cancel(self):
        pass


if __name__ == "__main__":
    unittest.main()
