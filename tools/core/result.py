"""ToolResult：所有工具的返回值都必须长这个样子。

不管是本地 Python、本地模型还是联网 HTTP，出去的都是同一个结构，
这样 Context / Response Planner 不需要知道背后是谁在干活。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolResult:
    name: str
    ok: bool = True
    mode: str = "LOCAL"
    level: str = "L0"
    text: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    cached: bool = False
    elapsed_ms: int = 0
    llm_tokens: int = 0
    http_requests: int = 0
    source_type: str = ""
    confidence: float = 0.0

    def to_perception(self) -> dict:
        """统一感知结构（图片 / 视频 / 语音 / 文档都走这里）。"""
        return {
            "type": "perception_result",
            "source_type": self.source_type or str(self.data.get("source_type", "")),
            "summary": self.text,
            "text": str(self.data.get("text", "")),
            "metadata": dict(self.data.get("metadata", {})),
            "confidence": float(self.confidence or self.data.get("confidence", 0.0) or 0.0),
            "local": self.mode == "LOCAL",
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "mode": self.mode,
            "level": self.level,
            "text": self.text,
            "data": dict(self.data),
            "error": self.error,
            "cached": self.cached,
            "elapsed_ms": self.elapsed_ms,
            "llm_tokens": self.llm_tokens,
            "http_requests": self.http_requests,
        }

    @classmethod
    def failure(cls, name: str, error: str, *, mode: str = "LOCAL", level: str = "L0") -> "ToolResult":
        return cls(name=name, ok=False, mode=mode, level=level, error=str(error)[:200])
