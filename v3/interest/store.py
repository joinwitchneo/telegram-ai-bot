"""InterestStore：原子写；兴趣不会无限膨胀（按证据数淘汰最低分话题）。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_text
from v3.interest.models import TopicInterest

MAX_TOPICS = 200


class InterestStore:
    def __init__(self, path: Path, *, max_topics: int = MAX_TOPICS) -> None:
        self.path = Path(path)
        self.max_topics = max(10, int(max_topics))
        self._lock = threading.RLock()
        self.items: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.items = dict(data.get("topics") or {})
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] interests.json 损坏，从空开始")

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps({"topics": self.items}, ensure_ascii=False, indent=1)
        try:
            atomic_write_text(self.path, snapshot)
        except OSError as exc:
            logging.warning("[v3] 写 interests.json 失败：%s", exc)

    def get(self, topic: str) -> TopicInterest | None:
        with self._lock:
            item = self.items.get(str(topic))
        return TopicInterest.from_dict(item) if item else None

    def upsert(self, interest: TopicInterest) -> TopicInterest:
        with self._lock:
            self.items[interest.topic] = interest.to_dict()
            self._trim()
        self.save()
        return TopicInterest.from_dict(self.items[interest.topic])

    def _trim(self) -> None:
        if len(self.items) <= self.max_topics:
            return
        ordered = sorted(
            self.items.items(),
            key=lambda kv: (int(kv[1].get("evidence", 0)), abs(float(kv[1].get("attraction", 0.5)) - 0.5)),
        )
        for key, _ in ordered[: len(self.items) - self.max_topics]:
            self.items.pop(key, None)

    def all(self) -> list[TopicInterest]:
        with self._lock:
            return [TopicInterest.from_dict(item) for item in self.items.values()]

    def count(self) -> int:
        with self._lock:
            return len(self.items)

    def top(self, *, limit: int = 8) -> list[TopicInterest]:
        items = self.all()
        items.sort(key=lambda item: (-item.attraction, item.topic))
        return items[: max(1, int(limit))]

    def stats(self) -> dict:
        items = self.all()
        return {
            "topics": len(items),
            "curious_but_negative": sum(1 for i in items if i.valence < 0 and i.curiosity > 0.5),
        }
