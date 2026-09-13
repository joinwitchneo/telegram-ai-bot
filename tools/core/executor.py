"""Tool Executor：统一入口。缓存 -> 权限 -> 超时 -> 执行 -> 错误兜底 -> 记成本。"""

from __future__ import annotations

import json
import logging
import threading
import time

from tools.core.cache import TTLCache
from tools.core.cost import CostTracker
from tools.core.permissions import Permissions
from tools.core.registry import ToolRegistry
from tools.core.result import ToolResult


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        cache: TTLCache | None = None,
        cost: CostTracker | None = None,
        permissions: Permissions | None = None,
        timeout: float = 12.0,
        enabled: bool = True,
    ) -> None:
        self.registry = registry
        self.cache = cache
        self.cost = cost
        self.permissions = permissions or Permissions()
        self.timeout = max(1.0, float(timeout))
        self.enabled = bool(enabled)
        self._lock = threading.Lock()

    @staticmethod
    def cache_key(name: str, kwargs: dict) -> str:
        try:
            payload = json.dumps(kwargs, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            payload = str(sorted((str(k), str(v)) for k, v in kwargs.items()))
        return f"{name}:{payload}"

    # -- 主入口 -------------------------------------------------------
    def run(self, tool: str, **kwargs) -> ToolResult:
        """tool 是工具名；其余关键字原样传给工具，所以工具名不能和参数名撞车。"""
        started = time.time()
        entry = self.registry.get(tool)
        if entry is None:
            return ToolResult.failure(tool, "没有这个工具")
        if not self.enabled:
            return ToolResult.failure(
                tool, "工具层已关闭", mode=entry.spec.mode, level=entry.spec.level
            )

        missing = entry.spec.missing_requirements()
        if missing:
            self._record(tool, entry.spec.mode, error=True)
            return ToolResult.failure(
                tool, f"缺少本机依赖：{', '.join(missing)}",
                mode=entry.spec.mode, level=entry.spec.level,
            )

        allowed, why = self.permissions.check(tool, entry.spec.mode)
        if not allowed:
            self._record(tool, entry.spec.mode, error=True)
            return ToolResult.failure(tool, why, mode=entry.spec.mode, level=entry.spec.level)

        key = self.cache_key(tool, kwargs)
        if self.cache is not None and entry.spec.cache_ttl > 0:
            cached = self.cache.get(key)
            if cached is not None:
                self._record(tool, entry.spec.mode, cache_hit=True)
                return ToolResult(
                    name=tool, ok=True, mode=entry.spec.mode, level=entry.spec.level,
                    text=str(cached.get("text", "")), data=dict(cached.get("data", {})),
                    cached=True, elapsed_ms=int((time.time() - started) * 1000),
                    source_type=str(cached.get("source_type", "")),
                    confidence=float(cached.get("confidence", 0.0) or 0.0),
                )

        try:
            result = self._run_with_timeout(entry.handler, kwargs)
        except Exception as exc:  # noqa: BLE001 - 单个工具炸了不能影响聊天
            logging.warning("工具 %s 执行失败：%s", tool, exc)
            result = ToolResult.failure(
                tool, f"{type(exc).__name__}: {exc}", mode=entry.spec.mode, level=entry.spec.level
            )
        if not isinstance(result, ToolResult):
            result = ToolResult(name=tool, ok=True, text=str(result), data={"value": result})
        result.name = tool
        result.mode = entry.spec.mode
        result.level = entry.spec.level
        result.elapsed_ms = int((time.time() - started) * 1000)

        if result.ok and self.cache is not None and entry.spec.cache_ttl > 0:
            self.cache.set(key, {
                "text": result.text,
                "data": result.data,
                "source_type": result.source_type,
                "confidence": result.confidence,
            }, entry.spec.cache_ttl)
        self._record(
            tool, entry.spec.mode,
            llm_tokens=result.llm_tokens, http_requests=result.http_requests,
            error=not result.ok,
        )
        return result

    def _run_with_timeout(self, handler, kwargs: dict) -> ToolResult:
        box: dict = {}

        def target() -> None:
            try:
                box["value"] = handler(**kwargs)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        thread = threading.Thread(target=target, name="tool-run", daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            raise TimeoutError(f"工具超时（超过 {self.timeout:.0f} 秒）")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _record(self, name: str, mode: str, **kwargs) -> None:
        if self.cost is None:
            return
        self.cost.record(name, mode=mode, **kwargs)
