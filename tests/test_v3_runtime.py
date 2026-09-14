"""V3 运行期：队列/租约/重试、Reward、Motivation/Decision、观测与跨周期闭环。"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from core.llm_client import LLMResult
from v3.budget import V3Budget
from v3.continuity.cycle import CognitiveCycle
from v3.continuity.rules import WAKE_LOW, WAKE_NORMAL, plan_next_wake
from v3.continuity.store import ContinuityStore
from v3.decision.engine import DecisionEngine, JOURNAL_NOTE, NO_ACTION
from v3.interest.engine import InterestEngine
from v3.interest.store import InterestStore
from v3.journal import InnerJournal
from v3.motivation.engine import MotivationEngine
from v3.observation.recorder import ObservationRecorder
from v3.reward.engine import RewardEngine
from v3.scheduler.queue import WakeQueue
from v3.thought.generator import ThoughtGenerator
from v3.thought.lifecycle import ThoughtLifecycle
from v3.thought.models import Thought
from v3.thought.store import ThoughtStore


def at(offset_minutes: int = 0) -> datetime.datetime:
    base = datetime.datetime(2026, 9, 13, 12, 0, 0)
    return base + datetime.timedelta(minutes=offset_minutes)


class FakeRuntime:
    """最小 RuntimeConfig 替身：只暴露 cycle 用到的 mode/limit_int。"""

    def __init__(self, *, mode="observe", limits=None) -> None:
        self._mode = mode
        self._limits = dict(limits or {})

    def mode(self) -> str:
        return self._mode

    def limit(self, name, default):
        return float(self._limits.get(name, default))

    def limit_int(self, name, default):
        return int(self._limits.get(name, default))


class WakeQueueTest(unittest.TestCase):
    def build(self):
        tmp = Path(tempfile.mkdtemp())
        return WakeQueue(tmp / "wake_queue.json"), tmp

    def test_enqueue_claim_complete(self):
        queue, tmp = self.build()
        task = queue.enqueue(reason="test", earliest_at=at().isoformat(timespec="seconds"))
        self.assertEqual(task["status"], "PENDING")
        self.assertTrue(queue.claim(task["id"], now=at(1)))
        self.assertEqual(queue.all()[0]["status"], "RUNNING")
        self.assertTrue(queue.complete(task["id"], now=at(2)))
        self.assertEqual(queue.all()[0]["status"], "COMPLETED")
        self.assertFalse((tmp / "queue.lock").exists(), "lock 必须在操作后释放")

    def test_claim_only_pending(self):
        queue, _ = self.build()
        task = queue.enqueue(reason="t", earliest_at=at().isoformat(timespec="seconds"))
        queue.claim(task["id"], now=at(1))
        self.assertFalse(queue.claim(task["id"], now=at(2)), "已在执行的任务不能被再次认领")

    def test_lease_expiry_recovers_to_pending(self):
        queue, _ = self.build()
        task = queue.enqueue(reason="t", earliest_at=at().isoformat(timespec="seconds"))
        queue.claim(task["id"], lease_minutes=1, now=at(0))
        self.assertEqual(queue.recover_stale(now=at(0)), 0)
        self.assertEqual(queue.recover_stale(now=at(5)), 1)
        self.assertEqual(queue.all()[0]["status"], "PENDING")
        self.assertEqual(queue.all()[0]["stale_recovered"], 1)

    def test_retry_then_failed_permanent(self):
        queue, _ = self.build()
        task = queue.enqueue(reason="t", earliest_at=at().isoformat(timespec="seconds"),
                             max_attempts=2)
        queue.claim(task["id"], now=at(0))
        failed = queue.fail(task["id"], error="boom", now=at(1))
        self.assertEqual(failed["status"], "PENDING")          # 第一版：退避重试
        self.assertGreater(failed["earliest_at"], at(1).isoformat(timespec="seconds"))
        queue.claim(task["id"], now=at(30))
        final = queue.fail(task["id"], error="boom again", now=at(31))
        self.assertEqual(final["status"], "FAILED_PERMANENT")
        self.assertEqual(queue.stats()["failed_permanent"], [task["id"]])

    def test_requeue_revives_permanent_failure(self):
        queue, _ = self.build()
        task = queue.enqueue(reason="t", earliest_at=at().isoformat(timespec="seconds"),
                             max_attempts=1)
        queue.claim(task["id"], now=at(0))
        queue.fail(task["id"], error="x", now=at(1))
        self.assertEqual(queue.all()[0]["status"], "FAILED_PERMANENT")
        self.assertTrue(queue.requeue(task["id"], now=at(2)))
        self.assertEqual(queue.all()[0]["status"], "PENDING")
        self.assertEqual(queue.all()[0]["attempts"], 0)

    def test_due_only_returns_ready_pending(self):
        queue, _ = self.build()
        queue.enqueue(reason="future", earliest_at=at(120).isoformat(timespec="seconds"))
        self.assertEqual(queue.due(now=at(0)), [])
        self.assertEqual(len(queue.due(now=at(130))), 1)

    def test_silence_guard_enqueues_low_when_idle(self):
        queue, _ = self.build()
        task = queue.enqueue(reason="t", earliest_at=at().isoformat(timespec="seconds"))
        queue.claim(task["id"], now=at(0))
        queue.complete(task["id"], now=at(1))
        self.assertEqual(queue.ensure_silence_guard(now=at(10), max_silence_hours=48,
                                                    min_interval_minutes=60), {})
        guarded = queue.ensure_silence_guard(now=at(60 * 72), max_silence_hours=48,
                                             min_interval_minutes=60)
        self.assertEqual(guarded["priority"], WAKE_LOW)
        self.assertEqual(guarded["reason"], "max_silence_guard")

    def test_plan_next_wake_clamps_and_blocks_high_on_no_action(self):
        plan = plan_next_wake(now=at(0), min_interval_minutes=60, max_silence_hours=48,
                              hint_minutes=5, priority="high", has_unfinished=False,
                              has_gain=False, no_action=True)
        self.assertEqual(plan["priority"], WAKE_LOW)
        self.assertEqual(plan["earliest_at"], at(60).isoformat(timespec="seconds"))
        self.assertTrue(plan["clamped_to_min_interval"])

    def test_plan_next_wake_normal_with_unfinished(self):
        plan = plan_next_wake(now=at(0), min_interval_minutes=60, max_silence_hours=48,
                              priority="high", has_unfinished=True, has_gain=False, no_action=True)
        self.assertEqual(plan["priority"], WAKE_NORMAL)


class RewardTest(unittest.TestCase):
    def test_llm_disabled_renormalizes_to_objective(self):
        engine = RewardEngine(llm_eval_enabled=False)
        weights = engine.effective_weights()
        self.assertEqual(weights["llm_eval"], 0.0)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=3)
        self.assertGreater(weights["objective"], weights["behavioral"])

    def test_objective_dominates(self):
        engine = RewardEngine(llm_eval_enabled=False)
        strong = engine.evaluate(information_gain=1.0, unfinished_progress=1.0)
        weak = engine.evaluate(information_gain=0.0, unfinished_progress=0.0)
        self.assertGreater(strong.reward, weak.reward)
        self.assertEqual(strong.components["llm_eval"], 0.0)
        self.assertFalse(strong.llm_eval_used)

    def test_llm_eval_capped_and_decays(self):
        engine = RewardEngine(llm_eval_enabled=True)
        first = engine.evaluate(information_gain=0.5, llm_eval_raw=1.0, topic_repeat_count=1)
        later = engine.evaluate(information_gain=0.5, llm_eval_raw=1.0, topic_repeat_count=4)
        self.assertLessEqual(first.components["llm_eval"] * first.weights["llm_eval"],
                             engine.llm_eval_cap + 1e-6)
        self.assertLess(later.components["llm_eval"], first.components["llm_eval"])

    def test_prediction_error_sign(self):
        engine = RewardEngine()
        better = engine.evaluate(information_gain=1.0, expected_reward=0.2)
        worse = engine.evaluate(information_gain=0.0, expected_reward=0.9)
        self.assertGreater(better.prediction_error, 0)
        self.assertLess(worse.prediction_error, 0)


class MotivationDecisionTest(unittest.TestCase):
    def test_low_gain_lowers_motivation(self):
        engine = MotivationEngine()
        fresh = Thought(id="t1", content="新话题", topic="音乐", information_gain=0.9, novelty=1.0,
                        is_unfinished=False)
        stale = Thought(id="t2", content="同一件事", topic="音乐", information_gain=0.0, novelty=0.1,
                        is_unfinished=False)
        class I:
            topic = "音乐"
            attraction = 0.8
            curiosity = 0.8
        high = engine.evaluate(thoughts=[fresh], interests=[I()])
        low = engine.evaluate(thoughts=[stale], interests=[I()])
        self.assertGreater(high.score, low.score)

    def test_unfinished_pulls_harder(self):
        engine = MotivationEngine()
        class I:
            topic = "x"
            attraction = 0.5
            curiosity = 0.5
        done = Thought(id="a", content="聊完了", topic="x", information_gain=0.3, novelty=0.3)
        unfinished = Thought(id="b", content="没聊完", topic="x", information_gain=0.3, novelty=0.3,
                             is_unfinished=True)
        self.assertGreater(engine.evaluate(thoughts=[unfinished], interests=[I()]).score,
                           engine.evaluate(thoughts=[done], interests=[I()]).score)

    def test_decision_threshold_and_observe_mode(self):
        class M:
            score = 0.2
            def to_dict(self):
                return {"score": self.score}

        engine = DecisionEngine(mode_getter=lambda: "observe")
        self.assertEqual(engine.decide(motivation=M()).action, NO_ACTION)

        class High:
            score = 0.9
        decision = engine.decide(motivation=High(), thought=Thought(id="x", content="想记录一句"))
        self.assertEqual(decision.action, JOURNAL_NOTE)
        self.assertEqual(decision.would_action, JOURNAL_NOTE)

    def test_decision_never_returns_message_action(self):
        class High:
            score = 1.0
        decision = DecisionEngine(mode_getter=lambda: "live").decide(
            motivation=High(), thought=Thought(id="x", content="想说话")
        )
        self.assertIn(decision.action, (NO_ACTION, JOURNAL_NOTE))


class ObservationTest(unittest.TestCase):
    def build(self, keep=500):
        tmp = Path(tempfile.mkdtemp())
        return ObservationRecorder(tmp / "observations.jsonl", keep=keep)

    def test_records_only_existing_data(self):
        recorder = self.build()
        entry = recorder.append(
            user_message="我今天好累", bot_response_excerpt="那先歇会儿",
            response_mode="CASUAL", message_count=2,
            emotion_delta={"fatigue": 0.05}, relationship_delta={"interaction_heat": 0.05},
            chat_id=1,
        )
        for key in ("user_message", "bot_response_excerpt", "response_mode", "message_count",
                    "emotion_delta", "relationship_delta", "chat_id", "timestamp"):
            self.assertIn(key, entry)
        self.assertFalse(any("summary" in key for key in entry), "观测里不应出现模型摘要字段")

    def test_mark_consumed_and_recent(self):
        recorder = self.build()
        recorder.append(user_message="一", bot_response_excerpt="二")
        self.assertEqual(len(recorder.recent(limit=5, unconsumed_only=True)), 1)
        self.assertEqual(recorder.mark_consumed("cy_test"), 1)
        self.assertEqual(recorder.recent(limit=5, unconsumed_only=True), [])

    def test_rotation_keeps_file_bounded(self):
        recorder = self.build(keep=50)
        for index in range(60):
            recorder.append(user_message=f"消息{index}", bot_response_excerpt="回")
        self.assertLessEqual(recorder.count(), 60)
        self.assertTrue((recorder.path.parent / "observations_archive.jsonl").exists())


class FakeClient:
    """假模型：返回合法 JSON 念头，不发网络请求。"""

    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    def chat(self, messages, *, tier="main", category="chat", max_tokens=None, temperature=1.0):
        self.calls += 1
        index = min(self.calls - 1, len(self.contents) - 1)
        return LLMResult(ok=True, content=self.contents[index], model="fake", tier=tier,
                         input_tokens=10, output_tokens=10)


class CycleIntegrationTest(unittest.TestCase):
    def build_stack(self, runtime=None):
        tmp = Path(tempfile.mkdtemp())
        thoughts = ThoughtStore(tmp / "thoughts.json")
        interests = InterestStore(tmp / "interests.json")
        continuity = ContinuityStore(tmp / "continuity")
        observations = ObservationRecorder(tmp / "observations.jsonl")
        journal = InnerJournal(tmp / "journal.jsonl")
        queue = WakeQueue(tmp / "wake_queue.json")
        budget = V3Budget(tmp / "budget.json")
        client = FakeClient([
            json.dumps({"thoughts": [
                {"type": "unfinished", "content": "他说最近在忙，我还没问结果",
                 "topic": "近况", "is_unfinished": True, "evidence": ["他说最近在忙"]},
            ]}, ensure_ascii=False),
            json.dumps({"thoughts": [
                {"type": "curiosity", "content": "他今天有没有好一点",
                 "topic": "近况", "is_unfinished": False, "evidence": ["他说累"]},
            ]}, ensure_ascii=False),
        ])
        cycle = CognitiveCycle(
            runtime=runtime, budget=budget, thoughts=thoughts, lifecycle=ThoughtLifecycle(thoughts),
            generator=ThoughtGenerator(client=client, budget=budget), interests=interests,
            interest_engine=InterestEngine(interests), motivation_engine=MotivationEngine(),
            decision_engine=DecisionEngine(mode_getter=lambda: "observe"),
            reward_engine=RewardEngine(llm_eval_enabled=False), continuity=continuity,
            observations=observations, journal=journal, wake_queue=queue,
        )
        return {
            "thoughts": thoughts, "interests": interests, "continuity": continuity,
            "observations": observations, "journal": journal, "queue": queue,
            "budget": budget, "client": client, "cycle": cycle,
        }

    def test_cycle_runs_without_sending_anything(self):
        stack = self.build_stack()
        stack["observations"].append(user_message="他今天说他最近在忙，还没聊完",
                                     bot_response_excerpt="那你先忙，回头说",
                                     emotion_delta={"interest": 0.05})
        result = stack["cycle"].run(trigger="test", now=at(0))
        self.assertTrue(result["ran"])
        cycle = result["cycle"]
        self.assertNotIn("sent", cycle)
        self.assertNotIn("messages", cycle)
        self.assertEqual(cycle["thoughts_created"], 1)
        self.assertEqual(stack["journal"].count(), 1)
        self.assertTrue(result["seed"]["unfinished_thought_ids"])
        self.assertGreaterEqual(result["next_wake"]["earliest_at"], at(60).isoformat(timespec="seconds"))

    def test_continuity_across_two_cycles(self):
        stack = self.build_stack()
        stack["observations"].append(user_message="他今天说他最近在忙，还没聊完",
                                     bot_response_excerpt="那你先忙，回头说")
        first = stack["cycle"].run(trigger="c1", now=at(0))
        unfinished_ids = first["seed"]["unfinished_thought_ids"]
        self.assertTrue(unfinished_ids)
        stack["observations"].append(user_message="他说今天好一点了", bot_response_excerpt="那就好")
        second = stack["cycle"].run(trigger="c2", now=at(120))
        seed = stack["continuity"].load_seed()
        self.assertEqual(seed.cycle_id, second["cycle"]["cycle_id"])
        self.assertTrue(stack["thoughts"].unfinished(limit=5))
        self.assertGreaterEqual(stack["budget"].data["cycles"], 2)
        self.assertEqual(stack["budget"].data["llm_calls"], 2)
        self.assertEqual(stack["queue"].pending_count(), 2)

    def test_budget_exhaustion_blocks_cycle(self):
        stack = self.build_stack()
        stack["budget"].data["cycles"] = 99
        result = stack["cycle"].run(trigger="blocked", now=at(0))
        self.assertFalse(result["ran"])
        self.assertIn("上限", result["skip_reason"])

    def test_thoughts_do_not_call_llm_twice(self):
        stack = self.build_stack()
        stack["observations"].append(user_message="随便聊聊", bot_response_excerpt="嗯")
        stack["cycle"].run(trigger="once", now=at(0))
        self.assertEqual(stack["client"].calls, 1, "一轮认知只允许一次 thought 调用")

    def test_observations_are_marked_consumed(self):
        stack = self.build_stack()
        stack["observations"].append(user_message="他今天说他最近在忙", bot_response_excerpt="好")
        stack["cycle"].run(trigger="c1", now=at(0))
        self.assertEqual(stack["observations"].recent(limit=5, unconsumed_only=True), [],
                         "读过的新观测必须标记，避免同一素材被反复发现")

    def test_wake_task_uses_configured_max_attempts(self):
        runtime = FakeRuntime(limits={"V3_MAX_ATTEMPTS": 2})
        stack = self.build_stack(runtime=runtime)
        stack["observations"].append(user_message="他今天说他最近在忙", bot_response_excerpt="好")
        stack["cycle"].run(trigger="c1", now=at(0))
        self.assertEqual(stack["queue"].all()[-1]["max_attempts"], 2)


if __name__ == "__main__":
    unittest.main()
