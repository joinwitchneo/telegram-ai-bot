import datetime
import json
import re
import tempfile
import unittest
from pathlib import Path

from core.event_bus import EventBus
from emotion import emotion_events as events_mod
from emotion.emotion_engine import ALLOWED_SOURCES, EmotionEngine


def make_engine(bus=None, inertia=0.6):
    tmp = Path(tempfile.mkdtemp())
    return EmotionEngine(tmp / "emotion_state.json", bus=bus, inertia=inertia)


class EmotionInitialTest(unittest.TestCase):
    def test_starts_at_baseline(self):
        engine = make_engine()
        for name, value in events_mod.BASELINE.items():
            self.assertAlmostEqual(engine.value(name), value, places=6)

    def test_twelve_dimensions(self):
        engine = make_engine()
        self.assertEqual(len(engine.emotions()), 12)
        for name in events_mod.EMOTION_NAMES:
            self.assertIn(name, engine.emotions())

    def test_reset(self):
        engine = make_engine()
        engine.apply_event("USER_PRAISE")
        engine.reset()
        self.assertAlmostEqual(engine.value("joy"), events_mod.BASELINE["joy"], places=6)


class EmotionEventTest(unittest.TestCase):
    def test_praise_raises_joy_and_affection(self):
        engine = make_engine()
        before = engine.value("joy")
        changed = engine.apply_event("USER_PRAISE")
        self.assertGreater(engine.value("joy"), before)
        self.assertIn("joy", changed)
        self.assertIn("affection", changed)

    def test_insult_raises_anger_but_not_to_one(self):
        engine = make_engine()
        engine.apply_event("USER_INSULT")
        self.assertGreater(engine.value("anger"), events_mod.BASELINE["anger"])
        self.assertLess(engine.value("anger"), 0.6)

    def test_apology_reduces_anger(self):
        engine = make_engine()
        for _ in range(5):
            engine.apply_event("USER_INSULT")
        peak = engine.value("anger")
        engine.apply_event("USER_APOLOGY")
        self.assertLess(engine.value("anger"), peak)

    def test_unknown_event_is_noop(self):
        engine = make_engine()
        self.assertEqual(engine.apply_event("NOT_AN_EVENT"), {})

    def test_event_mapper_from_text(self):
        engine = make_engine()
        engine.apply_events(events_mod.map_text_to_events("你真好，谢谢你"))
        self.assertGreater(engine.value("joy"), events_mod.BASELINE["joy"])


class EmotionInertiaTest(unittest.TestCase):
    def test_single_message_does_not_jump(self):
        engine = make_engine(inertia=0.6)
        before = engine.value("joy")
        engine.apply_event("USER_PRAISE")
        delta = engine.value("joy") - before
        self.assertLess(delta, 0.1)          # 单次变化被惯性压住
        self.assertGreater(delta, 0.0)

    def test_inertia_dampens_more_when_higher(self):
        low = make_engine(inertia=0.2)
        high = make_engine(inertia=0.9)
        low.apply_event("USER_PRAISE")
        high.apply_event("USER_PRAISE")
        self.assertGreater(
            low.value("joy") - events_mod.BASELINE["joy"],
            high.value("joy") - events_mod.BASELINE["joy"],
        )

    def test_repeated_events_approach_but_never_exceed_one(self):
        engine = make_engine()
        for _ in range(200):
            engine.apply_event("USER_PRAISE")
        self.assertLessEqual(engine.value("joy"), 1.0)
        self.assertLessEqual(engine.value("excitement"), 1.0)

    def test_continuous_changes_are_monotonic(self):
        engine = make_engine()
        series = []
        for _ in range(5):
            engine.apply_event("USER_CARE")
            series.append(round(engine.value("affection"), 4))
        self.assertEqual(series, sorted(series))
        self.assertEqual(len(set(series)), len(series))

    def test_no_randomness(self):
        first = make_engine()
        second = make_engine()
        for _ in range(3):
            first.apply_event("USER_PRAISE")
            second.apply_event("USER_PRAISE")
        self.assertEqual(first.emotions(), second.emotions())


class EmotionBoundsTest(unittest.TestCase):
    def test_lower_bound(self):
        engine = make_engine()
        for _ in range(200):
            engine.apply_event("USER_INSULT")
        for value in engine.emotions().values():
            self.assertGreaterEqual(value, 0.0)

    def test_extreme_weight(self):
        engine = make_engine()
        engine.apply_event("USER_INSULT", weight=50)
        engine.apply_event("USER_PRAISE", weight=50)
        for value in engine.emotions().values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class EmotionOutputTest(unittest.TestCase):
    def test_describe_has_no_numbers(self):
        engine = make_engine()
        engine.apply_event("USER_PRAISE")
        text = engine.describe()
        self.assertFalse(re.search(r"\d", text), text)
        self.assertIn("当前情绪", text)

    def test_behavior_hints_limited(self):
        engine = make_engine()
        for _ in range(10):
            engine.apply_event("USER_PRAISE")
        self.assertLessEqual(len(engine.behavior_hints()), 3)

    def test_dominant_reflects_deviation(self):
        engine = make_engine()
        for _ in range(10):
            engine.apply_event("USER_INSULT")
        name, deviation = engine.dominant()
        self.assertIn(name, events_mod.EMOTION_NAMES)
        self.assertGreater(deviation, 0)

    def test_persistence(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "emotion_state.json"
        engine = EmotionEngine(path)
        engine.apply_event("USER_PRAISE")
        expected = engine.emotions()
        reloaded = EmotionEngine(path)
        self.assertEqual(reloaded.emotions(), expected)
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["emotions"])


class EmotionEventBusTest(unittest.TestCase):
    def test_publishes_emotion_changed(self):
        bus = EventBus()
        seen = []
        bus.subscribe("EmotionChanged", lambda e: seen.append(e.payload))
        engine = make_engine(bus=bus)
        engine.apply_event("USER_PRAISE")
        self.assertTrue(seen)
        self.assertIn("joy", seen[0]["dimensions"])

    def test_whitelist_excludes_changed_events(self):
        self.assertNotIn("EmotionChanged", ALLOWED_SOURCES)
        self.assertNotIn("RelationshipChanged", ALLOWED_SOURCES)

    def test_subscribes_to_message_events(self):
        bus = EventBus()
        engine = make_engine(bus=bus)
        before = engine.value("joy")
        bus.publish("UserMessageReceived", text="谢谢你，真好")
        self.assertGreater(engine.value("joy"), before)

    def test_user_returned_boosts_joy(self):
        bus = EventBus()
        engine = make_engine(bus=bus)
        engine.tick(now=datetime.datetime.now(), hours=48)   # 先衰减
        before = engine.value("joy")
        bus.publish("UserReturned", gap_hours=24)
        self.assertGreater(engine.value("joy"), before)


if __name__ == "__main__":
    unittest.main()
