import tempfile
import time
import unittest
from pathlib import Path

from tools.core.cache import TTLCache
from tools.core.cost import CostTracker
from tools.core.executor import ToolExecutor
from tools.core.permissions import Permissions
from tools.core.registry import ToolRegistry
from tools.core.result import ToolResult
from tools.core.schema import ToolSpec, which


def build_executor(**kwargs):
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="echo", mode="LOCAL", level="L0", description="回声", cache_ttl=60),
        lambda text="", **_: ToolResult(name="echo", ok=True, text=f"回声：{text}"),
    )
    registry.register(
        ToolSpec(name="boom", mode="LOCAL", level="L0", description="会炸"),
        lambda **_: (_ for _ in ()).throw(RuntimeError("炸了")),
    )
    registry.register(
        ToolSpec(name="slow", mode="LOCAL", level="L0", description="慢"),
        lambda **_: (time.sleep(3), ToolResult(name="slow"))[1],
    )
    registry.register(
        ToolSpec(name="needs_binary", mode="LOCAL", level="L1", requires=("绝对不存在的程序",)),
        lambda **_: ToolResult(name="needs_binary"),
    )
    registry.register(
        ToolSpec(name="net", mode="FREE_NETWORK", level="L1", cache_ttl=30),
        lambda **_: ToolResult(name="net", ok=True, text="联网结果", http_requests=1),
    )
    return registry, ToolExecutor(registry, timeout=0.5, **kwargs)


class SchemaTest(unittest.TestCase):
    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            ToolSpec(name="x", mode="CLOUD")

    def test_rejects_unknown_level(self):
        with self.assertRaises(ValueError):
            ToolSpec(name="x", level="L9")

    def test_missing_requirements_detected(self):
        spec = ToolSpec(name="x", requires=("绝对不存在的程序",))
        self.assertFalse(spec.available())
        self.assertIn("绝对不存在的程序", spec.missing_requirements())

    def test_configured_flag(self):
        self.assertFalse(ToolSpec(name="x", configured=False).available())

    def test_which_handles_empty(self):
        self.assertEqual(which(""), "")


class RegistryTest(unittest.TestCase):
    def test_register_and_lookup(self):
        registry, _ = build_executor()
        self.assertIn("echo", registry.names())
        self.assertIsNotNone(registry.get("echo"))
        self.assertIsNone(registry.get("nope"))

    def test_groups_by_mode_and_level(self):
        registry, _ = build_executor()
        self.assertEqual([tool.name for tool in registry.by_mode("FREE_NETWORK")], ["net"])
        self.assertTrue(all(tool.spec.level == "L0" for tool in registry.by_level("L0")))

    def test_cost_report_lists_unavailable(self):
        registry, _ = build_executor()
        report = registry.cost_report()
        self.assertIn("needs_binary", report["unavailable"])
        self.assertIn("net", report["by_mode"]["FREE_NETWORK"])

    def test_unregister(self):
        registry, _ = build_executor()
        self.assertTrue(registry.unregister("echo"))
        self.assertFalse(registry.unregister("echo"))


class CacheTest(unittest.TestCase):
    def test_set_get_and_expire(self):
        cache = TTLCache(enabled=True)
        cache.set("k", {"v": 1}, ttl=0.2)
        self.assertEqual(cache.get("k"), {"v": 1})
        time.sleep(0.25)
        self.assertIsNone(cache.get("k"))

    def test_zero_ttl_is_not_stored(self):
        cache = TTLCache()
        cache.set("k", 1, ttl=0)
        self.assertIsNone(cache.get("k"))

    def test_stats(self):
        cache = TTLCache()
        cache.set("k", 1, ttl=10)
        cache.get("k")
        cache.get("missing")
        stats = cache.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)

    def test_persist_and_reload(self):
        tmp = Path(tempfile.mkdtemp()) / "cache.json"
        cache = TTLCache(tmp)
        cache.set("k", "v", ttl=60)
        cache.save()
        again = TTLCache(tmp)
        self.assertEqual(again.get("k"), "v")


class CostTest(unittest.TestCase):
    def test_records_by_day(self):
        tracker = CostTracker()
        tracker.record("weather", mode="FREE_NETWORK", http_requests=2)
        tracker.record("main_llm", mode="PAID_API", llm_tokens=120)
        day = tracker.day()
        self.assertEqual(day["calls"], 2)
        self.assertEqual(day["http_requests"], 2)
        self.assertEqual(day["llm_tokens"], 120)
        self.assertEqual(day["paid_calls"], 1)

    def test_summary(self):
        tracker = CostTracker()
        tracker.record("clock", mode="LOCAL")
        tracker.record("clock", mode="LOCAL", cache_hit=True)
        summary = tracker.summary()
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["cache_hits"], 1)


class PermissionsTest(unittest.TestCase):
    def test_network_can_be_disabled(self):
        permissions = Permissions(allow_network=False)
        allowed, why = permissions.check("weather", "FREE_NETWORK")
        self.assertFalse(allowed)
        self.assertIn("联网", why)

    def test_paid_can_be_disabled(self):
        permissions = Permissions(allow_paid_api=False)
        self.assertFalse(permissions.check("main_llm", "PAID_API")[0])

    def test_blocked_tool(self):
        permissions = Permissions(blocked_tools=["game"])
        self.assertFalse(permissions.check("game", "LOCAL")[0])

    def test_local_allowed_by_default(self):
        self.assertTrue(Permissions().check("clock", "LOCAL")[0])


class ExecutorTest(unittest.TestCase):
    def test_run_ok(self):
        _, executor = build_executor()
        result = executor.run("echo", text="你好")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "回声：你好")
        self.assertEqual(result.mode, "LOCAL")
        self.assertEqual(result.level, "L0")

    def test_cache_hit_second_time(self):
        cache = TTLCache()
        _, executor = build_executor(cache=cache)
        first = executor.run("echo", text="你好")
        second = executor.run("echo", text="你好")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(second.text, first.text)

    def test_different_args_not_cached_together(self):
        cache = TTLCache()
        _, executor = build_executor(cache=cache)
        executor.run("echo", text="一")
        other = executor.run("echo", text="二")
        self.assertFalse(other.cached)

    def test_error_is_contained(self):
        _, executor = build_executor()
        result = executor.run("boom")
        self.assertFalse(result.ok)
        self.assertIn("炸了", result.error)

    def test_timeout(self):
        _, executor = build_executor()
        result = executor.run("slow")
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)

    def test_missing_binary_is_reported(self):
        _, executor = build_executor()
        result = executor.run("needs_binary")
        self.assertFalse(result.ok)
        self.assertIn("缺少本机依赖", result.error)

    def test_unknown_tool(self):
        _, executor = build_executor()
        self.assertFalse(executor.run("ghost").ok)

    def test_disabled_layer(self):
        _, executor = build_executor(enabled=False)
        self.assertFalse(executor.run("echo", text="嗨").ok)

    def test_permission_blocks_network_tool(self):
        permissions = Permissions(allow_network=False)
        _, executor = build_executor(permissions=permissions)
        result = executor.run("net")
        self.assertFalse(result.ok)
        self.assertIn("联网", result.error)

    def test_cost_is_recorded(self):
        tracker = CostTracker()
        _, executor = build_executor(cost=tracker)
        executor.run("echo", text="一")
        self.assertEqual(tracker.day()["calls"], 1)

    def test_result_serializable(self):
        _, executor = build_executor()
        data = executor.run("echo", text="一").to_dict()
        self.assertIn("mode", data)
        self.assertIn("elapsed_ms", data)


class ResultTest(unittest.TestCase):
    def test_perception_shape(self):
        result = ToolResult(
            name="x", ok=True, text="一只猫", data={"text": "喵", "metadata": {"w": 1}},
            source_type="image", confidence=0.9,
        )
        perception = result.to_perception()
        self.assertEqual(perception["type"], "perception_result")
        self.assertEqual(perception["source_type"], "image")
        self.assertTrue(perception["local"])
        self.assertEqual(perception["metadata"], {"w": 1})

    def test_failure_helper(self):
        result = ToolResult.failure("x", "坏了")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "坏了")


if __name__ == "__main__":
    unittest.main()
