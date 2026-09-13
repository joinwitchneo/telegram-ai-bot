"""Tool Registry：所有工具的登记处，顺便回答"这个能力现在能不能用"。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tools.core.schema import ToolSpec


@dataclass
class Tool:
    spec: ToolSpec
    handler: Callable

    @property
    def name(self) -> str:
        return self.spec.name


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, spec: ToolSpec, handler: Callable) -> None:
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        self._tools[spec.name] = Tool(spec=spec, handler=handler)

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def by_mode(self, mode: str) -> list[Tool]:
        return [tool for tool in self._tools.values() if tool.spec.mode == mode]

    def by_level(self, level: str) -> list[Tool]:
        return [tool for tool in self._tools.values() if tool.spec.level == level]

    def available(self, name: str) -> bool:
        tool = self.get(name)
        return bool(tool and tool.spec.available())

    def describe(self) -> list[dict]:
        return [tool.spec.to_dict() for tool in sorted(self._tools.values(), key=lambda t: t.name)]

    def cost_report(self) -> dict:
        """按成本等级统计工具数量 + 哪些因为缺依赖暂时用不了。"""
        report: dict[str, list[str]] = {"LOCAL": [], "FREE_NETWORK": [], "PAID_API": []}
        unavailable: list[str] = []
        for tool in self._tools.values():
            report.setdefault(tool.spec.mode, []).append(tool.name)
            if not tool.spec.available():
                unavailable.append(tool.name)
        return {
            "by_mode": {key: sorted(value) for key, value in report.items()},
            "unavailable": sorted(unavailable),
            "total": len(self._tools),
        }

    def with_keywords(self) -> list[Tool]:
        return [tool for tool in self._tools.values() if tool.spec.keywords]
