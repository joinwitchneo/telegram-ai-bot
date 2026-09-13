import datetime
import json
import re
import tempfile
import unittest
from pathlib import Path

from core.event_bus import EventBus
from relationship import relationship_events as events_mod
from relationship.relationship_engine import ALLOWED_SOURCES, RelationshipEngine


def make_engine(bus=None, inertia=0.35):
    tmp = Path(tempfile.mkdtemp())
    return RelationshipEngine(tmp / "relationship_state.json", bus=bus, inertia=inertia)


class RelationshipInitialTest(unittest.TestCase):
    def test_five_dimensions_at_baseline(self):
        engine = make_engine()
        dims = engine.dimensions()
        self.assertEqual(set(dims), set(events_mod.DIMENSIONS))
        for name, value in events_mod.BASELINE.items():
            self.assertAlmostEqual(dims[name], value, places=6)


class RelationshipEventTest(unittest.TestCase):
    def test_normal_chat_raises_heat_and_familiarity(self):
        engine = make_engine()
        engine.apply_event("USER_CHAT")
        self.assertGreater(engine.value("interaction_heat"), events_mod.BASELINE["interaction_heat"])
        self.assertGreater(engine.value("familiarity"), events_mod.BASELINE["familiarity"])

    def test_care_raises_trust_and_intimacy(self):
        engine = make_engine()
        engine.apply_event("USER_CARE")
        self.assertGreater(engine.value("trust"), events_mod.BASELINE["trust"])
        self.assertGreater(engine.value("intimacy"), events_mod.BASELINE["intimacy"])

    def test_insult_lowers_trust(self):
        engine = make_engine()
        for _ in range(5):
            engine.apply_event("USER_INSULT")
        self.assertLess(engine.value("trust"), events_mod.BASELINE["trust"])

    def test_agreement_completed_raises_shared_experience(self):
        engine = make_engine()
        engine.apply_event("AGREEMENT_COMPLETED")
        self.assertGreater(engine.value("shared_experience"), events_mod.BASELINE["shared_experience"])

    def test_different_speeds(self):
        """热度是快变量，亲密度是慢变量。"""
        engine = make_engine()
        engine.apply_event("USER_CHAT")
        heat_gain = engine.value("interaction_heat") - events_mod.BASELINE["interaction_heat"]
        familiarity_gain = engine.value("familiarity") - events_mod.BASELINE["familiarity"]
        self.assertGreater(heat_gain, familiarity_gain * 5)

    def test_hundred_turns_do_not_max_intimacy(self):
        engine = make_engine()
        for _ in range(100):
            engine.apply_event("USER_CHAT")
        self.assertLess(engine.value("intimacy"), 0.2)      # 聊天不会刷满亲密
        self.assertLess(engine.value("trust"), 0.5)
        self.assertLess(engine.value("familiarity"), 0.5)

    def test_bounds_with_extremes(self):
        engine = make_engine()
        for _ in range(500):
            engine.apply_event("USER_CARE")
        for value in engine.dimensions().values():
            self.assertLessEqual(value, 1.0)
        for _ in range(500):
            engine.apply_event("USER_INSULT")
        for value in engine.dimensions().values():
            self.assertGreaterEqual(value, 0.0)

    def test_no_randomness(self):
        first, second = make_engine(), make_engine()
        for _ in range(3):
            first.apply_event("USER_CARE")
            second.apply_event("USER_CARE")
        self.assertEqual(first.dimensions(), second.dimensions())


class RelationshipDecayTest(unittest.TestCase):
    def test_heat_decays_but_trust_does_not(self):
        engine = make_engine()
        for _ in range(6):
            engine.apply_event("USER_CARE")
        heat_before = engine.value("interaction_heat")
        trust_before = engine.value("trust")
        engine.tick(hours=48)
        self.assertLess(engine.value("interaction_heat"), heat_before)
        self.assertAlmostEqual(engine.value("trust"), trust_before, places=4)

    def test_shared_experience_does_not_decay(self):
        engine = make_engine()
        engine.apply_event("SHARED_EVENT")
        before = engine.value("shared_experience")
        engine.tick(hours=24 * 30)
        self.assertAlmostEqual(engine.value("shared_experience"), before, places=4)

    def test_heat_returns_near_baseline(self):
        engine = make_engine()
        for _ in range(10):
            engine.apply_event("USER_CHAT")
        engine.tick(hours=24 * 7)
        self.assertLess(
            abs(engine.value("interaction_heat") - events_mod.BASELINE["interaction_heat"]), 0.05
        )

    def test_time_steps_case4(self):
        """规范的 Case 4：1h / 6h / 24h / 3d / 7d。"""
        engine = make_engine()
        for _ in range(4):
            engine.apply_event("USER_CARE")
        series = [round(engine.value("interaction_heat"), 4)]
        for hours in (1, 6, 24, 72, 168):
            engine.tick(hours=hours)
            series.append(round(engine.value("interaction_heat"), 4))
        self.assertEqual(series, sorted(series, reverse=True))
        self.assertGreater(engine.value("trust"), events_mod.BASELINE["trust"])


class RelationshipOutputTest(unittest.TestCase):
    def test_describe_has_no_numbers(self):
        engine = make_engine()
        for _ in range(20):
            engine.apply_event("USER_CARE")
        text = engine.describe()
        self.assertFalse(re.search(r"\d", text), text)
        self.assertIn("关系", text)

    def test_behavior_hints_limited(self):
        engine = make_engine()
        for _ in range(20):
            engine.apply_event("USER_CARE")
        self.assertLessEqual(len(engine.behavior_hints()), 3)

    def test_is_close(self):
        engine = make_engine()
        self.assertFalse(engine.is_close())
        for _ in range(200):
            engine.apply_event("USER_CHAT")     # 熟悉度靠日常聊天
            engine.apply_event("USER_CARE")
        self.assertGreater(engine.value("familiarity"), 0.3)
        self.assertGreater(engine.value("intimacy"), 0.2)
        self.assertTrue(engine.is_close(threshold=0.2))

    def test_persistence(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "relationship_state.json"
        engine = RelationshipEngine(path)
        engine.apply_event("USER_CARE")
        expected = engine.dimensions()
        reloaded = RelationshipEngine(path)
        self.assertEqual(reloaded.dimensions(), expected)
        self.assertIn("dimensions", json.loads(path.read_text(encoding="utf-8")))


class RelationshipEventBusTest(unittest.TestCase):
    def test_publishes_relationship_changed(self):
        bus = EventBus()
        seen = []
        bus.subscribe("RelationshipChanged", lambda e: seen.append(e.payload))
        engine = make_engine(bus=bus)
        engine.apply_event("USER_CARE")
        self.assertTrue(seen)
        self.assertIn("interaction_heat", seen[0]["dimensions"])

    def test_whitelist_excludes_changed_events(self):
        self.assertNotIn("EmotionChanged", ALLOWED_SOURCES)
        self.assertNotIn("RelationshipChanged", ALLOWED_SOURCES)

    def test_reacts_to_user_message(self):
        bus = EventBus()
        engine = make_engine(bus=bus)
        before = engine.value("interaction_heat")
        bus.publish("UserMessageReceived", text="在吗")
        self.assertGreater(engine.value("interaction_heat"), before)


if __name__ == "__main__":
    unittest.main()
