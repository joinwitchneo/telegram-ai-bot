import datetime
import tempfile
import unittest
from pathlib import Path

from memory.memory_store import MemoryStore
from memory.topic_memory import TopicMemory
from proactive.character_life import CharacterLife
from proactive.proactive_engine import ProactiveEngine, ProactiveHistory


def signals(heat=0.5, shared=0.2, intimacy=0.2, fatigue=0.3) -> dict:
    return {
        "emotion": {"fatigue": fatigue},
        "relationship": {
            "interaction_heat": heat, "shared_experience": shared, "intimacy": intimacy,
            "familiarity": 0.3, "trust": 0.3,
        },
    }


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.life = CharacterLife(self.tmp / "life.json")
        self.memory = MemoryStore(self.tmp / "memories.json")
        self.topics = TopicMemory(self.tmp / "topics.json")
        self.history = ProactiveHistory(self.tmp / "history.json")
        self.engine = ProactiveEngine(
            life=self.life, memory_store=self.memory, topics=self.topics, history=self.history,
            memory_min_age_hours=0.0,
        )

    def build(self, *, last_user_at=None, now=None, **kwargs):
        return self.engine.build_candidates(
            signals=kwargs.pop("signals", signals()),
            last_user_at=last_user_at,
            now=now or datetime.datetime(2026, 9, 13, 20, 0, 0),
            **kwargs,
        )

    # ── 没有理由就不主动（Case 3）──────────────────────────────────
    def test_no_sources_means_no_candidate(self):
        self.assertEqual(self.build(), [])

    def test_fresh_chat_alone_is_not_a_reason(self):
        now = datetime.datetime(2026, 9, 13, 20, 0, 0)
        candidates = self.build(last_user_at=now - datetime.timedelta(hours=2))
        self.assertEqual(candidates, [])

    def test_long_gap_needs_warmth(self):
        now = datetime.datetime(2026, 9, 13, 20, 0, 0)
        cold = self.build(
            last_user_at=now - datetime.timedelta(hours=72), signals=signals(heat=0.1)
        )
        warm = self.build(
            last_user_at=now - datetime.timedelta(hours=72), signals=signals(heat=0.6)
        )
        self.assertEqual(cold, [])
        self.assertEqual(warm[0].reason, "interaction_gap")

    # ── Character Life 来源 ─────────────────────────────────────────
    def test_internal_thought_becomes_candidate(self):
        self.life.add("unfinished_thought", "她有点想知道用户考试考得怎么样", importance=0.7)
        candidates = self.build()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].reason, "unfinished_topic")
        self.assertEqual(candidates[0].source, "life")

    def test_unfinished_topic_reason(self):
        self.life.add("unfinished_topic", "Python 到底要不要继续学", importance=0.8)
        self.assertEqual(self.build()[0].reason, "unfinished_topic")

    def test_confirmed_event_is_special(self):
        self.life.add(
            "important_event", "用户明天要考试", source="CONFIRMED", importance=0.9
        )
        candidate = self.build()[0]
        self.assertTrue(candidate.special)
        self.assertEqual(candidate.reason, "important_event")

    def test_completed_life_entry_is_ignored(self):
        record = self.life.add("unfinished_thought", "想知道结果")
        self.life.complete(record["id"])
        self.assertEqual(self.build(), [])

    # ── Memory 来源 ─────────────────────────────────────────────────
    def test_memory_event_becomes_candidate(self):
        record, _ = self.memory.create(type="fact", content="用户下周三要考试", importance=0.8)
        candidates = self.build()
        self.assertEqual(candidates[0].reason, "important_event")
        self.assertIn(record["id"], candidates[0].memory_ids)

    def test_memory_too_fresh_is_skipped(self):
        engine = ProactiveEngine(
            life=self.life, memory_store=self.memory, topics=self.topics,
            history=self.history, memory_min_age_hours=2.0,
        )
        self.memory.create(type="fact", content="用户明天要考试", importance=0.8)
        self.assertEqual(
            engine.build_candidates(signals=signals(), now=datetime.datetime.now()),
            [],
        )

    def test_low_importance_memory_skipped(self):
        self.memory.create(type="fact", content="用户明天要考试", importance=0.3)
        self.assertEqual(self.build(), [])

    def test_topic_memory_becomes_unfinished_topic(self):
        self.memory.create(type="topic", content="用户在纠结要不要学爬虫", importance=0.7)
        self.assertEqual(self.build()[0].reason, "unfinished_topic")

    # ── Topic 复活 ──────────────────────────────────────────────────
    def test_dormant_topic_without_new_reason_is_ignored(self):
        topic, _ = self.topics.upsert("Python 学习", importance=0.8)
        self.topics.set_status(topic["id"], "DORMANT")
        self.assertEqual(self.build(), [])

    def test_dormant_topic_with_new_reason_revives(self):
        topic, _ = self.topics.upsert("Python 学习", importance=0.8)
        self.topics.set_status(topic["id"], "DORMANT")
        self.memory.create(type="fact", content="用户又提到 Python 学习的事", importance=0.7)
        candidates = self.build()
        reasons = {item.reason for item in candidates}
        self.assertIn("topic_revival", reasons)

    # ── 排序与冷却 ──────────────────────────────────────────────────
    def test_ranking_is_deterministic_and_score_ordered(self):
        self.life.add("unfinished_thought", "小事", importance=0.2)
        self.memory.create(type="fact", content="用户明天要参加考试", importance=0.95)
        first = self.build()
        second = self.build()
        self.assertEqual([item.id for item in first], [item.id for item in second])
        self.assertGreaterEqual(first[0].score, first[-1].score)

    def test_recent_topic_is_cooled_down(self):
        record = self.life.add("unfinished_topic", "Python 学不学", importance=0.8)
        now = datetime.datetime(2026, 9, 13, 20, 0, 0)
        candidate = self.build(now=now)[0]
        self.history.record(candidate=candidate, message="在吗", now=now - datetime.timedelta(hours=2))
        self.assertEqual(self.build(now=now), [])

    def test_special_candidate_also_respects_topic_cooldown(self):
        self.life.add("important_event", "用户明天考试", source="CONFIRMED", importance=0.9)
        now = datetime.datetime(2026, 9, 13, 20, 0, 0)
        candidate = self.build(now=now)[0]
        self.history.record(candidate=candidate, message="考完没", now=now - datetime.timedelta(hours=1))
        survivors = self.build(now=now)
        self.assertEqual(survivors, [])

    def test_special_candidate_returns_after_cooldown(self):
        self.life.add("important_event", "用户明天考试", source="CONFIRMED", importance=0.9)
        now = datetime.datetime(2026, 9, 13, 20, 0, 0)
        candidate = self.build(now=now)[0]
        self.history.record(candidate=candidate, message="考完没", now=now - datetime.timedelta(hours=100))
        survivors = self.build(now=now)
        self.assertEqual(len(survivors), 1)
        self.assertTrue(survivors[0].special)

    def test_history_stats(self):
        now = datetime.datetime(2026, 9, 13, 20, 0, 0)
        self.life.add("unfinished_topic", "Python", importance=0.8)
        candidate = self.build(now=now)[0]
        self.history.record(candidate=candidate, message="在吗", now=now)
        self.assertEqual(self.history.stats()["sent"], 1)
        self.history.mark_replied(now=now + datetime.timedelta(minutes=5))
        self.assertEqual(self.history.stats()["replied"], 1)

    def test_candidate_to_dict_is_serializable(self):
        self.life.add("unfinished_topic", "Python", importance=0.8)
        data = self.build()[0].to_dict()
        for key in ("id", "topic", "reason", "score", "hint", "features"):
            self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
