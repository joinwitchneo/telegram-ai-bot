"""Phase 6.1–6.4：Thought 数据模型 / 动力学 / 阈值 / 检查点。"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from v3.checkpoint.engine import CheckpointEngine
from v3.checkpoint.threshold import (
    BAND_ATTENTION,
    BAND_COGNITIVE,
    BAND_IGNORE,
    BAND_INCUBATING,
    Thresholds,
)
from v3.motivation.engine import MotivationEngine
from v3.thought.dynamics import DEFAULT_TRIGGER_WEIGHTS, DynamicsConfig, ThoughtDynamics
from v3.thought.models import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_BORN,
    STATE_DECAYING,
    STATE_MERGED,
    Thought,
)
from v3.thought.store import ThoughtStore
from v3.trace import TraceLog


class FakeInterest:
    def __init__(self, topic, attraction=0.5, curiosity=0.5):
        self.topic = topic
        self.attraction = attraction
        self.curiosity = curiosity


class FakeInterests:
    def __init__(self, items=()):
        self.items = list(items)

    def all(self):
        return list(self.items)


class ThoughtModelTest(unittest.TestCase):
    def test_new_thought_starts_born_and_syncs_legacy_status(self):
        thought = Thought(id="th_1", content="他最近在忙")
        self.assertEqual(thought.lifecycle_state, STATE_BORN)
        self.assertEqual(thought.status, "ephemeral")
        self.assertEqual(thought.age, 0)
        self.assertEqual(thought.activation, 0.5)
        self.assertEqual(thought.times_ignored, 0)
        self.assertTrue(thought.updated_at)

    def test_legacy_status_maps_to_phase6_state(self):
        cases = {"ephemeral": STATE_BORN, "candidate": "INCUBATING", "active": STATE_ACTIVE,
                 "revisited": STATE_ACTIVE, "reinforced": "ACTED", "pattern": "RESOLVED"}
        for legacy, expected in cases.items():
            restored = Thought.from_dict({"id": "x", "status": legacy})
            self.assertEqual(restored.lifecycle_state, expected, legacy)

    def test_set_state_keeps_legacy_status_in_sync(self):
        thought = Thought(id="th_2")
        thought.set_state(STATE_ACTIVE)
        self.assertEqual(thought.status, "active")
        thought.set_state(STATE_DECAYING)
        self.assertEqual(thought.status, "candidate")
        thought.set_state(STATE_MERGED)
        self.assertEqual(thought.status, "pattern")
        self.assertTrue(thought.is_closed())

    def test_round_trip_keeps_phase6_fields(self):
        thought = Thought(id="th_3", content="想问他考试怎么样", importance=0.8)
        restored = Thought.from_dict(json.loads(json.dumps(thought.to_dict())))
        self.assertEqual(restored.importance, 0.8)
        self.assertEqual(restored.lifecycle_state, thought.lifecycle_state)
        self.assertEqual(restored.status, thought.status)


class TriggerScoreTest(unittest.TestCase):
    def build(self, **weights):
        config = DynamicsConfig(weights={**DEFAULT_TRIGGER_WEIGHTS, **weights})
        return ThoughtDynamics(config=config)

    def test_formula_is_deterministic_and_explainable(self):
        engine = self.build()
        thought = Thought(id="a", importance=0.8, motivation=0.6, urgency=0.4,
                          curiosity=0.5, novelty=0.3, relationship_relevance=0.2,
                          persistence=0.4)
        score, components = engine.trigger_score(thought)
        self.assertEqual(components["importance"], 0.8)
        expected = round(
            0.8 * 0.25 + 0.6 * 0.25 + 0.4 * 0.15 + 0.5 * 0.10
            + 0.3 * 0.10 + 0.2 * 0.10 + 0.4 * 0.05, 4)
        self.assertAlmostEqual(score, expected, places=4)
        self.assertAlmostEqual(score, engine.trigger_score(thought)[0], places=6)

    def test_weights_are_configurable(self):
        engine = self.build(motivation=0.0, urgency=0.0, curiosity=0.0,
                            novelty=0.0, relationship_relevance=0.0, persistence=0.0)
        thought = Thought(id="a", importance=0.5, motivation=1.0)
        score, _ = engine.trigger_score(thought)
        self.assertAlmostEqual(score, 0.5, places=6, msg="只有 importance 有权重时，分数=importance")

    def test_values_are_clamped(self):
        engine = self.build()
        thought = Thought(id="a", importance=5.0, motivation=-3.0)
        score, components = engine.trigger_score(thought)
        self.assertLessEqual(score, 1.0)
        self.assertEqual(components["motivation"], 0.0)
        self.assertEqual(components["importance"], 1.0)


class DynamicsTest(unittest.TestCase):
    def test_important_thoughts_decay_slower(self):
        engine = ThoughtDynamics()
        fragile = Thought(id="a", importance=0.1)
        important = Thought(id="b", importance=0.9)
        for _ in range(5):
            engine.tick(fragile)
            engine.tick(important)
        self.assertGreater(important.activation, fragile.activation)
        self.assertEqual(fragile.age, 5)

    def test_activation_rises_on_related_event(self):
        engine = ThoughtDynamics()
        thought = Thought(id="a", activation=0.2, persistence=0.3)
        engine.activate(thought, strength=0.3)
        self.assertGreater(thought.activation, 0.2)
        self.assertGreater(thought.persistence, 0.3)
        self.assertEqual(thought.times_activated, 1)
        self.assertTrue(thought.last_activated_at)

    def test_decaying_thought_is_revived_by_activation(self):
        engine = ThoughtDynamics()
        thought = Thought(id="a", activation=0.01)
        engine.tick(thought)
        self.assertEqual(thought.lifecycle_state, STATE_DECAYING)
        engine.activate(thought, strength=0.4)
        self.assertEqual(thought.lifecycle_state, STATE_ACTIVE)

    def test_unfinished_thought_gets_urgent_over_time(self):
        """没想完的事会越来越惦记：这是"不会永久睡死"的核心机制。"""
        engine = ThoughtDynamics()
        thought = Thought(id="a", is_unfinished=True, importance=0.8, activation=0.6)
        first = engine.trigger_score(thought)[0]
        for _ in range(10):
            engine.tick(thought)
            engine.motivation = 0.6
            thought.motivation = 0.6
        later = engine.trigger_score(thought)[0]
        self.assertGreater(thought.urgency, 0.0)
        self.assertGreater(later, first, "同样的念头，放久了触发分要升高而不是降为 0")

    def test_link_activates_and_records_relation(self):
        engine = ThoughtDynamics()
        left = Thought(id="a", content="用户最近很忙", activation=0.3)
        right = Thought(id="b", content="用户今天没说话", activation=0.2)
        result = engine.link(left, right, kind="supports")
        self.assertIn("b", left.related_ids)
        self.assertIn("a", right.related_ids)
        self.assertEqual(left.metadata["links"]["b"], "supports")
        self.assertGreater(right.activation, 0.2)
        self.assertEqual(result["kind"], "supports")

    def test_merge_archives_the_other_thought(self):
        engine = ThoughtDynamics()
        primary = Thought(id="a", content="", importance=0.3, activation=0.2)
        other = Thought(id="b", content="他考试考完了", importance=0.7, activation=0.6,
                        seen_count=3)
        engine.merge(primary, other)
        self.assertEqual(primary.content, "他考试考完了")
        self.assertEqual(primary.importance, 0.7)
        self.assertEqual(other.lifecycle_state, STATE_MERGED)
        self.assertEqual(other.metadata["merged_into"], "a")

    def test_archive_marks_state(self):
        engine = ThoughtDynamics()
        thought = Thought(id="a")
        engine.archive(thought, reason="太久没动")
        self.assertEqual(thought.lifecycle_state, STATE_ARCHIVED)
        self.assertEqual(thought.metadata["archived_reason"], "太久没动")

    def test_outcome_updates_counters(self):
        engine = ThoughtDynamics()
        thought = Thought(id="a", activation=0.5, motivation=0.5)
        engine.apply_outcome(thought, status="SENT", positive=True)
        self.assertEqual(thought.times_acted, 1)
        self.assertEqual(thought.last_outcome, "SENT")
        engine.apply_outcome(thought, status="REJECTED", positive=False)
        self.assertEqual(thought.times_ignored, 1)


class ThresholdTest(unittest.TestCase):
    def test_bands(self):
        thresholds = Thresholds()
        self.assertEqual(thresholds.band(0.10), BAND_IGNORE)
        self.assertEqual(thresholds.band(0.30), BAND_INCUBATING)
        self.assertEqual(thresholds.band(0.60), BAND_ATTENTION)
        self.assertEqual(thresholds.band(0.85), BAND_COGNITIVE)

    def test_from_runtime_reads_config(self):
        class Runtime:
            def limit(self, name, default):
                return {"V3_THRESHOLD_COGNITIVE": 0.9}.get(name, float(default))

        thresholds = Thresholds.from_runtime(Runtime())
        self.assertEqual(thresholds.cognitive, 0.9)
        self.assertEqual(thresholds.band(0.85), BAND_ATTENTION)


class CheckpointEngineTest(unittest.TestCase):
    def build(self, thresholds=None, dynamics=None):
        tmp = Path(tempfile.mkdtemp())
        store = ThoughtStore(tmp / "thoughts.json")
        trace = TraceLog(tmp / "events.jsonl")
        engine = CheckpointEngine(
            thoughts=store, dynamics=dynamics or ThoughtDynamics(),
            thresholds=thresholds or Thresholds(),
            motivation_engine=MotivationEngine(),
            interests=FakeInterests([FakeInterest("考试", attraction=0.8, curiosity=0.8)]),
            trace=trace, log_path=tmp / "checkpoints.jsonl",
        )
        return store, engine, trace, tmp

    def test_low_value_thought_does_not_trigger(self):
        store, engine, trace, _ = self.build()
        store.add(Thought(id="", content="随口一提的小事", topic="闲聊",
                          importance=0.1, novelty=0.05, curiosity=0.1, activation=0.1))
        record = engine.tick()
        self.assertFalse(record.triggered)
        self.assertEqual(record.band, BAND_IGNORE)
        self.assertEqual(record.llm_calls, 0, "检查点不允许调用 LLM")

    def test_high_value_thought_triggers_cognition(self):
        store, engine, _, _ = self.build()
        store.add(Thought(id="", content="他明天考试，我想问问", topic="考试",
                          is_unfinished=True, importance=0.95, urgency=0.9, novelty=0.8,
                          curiosity=0.9, activation=0.9, persistence=0.9,
                          information_gain=0.6, relationship_relevance=0.8))
        record = engine.tick()
        self.assertTrue(record.triggered)
        self.assertEqual(record.band, BAND_COGNITIVE)
        self.assertGreaterEqual(record.top["trigger_score"], 0.80)
        self.assertIn("考试", record.top["topic"])

    def test_checkpoint_writes_log_and_trace_without_llm(self):
        store, engine, trace, tmp = self.build()
        store.add(Thought(id="", content="他在忙", topic="近况", importance=0.5))
        engine.tick()
        log = (tmp / "checkpoints.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(log), 1)
        payload = json.loads(log[0])
        self.assertEqual(payload["evaluated"], 1)
        self.assertTrue(payload["trace_id"])
        kinds = [item["kind"] for item in trace.recent(limit=20)]
        self.assertIn("checkpoint_started", kinds)
        self.assertIn("threshold_evaluated", kinds)
        self.assertTrue(all("timestamp" in item for item in trace.recent(limit=5)))

    def test_closed_thoughts_are_skipped(self):
        store, engine, _, _ = self.build()
        thought, _ = store.add(Thought(id="", content="已经结束的事", topic="旧事"))
        thought.set_state(STATE_ARCHIVED)
        store.items[thought.id] = thought.to_dict()
        store.save()
        record = engine.tick()
        self.assertEqual(record.evaluated, 0)
        self.assertFalse(record.triggered)

    def test_interaction_gap_feeds_motivation(self):
        store, engine, _, _ = self.build()
        store.add(Thought(id="", content="想问问他", topic="考试", is_unfinished=True,
                          importance=0.7, novelty=0.5))
        far = engine.tick(interaction_gap_hours=48.0)
        self.assertGreater(far.top["motivation"], 0.0)


if __name__ == "__main__":
    unittest.main()
