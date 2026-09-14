"""V3 MVP 核心：RuntimeConfig / 预算 / Thought / Novelty / Interest 收敛。"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from v3.budget import V3Budget
from v3.config import RuntimeConfig
from v3.interest.engine import InterestEngine
from v3.interest.store import InterestStore
from v3.thought.lifecycle import ThoughtLifecycle
from v3.thought.models import Thought
from v3.thought.novelty import compute_information_gain, compute_novelty
from v3.thought.store import ThoughtStore
from v3.thought.validator import ThoughtValidator


class FakeConfig:
    """最小 Config 替身：只实现 V3 用到的读取接口。"""

    def __init__(self, values: dict) -> None:
        self.values = values

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


class RuntimeConfigTest(unittest.TestCase):
    def build(self, *, enabled=True, base="observe"):
        tmp = Path(tempfile.mkdtemp())
        config = FakeConfig({"V3_ENABLED": enabled, "V3_MODE": base, "V3_DATA_DIR": str(tmp / "v3")})
        return RuntimeConfig(config, base_dir=tmp), tmp

    def test_base_mode_used_when_no_override(self):
        runtime, _ = self.build(base="dry_run")
        self.assertEqual(runtime.mode(), "dry_run")
        self.assertEqual(runtime.mode_report()["effective_mode"], "dry_run")

    def test_runtime_override_wins_and_is_visible(self):
        runtime, _ = self.build(base="observe")
        self.assertTrue(runtime.set_runtime_mode("dry_run", actor="test"))
        self.assertEqual(runtime.mode(), "dry_run")
        report = runtime.mode_report()
        self.assertEqual(report["base_mode"], "observe")
        self.assertEqual(report["runtime_override"], "dry_run")
        self.assertEqual(report["effective_mode"], "dry_run")

    def test_disable_forces_observe(self):
        runtime, _ = self.build(enabled=False, base="live")
        self.assertFalse(runtime.enabled())
        self.assertEqual(runtime.mode(), "observe")
        self.assertFalse(runtime.actions_allowed())

    def test_invalid_mode_rejected(self):
        runtime, _ = self.build()
        self.assertFalse(runtime.set_runtime_mode("wild"))
        self.assertEqual(runtime.mode(), "observe")

    def test_invalid_override_falls_back_to_base(self):
        runtime, tmp = self.build(base="dry_run")
        path = tmp / "v3" / "runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": "nonsense"}), encoding="utf-8")
        self.assertEqual(runtime.mode(), "dry_run")

    def test_paths_created(self):
        runtime, _ = self.build()
        runtime.ensure_dirs()
        for path in runtime.paths().values():
            self.assertTrue(path.exists() or path.parent.exists())


class BudgetTest(unittest.TestCase):
    def build(self, **limits):
        tmp = Path(tempfile.mkdtemp())
        return V3Budget(tmp / "budget.json", **limits)

    def test_limits_enforced(self):
        budget = self.build(llm_per_day=2, cycles_per_day=1)
        self.assertTrue(budget.can_llm()[0])
        budget.spend_llm(); budget.spend_llm()
        ok, why = budget.can_llm()
        self.assertFalse(ok)
        self.assertIn("上限", why)
        self.assertTrue(budget.can_cycle()[0])
        budget.spend_cycle()
        self.assertFalse(budget.can_cycle()[0])

    def test_day_rollover_resets(self):
        budget = self.build(llm_per_day=1)
        budget.spend_llm()
        self.assertFalse(budget.can_llm()[0])
        budget.data["date"] = "1970-01-01"
        self.assertTrue(budget.can_llm()[0])

    def test_snapshot_shape(self):
        budget = self.build()
        snap = budget.snapshot()
        self.assertIn("limits", snap)
        self.assertEqual(snap["limits"]["llm_calls"], 6)

    def test_chain_limit_is_per_cycle(self):
        """链长是"单轮"限制：一轮内不能无限调 LLM，下一轮要能恢复。"""
        budget = self.build(llm_per_day=10, max_chain=2)
        budget.begin_chain()
        self.assertTrue(budget.can_llm()[0])
        budget.spend_llm()
        self.assertTrue(budget.can_llm()[0])
        budget.spend_llm()
        ok, why = budget.can_llm()
        self.assertFalse(ok)
        self.assertIn("链长", why)
        budget.begin_chain()
        self.assertTrue(budget.can_llm()[0], "下一轮认知必须重新获得链长额度")
        self.assertEqual(budget.snapshot()["chain_calls"], 0)

    def test_global_budget_blocks(self):
        class Blocker:
            def level(self):
                return "hard"

        budget = V3Budget(Path(tempfile.mkdtemp()) / "b.json", global_budget=Blocker())
        ok, why = budget.can_cycle()
        self.assertFalse(ok)
        self.assertIn("全局预算", why)


class ThoughtTest(unittest.TestCase):
    def build(self):
        tmp = Path(tempfile.mkdtemp())
        return ThoughtStore(tmp / "thoughts.json", archive_path=tmp / "archive.json")

    def test_add_and_persist(self):
        store = self.build()
        thought = Thought(id="", content="他在忙什么", topic="近况")
        saved, action = store.add(thought)
        self.assertEqual(action, "created")
        self.assertTrue(saved.id.startswith("th_"))
        self.assertEqual(store.count(), 1)

    def test_same_topic_same_content_counts_as_revisit(self):
        store = self.build()
        store.add(Thought(id="", content="他在忙什么", topic="近况"))
        saved, action = store.add(Thought(id="", content="他在忙什么", topic="近况"))
        self.assertEqual(action, "revisited")
        self.assertEqual(saved.seen_count, 2)

    def test_recall_prefers_persistent(self):
        store = self.build()
        store.add(Thought(id="", content="瞬时念头", topic="a", status="ephemeral"))
        store.add(Thought(id="", content="未完成的事", topic="b", status="active", is_unfinished=True))
        recalled = store.recall(limit=5)
        self.assertEqual([t.content for t in recalled], ["未完成的事"])

    def test_archive_expires_only_ephemeral(self):
        store = self.build()
        old = Thought(id="", content="很久以前的念头", topic="x", status="ephemeral",
                      created_at="2020-01-01T00:00:00")
        store.add(old)
        keep = Thought(id="", content="未完成的重要事", topic="y", status="active", is_unfinished=True,
                       created_at="2020-01-01T00:00:00")
        store.add(keep)
        archived = store.archive_expired(days=30)
        self.assertEqual(archived, 1)
        self.assertEqual([t.content for t in store.all()], ["未完成的重要事"])

    def test_validator_rejects_cot_and_missing_evidence(self):
        validator = ThoughtValidator()
        ok, reason, _ = validator.validate({"content": "chain of thought: 我先想…", "evidence": ["x"]})
        self.assertFalse(ok)
        ok2, reason2, _ = validator.validate({"content": "他在忙什么"})
        self.assertFalse(ok2)
        self.assertIn("证据", reason2)

    def test_validator_normalizes(self):
        validator = ThoughtValidator()
        ok, _, normalized = validator.validate(
            {"content": "他今天好像很累", "type": "unknown", "topic": "状态", "evidence": ["他说累"]}
        )
        self.assertTrue(ok)
        self.assertEqual(normalized["type"], "observation")

    def test_lifecycle_promotes_unfinished(self):
        store = self.build()
        saved, _ = store.add(Thought(id="", content="还没聊完的事", topic="t", is_unfinished=True))
        lifecycle = ThoughtLifecycle(store)
        self.assertTrue(lifecycle.promote(saved.id))
        self.assertEqual(store.get(saved.id).status, "active")

    def test_store_recovers_from_broken_file(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "thoughts.json"
        path.write_text("{ 坏 json", encoding="utf-8")
        store = ThoughtStore(path)
        self.assertEqual(store.count(), 0)


class NoveltyTest(unittest.TestCase):
    def test_first_thought_is_novel(self):
        self.assertEqual(compute_novelty("今天天气不错"), 1.0)

    def test_repeat_lowers_novelty(self):
        first = compute_novelty("他在忙什么", recent_contents=[])
        second = compute_novelty("他在忙什么", recent_contents=["他在忙什么"])
        self.assertLess(second, first)
        self.assertLessEqual(second, 0.2)

    def test_information_gain_decays_with_topic_repeats(self):
        gains = [
            compute_information_gain(
                "突然想看一部老电影", recent_contents=["他在忙什么工作"], topic_seen_count=n
            )
            for n in (1, 2, 3, 4)
        ]
        self.assertEqual(gains, sorted(gains, reverse=True))
        self.assertGreater(gains[0], gains[-1])
        self.assertGreater(gains[0], 0.0)

    def test_old_observation_gives_less_gain(self):
        fresh = compute_information_gain("新信息", observation_is_new=True)
        stale = compute_information_gain("新信息", observation_is_new=False)
        self.assertGreater(fresh, stale)


class InterestTest(unittest.TestCase):
    def build(self):
        tmp = Path(tempfile.mkdtemp())
        store = InterestStore(tmp / "interests.json")
        return store, InterestEngine(store)

    def test_single_update_is_bounded(self):
        store, engine = self.build()
        _, detail = engine.update("音乐", information_gain=1.0)
        self.assertLessEqual(abs(detail["attraction_delta"]), 0.05)

    def test_increments_decay_and_converge(self):
        """固定输入重复 N 次：增量单调递减，总量收敛，永不触顶。"""
        store, engine = self.build()
        deltas = []
        for _ in range(8):
            _, detail = engine.update("音乐", information_gain=1.0)
            deltas.append(detail["attraction_delta"])
        for earlier, later in zip(deltas, deltas[1:]):
            self.assertLess(later, earlier)
        final = store.get("音乐")
        self.assertLess(final.attraction, 0.75)      # 收敛，远未触顶
        self.assertLess(sum(deltas), 0.2)

    def test_negative_valence_keeps_curiosity(self):
        store, engine = self.build()
        engine.update("短视频", information_gain=0.8, valence_signal=-1.0)
        item = store.get("短视频")
        self.assertLess(item.valence, 0)
        self.assertGreater(item.curiosity, 0.5)      # 讨厌但好奇

    def test_values_are_clamped(self):
        store, engine = self.build()
        for _ in range(50):
            engine.update("音乐", information_gain=1.0)
        item = store.get("音乐")
        self.assertLessEqual(item.attraction, 1.0)
        self.assertGreaterEqual(item.attraction, 0.0)
        self.assertLessEqual(item.curiosity, 1.0)

    def test_persistence(self):
        store, engine = self.build()
        engine.update("摄影", information_gain=0.9)
        again = InterestStore(store.path)
        self.assertIsNotNone(again.get("摄影"))


if __name__ == "__main__":
    unittest.main()
