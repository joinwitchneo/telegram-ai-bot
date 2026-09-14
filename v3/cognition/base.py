"""CognitiveProvider 接口与数据结构（Phase 6 §十四/§十五/§十六）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# LLM 只能提议这几种意图；其它一律当 none（不允许它决定"执行什么"）
INTENTION_TYPES = ("none", "journal", "message")


def _clamp(value, low=0.0, high=1.0, default=0.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class Intention:
    """LLM 的"打算"——只是提议，DecisionEngine 才决定要不要兑现。"""

    type: str = "none"
    reason: str = ""
    confidence: float = 0.0
    content: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CognitiveContext:
    """交给认知器官的上下文：只放相关的东西，不把全部历史塞进去（§十五）。"""

    trace_id: str = ""
    cycle_id: str = ""
    candidate: dict = field(default_factory=dict)
    related_thoughts: list = field(default_factory=list)
    memories: list = field(default_factory=list)
    internal_state: dict = field(default_factory=dict)
    recent_outcomes: list = field(default_factory=list)
    available_actions: list = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    observations: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary_lines(self) -> list:
        """紧凑的可读摘要（也用于 prompt / 日志，不包含任何执行指令）。"""
        lines = []
        candidate = self.candidate or {}
        lines.append(f"触发念头：{candidate.get('content', '')}"
                     f"（话题 {candidate.get('topic', '-')}，触发分 {candidate.get('trigger_score')}）")
        if self.related_thoughts:
            lines.append("相关念头：" + "；".join(
                str(item.get("content", ""))[:40] for item in self.related_thoughts[:3]))
        if self.observations:
            lines.append("最近的聊天观测：" + "；".join(
                str(item.get("user_message", ""))[:40] for item in self.observations[-4:]))
        if self.internal_state:
            lines.append("内部状态：" + "；".join(
                f"{k}={v}" for k, v in list(self.internal_state.items())[:6]))
        if self.recent_outcomes:
            lines.append("最近行动结果：" + "；".join(
                f"{item.get('kind')}:{item.get('status')}" for item in self.recent_outcomes[-3:]))
        lines.append("允许的意图类型：" + "/".join(INTENTION_TYPES) + "（不允许其它）")
        return lines


@dataclass
class CognitiveResult:
    """认知器官的产出：结构化 Proposal。任何字段都不能直接变成行动。"""

    ok: bool = False
    provider: str = "base"
    intention: Intention = field(default_factory=Intention)
    new_thoughts: list = field(default_factory=list)
    thought_update: dict = field(default_factory=dict)
    llm_calls: int = 0
    error: str = ""
    raw: str = ""
    rejected: list = field(default_factory=list)
    trace_id: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "provider": self.provider,
            "intention": self.intention.to_dict(),
            "new_thoughts": list(self.new_thoughts),
            "thought_update": dict(self.thought_update),
            "llm_calls": self.llm_calls, "error": self.error,
            "rejected": list(self.rejected), "trace_id": self.trace_id,
        }


class CognitiveProvider:
    """认知器官接口。Core 只依赖这个接口（§十四）。"""

    name = "base"

    def available(self) -> bool:
        return True

    def process(self, context: CognitiveContext) -> CognitiveResult:  # pragma: no cover
        raise NotImplementedError


class MockCognitiveProvider(CognitiveProvider):
    """确定性替身：测试与 dry_run 用，不联网、不花钱。"""

    name = "mock"

    def __init__(self, *, intention_type: str = "journal", confidence: float = 0.8,
                 content: str = "", new_thoughts=None, thought_update=None,
                 ok: bool = True, error: str = "", available: bool = True) -> None:
        self.intention_type = intention_type if intention_type in INTENTION_TYPES else "none"
        self.confidence = _clamp(confidence)
        self.content = content
        self.new_thoughts = list(new_thoughts or [])
        self.thought_update = dict(thought_update or {})
        self._ok = bool(ok)
        self.error = error
        self._available = bool(available)
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def process(self, context: CognitiveContext) -> CognitiveResult:
        self.calls += 1
        if not self._ok:
            return CognitiveResult(ok=False, provider=self.name, error=self.error,
                                   llm_calls=0, trace_id=context.trace_id)
        return CognitiveResult(
            ok=True, provider=self.name,
            intention=Intention(type=self.intention_type, reason="mock",
                                confidence=self.confidence, content=self.content),
            new_thoughts=[dict(item) for item in self.new_thoughts],
            thought_update=dict(self.thought_update),
            llm_calls=0, trace_id=context.trace_id,
        )
