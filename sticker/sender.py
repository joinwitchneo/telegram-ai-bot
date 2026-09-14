"""Sticker Sender：真正发送（S1 只提供能力，默认不自动发）。"""

from __future__ import annotations

import logging


class StickerSender:
    def __init__(self, send_callback, *, dry_run: bool = True) -> None:
        self.send = send_callback          # 形如 bot.send_sticker(chat_id, file_id)
        self.dry_run = bool(dry_run)
        self.last_error = ""

    def send_sticker(self, chat_id: int, file_id: str) -> bool:
        """发送贴纸。失败绝不抛异常、绝不影响主聊天。"""
        if not file_id:
            self.last_error = "file_id 为空"
            return False
        if self.dry_run:
            logging.info("[sticker] dry_run：本应发送 %s（未真的发送）", str(file_id)[:24])
            return False
        try:
            self.send(chat_id, file_id)
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - 发贴纸失败不能影响聊天
            self.last_error = f"{type(exc).__name__}: {exc}"
            logging.warning("[sticker] 发送失败：%s", self.last_error)
            return False
