"""Telegram 环境适配器（Phase 6 §十八：Telegram 只是第一个环境实现）。

它把"发消息"包起来，Core 只通过 Environment 接口调用。
sender 是外部注入的可调用对象（例如 bot.send_message），
所以本模块不需要、也不允许自己去碰网络或 token。
"""

from __future__ import annotations

from v3.environments.base import Environment


class TelegramEnvironment(Environment):
    name = "telegram"

    def __init__(self, *, sender=None, dry_run: bool = False,
                 enabled: bool = True) -> None:
        self._sender = sender
        self.dry_run = bool(dry_run)
        self.enabled = bool(enabled)
        self.sent: list = []

    def available(self) -> bool:
        if not self.enabled:
            return False
        return bool(self.dry_run or self._sender is not None)

    def send_message(self, chat_id, text: str) -> dict:
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "simulated": False, "error": "内容为空"}
        if not self.available():
            return {"ok": False, "simulated": False, "error": "环境不可用"}
        if self.dry_run or self._sender is None:
            self.sent.append({"chat_id": chat_id, "text": text, "simulated": True})
            return {"ok": True, "simulated": True, "error": ""}
        try:
            self._sender(chat_id, text)
        except Exception as exc:  # noqa: BLE001 - 发送失败也要变成结果，不能炸掉生命循环
            return {"ok": False, "simulated": False, "error": f"{type(exc).__name__}: {exc}"}
        self.sent.append({"chat_id": chat_id, "text": text, "simulated": False})
        return {"ok": True, "simulated": False, "error": ""}
