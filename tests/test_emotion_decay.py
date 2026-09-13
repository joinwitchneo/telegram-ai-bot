import unittest

from emotion import emotion_events as events_mod
from emotion.emotion_decay import EmotionDecay


class DecayMathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.decay = EmotionDecay(events_mod.BASELINE)

    def test_zero_hours_changes_nothing(self):
        state = dict(events_mod.BASELINE)
        state["joy"] = 0.9
        self.assertEqual(self.decay.decay(state, 0), state)

    def test_moves_toward_baseline(self):
        state = dict(events_mod.BASELINE)
        state["joy"] = 0.9
        after = self.decay.decay(state, 6)
        self.assertLess(after["joy"], 0.9)
        self.assertGreater(after["joy"], events_mod.BASELINE["joy"])

    def test_never_crosses_baseline(self):
        state = dict(events_mod.BASELINE)
        state["joy"] = 0.9
        for hours in (1, 6, 24, 24 * 7, 24 * 30):
            state = self.decay.decay(state, hours)
        self.assertAlmostEqual(state["joy"], events_mod.BASELINE["joy"], places=3)

    def test_below_baseline_recovers_upward(self):
        state = dict(events_mod.BASELINE)
        state["joy"] = 0.05
        after = self.decay.decay(state, 12)
        self.assertGreater(after["joy"], 0.05)
        self.assertLessEqual(after["joy"], events_mod.BASELINE["joy"] + 1e-9)

    def test_monotonic_with_time(self):
        state = dict(events_mod.BASELINE)
        state["excitement"] = 0.95
        previous = state["excitement"]
        for hours in (1, 2, 4, 8, 16):
            state = self.decay.decay(state, hours)
            self.assertLess(state["excitement"], previous)
            previous = state["excitement"]

    def test_different_speeds(self):
        state = dict(events_mod.BASELINE)
        state["excitement"] = 0.9
        state["trust"] = 0.9
        after = self.decay.decay(state, 6)
        excitement_drop = 0.9 - after["excitement"]
        trust_drop = 0.9 - after["trust"]
        self.assertGreater(excitement_drop, trust_drop * 5)

    def test_trust_barely_moves(self):
        state = dict(events_mod.BASELINE)
        state["trust"] = 0.9
        after = self.decay.decay(state, 24)
        self.assertGreater(after["trust"], 0.8)
        self.assertLess(0.9 - after["trust"], 0.1)

    def test_deviation_measure(self):
        calm_state = dict(events_mod.BASELINE)
        excited = dict(events_mod.BASELINE)
        excited["joy"] = 0.95
        self.assertGreater(self.decay.deviation(excited), self.decay.deviation(calm_state))

    def test_extreme_hours_safe(self):
        state = dict(events_mod.BASELINE)
        state["joy"] = 1.0
        after = self.decay.decay(state, 10 ** 9)
        self.assertAlmostEqual(after["joy"], events_mod.BASELINE["joy"], places=3)

    def test_custom_rates_override(self):
        fast = EmotionDecay(events_mod.BASELINE, {"joy": 5.0})
        slow = EmotionDecay(events_mod.BASELINE, {"joy": 0.001})
        state = dict(events_mod.BASELINE)
        state["joy"] = 1.0
        self.assertLess(fast.decay(state, 1)["joy"], slow.decay(state, 1)["joy"])

    def test_factor_bounds(self):
        self.assertLessEqual(self.decay.factor("joy", 1000), 1.0)
        self.assertGreaterEqual(self.decay.factor("joy", 0), 0.0)


if __name__ == "__main__":
    unittest.main()
