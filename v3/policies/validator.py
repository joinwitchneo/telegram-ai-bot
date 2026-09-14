"""ActionValidator（Phase 6 §十九）。

所有行动都必须过这一关：是否允许 / 是否可用 / 是否超预算 / 是否冷却 /
环境是否允许 / 参数是否有效。任何失败都变成"被拒的 Outcome"，而不是异常。
"""

from __future__ import annotations

from dataclasses import dataclass

from v3.actions.base import ACTION_KINDS, MESSAGE

MAX_MESSAGE_CHARS = 800


@dataclass
class ActionVerdict:
    ok: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason}


class ActionValidator:
    def __init__(
        self,
        *,
        registry,
        budget=None,
        environment=None,
        mode_getter=None,
        allowed_kinds=ACTION_KINDS,
        max_message_chars: int = MAX_MESSAGE_CHARS,
    ) -> None:
        self.registry = registry
        self.budget = budget
        self.environment = environment
        self.mode_getter = mode_getter or (lambda: "observe")
        self.allowed_kinds = tuple(allowed_kinds)
        self.max_message_chars = max(20, int(max_message_chars))

    def check(self, request, *, now=None) -> ActionVerdict:
        kind = str(request.kind)
        if kind not in self.allowed_kinds:
            return ActionVerdict(False, f"行动类型 {kind} 不允许")
        provider = self.registry.get(kind) if self.registry is not None else None
        if provider is None:
            return ActionVerdict(False, f"没有 {kind} 的 ActionProvider")
        if not provider.available():
            return ActionVerdict(False, f"{kind} 当前不可用")

        mode = str(self.mode_getter() or "observe").lower()
        if request.is_outward and mode in ("observe", "off", ""):
            label = "观察档" if mode in ("observe", "off", "") else f"{mode} 档"
            return ActionVerdict(False, f"{label}不允许对外行动（只观察）")
        if self.environment is not None and request.is_outward and not self.environment.available():
            return ActionVerdict(False, "环境不可用")

        if kind == MESSAGE:
            content = str(request.payload.get("content", "")).strip()
            if not content:
                return ActionVerdict(False, "消息内容为空")
            if len(content) > self.max_message_chars:
                return ActionVerdict(False, f"消息过长（{len(content)} 字）")
            if request.payload.get("chat_id") in (None, "") and not getattr(
                    provider, "chat_id", None):
                return ActionVerdict(False, "没有目标会话")
            if self.budget is not None:
                allowed, why = self.budget.can_message(now=now)
                if not allowed:
                    return ActionVerdict(False, why)
        return ActionVerdict(True, "允许")
