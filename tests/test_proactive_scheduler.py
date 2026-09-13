import datetime
import tempfile
import unittest
from pathlib import Path

from proactive.proactive_engine import Candidate, ProactiveHistory
from proactive.proactive_scheduler import ProactiveScheduler, ProactiveState

NOW = datetime.datetime(2026, 9, 13, 14, 0, 0)


def candidate(score=0.8, *, special=False, reason="important_event", topic="life_000001") -> Candidate:
    return Candidate(
        id=topic, topic=topic, reason=reason, score=score,
        hint="你突然想起：用户明天要考试", special=special,
    )


class FakeEngine:
    def __init__(self, candidates=None):
        self.candidates = list(candidates or [])
        self.build_calls = 0
        self.kwargs = {}

    def build_candidates(self, **kwargs):
        self.build_calls += 1
        self.kwargs = kwargs
        return list(self.candidates)

    def best(self, candidates):
        return candidates[0] if candidates else None


class FakeDecision:
    def __init__(self, use_llm=True, reason="正式聊天用主模型"):
        self.use_llm = use_llm
        self.reason = reason


class SchedulerTest(unittest.TestCase):
    def build(self, candidates=None, *, runner=None, policy=None, **kwargs):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = ProactiveState(self.tmp / "state.json")
        self.history = ProactiveHistory(self.tmp / "history.json")
        self.engine = FakeEngine(candidates)
        self.calls = []

        def default_runner(cand):
            self.calls.append(cand)
            return {"sent": ["在吗"], "plan": {"length": "SHORT", "tone": "NEUTRAL"}, "input_tokens": 100}

        scheduler = ProactiveScheduler(
            engine=self.engine,
            state=self.state,
            history=self.history,
            runner=runner or default_runner,
            policy=policy or (lambda text, **kw: FakeDecision()),
            now_fn=lambda: NOW,
            **kwargs,
        )
        return scheduler

    # ── Quiet hours ─────────────────────────────────────────────────
    def test_quiet_hours_boundaries(self):
        scheduler = self.build()
        at = lambda h, m: NOW.replace(hour=h, minute=m)
        self.assertTrue(scheduler.quiet_now(at(23, 45)))
        self.assertTrue(scheduler.quiet_now(at(3, 0)))
        self.assertTrue(scheduler.quiet_now(at(7, 59)))
        self.assertFalse(scheduler.quiet_now(at(8, 0)))
        self.assertFalse(scheduler.quiet_now(at(14, 0)))
        self.assertFalse(scheduler.quiet_now(at(23, 0)))

    def test_quiet_hours_skips_without_tokens(self):
        scheduler = self.build([candidate()])
        result = scheduler.tick(now=NOW.replace(hour=2, minute=0))
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "quiet_hours")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.engine.build_calls, 0)

    # ── 硬上限 ──────────────────────────────────────────────────────
    def test_daily_limit(self):
        scheduler = self.build([candidate()], daily_limit=2)
        self.state.bump_sent(NOW.date().isoformat())
        self.state.bump_sent(NOW.date().isoformat())
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["reason"], "daily_limit")
        self.assertEqual(self.calls, [])

    def test_min_interval(self):
        scheduler = self.build([candidate()])
        self.state.data["last_sent_at"] = (NOW - datetime.timedelta(minutes=10)).isoformat()
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["reason"], "min_interval")
        self.assertEqual(self.calls, [])

    def test_recent_chat(self):
        scheduler = self.build([candidate()])
        scheduler.on_user_message(now=NOW - datetime.timedelta(minutes=5))
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["reason"], "recent_chat")
        self.assertEqual(self.calls, [])

    def test_cooldown(self):
        scheduler = self.build([candidate()])
        self.state.data["cooldown_until"] = (NOW + datetime.timedelta(hours=3)).isoformat()
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["reason"], "cooldown")
        self.assertEqual(self.calls, [])

    # ── 候选相关 ────────────────────────────────────────────────────
    def test_no_candidate_means_zero_tokens(self):
        scheduler = self.build([])
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["reason"], "no_candidate")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.engine.build_calls, 1)

    def test_low_score_is_skipped(self):
        scheduler = self.build([candidate(score=0.3)])
        result = scheduler.tick(now=NOW)
        self.assertEqual(result["reason"], "low_score")
        self.assertEqual(self.calls, [])

    def test_policy_blocks_without_calling_model(self):
        scheduler = self.build(
            [candidate()],
            policy=lambda text, **kw: FakeDecision(False, "当日预算已达硬上限，改为规则回复"),
        )
        result = scheduler.tick(now=NOW)
        self.assertFalse(result["sent"])
        self.assertTrue(result["reason"].startswith("policy:"))
        self.assertEqual(self.calls, [])

    def test_disabled_scheduler(self):
        scheduler = self.build([candidate()], enabled=False)
        self.assertEqual(scheduler.tick(now=NOW)["reason"], "disabled")
        self.assertEqual(self.calls, [])

    # ── 正常发送 ────────────────────────────────────────────────────
    def test_successful_send_updates_state_and_history(self):
        scheduler = self.build([candidate()])
        result = scheduler.tick(now=NOW)
        self.assertTrue(result["sent"])
        self.assertEqual(result["messages"], ["在吗"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.state.sent_count(NOW.date().isoformat()), 1)
        self.assertTrue(self.state.data["last_sent_at"])
        self.assertEqual(len(self.state.data["pending"]), 1)
        self.assertEqual(self.history.stats()["sent"], 1)
        self.assertIn(result["level"], ("full", "short"))

    def test_full_level_needs_high_score(self):
        scheduler = self.build([candidate(score=0.8)])
        self.assertEqual(scheduler.tick(now=NOW)["level"], "full")
        scheduler2 = self.build([candidate(score=0.6)])
        self.assertEqual(scheduler2.tick(now=NOW)["level"], "short")

    def test_runner_failure_does_not_record(self):
        scheduler = self.build([candidate()], runner=lambda cand: {"sent": []})
        result = scheduler.tick(now=NOW)
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "generation_failed")
        self.assertEqual(self.state.sent_count(NOW.date().isoformat()), 0)

    def test_runner_exception_is_contained(self):
        def boom(cand):
            raise RuntimeError("send failed")

        scheduler = self.build([candidate()], runner=boom)
        result = scheduler.tick(now=NOW)
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "generation_failed")

    # ── 连续不回复 ──────────────────────────────────────────────────
    def test_two_ignores_trigger_cooldown(self):
        scheduler = self.build([])
        first = (NOW - datetime.timedelta(hours=5)).isoformat()
        self.state.data["pending"] = [{"id": "a", "ts": first, "topic": "a"}]
        settled = scheduler.settle_pending(NOW)
        self.assertEqual(settled["ignored"], 1)
        self.assertEqual(self.state.data["nonresponse_streak"], 1)
        self.assertEqual(self.state.data["cooldown_until"], "")
        self.state.data["pending"] = [{"id": "b", "ts": first, "topic": "b"}]
        scheduler.settle_pending(NOW)
        self.assertEqual(self.state.data["nonresponse_streak"], 2)
        self.assertTrue(self.state.data["cooldown_until"])

    def test_pending_not_yet_expired_stays(self):
        scheduler = self.build([])
        self.state.data["pending"] = [{"id": "a", "ts": (NOW - datetime.timedelta(minutes=10)).isoformat(), "topic": "a"}]
        settled = scheduler.settle_pending(NOW)
        self.assertEqual(settled["ignored"], 0)
        self.assertEqual(len(self.state.data["pending"]), 1)

    def test_user_return_clears_cooldown(self):
        scheduler = self.build([])
        self.state.data["cooldown_until"] = (NOW + datetime.timedelta(hours=3)).isoformat()
        self.state.data["nonresponse_streak"] = 2
        self.state.data["pending"] = [{"id": "a", "ts": NOW.isoformat(), "topic": "a"}]
        result = scheduler.on_user_message(now=NOW)
        self.assertTrue(result["cooldown_reset"])
        self.assertEqual(self.state.data["cooldown_until"], "")
        self.assertEqual(self.state.data["nonresponse_streak"], 0)
        self.assertEqual(self.state.data["pending"], [])

    def test_user_return_marks_history_replied(self):
        scheduler = self.build([])
        self.history.record(candidate=candidate(), message="在吗", now=NOW - datetime.timedelta(minutes=30))
        self.assertEqual(scheduler.on_user_message(now=NOW)["marked_replied"], 1)
        self.assertEqual(self.history.stats()["replied"], 1)

    # ── 特殊事件 ────────────────────────────────────────────────────
    def test_special_event_overrides_low_score(self):
        scheduler = self.build([candidate(score=0.2, special=True)])
        result = scheduler.tick(now=NOW)
        self.assertTrue(result["sent"])
        self.assertTrue(result["special"])

    def test_special_event_overrides_recent_chat(self):
        scheduler = self.build([candidate(score=0.2, special=True)])
        scheduler.on_user_message(now=NOW - datetime.timedelta(minutes=5))
        self.assertTrue(scheduler.tick(now=NOW)["sent"])

    def test_special_event_still_blocked_by_cooldown(self):
        scheduler = self.build([candidate(score=0.9, special=True)])
        self.state.data["cooldown_until"] = (NOW + datetime.timedelta(hours=3)).isoformat()
        self.assertEqual(scheduler.tick(now=NOW)["reason"], "cooldown")

    def test_special_event_still_blocked_by_daily_limit(self):
        scheduler = self.build([candidate(score=0.9, special=True)], daily_limit=1)
        self.state.bump_sent(NOW.date().isoformat())
        self.assertEqual(scheduler.tick(now=NOW)["reason"], "daily_limit")

    def test_special_event_still_blocked_by_quiet_hours(self):
        scheduler = self.build([candidate(score=0.9, special=True)])
        self.assertEqual(scheduler.tick(now=NOW.replace(hour=3))["reason"], "quiet_hours")

    def test_override_can_be_disabled(self):
        scheduler = self.build([candidate(score=0.2, special=True)], allow_special_override=False)
        self.assertEqual(scheduler.tick(now=NOW)["reason"], "low_score")

    # ── 状态 ────────────────────────────────────────────────────────
    def test_status_shape(self):
        scheduler = self.build([candidate()])
        scheduler.tick(now=NOW)
        status = scheduler.status(now=NOW)
        for key in ("enabled", "quiet", "sent_today", "daily_limit", "nonresponse_streak", "history"):
            self.assertIn(key, status)

    def test_engine_receives_context(self):
        scheduler = self.build([candidate()])
        scheduler.on_user_message(now=NOW - datetime.timedelta(hours=50))
        scheduler.tick(now=NOW)
        kwargs = self.engine.kwargs
        self.assertEqual(kwargs["daily_limit"], 3)
        self.assertEqual(kwargs["nonresponse_limit"], 2)
        self.assertIsNotNone(kwargs["last_user_at"])

    def test_scheduler_starts_and_stops(self):
        scheduler = self.build([candidate()], tick_minutes=999)
        scheduler.start()
        self.assertIsNotNone(scheduler._thread)
        scheduler.stop()
        self.assertIsNone(scheduler._thread)


if __name__ == "__main__":
    unittest.main()
