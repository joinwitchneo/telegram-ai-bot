"""ActionRequest / Outcome / ActionProvider / ActionRegistry（Phase 6 §十八）。"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field

NO_ACTION = "NO_ACTION"
JOURNAL = "JOURNAL"
MESSAGE = "MESSAGE"

ACTION_KINDS = (JOURNAL, MESSAGE)
OUTWARD_KINDS = (MESSAGE,)

STATUS_EXECUTED = "EXECUTED"
STATUS_SENT = "SENT"
STATUS_SIMULATED = "SIMULATED"
STATUS_SKIPPED = "SKIPPED"
STATUS_REJECTED = "REJECTED"
STATUS_FAILED = "FAILED"


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclass
class ActionRequest:
    action_id: str
    kind: str
    payload: dict = field(default_factory=dict)
    thought_id: str = ""
    trace_id: str = ""
    cycle_id: str = ""
    reason: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_outward(self) -> bool:
        return self.kind in OUTWARD_KINDS


@dataclass
class Outcome:
    action_id: str
    kind: str
    status: str
    detail: str = ""
    simulated: bool = False
    error: str = ""
    trace_id: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def executed(self) -> bool:
        return self.status in (STATUS_EXECUTED, STATUS_SENT, STATUS_SIMULATED)


class ActionProvider:
    """行动接口。Core 只依赖它，不依赖任何具体环境（§十八/§二十八）。"""

    name = "base"
    kind = ""

    def available(self) -> bool:
        return True

    def execute(self, request: ActionRequest) -> Outcome:  # pragma: no cover
        raise NotImplementedError


class ActionRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ActionProvider] = {}

    def register(self, provider: ActionProvider) -> None:
        self._providers[str(provider.kind)] = provider

    def get(self, kind: str) -> ActionProvider | None:
        return self._providers.get(str(kind))

    def kinds(self) -> list:
        return [kind for kind, provider in self._providers.items() if provider.available()]

    def describe(self) -> list:
        return [{"kind": kind, "provider": provider.name, "available": provider.available()}
                for kind, provider in sorted(self._providers.items())]

    def execute(self, request: ActionRequest) -> Outcome:
        provider = self.get(request.kind)
        if provider is None:
            return Outcome(action_id=request.action_id, kind=request.kind,
                           status=STATUS_REJECTED, error="没有对应的 ActionProvider")
        try:
            return provider.execute(request)
        except Exception as exc:  # noqa: BLE001 - 行动失败要变成 Outcome，不是崩溃
            return Outcome(action_id=request.action_id, kind=request.kind,
                           status=STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")
