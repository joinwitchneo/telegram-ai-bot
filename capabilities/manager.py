"""CapabilityManager：登记所有能力，回答"有没有"，并统一转发调用。"""

from __future__ import annotations

from capabilities.base import Capability, CapabilityResult


class CapabilityManager:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self._items: dict[str, Capability] = {}

    def register(self, capability: Capability) -> None:
        self._items[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return sorted(self._items)

    def has(self, name: str) -> bool:
        """上层唯一需要知道的事：我现在有没有这个能力。"""
        if not self.enabled:
            return False
        capability = self._items.get(name)
        return bool(capability and capability.available())

    def execute(self, name: str, **request) -> CapabilityResult:
        capability = self._items.get(name)
        if capability is None or not self.enabled:
            return CapabilityResult(
                success=False, capability=name, degraded=True, error=f"没有登记这个能力：{name}"
            )
        return capability.call(**request)

    def states(self) -> dict:
        return {name: item.state.to_dict() for name, item in sorted(self._items.items())}

    def report(self) -> dict:
        """诊断用：装了什么、能不能用。不给 LLM。"""
        return {
            "capabilities": {
                name: {
                    "description": item.description,
                    "installed": item.installed(),
                    "available": item.available(),
                    "persistent": item.persistent,
                    "state": item.state.to_dict(),
                }
                for name, item in sorted(self._items.items())
            }
        }
