"""Capability 统一接口。

核心 Bot 不应该知道 Ollama/Whisper/FFmpeg/OCR 是怎么启动、怎么调用的；
它只需要问："我现在有没有这个能力？"

能力自己负责：探测、启动、常驻、健康检查、降级、结构化结果。
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field


@dataclass
class CapabilityResult:
    """所有能力统一返回结构化结果——**不允许**返回自然语言台词。"""

    success: bool = False
    capability: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    latency_ms: int = 0
    degraded: bool = False
    source_type: str = ""
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "capability": self.capability,
            "data": dict(self.data),
            "error": self.error,
            "latency_ms": self.latency_ms,
            "degraded": self.degraded,
            "source_type": self.source_type,
        }


@dataclass
class CapabilityState:
    """状态只给 Python / 日志 / 诊断用，**不进 LLM Context**。"""

    name: str
    installed: bool = False
    running: bool = False
    healthy: bool = False
    last_used_at: str = ""
    last_error: str = ""
    calls: int = 0
    failures: int = 0

    def to_dict(self) -> dict:
        return {
            "installed": self.installed,
            "running": self.running,
            "healthy": self.healthy,
            "calls": self.calls,
            "failures": self.failures,
            "last_used_at": self.last_used_at,
            "last_error": self.last_error,
        }


class Capability:
    """能力基类：生命周期 + 健康检查 + 执行，全部由能力自己负责。"""

    name = "capability"
    description = ""
    # True 表示"可以按需启动一个常驻进程"（例如 Ollama）；CLI 工具用 False
    persistent = False

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.log = logging.getLogger(self.name)
        self.state = CapabilityState(name=self.name)

    # ── 生命周期 ────────────────────────────────────────────────────
    def installed(self) -> bool:
        return True

    def health_check(self) -> bool:
        return self.installed()

    def ensure_running(self) -> bool:
        """按需启动（常驻服务才有意义）；默认什么都不做。"""
        return self.health_check()

    def start(self) -> bool:
        return self.ensure_running()

    def stop(self) -> bool:
        return True

    def available(self) -> bool:
        """上层只问这一个问题。"""
        if not self.enabled:
            return False
        if not self.installed():
            return False
        if self.health_check():
            return True
        return bool(self.ensure_running() and self.health_check())

    # ── 执行 ────────────────────────────────────────────────────────
    def execute(self, **request) -> CapabilityResult:
        raise NotImplementedError

    def call(self, **request) -> CapabilityResult:
        """带状态、日志、失败后一次自动恢复的调用入口。"""
        started = datetime.datetime.now()
        self.state.calls += 1
        self.log.info("health check")
        if not self.available():
            self.state.healthy = False
            self.state.last_error = "不可用"
            self.state.failures += 1
            self.log.warning("unavailable（缺依赖或服务起不来）")
            return CapabilityResult(
                success=False, capability=self.name, degraded=True,
                error=f"{self.name} 现在不可用",
                latency_ms=int((datetime.datetime.now() - started).total_seconds() * 1000),
            )
        self.state.healthy = True
        try:
            result = self.execute(**request)
        except Exception as exc:  # noqa: BLE001 - 单次失败不致命
            self.state.last_error = f"{type(exc).__name__}: {exc}"
            self.state.failures += 1
            self.log.warning("failed: %s", self.state.last_error)
            return CapabilityResult(
                success=False, capability=self.name, degraded=True,
                error=self.state.last_error,
                latency_ms=int((datetime.datetime.now() - started).total_seconds() * 1000),
            )
        if not isinstance(result, CapabilityResult):
            result = CapabilityResult(success=True, capability=self.name, data={"value": result})
        result.capability = self.name
        if not result.latency_ms:
            result.latency_ms = int((datetime.datetime.now() - started).total_seconds() * 1000)
        self.state.last_used_at = datetime.datetime.now().isoformat(timespec="seconds")
        if not result.success:
            self.state.failures += 1
            self.state.last_error = result.error
        self.log.info("success=%s latency=%dms", result.success, result.latency_ms)
        return result
