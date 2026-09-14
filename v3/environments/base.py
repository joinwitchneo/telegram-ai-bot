"""Environment 接口（Phase 6 §二十八）。"""

from __future__ import annotations


class Environment:
    name = "base"

    def available(self) -> bool:
        return False

    def send_message(self, chat_id, text: str) -> dict:  # pragma: no cover
        """返回 {"ok": bool, "simulated": bool, "error": str}。"""
        raise NotImplementedError


class NullEnvironment(Environment):
    """没有接任何环境时的占位：永远不可用。"""

    name = "null"

    def available(self) -> bool:
        return False

    def send_message(self, chat_id, text: str) -> dict:
        return {"ok": False, "simulated": False, "error": "没有接入任何环境"}
