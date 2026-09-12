"""备忘录存储：给每个会话保存可以随时翻看的资料型笔记。

和提醒（reminders.py）的区别：备忘录没有时间，只负责"记下来、以后能查"。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path


class MemoStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: list[dict] = []
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    self.data = loaded
            except (json.JSONDecodeError, OSError):
                self.data = []

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=2)
        try:
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save memos: %s", exc)

    def add(self, chat_id: int, text: str) -> dict:
        with self._lock:
            item = {
                "id": max((m.get("id", 0) for m in self.data), default=0) + 1,
                "chat_id": chat_id,
                "text": text.strip(),
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            self.data.append(item)
        self.save()
        return item

    def remove(self, memo_id: int) -> dict | None:
        with self._lock:
            target = next((m for m in self.data if m.get("id") == memo_id), None)
            if target is None:
                return None
            self.data = [m for m in self.data if m.get("id") != memo_id]
        self.save()
        return target

    def for_chat(self, chat_id: int) -> list[dict]:
        with self._lock:
            return [dict(m) for m in self.data if m.get("chat_id") == chat_id]

    def get(self, chat_id: int, memo_id: int) -> dict | None:
        with self._lock:
            for item in self.data:
                if item.get("chat_id") == chat_id and item.get("id") == memo_id:
                    return dict(item)
        return None
