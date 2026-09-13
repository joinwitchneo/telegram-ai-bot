import datetime
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
from memory.memory_store import MemoryStore
from memory.topic_memory import TopicMemory
from personality.consistency_checker import ConsistencyChecker
from proactive.character_life import CharacterLife
from proactive.proactive_engine import Candidate, ProactiveEngine, ProactiveHistory
from proactive.proactive_scheduler import ProactiveScheduler, ProactiveState
from style.message_renderer import MessageRenderer, RenderLimits

NOW = datetime.datetime(2026, 9, 13, 14, 0, 0)


class FakeClient:
    """假客户端，但和真实 LLMClient 一样回写用量。"""

    def __init__(self, contents=("在吗",), ok=True, usage=None):
        self.contents = list(contents) or [""]
        self.calls = []
        self.ok = ok
        self.usage = usage

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls.append({"category": category, "tier": tier, "messages": messages})
        if not self.ok:
            return LLMResult(ok=False, error="boom", tier=tier, request_id="req_x")
        content = self.contents[min(len(self.calls) - 1, len(self.contents) - 1)]
        if self.usage is not None:
            self.usage.record(
                category=category, model="fake", tier=tier,
                input_tokens=120, output_tokens=30, cached_tokens=60,
            )
        return LLMResult(
            ok=True, content=content, model="fake", tier=tier,
            input_tokens=120, output_tokens=30, cached_tokens=60, request_id=f"req_{len(self.calls)}",
        )


class Pipeline:
    """把 Phase 6 的零件装成一条完整链路。"""

    def __init__(self, contents=("在吗",), ok=True, **kwargs):
        self.tmp = Path(tempfile.mkdtemp())
        self.chat_id = 1
        self.usage = UsageLogger(self.tmp / "usage.json")
        self.life = CharacterLife(self.tmp / "life.json")
        self.memory = MemoryStore(self.tmp / "memories.json")
        self.topics = TopicMemory(self.tmp / "topics.json")
        self.history = ProactiveHistory(self.tmp / "history.json")
        self.sent = []
        limits = RenderLimits()
        self.renderer = MessageRenderer(
            send=lambda chat, text: self.sent.append(text), typing=None, limits=limits,
            sleep=lambda seconds: None,
        )
        self.conversation = Conversation(
            llm_client=FakeClient(contents, ok=ok, usage=self.usage),
            renderer=self.renderer,
            context_manager=ContextManager(
                contract="【系统合同】\n测试前缀", persona="【人格】\n测试人格",
                token_budget=TokenBudget(daily_token_budget=100000),
            ),
            token_budget=TokenBudget(daily_token_budget=100000),
            usage_logger=self.usage,
            event_bus=EventBus(),
            planner=ResponsePlanner(),
            validator=ResponseValidator(limits=limits),
            checker=ConsistencyChecker(),
            signal_provider=lambda: {
                "emotion": {"fatigue": 0.3, "joy": 0.45},
                "relationship": {"interaction_heat": 0.6, "intimacy": 0.2, "shared_experience": 0.2},
            },
            memory_by_id=self._memory_by_id,
            dry_run=False,
            static_prefix="",
        )
        self.engine = ProactiveEngine(
            life=self.life, memory_store=self.memory, topics=self.topics,
            history=self.history, memory_min_age_hours=0.0,
        )
        self.state = ProactiveState(self.tmp / "state.json")
        self.scheduler = ProactiveScheduler(
            engine=self.engine,
            state=self.state,
            history=self.history,
            runner=lambda cand: self.conversation.proactive(self.chat_id, candidate=cand),
            signals_provider=lambda: {
                "emotion": {"fatigue": 0.3},
                "relationship": {"interaction_heat": 0.6, "shared_experience": 0.2, "intimacy": 0.2},
            },
            now_fn=lambda: NOW,
            **kwargs,
        )

    def _memory_by_id(self, ids):
        result = []
        for memory_id in ids:
            record = self.memory.get(memory_id)
            if record:
                result.append(record)
        return result


class ProactivePipelineTest(unittest.TestCase):
    def test_full_chain_sends_message(self):
        pipe = Pipeline(contents=("考完没？",))
        pipe.life.add("important_event", "用户明天要考试", source="CONFIRMED", importance=0.9)
        result = pipe.scheduler.tick(now=NOW)
        self.assertTrue(result["sent"])
        self.assertEqual(result["messages"], ["考完没？"])
        self.assertEqual(result["history_entry"]["reason"], "important_event")
        self.assertEqual(pipe.sent, ["考完没？"])

    def test_proactive_uses_same_pipeline_as_chat(self):
        pipe = Pipeline(contents=("考完没？",))
        pipe.life.add("important_event", "用户明天要考试", source="CONFIRMED", importance=0.9)
        pipe.scheduler.tick(now=NOW)
        call = pipe.conversation.llm.calls[0]
        self.assertEqual(call["category"], "proactive")
        contents = [message["content"] for message in call["messages"]]
        self.assertTrue(any("主动消息" in text for text in contents))
        self.assertTrue(any("回复倾向" in text for text in contents))
        # 主动消息不带用户发言
        self.assertFalse(any(message["role"] == "user" for message in call["messages"]))

    def test_generated_message_enters_history(self):
        pipe = Pipeline(contents=("考完没？",))
        pipe.life.add("unfinished_topic", "Python 学不学", importance=0.9)
        pipe.scheduler.on_user_message(now=NOW - datetime.timedelta(hours=40))
        pipe.scheduler.tick(now=NOW)
        history = pipe.conversation.history(1)
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertEqual(history[-1]["content"], "考完没？")

    def test_validator_caps_message_count(self):
        pipe = Pipeline(contents=("一\n二\n三\n四\n五\n六",))
        candidate = Candidate(id="x", topic="x", reason="important_event", score=0.8, hint="想起考试")
        result = pipe.conversation.proactive(1, candidate=candidate)
        self.assertLessEqual(len(result["sent"]), 4)

    def test_short_plan_for_medium_score(self):
        pipe = Pipeline(contents=("在吗",))
        candidate = Candidate(id="x", topic="x", reason="character_thought", score=0.55, hint="突然想到")
        result = pipe.conversation.proactive(1, candidate=candidate)
        self.assertEqual(result["plan"]["length"], "SHORT")

    def test_normal_plan_for_strong_candidate(self):
        pipe = Pipeline(contents=("在吗",))
        candidate = Candidate(id="x", topic="x", reason="important_event", score=0.9, hint="想起考试")
        result = pipe.conversation.proactive(1, candidate=candidate)
        self.assertEqual(result["plan"]["length"], "NORMAL")

    def test_ai_flavor_retry_once(self):
        pipe = Pipeline(contents=("感谢你的分享，我很高兴能够帮助你。", "考完没？"))
        candidate = Candidate(id="x", topic="x", reason="important_event", score=0.8, hint="想起考试")
        result = pipe.conversation.proactive(1, candidate=candidate)
        self.assertTrue(result["retry_used"])
        self.assertEqual([call["category"] for call in pipe.conversation.llm.calls], ["proactive", "retry"])
        self.assertEqual(result["sent"], ["考完没？"])

    def test_ai_flavor_failure_cancels_message(self):
        bad = "感谢你的分享，我很高兴能够帮助你。"
        pipe = Pipeline(contents=(bad, bad))
        candidate = Candidate(id="x", topic="x", reason="important_event", score=0.8, hint="想起考试")
        result = pipe.conversation.proactive(1, candidate=candidate)
        self.assertEqual(result["sent"], [])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["reason"], "ai_flavor")
        self.assertEqual(pipe.sent, [])

    def test_llm_failure_cancels_without_fallback_spam(self):
        pipe = Pipeline(ok=False)
        candidate = Candidate(id="x", topic="x", reason="important_event", score=0.8, hint="想起考试")
        result = pipe.conversation.proactive(1, candidate=candidate)
        self.assertEqual(result["sent"], [])
        self.assertTrue(result["cancelled"])

    def test_scheduler_skips_when_no_candidate(self):
        pipe = Pipeline()
        result = pipe.scheduler.tick(now=NOW)
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "no_candidate")
        self.assertEqual(pipe.conversation.llm.calls, [])

    def test_memory_backed_candidate_includes_memory_in_context(self):
        pipe = Pipeline(contents=("考完没？",))
        record, _ = pipe.memory.create(
            type="fact", content="用户下周三要考试", importance=0.8, protected=True
        )
        # 测试用的是固定时间点 NOW，而记忆按真实时间创建；若真实时间已晚于 NOW，
        # 记忆会变成"来自未来"并被"太新"规则跳过（那是正确行为）。这里把创建时间回拨，
        # 保证测试与运行时刻无关。
        for item in pipe.memory.data:
            if item.get("id") == record["id"]:
                item["created_at"] = (NOW - datetime.timedelta(hours=5)).isoformat()
        result = pipe.scheduler.tick(now=NOW)
        self.assertTrue(result["sent"])
        contents = [message["content"] for message in pipe.conversation.llm.calls[0]["messages"]]
        self.assertTrue(any("相关记忆" in text and "考试" in text for text in contents))
        self.assertEqual(result["candidate"]["memory_ids"], [record["id"]])

    def test_user_reply_resets_and_marks(self):
        pipe = Pipeline(contents=("考完没？",))
        pipe.life.add("important_event", "用户明天要考试", source="CONFIRMED", importance=0.9)
        pipe.scheduler.tick(now=NOW)
        self.assertEqual(pipe.history.stats()["replied"], 0)
        pipe.scheduler.on_user_message(now=NOW + datetime.timedelta(minutes=20))
        self.assertEqual(pipe.history.stats()["replied"], 1)
        self.assertEqual(pipe.state.data["nonresponse_streak"], 0)

    def test_two_ignores_trigger_cooldown_and_block_next_tick(self):
        pipe = Pipeline(contents=("在吗",))
        pipe.life.add("unfinished_thought", "想知道他考试怎么样", importance=0.6)
        old = (NOW - datetime.timedelta(hours=5)).isoformat()
        pipe.state.data["pending"] = [{"id": "a", "ts": old, "topic": "a"}]
        pipe.scheduler.settle_pending(NOW)
        pipe.state.data["pending"] = [{"id": "b", "ts": old, "topic": "b"}]
        pipe.scheduler.settle_pending(NOW)
        self.assertTrue(pipe.state.data["cooldown_until"])
        blocked = pipe.scheduler.tick(now=NOW)
        self.assertFalse(blocked["sent"])
        self.assertEqual(blocked["reason"], "cooldown")
        self.assertEqual(pipe.conversation.llm.calls, [])

    def test_quiet_hours_blocks_real_pipeline(self):
        pipe = Pipeline(contents=("在吗",))
        pipe.life.add("important_event", "用户明天要考试", source="CONFIRMED", importance=0.9)
        result = pipe.scheduler.tick(now=NOW.replace(hour=2, minute=30))
        self.assertEqual(result["reason"], "quiet_hours")
        self.assertEqual(pipe.conversation.llm.calls, [])

    def test_tokens_are_recorded_for_proactive(self):
        pipe = Pipeline(contents=("考完没？",))
        pipe.life.add("important_event", "用户明天要考试", source="CONFIRMED", importance=0.9)
        pipe.scheduler.tick(now=NOW)
        day = pipe.usage.day()
        self.assertEqual(day["requests"], 1)
        self.assertIn("proactive", day["by_category"])
        self.assertEqual(day["by_category"]["proactive"]["input_tokens"], 120)

    def test_no_candidate_costs_zero_tokens(self):
        pipe = Pipeline()
        pipe.scheduler.tick(now=NOW)
        self.assertEqual(pipe.usage.day()["requests"], 0)


if __name__ == "__main__":
    unittest.main()
