"""LLM 客户端：模型全部来自配置、统一封装调用、回传 usage、失败自动降级。

硬性约束（V2 方案）：
- 不硬编码任何模型名；
- 启动时校验模型可用性，不可用就按 Strong → Main → Cheap 降级；
- 每次调用都要把 usage（含 cache 命中）交给 UsageLogger。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable

TIERS = ("cheap", "main", "strong")


@dataclass
class LLMResult:
    ok: bool
    content: str = ""
    model: str = ""
    tier: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_hit: bool = False
    latency_ms: int = 0
    fallback_from: str = ""
    error: str = ""
    request_id: str = ""
    raw_usage: dict = field(default_factory=dict)


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", **headers}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _http_get_json(url: str, headers: dict, timeout: int) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


class LLMClient:
    def __init__(
        self,
        *,
        backend: str = "deepseek",
        api_key: str = "",
        base_url: str = "https://api.deepseek.com/v1",
        models: dict | None = None,
        fallback_order: tuple[str, ...] = ("strong", "main", "cheap"),
        timeout: int = 60,
        usage_logger=None,
        token_budget=None,
        post: Callable[[str, dict, dict, int], dict] | None = None,
        get: Callable[[str, dict, int], dict] | None = None,
    ) -> None:
        self.backend = backend
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.models = {tier: str((models or {}).get(tier, "") or "") for tier in TIERS}
        self.fallback_order = tuple(t for t in fallback_order if t in TIERS) or ("main", "cheap")
        self.timeout = timeout
        self.usage = usage_logger
        self.budget = token_budget
        self._post = post or _http_post_json
        self._get = get or _http_get_json
        self._available: set[str] | None = None
        self._validate_report: dict = {}

    # ── 模型可用性 ──────────────────────────────────────────────────
    def list_models(self) -> list[str]:
        try:
            if self.backend == "ollama":
                data = self._get(self.base_url + "/api/tags", {}, min(self.timeout, 20))
                return [str(m.get("name", "")) for m in data.get("models", []) if m.get("name")]
            data = self._get(
                self.base_url + "/models",
                {"Authorization": f"Bearer {self.api_key}"},
                min(self.timeout, 20),
            )
            return [str(m.get("id", "")) for m in data.get("data", []) if m.get("id")]
        except Exception as exc:  # noqa: BLE001 - 查不到就当"无法校验"
            logging.warning("无法获取模型列表（将不做校验）：%s", exc)
            return []

    def validate_models(self) -> dict:
        """校验配置的模型是否可用。

        注意：`/models` 接口有时不列全部可用模型（实测 deepseek-v4-flash 能用但不在列表里），
        所以"不在列表"不等于"不可用"——会再做一次极小的真实调用确认，
        只有真的调不通才按降级链顶替。
        """
        available = self.list_models()
        self._available = set(available) if available else None
        effective = dict(self.models)
        report: dict = {"available": available, "fallback": {}, "unlisted_but_working": []}
        if self._available is None:
            report["checked"] = False
            self._validate_report = report
            return report
        report["checked"] = True
        for tier in ("strong", "main", "cheap"):
            model = self.models.get(tier, "")
            if not model:
                continue
            if model in self._available:
                continue
            if self._probe(model):
                report["unlisted_but_working"].append(model)
                logging.info("模型 %s 不在列表里，但实测可用，保留使用", model)
                continue
            # 该档模型不存在 → 找降级链里第一个可用的
            replacement = ""
            for candidate in self.fallback_order:
                name = self.models.get(candidate, "")
                if candidate == tier or not name:
                    continue
                if name in self._available or self._probe(name):
                    replacement = name
                    break
            effective[tier] = replacement
            report["fallback"][tier] = {"configured": model, "using": replacement or "（无可用模型）"}
            logging.warning("模型 %s（%s 档）不可用，改用 %s", model, tier, replacement or "无")
        self.models = effective
        self._validate_report = report
        return report

    def _probe(self, model: str) -> bool:
        """用一次极小调用确认模型到底能不能用。"""
        try:
            if self.backend == "ollama":
                self._post(
                    self.base_url + "/api/chat",
                    {"model": model, "messages": [{"role": "user", "content": "hi"}], "stream": False},
                    {},
                    min(self.timeout, 20),
                )
            else:
                self._post(
                    self.base_url + "/chat/completions",
                    {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
                    {"Authorization": f"Bearer {self.api_key}"},
                    min(self.timeout, 20),
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logging.info("模型 %s 实测失败：%s", model, exc)
            return False

    def resolve_chain(self, tier: str) -> list[str]:
        """降级链：从请求的档位开始**只往下降**（strong→main→cheap）。

        不做"往上补"：cheap 失败不会自动升到 main，否则成本控制就失效了。
        """
        if tier not in TIERS:
            tier = "main"
        hierarchy = [t for t in self.fallback_order if t in TIERS] or ["strong", "main", "cheap"]
        if tier not in hierarchy:
            hierarchy = ["strong", "main", "cheap"]
        chain = hierarchy[hierarchy.index(tier):]
        return [t for t in chain if self.models.get(t)]

    # ── 调用 ────────────────────────────────────────────────────────
    def chat(
        self,
        messages: list[dict],
        *,
        tier: str = "main",
        category: str = "chat",
        max_tokens: int | None = None,
        temperature: float = 1.0,
    ) -> LLMResult:
        request_id = f"req_{uuid.uuid4().hex[:10]}"
        if self.budget is not None:
            allowed, reason = self.budget.can_call(0, max_tokens or 0)
            if not allowed:
                logging.info("预算拦截本轮调用：%s", reason)
                return LLMResult(ok=False, error=reason, tier=tier, request_id=request_id)

        last_error = ""
        chain = self.resolve_chain(tier)
        for index, candidate in enumerate(chain):
            model = self.models.get(candidate, "")
            if not model:
                continue
            started = time.time()
            try:
                payload: dict = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                }
                if max_tokens:
                    payload["max_tokens"] = int(max_tokens)
                if self.backend == "ollama":
                    data = self._post(
                        self.base_url + "/api/chat",
                        {**payload, "options": {"temperature": temperature}},
                        {},
                        self.timeout,
                    )
                    content = str((data.get("message") or {}).get("content", ""))
                    usage_raw = {
                        "prompt_tokens": data.get("prompt_eval_count", 0),
                        "completion_tokens": data.get("eval_count", 0),
                    }
                else:
                    data = self._post(
                        self.base_url + "/chat/completions",
                        payload,
                        {"Authorization": f"Bearer {self.api_key}"},
                        self.timeout,
                    )
                    choices = data.get("choices") or [{}]
                    content = str((choices[0].get("message") or {}).get("content", ""))
                    usage_raw = data.get("usage") or {}
                latency_ms = int((time.time() - started) * 1000)
                if not content.strip():
                    raise RuntimeError("模型返回空内容")
                result = self._build_result(
                    content=content,
                    model=model,
                    tier=candidate,
                    requested_tier=tier,
                    usage_raw=usage_raw,
                    latency_ms=latency_ms,
                    request_id=request_id,
                )
                self._log_usage(result, category)
                return result
            except Exception as exc:  # noqa: BLE001 - 换下一档继续
                last_error = f"{type(exc).__name__}: {exc}"
                logging.warning("模型调用失败（%s/%s）：%s", candidate, model, last_error)
                if index < len(chain) - 1:
                    continue
        if self.usage is not None:
            self.usage.record(category="retry", model="", tier=tier, note=last_error[:120])
        return LLMResult(ok=False, error=last_error or "没有可用模型", tier=tier, request_id=request_id)

    def _build_result(
        self,
        *,
        content: str,
        model: str,
        tier: str,
        requested_tier: str,
        usage_raw: dict,
        latency_ms: int,
        request_id: str,
    ) -> LLMResult:
        input_tokens = int(usage_raw.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage_raw.get("completion_tokens", 0) or 0)
        cached = int(
            usage_raw.get("prompt_cache_hit_tokens", usage_raw.get("cached_tokens", 0)) or 0
        )
        return LLMResult(
            ok=True,
            content=content.strip(),
            model=model,
            tier=tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            cache_hit=cached > 0,
            latency_ms=latency_ms,
            fallback_from=requested_tier if tier != requested_tier else "",
            request_id=request_id,
            raw_usage=usage_raw,
        )

    def _log_usage(self, result: LLMResult, category: str) -> None:
        if self.usage is not None:
            self.usage.record(
                category=category,
                model=result.model,
                tier=result.tier,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_tokens=result.cached_tokens,
                latency_ms=result.latency_ms,
                fallback_from=result.fallback_from,
                retry=(str(category) == "retry"),
                request_id=result.request_id,
            )
        if self.budget is not None:
            self.budget.record(result.input_tokens + result.output_tokens)
