"""JournalAction：写一条内在日志（Phase 6 §十八，非对外行动）。"""

from __future__ import annotations

from v3.actions.base import (
    JOURNAL,
    STATUS_EXECUTED,
    STATUS_FAILED,
    ActionProvider,
    ActionRequest,
    Outcome,
)


class JournalAction(ActionProvider):
    name = "journal"
    kind = JOURNAL

    def __init__(self, *, journal, enabled: bool = True) -> None:
        self.journal = journal
        self.enabled = bool(enabled)

    def available(self) -> bool:
        return bool(self.enabled and self.journal is not None)

    def execute(self, request: ActionRequest) -> Outcome:
        content = str(request.payload.get("content", "") or request.reason)
        if not content:
            return Outcome(action_id=request.action_id, kind=self.kind, status=STATUS_FAILED,
                           error="没有可写的内容")
        try:
            self.journal.write(
                cycle_id=request.cycle_id, kind="action_journal", content=content,
                meta={"thought_id": request.thought_id, "trace_id": request.trace_id,
                      "action_id": request.action_id},
            )
        except Exception as exc:  # noqa: BLE001
            return Outcome(action_id=request.action_id, kind=self.kind, status=STATUS_FAILED,
                           error=f"{type(exc).__name__}: {exc}")
        return Outcome(action_id=request.action_id, kind=self.kind, status=STATUS_EXECUTED,
                       detail=content[:120])
