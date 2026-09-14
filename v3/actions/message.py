"""MessageAction：对外说话的接口实现（Phase 6 §十八/§二十）。

它只认识 Environment 接口，**不认识 Telegram**。
"""

from __future__ import annotations

from v3.actions.base import (
    MESSAGE,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SIMULATED,
    ActionProvider,
    ActionRequest,
    Outcome,
)


class MessageAction(ActionProvider):
    name = "message"
    kind = MESSAGE

    def __init__(self, *, environment, chat_id=None, enabled: bool = True) -> None:
        self.environment = environment
        self.chat_id = chat_id
        self.enabled = bool(enabled)

    def available(self) -> bool:
        return bool(self.enabled and self.environment is not None and self.environment.available())

    def execute(self, request: ActionRequest) -> Outcome:
        content = str(request.payload.get("content", "")).strip()
        if not content:
            return Outcome(action_id=request.action_id, kind=self.kind, status=STATUS_FAILED,
                           error="没有要说的内容")
        chat_id = request.payload.get("chat_id", self.chat_id)
        if chat_id is None:
            return Outcome(action_id=request.action_id, kind=self.kind, status=STATUS_FAILED,
                           error="没有目标会话")
        result = self.environment.send_message(chat_id, content)
        if not result.get("ok"):
            return Outcome(action_id=request.action_id, kind=self.kind, status=STATUS_FAILED,
                           error=str(result.get("error", "发送失败")))
        simulated = bool(result.get("simulated"))
        return Outcome(action_id=request.action_id, kind=self.kind,
                       status=STATUS_SIMULATED if simulated else STATUS_SENT,
                       detail=content[:120], simulated=simulated)
