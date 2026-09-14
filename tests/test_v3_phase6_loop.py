"""Phase 6.5–6.13：认知接口 / 决策 / 行动 / 校验 / 预算 / 闭环（含 §32 的实验 A–E）。"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from core.llm_client import LLMResult
from v3.actions.base import (
    JOURNAL,
    MESSAGE,
    NO_ACTION,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SENT,
    STATUS_SIMULATED,
    ActionProvider,
    ActionRegistry,
    ActionRequest,
)
from v3.actions.journal import JournalAction
from v3.actions.message import MessageAction
from v3.checkpoint.engine import CheckpointEngine
from v3.checkpoint.threshold import Thresholds
from v3.cognition.base import Intention, MockCognitiveProvider
from v3.cognition.context import CognitiveContextBuilder
from v3.cognition.deepseek import DeepSeekCognitiveProvider
from v3.cognition.validator import validate_proposal
from v3.environments.telegram import TelegramEnvironment
from v3.interest.engine import InterestEngine
from v3.interest.store import InterestStore
from v3.journal import InnerJournal
from v3.life_cycle import LifeCycle
from v3.motivation.engine import MotivationEngine
from v3.observation.recorder import ObservationRecorder
from v3.outcome import FeedbackEngine, OutcomeStore
from v3.policies.budget import ActionBudget
from v3.policies.decision import ActionDecisionEngine
from v3.policies.validator import ActionValidator
from v3.reward.engine import RewardEngine
from v3.scheduler.queue import WakeQueue
from v3.thought.dynamics import ThoughtDynamics
from v3.thought.models import STATE_ACTIVE, Thought
from v3.thought.store import ThoughtStore
from v3.trace import TraceLog

BASE = datetime.datetime(2026, 9, 14, 10, 0, 0)


def at(minutes: int = 0) -> datetime.datetime:
    return BASE + datetime.timedelta(minutes=minutes)


class FakeRuntime:
    def __init__(self, mode="observe", limits=None):
        self._mode = mode
        self._limits = {"V3_MIN_WAKE_INTERVAL_MINUTES": 60, "V3_MAX_SILENCE_HOURS": 48,
                        "V3_MAX_ATTEMPTS": 3, **(limits or {})}

    def mode(self):
        return self._mode

    def limit(self, name, default):
        return float(self._limits.get(name, default))

    def limit_int(self, name, default):
        return int(self._limits.get(name, default))


class FakeBudget:
    def __init__(self, *, llm_calls=6):
        self.data = {"llm_calls": 0, "cycles": 0}
        self.llm_calls = llm_calls
        self.chain = 0
        self.max_chain = 3

    def can_cycle(self):
        return True, ""

    def spend_cycle(self):
        self.data["cycles"] += 1

    def begin_chain(self):
        self.chain = 0

    def can_llm(self):
        if self.data["llm_calls"] >= self.llm_calls:
            return False, "今日自主 LLM 调用已达上限"
        if self.chain >= self.max_chain:
            return False, "本轮认知链长已达上限"
        return True, ""

    def spend_llm(self, n=1):
        self.data["llm_calls"] += n
        self.chain += n


class RecordingSender:
    def __init__(self, *, fail=False):
        self.sent = []
        self.fail = fail

    def __call__(self, chat_id, text):
        if self.fail:
            raise RuntimeError("网络断了")
        self.sent.append((chat_id, text))


def build_life(*, mode="observe", dry_run=False, provider=None, intent="journal",
               confidence=0.8, content="想跟他说一句", sender=None, thresholds=None,
               chat_id=1, action_budget=None):
    tmp = Path(tempfile.mkdtemp())
    thoughts = ThoughtStore(tmp / "thoughts.json")
    interests = InterestStore(tmp / "interests.json")
    journal = InnerJournal(tmp / "journal.jsonl")
    trace = TraceLog(tmp / "events.jsonl")
    outcomes = OutcomeStore(tmp / "outcomes.jsonl")
    dynamics = ThoughtDynamics()
    thresholds = thresholds or Thresholds()
    checkpoint = CheckpointEngine(
        thoughts=thoughts, dynamics=dynamics, thresholds=thresholds,
        motivation_engine=MotivationEngine(), interests=interests, trace=trace,
        log_path=tmp / "checkpoints.jsonl",
    )
    environment = TelegramEnvironment(sender=sender, dry_run=dry_run)
    registry = ActionRegistry()
    registry.register(JournalAction(journal=journal))
    registry.register(MessageAction(environment=environment, chat_id=chat_id))
    runtime = FakeRuntime(mode=mode)
    action_budget = action_budget or ActionBudget(
        tmp / "action_budget.json", daily_message_limit=3, message_cooldown_minutes=90,
        cognitive_cooldown_minutes=0, daily_cognitive_limit=12,
    )
    validator = ActionValidator(registry=registry, budget=action_budget, environment=environment,
                                mode_getter=runtime.mode)
    provider = provider or MockCognitiveProvider(
        intention_type=intent, confidence=confidence, content=content)
    context_builder = CognitiveContextBuilder(
        thoughts=thoughts, interests=interests, observations=None, outcomes=outcomes,
        available_actions=registry.kinds, mode_getter=runtime.mode)
    feedback = FeedbackEngine(dynamics=dynamics, reward_engine=RewardEngine(),
                              interests=interests, interest_engine=InterestEngine(interests),
                              trace=trace)
    life = LifeCycle(
        runtime=runtime, budget=FakeBudget(), checkpoint=checkpoint,
        context_builder=context_builder, provider=provider,
        decision_engine=ActionDecisionEngine(mode_getter=runtime.mode, action_threshold=0.80,
                                             min_confidence=0.60),
        registry=registry, validator=validator, action_budget=action_budget,
        outcomes=outcomes, feedback=feedback, thoughts=thoughts, dynamics=dynamics,
        interests=interests, interest_engine=InterestEngine(interests),
        continuity=__import__("v3.continuity.store", fromlist=["ContinuityStore"]).ContinuityStore(tmp / "continuity"),
        journal=journal, wake_queue=WakeQueue(tmp / "wake_queue.json"), trace=trace,
        observations=ObservationRecorder(tmp / "observations.jsonl"), environment=environment,
        chat_id=chat_id, log_path=tmp / "life_cycle.jsonl",
    )
    return {
        "life": life, "thoughts": thoughts, "journal": journal, "trace": trace,
        "outcomes": outcomes, "registry": registry, "environment": environment,
        "provider": provider, "action_budget": action_budget, "tmp": tmp,
        "dynamics": dynamics, "interests": interests,
    }


def strong_thought(store, **overrides):
    data = dict(content="他明天考试，我想问问", topic="考试", is_unfinished=True,
                importance=0.95, urgency=0.9, novelty=0.8, curiosity=0.9,
                activation=0.9, persistence=0.9, information_gain=0.6,
                relationship_relevance=0.8)
    data.update(overrides)
    thought, _ = store.add(Thought(id="", **data))
    return thought


class ProposalValidatorTest(unittest.TestCase):
    def test_unknown_intention_is_downgraded(self):
        _, _, intention, rejected = validate_proposal(
            {"intention": {"type": "DELETE_FILES", "confidence": 0.9}})
        self.assertEqual(intention.type, "none")
        self.assertTrue(any("未知意图" in reason for reason in rejected))

    def test_message_without_content_becomes_none(self):
        _, _, intention, rejected = validate_proposal(
            {"intention": {"type": "message", "confidence": 0.9, "content": "  "}})
        self.assertEqual(intention.type, "none")
        self.assertTrue(any("没给内容" in reason for reason in rejected))

    def test_hints_are_clamped_and_unknown_fields_rejected(self):
        _, update, _, rejected = validate_proposal({
            "thought_update": {"importance": 5, "curiosity": -2, "hacked": 1},
        })
        self.assertEqual(update["importance"], 1.0)
        self.assertEqual(update["curiosity"], 0.0)
        self.assertNotIn("hacked", update)
        self.assertTrue(any("不允许的字段" in reason for reason in rejected))

    def test_cot_and_unsupported_thoughts_are_rejected(self):
        accepted, _, _, rejected = validate_proposal({
            "new_thoughts": [
                {"type": "curiosity", "content": "让我一步步分析：思维链如下",
                 "evidence": ["x"]},
                {"type": "curiosity", "content": "他考试考完了吗", "evidence": ["他说考完了"]},
            ]})
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["content"], "他考试考完了吗")
        self.assertTrue(rejected)

    def test_new_thought_hints_are_kept_for_core_to_use(self):
        accepted, _, _, _ = validate_proposal({
            "new_thoughts": [{"type": "curiosity", "content": "想问问他", "evidence": ["他说明天考"],
                              "importance_hint": 0.9}]})
        self.assertEqual(accepted[0]["importance_hint"], 0.9)


class CognitiveProviderTest(unittest.TestCase):
    def test_mock_provider_returns_proposal_without_llm(self):
        provider = MockCognitiveProvider(intention_type="message", confidence=0.9,
                                         content="在忙吗")
        result = provider.process(
            __import__("v3.cognition.base", fromlist=["CognitiveContext"]).CognitiveContext())
        self.assertTrue(result.ok)
        self.assertEqual(result.intention.type, "message")
        self.assertEqual(result.llm_calls, 0)

    def test_deepseek_provider_returns_proposal(self):
        payload = json.dumps({
            "thought_update": {"importance": 0.9},
            "new_thoughts": [{"type": "curiosity", "content": "他考得怎么样",
                              "evidence": ["他明天考试"]}],
            "intention": {"type": "message", "reason": "想关心一下",
                          "confidence": 0.8, "content": "考完记得跟我说"},
        }, ensure_ascii=False)

        class Client:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, **kwargs):
                self.calls += 1
                return LLMResult(ok=True, content=payload, model="fake", tier="cheap",
                                 input_tokens=1, output_tokens=1)

        client = Client()
        budget = FakeBudget()
        provider = DeepSeekCognitiveProvider(client=client, budget=budget)
        context = __import__("v3.cognition.base", fromlist=["CognitiveContext"]).CognitiveContext(
            candidate={"content": "他明天考试", "topic": "考试", "trigger_score": 0.9})
        result = provider.process(context)
        self.assertTrue(result.ok)
        self.assertEqual(result.intention.type, "message")
        self.assertEqual(len(result.new_thoughts), 1)
        self.assertEqual(result.llm_calls, 1)
        self.assertEqual(budget.data["llm_calls"], 1, "调用要记账")

    def test_deepseek_provider_respects_budget(self):
        class Client:
            def chat(self, messages, **kwargs):
                raise AssertionError("预算不允许时不该调用模型")

        provider = DeepSeekCognitiveProvider(client=Client(), budget=FakeBudget(llm_calls=0))
        context = __import__("v3.cognition.base", fromlist=["CognitiveContext"]).CognitiveContext()
        result = provider.process(context)
        self.assertFalse(result.ok)
        self.assertIn("上限", result.error)

    def test_bad_json_is_not_an_action(self):
        class Client:
            def chat(self, messages, **kwargs):
                return LLMResult(ok=True, content="我觉得应该主动找她聊聊", model="fake",
                                 tier="cheap", input_tokens=1, output_tokens=1)

        provider = DeepSeekCognitiveProvider(client=Client(), budget=FakeBudget())
        context = __import__("v3.cognition.base", fromlist=["CognitiveContext"]).CognitiveContext()
        result = provider.process(context)
        self.assertFalse(result.ok)
        self.assertEqual(result.intention.type, "none")

    def test_context_builder_limits_what_goes_to_llm(self):
        tmp = Path(tempfile.mkdtemp())
        store = ThoughtStore(tmp / "t.json")
        for index in range(8):
            store.add(Thought(id="", content=f"考试相关 {index}", topic="考试"))
        builder = CognitiveContextBuilder(thoughts=store, max_related=3, max_observations=0)
        context = builder.build(candidate={"thought_id": "th_000001", "topic": "考试"})
        self.assertLessEqual(len(context.related_thoughts), 3)
        self.assertLessEqual(len(context.summary_lines()), 8)


class DecisionTest(unittest.TestCase):
    def build(self, mode="dry_run", **kwargs):
        return ActionDecisionEngine(mode_getter=lambda: mode, **kwargs)

    def result(self, kind, confidence=0.8, content="想说话", ok=True):
        class R:
            pass

        r = R()
        r.ok = ok
        r.error = "" if ok else "模型失败"
        r.intention = Intention(type=kind, confidence=confidence, content=content, reason="r")
        r.trace_id = "tr_x"
        return r

    def test_failed_cognition_means_no_action(self):
        decision = self.build().decide(candidate={"thought_id": "t", "trigger_score": 0.9},
                                       result=self.result("message", ok=False))
        self.assertEqual(decision.action, NO_ACTION)

    def test_none_intention_means_no_action(self):
        decision = self.build().decide(candidate={"thought_id": "t", "trigger_score": 0.9},
                                       result=self.result("none"))
        self.assertEqual(decision.action, NO_ACTION)

    def test_low_score_downgrades_message_to_journal(self):
        decision = self.build().decide(candidate={"thought_id": "t", "trigger_score": 0.5},
                                       result=self.result("message"))
        self.assertEqual(decision.action, JOURNAL)
        self.assertEqual(decision.would_action, MESSAGE)

    def test_low_confidence_downgrades_message_to_journal(self):
        decision = self.build().decide(candidate={"thought_id": "t", "trigger_score": 0.9},
                                       result=self.result("message", confidence=0.2))
        self.assertEqual(decision.action, JOURNAL)

    def test_observe_mode_never_messages(self):
        decision = self.build(mode="observe").decide(
            candidate={"thought_id": "t", "trigger_score": 0.95},
            result=self.result("message"))
        self.assertEqual(decision.action, JOURNAL)
        self.assertEqual(decision.would_action, MESSAGE)

    def test_active_mode_can_message(self):
        decision = self.build(mode="live").decide(
            candidate={"thought_id": "t", "trigger_score": 0.95},
            result=self.result("message"))
        self.assertEqual(decision.action, MESSAGE)

    def test_journal_intention_maps_to_journal(self):
        decision = self.build(mode="live").decide(
            candidate={"thought_id": "t", "trigger_score": 0.95},
            result=self.result("journal"))
        self.assertEqual(decision.action, JOURNAL)


class ActionLayerTest(unittest.TestCase):
    def test_dry_run_environment_does_not_really_send(self):
        sender = RecordingSender()
        environment = TelegramEnvironment(sender=sender, dry_run=True)
        action = MessageAction(environment=environment, chat_id=1)
        outcome = action.execute(ActionRequest(action_id="a1", kind=MESSAGE,
                                               payload={"content": "在吗"}))
        self.assertEqual(outcome.status, STATUS_SIMULATED)
        self.assertEqual(sender.sent, [])
        self.assertEqual(len(environment.sent), 1)

    def test_real_environment_sends_once(self):
        sender = RecordingSender()
        environment = TelegramEnvironment(sender=sender, dry_run=False)
        action = MessageAction(environment=environment, chat_id=7)
        outcome = action.execute(ActionRequest(action_id="a1", kind=MESSAGE,
                                               payload={"content": "在吗"}))
        self.assertEqual(outcome.status, STATUS_SENT)
        self.assertEqual(sender.sent, [(7, "在吗")])

    def test_send_failure_becomes_failed_outcome(self):
        environment = TelegramEnvironment(sender=RecordingSender(fail=True))
        action = MessageAction(environment=environment, chat_id=1)
        outcome = action.execute(ActionRequest(action_id="a1", kind=MESSAGE,
                                               payload={"content": "在吗"}))
        self.assertEqual(outcome.status, STATUS_FAILED)
        self.assertIn("网络断了", outcome.error)

    def test_registry_rejects_unknown_kind_without_crashing(self):
        registry = ActionRegistry()
        outcome = registry.execute(ActionRequest(action_id="a1", kind="DESKTOP_ACTION"))
        self.assertEqual(outcome.status, STATUS_REJECTED)

    def test_provider_exception_becomes_failed_outcome(self):
        class Boom(ActionProvider):
            kind = "BOOM"

            def execute(self, request):
                raise RuntimeError("炸了")

        registry = ActionRegistry()
        registry.register(Boom())
        outcome = registry.execute(ActionRequest(action_id="a1", kind="BOOM"))
        self.assertEqual(outcome.status, STATUS_FAILED)


class ActionValidatorTest(unittest.TestCase):
    def build(self, *, mode="live", daily_limit=3, cooldown=90, dry_run=True):
        tmp = Path(tempfile.mkdtemp())
        environment = TelegramEnvironment(sender=None, dry_run=dry_run)
        registry = ActionRegistry()
        registry.register(JournalAction(journal=InnerJournal(tmp / "j.jsonl")))
        registry.register(MessageAction(environment=environment, chat_id=1))
        budget = ActionBudget(tmp / "b.json", daily_message_limit=daily_limit,
                              message_cooldown_minutes=cooldown, cognitive_cooldown_minutes=0)
        validator = ActionValidator(registry=registry, budget=budget, environment=environment,
                                    mode_getter=lambda: mode)
        return validator, budget

    def test_observe_mode_rejects_outward_action(self):
        validator, _ = self.build(mode="observe")
        verdict = validator.check(ActionRequest(action_id="a", kind=MESSAGE,
                                                payload={"content": "hi", "chat_id": 1}))
        self.assertFalse(verdict.ok)
        self.assertIn("观察档", verdict.reason)

    def test_daily_limit_blocks(self):
        validator, budget = self.build(daily_limit=0)
        verdict = validator.check(ActionRequest(action_id="a", kind=MESSAGE,
                                                payload={"content": "hi", "chat_id": 1}))
        self.assertFalse(verdict.ok)
        self.assertIn("上限 0", verdict.reason)

    def test_cooldown_blocks_too_soon(self):
        validator, budget = self.build(cooldown=90)
        budget.note_message(now=at(0))
        verdict = validator.check(ActionRequest(action_id="a", kind=MESSAGE,
                                                payload={"content": "hi", "chat_id": 1}),
                                  now=at(10))
        self.assertFalse(verdict.ok)
        self.assertIn("冷却", verdict.reason)
        verdict_later = validator.check(ActionRequest(action_id="a", kind=MESSAGE,
                                                      payload={"content": "hi", "chat_id": 1}),
                                        now=at(200))
        self.assertTrue(verdict_later.ok)

    def test_long_message_rejected(self):
        validator, _ = self.build()
        verdict = validator.check(ActionRequest(action_id="a", kind=MESSAGE,
                                                payload={"content": "字" * 900, "chat_id": 1}))
        self.assertFalse(verdict.ok)
        self.assertIn("过长", verdict.reason)

    def test_journal_is_always_allowed(self):
        validator, _ = self.build(mode="observe")
        verdict = validator.check(ActionRequest(action_id="a", kind=JOURNAL,
                                                payload={"content": "记一笔"}))
        self.assertTrue(verdict.ok)


class ExperimentTest(unittest.TestCase):
    """设计文档 §32 的 A–E 五个实验。"""

    def test_experiment_a_no_action_is_cheap(self):
        stack = build_life()
        stack["thoughts"].add(Thought(id="", content="随口一提的小事", topic="闲聊",
                                      importance=0.1, novelty=0.05, curiosity=0.1,
                                      activation=0.1))
        for index in range(6):
            result = stack["life"].run(trigger="cp", now=at(index * 5))
            self.assertFalse(result["triggered"])
        self.assertEqual(stack["provider"].calls, 0, "没到阈值就不许调用认知器官")
        self.assertEqual(stack["action_budget"].data.cognitive_calls, 0)
        self.assertEqual(stack["journal"].count(), 0, "低价值念头不该产生任何输出")

    def test_experiment_b_threshold_is_decisive(self):
        stack = build_life()
        mild = Thought(id="", content="也许可以聊聊", topic="近况", importance=0.5,
                       activation=0.4, novelty=0.3)
        stack["thoughts"].add(mild)
        self.assertFalse(stack["life"].run(trigger="cp", now=at(0))["triggered"])
        strong_thought(stack["thoughts"])
        self.assertTrue(stack["life"].run(trigger="cp", now=at(5))["triggered"])

    def test_experiment_c_thought_persists_and_changes(self):
        stack = build_life(intent="none")
        thought = strong_thought(stack["thoughts"], importance=0.6, urgency=0.1,
                                 activation=0.5, information_gain=0.2, novelty=0.3)
        first = stack["life"].run(trigger="c1", now=at(0))
        self.assertTrue(first["ran"])
        self.assertTrue(stack["thoughts"].get(thought.id))
        later = stack["thoughts"].get(thought.id)
        self.assertGreater(later.age, 0)
        self.assertGreater(later.urgency, 0.1, "没想完的事会越来越惦记")
        self.assertGreater(later.trigger_score, 0.0)

    def test_experiment_d_autonomous_message_in_dry_run(self):
        sender = RecordingSender()
        stack = build_life(mode="dry_run", dry_run=True, intent="message",
                           confidence=0.9, content="考完记得跟我说一声", sender=sender)
        strong_thought(stack["thoughts"])
        result = stack["life"].run(trigger="autonomous", now=at(0))
        self.assertTrue(result["triggered"])
        decision = result["cycle"]["decision"]
        self.assertEqual(decision["action"], MESSAGE)
        outcome = result["cycle"]["outcome"]
        self.assertEqual(outcome["status"], STATUS_SIMULATED)
        self.assertEqual(sender.sent, [], "dry_run 绝不允许真的发出去")
        self.assertEqual(len(stack["environment"].sent), 1)

    def test_observe_mode_records_but_never_sends(self):
        sender = RecordingSender()
        stack = build_life(mode="observe", dry_run=False, intent="message",
                           confidence=0.9, content="在忙吗", sender=sender)
        strong_thought(stack["thoughts"])
        result = stack["life"].run(trigger="autonomous", now=at(0))
        self.assertEqual(result["cycle"]["decision"]["action"], JOURNAL)
        self.assertEqual(result["cycle"]["decision"]["would_action"], MESSAGE)
        self.assertEqual(sender.sent, [], "观察档不允许发出去")
        self.assertEqual(stack["journal"].count(), 1, "但要把「本来会说的话」记下来")

    def test_experiment_e_feedback_differs_by_outcome(self):
        sent_sender = RecordingSender()
        sent_stack = build_life(mode="live", dry_run=False, intent="message",
                                confidence=0.9, content="考完了吗", sender=sent_sender)
        thought = strong_thought(sent_stack["thoughts"])
        sent_result = sent_stack["life"].run(trigger="t", now=at(0))
        sent_after = sent_stack["thoughts"].get(thought.id)

        blocked_stack = build_life(
            mode="live", dry_run=True, intent="message", confidence=0.9, content="考完了吗",
            action_budget=ActionBudget(Path(tempfile.mkdtemp()) / "b.json",
                                      daily_message_limit=0, message_cooldown_minutes=0,
                                      cognitive_cooldown_minutes=0))
        blocked_thought = strong_thought(blocked_stack["thoughts"])
        blocked_result = blocked_stack["life"].run(trigger="t", now=at(0))
        blocked_after = blocked_stack["thoughts"].get(blocked_thought.id)

        self.assertEqual(sent_result["cycle"]["outcome"]["status"], STATUS_SENT)
        # 预算/冷却现在是"决策前的条件"（§九）：不够条件就降级成记日志，而不是发出去再被拒
        self.assertEqual(blocked_result["cycle"]["decision"]["action"], JOURNAL)
        self.assertEqual(blocked_result["cycle"]["decision"]["would_action"], MESSAGE)
        self.assertNotEqual(sent_after.last_outcome, blocked_after.last_outcome)
        self.assertGreater(sent_after.times_acted, blocked_after.times_acted)
        self.assertGreater(sent_after.activation, blocked_after.activation)

    def test_every_cycle_is_reconstructable_from_trace(self):
        stack = build_life(mode="dry_run", dry_run=True, intent="message",
                           confidence=0.9, content="在吗")
        strong_thought(stack["thoughts"])
        result = stack["life"].run(trigger="trace-me", now=at(0))
        chain = stack["trace"].by_trace(result["trace_id"])
        kinds = [item["kind"] for item in chain]
        for expected in ("checkpoint_started", "threshold_evaluated", "cognitive_triggered",
                         "cognitive_result", "decision_made", "action_requested",
                         "action_validated", "action_executed", "outcome_received",
                         "state_changed"):
            self.assertIn(expected, kinds, f"trace 里缺 {expected}")
        self.assertTrue(all(item["timestamp"] for item in chain))


if __name__ == "__main__":
    unittest.main()
