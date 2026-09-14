"""inner_journal：V3 唯一的对外可见痕迹（本地文件，不发 Telegram）。"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from pathlib import Path


class InnerJournal:
    def __init__(self, path: Path, *, keep: int = 1000) -> None:
        self.path = Path(path)
        self.keep = max(50, int(keep))
        self._lock = threading.RLock()
        self._count = self._count_lines()

    def write(self, *, cycle_id: str = "", kind: str = "note", content: str = "",
              meta: dict | None = None, now: datetime.datetime | None = None) -> dict:
        entry = {
            "cycle_id": str(cycle_id),
            "kind": str(kind),
            "content": str(content or "")[:400],
            "meta": dict(meta or {}),
            "timestamp": (now or datetime.datetime.now()).isoformat(timespec="seconds"),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._count += 1
        return entry

    def _count_lines(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def recent(self, limit: int = 5) -> list:
        if not self.path.is_file():
            return []
        items = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        logging.debug("[v3] 跳过损坏的 journal 行")
        except OSError:
            return []
        return items[-max(1, int(limit)) :]

    def count(self) -> int:
        with self._lock:
            return self._count
