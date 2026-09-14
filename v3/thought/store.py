"""ThoughtStore：原子写、归档、有限召回。"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_text
from v3.thought.models import Thought

MAX_ACTIVE = 200


class ThoughtStore:
    def __init__(self, path: Path, *, archive_path: Path | None = None, max_active: int = MAX_ACTIVE) -> None:
        self.path = Path(path)
        self.archive_path = Path(archive_path) if archive_path else self.path.with_name("thoughts_archive.json")
        self.max_active = max(20, int(max_active))
        self._lock = threading.RLock()
        self.items: dict[str, dict] = {}
        self._load()

    # ── 读写 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.items = dict(data.get("thoughts") or {})
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] thoughts.json 损坏，从空开始")

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps({"thoughts": self.items}, ensure_ascii=False, indent=1)
        try:
            atomic_write_text(self.path, snapshot)
        except OSError as exc:
            logging.warning("[v3] 写 thoughts.json 失败：%s", exc)

    def update(self, thought: Thought) -> None:
        """把内存里演化过的念头写回（不经 add 的去重逻辑）。"""
        self.update_many([thought])

    def update_many(self, thoughts) -> int:
        """批量写回，最后只落盘一次（检查点每轮都要写回全部念头）。"""
        written = 0
        with self._lock:
            for thought in thoughts or []:
                if not getattr(thought, "id", ""):
                    continue
                self.items[thought.id] = thought.to_dict()
                written += 1
        if written:
            self.save()
        return written

    def _next_id(self) -> str:
        biggest = 0
        for key in self.items:
            try:
                biggest = max(biggest, int(str(key).split("_")[-1]))
            except ValueError:
                continue
        return f"th_{biggest + 1:06d}"

    # ── 写入 ────────────────────────────────────────────────────────
    def add(self, thought: Thought) -> tuple[Thought, str]:
        """同 topic 且内容高度重复时视为"再次想到"，累加 seen_count 而不是新建。"""
        with self._lock:
            for existing in self.items.values():
                if existing.get("topic") and existing.get("topic") == thought.topic:
                    if str(existing.get("content", "")) == thought.content:
                        again = Thought.from_dict(existing)
                        again.seen_count = int(existing.get("seen_count", 1)) + 1
                        again.last_seen_at = thought.last_seen_at
                        if thought.is_unfinished:
                            again.is_unfinished = True
                        again.novelty = thought.novelty
                        again.information_gain = thought.information_gain
                        again.score = thought.score
                        self.items[again.id] = again.to_dict()
                        self._trim()
                        self.save()
                        return again, "revisited"
            if not thought.id:
                thought.id = self._next_id()
            self.items[thought.id] = thought.to_dict()
            self._trim()
        self.save()
        return thought, "created"

    def _trim(self) -> None:
        """只淘汰非持久化的旧念头；active/unfinished/reinforced 永不被自动删除。"""
        if len(self.items) <= self.max_active:
            return
        removable = [
            (key, item) for key, item in self.items.items()
            if not Thought.from_dict(item).is_persistent()
        ]
        removable.sort(key=lambda kv: str(kv[1].get("created_at", "")))
        for key, _ in removable[: len(self.items) - self.max_active]:
            self.items.pop(key, None)

    def archive_expired(self, *, days: int = 30, now: datetime.datetime | None = None) -> int:
        moment = now or datetime.datetime.now()
        cutoff = moment - datetime.timedelta(days=max(1, int(days)))
        moved: list[dict] = []
        with self._lock:
            for key, item in list(self.items.items()):
                thought = Thought.from_dict(item)
                if thought.is_persistent():
                    continue
                try:
                    created = datetime.datetime.fromisoformat(str(thought.created_at))
                except ValueError:
                    continue
                if created < cutoff:
                    moved.append(item)
                    self.items.pop(key, None)
            if moved:
                try:
                    existing = []
                    if self.archive_path.is_file():
                        existing = json.loads(self.archive_path.read_text(encoding="utf-8")) or []
                    existing.extend(moved)
                    atomic_write_text(self.archive_path, json.dumps(existing[-2000:], ensure_ascii=False, indent=1))
                except (OSError, json.JSONDecodeError) as exc:
                    logging.warning("[v3] 归档失败：%s", exc)
        if moved:
            self.save()
        return len(moved)

    # ── 查询 ────────────────────────────────────────────────────────
    def get(self, thought_id: str) -> Thought | None:
        with self._lock:
            item = self.items.get(str(thought_id))
        return Thought.from_dict(item) if item else None

    def all(self) -> list[Thought]:
        with self._lock:
            return [Thought.from_dict(item) for item in self.items.values()]

    def count(self) -> int:
        with self._lock:
            return len(self.items)

    def recall(self, *, limit: int = 5, include_ephemeral: bool = False) -> list[Thought]:
        """只取最近的高分念头，不做全库加载（这里是受控的 Top-K 排序）。"""
        pool = [t for t in self.all() if include_ephemeral or t.is_persistent()]
        pool.sort(key=lambda t: (-(t.score + t.information_gain * 0.5), str(t.last_seen_at)), reverse=False)
        return pool[: max(1, int(limit))]

    def unfinished(self, *, limit: int = 5) -> list[Thought]:
        pool = [t for t in self.all() if t.is_unfinished]
        pool.sort(key=lambda t: (-t.score, str(t.last_seen_at)))
        return pool[: max(1, int(limit))]

    def recent_contents(self, *, limit: int = 20) -> list[str]:
        pool = sorted(self.all(), key=lambda t: str(t.created_at), reverse=True)
        return [t.content for t in pool[: max(1, int(limit))]]

    def stats(self) -> dict:
        by_status: dict[str, int] = {}
        for item in self.items.values():
            status = str(item.get("status", ""))
            by_status[status] = by_status.get(status, 0) + 1
        return {"total": len(self.items), "by_status": by_status,
                "unfinished": sum(1 for item in self.items.values() if item.get("is_unfinished"))}
