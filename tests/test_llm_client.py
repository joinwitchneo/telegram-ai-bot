import tempfile
import unittest
from pathlib import Path

from core.llm_client import LLMClient
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger


def make_responder(models=None, fail_tiers=(), usage=None):
    """构造假的 HTTP 层：记录调用并返回指定结构。"""
    available = models or ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"]
    calls = []

    def fake_get(url, headers, timeout):
        return {"data": [{"id": name} for name in available]}

    def fake_post(url, payload, headers, timeout):
        model = payload.get("model", "")
        calls.append({"model": model, "payload": payload})
        if model in fail_tiers:
            raise RuntimeError(f"模型 {model} 不可用")
        body_usage = usage or {
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 300,
        }
        return {"choices": [{"message": {"content": "好，我在。"}}], "usage": body_usage}

    return fake_post, fake_get, calls


class ClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.usage = UsageLogger(self.tmp / "usage.json")
        self.budget = TokenBudget(daily_token_budget=100000)

    def build(self, models, available=None, fail=(), budget=None):
        post, get, calls = make_responder(models=available, fail_tiers=fail)
        client = LLMClient(
            api_key="k",
            models=models,
            usage_logger=self.usage,
            token_budget=budget or self.budget,
            post=post,
            get=get,
        )
        return client, calls

    def test_chat_success_parses_usage(self):
        client, calls = self.build({"cheap": "deepseek-flash", "main": "deepseek-v4-flash", "strong": ""})
        result = client.chat([{"role": "user", "content": "在吗"}], tier="main", category="chat")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "好，我在。")
        self.assertEqual(result.model, "deepseek-v4-flash")
        self.assertEqual(result.input_tokens, 1200)
        self.assertEqual(result.output_tokens, 80)
        self.assertEqual(result.cached_tokens, 900)
        self.assertTrue(result.cache_hit)
        self.assertEqual(len(calls), 1)
        day = self.usage.day()
        self.assertEqual(day["input_tokens"], 1200)
        self.assertEqual(day["cache_hit"], 1)

    def test_budget_records_tokens(self):
        client, _ = self.build({"cheap": "deepseek-flash", "main": "deepseek-v4-flash", "strong": ""})
        client.chat([{"role": "user", "content": "hi"}], tier="main")
        self.assertEqual(self.budget.used_today(), 1280)

    def test_fallback_when_model_fails(self):
        client, calls = self.build(
            {"cheap": "deepseek-flash", "main": "deepseek-v4-flash", "strong": "deepseek-v4-pro"},
            fail=("deepseek-v4-pro",),
        )
        result = client.chat([{"role": "user", "content": "hi"}], tier="strong")
        self.assertTrue(result.ok)
        self.assertEqual(result.model, "deepseek-v4-flash")
        self.assertEqual(result.fallback_from, "strong")
        self.assertEqual([c["model"] for c in calls], ["deepseek-v4-pro", "deepseek-v4-flash"])
        self.assertEqual(self.usage.day()["fallbacks"], 1)

    def test_all_models_fail_returns_error(self):
        client, calls = self.build(
            {"cheap": "deepseek-flash", "main": "deepseek-v4-flash", "strong": ""},
            fail=("deepseek-flash", "deepseek-v4-flash"),
        )
        result = client.chat([{"role": "user", "content": "hi"}], tier="main")
        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        # 主模型失败后按降级链试过便宜模型，两个都失败才报错
        self.assertEqual([c["model"] for c in calls], ["deepseek-v4-flash", "deepseek-flash"])
        self.assertEqual(self.usage.day()["by_category"].get("retry", {}).get("requests"), 1)

    def test_validate_models_replaces_missing(self):
        client, _ = self.build(
            {"cheap": "ghost-model", "main": "deepseek-v4-flash", "strong": ""},
            available=["deepseek-v4-flash"],
            fail=("ghost-model",),
        )
        report = client.validate_models()
        self.assertTrue(report["checked"])
        self.assertEqual(client.models["cheap"], "deepseek-v4-flash")
        self.assertIn("cheap", report["fallback"])

    def test_unlisted_but_working_model_is_kept(self):
        """不在 /models 列表里但实际能用的模型要保留（线上就遇到过这种情况）。"""
        client, _ = self.build(
            {"cheap": "deepseek-flash", "main": "deepseek-v4-flash", "strong": ""},
            available=["deepseek-flash"],
        )
        report = client.validate_models()
        self.assertEqual(client.models["main"], "deepseek-v4-flash")
        self.assertIn("deepseek-v4-flash", report["unlisted_but_working"])
        self.assertEqual(report["fallback"], {})

    def test_validate_models_without_api(self):
        def failing_get(url, headers, timeout):
            raise RuntimeError("no network")

        client = LLMClient(api_key="k", models={"main": "m"}, get=failing_get, usage_logger=self.usage)
        report = client.validate_models()
        self.assertFalse(report["checked"])
        self.assertEqual(client.models["main"], "m")

    def test_resolve_chain_order(self):
        client, _ = self.build({"cheap": "c", "main": "m", "strong": "s"})
        self.assertEqual(client.resolve_chain("strong"), ["strong", "main", "cheap"])
        self.assertEqual(client.resolve_chain("main"), ["main", "cheap"])
        self.assertEqual(client.resolve_chain("cheap"), ["cheap"])
        self.assertEqual(client.resolve_chain("unknown"), ["main", "cheap"])

    def test_resolve_chain_skips_empty_models(self):
        client, _ = self.build({"cheap": "c", "main": "m", "strong": ""})
        self.assertEqual(client.resolve_chain("strong"), ["main", "cheap"])

    def test_budget_hard_blocks_call(self):
        budget = TokenBudget(daily_token_budget=1000)
        budget.record(999)
        client, calls = self.build(
            {"cheap": "deepseek-flash", "main": "deepseek-v4-flash", "strong": ""}, budget=budget
        )
        result = client.chat([{"role": "user", "content": "hi"}], tier="main")
        self.assertFalse(result.ok)
        self.assertIn("硬上限", result.error)
        self.assertEqual(calls, [])

    def test_empty_content_is_treated_as_failure(self):
        def fake_post(url, payload, headers, timeout):
            return {"choices": [{"message": {"content": "   "}}], "usage": {}}

        def fake_get(url, headers, timeout):
            return {"data": [{"id": "m"}]}

        client = LLMClient(api_key="k", models={"main": "m"}, post=fake_post, get=fake_get, usage_logger=self.usage)
        result = client.chat([{"role": "user", "content": "hi"}], tier="main")
        self.assertFalse(result.ok)

    def test_ollama_backend_path(self):
        captured = {}

        def fake_post(url, payload, headers, timeout):
            captured["url"] = url
            captured["payload"] = payload
            return {"message": {"content": "嗯"}, "prompt_eval_count": 10, "eval_count": 5}

        client = LLMClient(backend="ollama", base_url="http://127.0.0.1:11434", models={"main": "qwen"},
                           post=fake_post, usage_logger=self.usage)
        result = client.chat([{"role": "user", "content": "hi"}], tier="main")
        self.assertTrue(result.ok)
        self.assertIn("/api/chat", captured["url"])
        self.assertEqual(result.input_tokens, 10)


if __name__ == "__main__":
    unittest.main()
