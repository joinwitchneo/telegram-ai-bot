"""Phase 6 主动唤醒（Autonomous Wake）：唤醒判断 + 规格里的五个核心测试。"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
import sys

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import v3_commands                                              # noqa: E402
from core.llm_client import LLMResult                           # noqa: E402
from v3.actions.base import JOURNAL, MESSAGE, STATUS_SENT, STATUS_SIMULATED  # noqa: E402
from v3.continuity.store import ContinuityStore                 # noqa: E402
from v3.interest.store import InterestStore                     # noqa: E402
from v3.observation.recorder import ObservationRecorder         # noqa: E402
from v3.scheduler.queue import WakeQueue                        # noqa: E402
from v3.scheduler.runner import SchedulerRunner                 # noqa: E402
from v3.service import V3Service                                # noqa: E402
from v3.thought.models import Thought                           # noqa: E402
from v3.thought.store import ThoughtStore                       # noqa: E402
from v3.trace import TraceLog                                   # noqa: E402
from v3.wake.wake_manager import SKIP, WAKE, WakeManager        # noqa: E402
from v3.wake.wake_policy import WakePolicy                      # noqa: E402
from v3.wake.wake_reason import (                               # noqa: E402
    CONTINUITY,
    INTEREST_DECAY_CHECK,
    TIME_RECHECK,
    UNFINISHED_THOUGHT,
    can_lead_to_message,
)

BASE_TIME = datetime.datetime(2026, 9, 14, 10, 0, 0)


def at(minutes: int = 0) -> datetime.datetime:
    return BASE_TIME + datetime.timedelta(minutes=minutes)


class FakeConfig:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, name, default=""):
        return str(self.values.get(name, default))

    def get_int(self, name, default=0):
        try:
            return int(self.values.get(name, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, name, default=0.0):
        try:
            return float(self.values.get(name, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, name, default=False):
        return str(self.values.get(name, default)).lower() in ("1", "true", "yes", "on")


class ScriptedClient:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls += 1
        return LLMResult(ok=True, content=self.payload, model="fake", tier=tier,
                         input_tokens=10, output_tokens=10)


class RecordingSender:
    def __init__(self):
        self.sent = []

    def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


PROPOSAL = json.dumps({
    "thought_update": {"importance": 0.9},
    "new_thoughts": [{"type": "curiosity", "content": "他后来到底怎么样了",
                      "topic": "考试", "evidence": ["他明天考试"]}],
    "intention": {"type": "message", "reason": "想继续昨天的话题", "confidence": 0.9,
                  "content": "昨天那个事我想接着问他"},
}, ensure_ascii=False)


def build_service(*, mode="observe", sender=None, payload=PROPOSAL, wake_overrides=None,
                  chat_id=1):
    tmp = Path(tempfile.mkdtemp())
    values = {
        "V3_ENABLED": "true", "V3_MODE": mode, "V3_DATA_DIR": str(tmp / "v3"),
        "V3_ACTION_COGNITIVE_COOLDOWN_MINUTES": "0",
    }
    values.update(wake_overrides or {})
    service = V3Service(FakeConfig(values), base_dir=tmp, client=ScriptedClient(payload),
                        environment_sender=sender, chat_id=chat_id)
    return service, tmp


def strong_unfinished(service, **overrides):
    data = dict(content="昨天那个问题我还没想完", topic="昨天的话题", is_unfinished=True,
                importance=0.95, urgency=0.9, novelty=0.8, curiosity=0.9,
                activation=0.9, persistence=0.9, information_gain=0.6,
                relationship_relevance=0.8)
    data.update(overrides)
    thought, _ = service.thoughts.add(Thought(id="", **data))
    return thought


class WakePolicyTest(unittest.TestCase):
    def build(self, *, thought_cooldown: int = 120):
        tmp = Path(tempfile.mkdtemp())
        thoughts = ThoughtStore(tmp / "t.json")
        manager = WakeManager(
            thoughts=thoughts, interests=InterestStore(tmp / "i.json"),
            continuity=ContinuityStore(tmp / "c"),
            wake_queue=WakeQueue(tmp / "q.json"),
            policy=WakePolicy(wake_threshold=0.55, daily_wake_limit=2,
                              min_gap_minutes=30,
                              per_thought_cooldown_minutes=thought_cooldown),
            trace=TraceLog(tmp / "e.jsonl"),
            state_path=tmp / "state.json", log_path=tmp / "wakes.jsonl",
            observations=ObservationRecorder(tmp / "o.jsonl"),
        )
        return manager, thoughts

    def test_time_recheck_never_triggers_and_never_messages(self):
        manager, _thoughts = self.build()
        decision = manager.evaluate(now=at(0))
        self.assertEqual(decision.decision, SKIP)
        self.assertFalse(can_lead_to_message(TIME_RECHECK))
        self.assertFalse(can_lead_to_message(INTEREST_DECAY_CHECK))
        self.assertTrue(can_lead_to_message(UNFINISHED_THOUGHT))

    def test_unfinished_thought_triggers_a_wake(self):
        manager, thoughts = self.build()
        thoughts.add(Thought(id="", content="还没想完的事", topic="t", is_unfinished=True,
                             urgency=0.9, curiosity=0.8, motivation=0.8, novelty=0.9,
                             relationship_relevance=0.7))
        decision = manager.evaluate(now=at(0))
        self.assertEqual(decision.decision, WAKE)
        self.assertEqual(decision.candidate["reason"], UNFINISHED_THOUGHT)

    def test_daily_limit_and_min_gap_block(self):
        manager, thoughts = self.build(thought_cooldown=0)
        thoughts.add(Thought(id="", content="没想完", topic="t", is_unfinished=True,
                             urgency=0.9, motivation=0.8, novelty=0.9))
        first = manager.tick(now=at(0))
        self.assertTrue(first["created"], "第一次应该排入唤醒")
        second = manager.tick(now=at(10))
        self.assertFalse(second["created"])
        self.assertIn("最小间隔", second.get("blocked_by", ""))
        third = manager.tick(now=at(200))
        self.assertTrue(third["created"], "过了最小间隔可以再来一次（仍在每日上限内）")
        fourth = manager.tick(now=at(400))
        self.assertFalse(fourth["created"], "超过每日上限就不许再唤醒了")

    def test_all_wakes_go_through_the_queue(self):
        manager, thoughts = self.build()
        thoughts.add(Thought(id="", content="没想完", topic="t", is_unfinished=True,
                             urgency=0.9, motivation=0.8, novelty=0.9))
        result = manager.tick(now=at(0))
        self.assertTrue(result["created"])
        tasks = manager.wake_queue.all()
        self.assertEqual(len(tasks), 1, "唤醒必须排进 wake_queue，不许直接调 LifeCycle")
        self.assertTrue(str(tasks[0]["reason"]).startswith("autonomous:"))

    def test_same_thought_has_cooldown(self):
        manager, thoughts = self.build()
        thought, _ = thoughts.add(Thought(id="", content="没想完", topic="t", is_unfinished=True,
                                          urgency=0.9, motivation=0.8, novelty=0.9))
        manager.tick(now=at(0))
        candidates = manager.candidates(now=at(10))
        blocked = [c for c in candidates if c.thought_id == thought.id]
        self.assertTrue(blocked and blocked[0].blocked_by, "同一个念头冷却期内要标 blocked")


class CoreScenarioTest(unittest.TestCase):
    """规格 §14–§18 的五个核心测试。"""

    def test_1_thinking_continues_without_any_new_user_message(self):
        """无人聊天也能继续思考：Thought B 必须来自自主唤醒，且晚于 Thought A。"""
        service, _tmp = build_service()
        service.observations.append(user_message="我最近在想 AI 角色自主性的问题，还没想清楚",
                                    bot_response_excerpt="嗯", now=at(0))
        service.run_cycle(trigger="chat", now=at(0))
        thought_a = [t for t in service.thoughts.all() if t.source == "observation"][0]
        self.assertTrue(thought_a.is_unfinished, "这句话应该留下未完成的念头")

        thought_a.urgency = 0.9
        thought_a.importance = 0.95
        thought_a.novelty = 0.9
        thought_a.motivation = 0.8
        thought_a.curiosity = 0.9
        thought_a.persistence = 0.9
        thought_a.relationship_relevance = 0.8
        thought_a.information_gain = 0.7
        thought_a.activation = 0.95
        service.thoughts.update(thought_a)
        service.action_budget.cognitive_cooldown_minutes = 0
        service.budget.data["llm_calls"] = 0

        queued = service.wake_manager.tick(now=at(90))
        self.assertTrue(queued["created"], "内部状态够了就该自己醒来")
        reason = queued["reason"]

        result = service.run_cycle(trigger="wake", wake_reason=reason, now=at(95))
        created = [t for t in service.thoughts.all() if t.source == "autonomous_wake"]
        self.assertTrue(created, "自主唤醒后的认知必须留下 source=autonomous_wake 的念头")
        thought_b = created[-1]
        self.assertGreater(thought_b.created_at, thought_a.created_at)
        self.assertEqual(result["cycle"]["wake_reason"], reason)

    def test_2_wake_without_messaging(self):
        """醒来但不聊天：动机不够 → NO_ACTION，发送 0 次。"""
        sender = RecordingSender()
        service, _tmp = build_service(mode="live", sender=sender)
        service.thoughts.add(Thought(id="", content="随便一个低价值念头", topic="闲聊",
                                     importance=0.2, urgency=0.05, novelty=0.1,
                                     activation=0.2))
        result = service.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(0))
        self.assertTrue(result["ran"], "唤醒本身是成功的")
        self.assertFalse(result["threshold_reached"])
        self.assertEqual(sender.sent, [], "醒来不等于要说话")
        self.assertEqual(service.budget.data["llm_calls"], 0)

    def test_3_real_message_intent_through_full_chain(self):
        """真的产生主动联系意图：observe 0 发送 / dry_run 走假环境 / live 假 sender 1 次。"""
        observe_sender = RecordingSender()
        observe, _ = build_service(mode="observe", sender=observe_sender)
        strong_unfinished(observe)
        r_observe = observe.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(0))
        self.assertEqual(r_observe["cycle"]["decision"]["action"], JOURNAL)
        self.assertEqual(r_observe["cycle"]["decision"]["would_action"], MESSAGE)
        self.assertEqual(observe_sender.sent, [])
        self.assertEqual(len(observe.environment.sent), 0)

        dry_sender = RecordingSender()
        dry, _ = build_service(mode="dry_run", sender=dry_sender)
        strong_unfinished(dry)
        r_dry = dry.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(0))
        self.assertEqual(r_dry["cycle"]["decision"]["action"], MESSAGE)
        self.assertEqual(r_dry["cycle"]["outcome"]["status"], STATUS_SIMULATED)
        self.assertEqual(dry_sender.sent, [], "dry_run 不允许真的发")
        self.assertEqual(len(dry.environment.sent), 1, "但要走完环境接口")

        live_sender = RecordingSender()
        live, _ = build_service(mode="live", sender=live_sender)
        strong_unfinished(live)
        r_live = live.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(0))
        self.assertEqual(r_live["cycle"]["outcome"]["status"], STATUS_SENT)
        self.assertEqual(len(live_sender.sent), 1)

    def test_4_not_a_timer_robot(self):
        """不是定时机器人：连着几轮 NO_ACTION，直到出现真正值得处理的未完成念头。"""
        sender = RecordingSender()
        service, _tmp = build_service(mode="live", sender=sender)
        quiet = service.thoughts.add(Thought(id="", content="没什么要紧的事", topic="闲聊",
                                             importance=0.2, urgency=0.05, activation=0.2))[0]
        for index in range(3):
            result = service.run_cycle(trigger="wake", wake_reason=TIME_RECHECK,
                                       now=at(index * 60))
            self.assertEqual(result["cycle"]["decision"]["action"], "NO_ACTION")
            self.assertEqual(sender.sent, [], "定时唤醒来一次就该 NO_ACTION 一次")
        strong_unfinished(service)
        final = service.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(300))
        self.assertEqual(final["cycle"]["decision"]["action"], MESSAGE)
        self.assertEqual(len(sender.sent), 1, "只有真正有理由时才发第一条")

    def test_5_wants_to_talk_but_holds_back_then_speaks(self):
        """想聊天但能忍住：冷却期 NO_ACTION，但意向留到冷却解除后兑现。"""
        sender = RecordingSender()
        service, _tmp = build_service(mode="live", sender=sender)
        service.action_budget.message_cooldown_minutes = 600
        service.action_budget.note_message(now=at(-5))     # 刚发过 → 处于冷却
        thought = strong_unfinished(service)

        first = service.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(0))
        self.assertEqual(first["cycle"]["decision"]["action"], JOURNAL, "冷却期内忍住")
        self.assertEqual(first["cycle"]["decision"]["would_action"], MESSAGE)
        self.assertEqual(sender.sent, [])
        saved = service.thoughts.get(thought.id)
        self.assertTrue((saved.metadata or {}).get("pending_intention"),
                        "想做却没做的意向必须留成内部状态")

        # 冷却解除 + 时间往前走 → 意向仍然在，重新计算后兑现
        service.action_budget.data.last_message_at = at(-1000).isoformat(timespec="seconds")
        service.action_budget.save()
        strong_unfinished(service, content=saved.content, topic=saved.topic)
        second = service.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(30))
        self.assertEqual(second["cycle"]["decision"]["action"], MESSAGE)
        self.assertEqual(len(sender.sent), 1)
        after = service.thoughts.get(thought.id)
        self.assertFalse((after.metadata or {}).get("pending_intention"),
                         "真正说出去之后，未兑现的意向要清掉")

    def test_wake_records_are_written_and_queryable(self):
        service, _tmp = build_service()
        strong_unfinished(service)
        result = service.run_cycle(trigger="wake", wake_reason=UNFINISHED_THOUGHT, now=at(0))
        service.record_wake_result(wake_id="wake_000123", reason=UNFINISHED_THOUGHT,
                                   result=result, started_at=at(0).isoformat())
        last = service.whyawake()
        self.assertEqual(last["wake_id"], "wake_000123")
        self.assertEqual(last["reason"], UNFINISHED_THOUGHT)
        self.assertTrue(last["decision"])
        summary = service.wake_reasons()
        self.assertEqual(summary["by_reason"][UNFINISHED_THOUGHT]["wakes"], 1)


class WakeCommandTest(unittest.TestCase):
    def build(self):
        service, _tmp = build_service(mode="dry_run")
        return service, _tmp

    def test_wake_commands(self):
        service, _tmp = self.build()
        bot = type("B", (), {"v3_service": service, "sent": [],
                             "send_message": lambda self, c, t: self.sent.append((c, t))})()
        for command in ("/wake", "/whyawake", "/wakereasons"):
            self.assertTrue(v3_commands.dispatch(bot, 1, command, ""), command)
        self.assertIn("自主唤醒", bot.sent[0][1])
        self.assertIn("唤醒", bot.sent[1][1])
        self.assertIn("唤醒原因统计", bot.sent[2][1])

    def test_waketest_respects_the_normal_pipeline(self):
        service, _tmp = self.build()
        bot = type("B", (), {"v3_service": service, "sent": [],
                             "send_message": lambda self, c, t: self.sent.append((c, t))})()
        self.assertTrue(v3_commands.dispatch(bot, 1, "/waketest", "unfinished now"))
        text = bot.sent[0][1]
        self.assertIn("已构造内部场景", text)
        self.assertTrue("模拟发送" in text or "不会真的发出去" in text, text)
        self.assertEqual(len(service.environment.sent), 1, "dry_run 下走假环境")
        self.assertEqual(service.budget.data["llm_calls"], 1, "受预算约束（只允许一次）")

    def test_waketest_bad_kind(self):
        service, _tmp = self.build()
        bot = type("B", (), {"v3_service": service, "sent": [],
                             "send_message": lambda self, c, t: self.sent.append((c, t))})()
        v3_commands.dispatch(bot, 1, "/waketest", "nonsense")
        self.assertIn("不认识", bot.sent[0][1])


class SchedulerWakeIntegrationTest(unittest.TestCase):
    def test_idle_scheduler_can_wake_itself(self):
        service, tmp = build_service()
        strong_unfinished(service)
        queue = service.wake_queue
        calls = []

        def spawn(task_id):
            calls.append(task_id)
            return 0, "", ""

        runner = SchedulerRunner(queue=queue, base_dir=tmp, spawn=spawn,
                                 lease_minutes=30, max_silence_hours=48,
                                 min_interval_minutes=60, wake_manager=service.wake_manager)
        result = runner.tick(now=at(0))
        self.assertTrue(result["woke"].get("created"), "空闲时调度器应该自己醒来")
        second = runner.tick(now=at(1))
        self.assertTrue(second["executed"], "排进去的唤醒要被执行")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
