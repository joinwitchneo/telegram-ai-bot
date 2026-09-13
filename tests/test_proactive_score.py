import unittest

from proactive.proactive_score import (
    DEFAULT_WEIGHTS,
    FEATURES,
    gap_feature,
    normalize_features,
    recent_chat_feature,
    score_candidate,
)


class NormalizeTest(unittest.TestCase):
    def test_all_features_present(self):
        values = normalize_features(None)
        self.assertEqual(set(values), set(FEATURES))
        self.assertTrue(all(value == 0.0 for value in values.values()))

    def test_clamps_out_of_range(self):
        values = normalize_features({"unfinished_topic": 3.0, "fatigue_penalty": -2.0})
        self.assertEqual(values["unfinished_topic"], 1.0)
        self.assertEqual(values["fatigue_penalty"], 0.0)

    def test_non_numeric_becomes_zero(self):
        values = normalize_features({"important_event": "nope"})
        self.assertEqual(values["important_event"], 0.0)


class ScoreTest(unittest.TestCase):
    def test_zero_when_nothing(self):
        score, parts = score_candidate({})
        self.assertEqual(score, 0.0)
        self.assertEqual(set(parts), set(FEATURES))

    def test_positive_weights_add_up(self):
        score, parts = score_candidate({"important_event": 1.0})
        self.assertAlmostEqual(score, DEFAULT_WEIGHTS["important_event"], places=3)
        self.assertGreater(parts["important_event"], 0)

    def test_penalties_subtract(self):
        base, _ = score_candidate({"unfinished_topic": 1.0, "interaction_gap": 1.0})
        penalised, parts = score_candidate(
            {"unfinished_topic": 1.0, "interaction_gap": 1.0, "recent_chat_penalty": 1.0}
        )
        self.assertLess(penalised, base)
        self.assertLess(parts["recent_chat_penalty"], 0)

    def test_two_strong_reasons_cross_full_threshold(self):
        score, _ = score_candidate(
            {
                "unfinished_topic": 1.0, "important_event": 1.0,
                "interaction_gap": 1.0, "relationship_heat": 0.8,
            }
        )
        self.assertGreaterEqual(score, 0.75)

    def test_single_reason_stays_in_short_band(self):
        single, _ = score_candidate({"important_event": 1.0})
        pair, _ = score_candidate({"important_event": 1.0, "interaction_gap": 1.0})
        self.assertLess(single, 0.5)
        self.assertGreaterEqual(pair, 0.5)
        self.assertLess(pair, 0.75)

    def test_weak_candidate_below_short_threshold(self):
        score, _ = score_candidate({"character_thought": 0.3, "fatigue_penalty": 0.8})
        self.assertLess(score, 0.5)

    def test_never_exceeds_one(self):
        score, _ = score_candidate({name: 1.0 for name in FEATURES})
        self.assertLessEqual(score, 1.0)
        self.assertGreaterEqual(score, 0.0)

    def test_custom_weights(self):
        score, _ = score_candidate({"important_event": 1.0}, {"important_event": 0.5})
        self.assertAlmostEqual(score, 0.5, places=3)

    def test_parts_are_explainable(self):
        _, parts = score_candidate({"unfinished_topic": 1.0, "fatigue_penalty": 1.0})
        self.assertAlmostEqual(parts["unfinished_topic"], DEFAULT_WEIGHTS["unfinished_topic"], places=3)
        self.assertAlmostEqual(parts["fatigue_penalty"], -DEFAULT_WEIGHTS["fatigue_penalty"], places=3)


class FeatureTest(unittest.TestCase):
    def test_gap_feature_boundaries(self):
        self.assertEqual(gap_feature(3), 0.0)
        self.assertEqual(gap_feature(60), 1.0)
        middle = gap_feature(24)
        self.assertGreater(middle, 0.0)
        self.assertLess(middle, 1.0)

    def test_recent_chat_feature(self):
        self.assertEqual(recent_chat_feature(0), 1.0)
        self.assertEqual(recent_chat_feature(120), 0.0)
        self.assertGreater(recent_chat_feature(15), recent_chat_feature(40))


if __name__ == "__main__":
    unittest.main()
