"""Sticker 使用历史（S1 只记录，不做学习）。"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_text

MAX_RECORDS = 500


class StickerHistory:
    def __init__(self, path: Path, *, max_records: int = MAX_RECORDS) -> None:
        self.path = Path(path)
        self.max_records = max(20, int(max_records))
        self._lock = threading.RLock()
        self.records: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.records = [item for item in data if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            logging.warning("[sticker] history 损坏，从空开始")

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self.records[-self.max_records :], ensure_ascii=False, indent=1)
        try:
            atomic_write_text(self.path, payload)
        except OSError as exc:
            logging.warning("[sticker] 写 history 失败：%s", exc)

    def record(
        self,
        *,
        file_unique_id: str,
        chat_id: int | str = "",
        intent: str = "",
        score: float = 0.0,
        source: str = "",
        sent: bool = False,
        now: datetime.datetime | None = None,
    ) -> dict:
        moment = now or datetime.datetime.now()
        entry = {
            "file_unique_id": str(file_unique_id),
            "chat_id": str(chat_id),
            "intent": str(intent),
            "score": round(float(score), 4),
            "source": str(source),
            "sent": bool(sent),
            "ts": moment.isoformat(timespec="seconds"),
        }
        with self._lock:
            self.records.append(entry)
            self.records = self.records[-self.max_records :]
        self.save()
        return entry

    def recent(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self.records[-max(1, int(limit)) :]]

    def last_sent_at(self, file_unique_id: str) -> str:
        key = str(file_unique_id)
        with self._lock:
            for item in reversed(self.records):
                if item.get("file_unique_id") == key and item.get("sent"):
                    return str(item.get("ts", ""))
        return ""

    def recent_ids(self, limit: int = 5) -> list[str]:
        with self._lock:
            ids = [str(item.get("file_unique_id", "")) for item in self.records[-max(1, int(limit)) :]]
        return [item for item in ids if item]

    def stats(self) -> dict:
        with self._lock:
            total = len(self.records)
            sent = sum(1 for item in self.records if item.get("sent"))
            by_intent: dict[str, int] = {}
            for item in self.records:
                intent = str(item.get("intent") or "")
                if intent:
                    by_intent[intent] = by_intent.get(intent, 0) + 1
        return {"records": total, "sent": sent, "by_intent": by_intent}
