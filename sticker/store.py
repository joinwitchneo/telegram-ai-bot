"""Sticker 存储：索引 + 收藏集缓存 + 偏好。与 Memory 完全分离。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_text
from sticker.models import StickerRecord

MAX_STICKERS = 2000
MAX_SETS = 200


class StickerStore:
    def __init__(
        self,
        index_path: Path,
        preferences_path: Path | None = None,
        *,
        max_stickers: int = MAX_STICKERS,
        max_sets: int = MAX_SETS,
    ) -> None:
        self.path = Path(index_path)
        self.preferences_path = Path(preferences_path) if preferences_path else None
        self.max_stickers = max(50, int(max_stickers))
        self.max_sets = max(10, int(max_sets))
        self._lock = threading.RLock()
        self.stickers: dict[str, dict] = {}
        self.sets: dict[str, dict] = {}
        self.preferences: dict[str, str] = {}
        self._load()

    # ── 读写 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.stickers = dict(data.get("stickers") or {})
                    self.sets = dict(data.get("sets") or {})
            except (OSError, json.JSONDecodeError):
                logging.warning("[sticker] 索引文件损坏，从空开始：%s", self.path)
        if self.preferences_path and self.preferences_path.is_file():
            try:
                data = json.loads(self.preferences_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.preferences = {str(k): str(v) for k, v in data.items() if str(v)}
            except (OSError, json.JSONDecodeError):
                logging.warning("[sticker] 偏好文件损坏，从空开始")

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(
                {"stickers": self.stickers, "sets": self.sets}, ensure_ascii=False, indent=1
            )
        try:
            atomic_write_text(self.path, payload)
        except OSError as exc:
            logging.warning("[sticker] 写索引失败：%s", exc)

    def save_preferences(self) -> None:
        if not self.preferences_path:
            return
        with self._lock:
            payload = json.dumps(self.preferences, ensure_ascii=False, indent=1)
        try:
            atomic_write_text(self.preferences_path, payload)
        except OSError as exc:
            logging.warning("[sticker] 写偏好失败：%s", exc)

    # ── 收录 ────────────────────────────────────────────────────────
    def add(self, record: StickerRecord) -> tuple[StickerRecord, str]:
        """收录一个贴纸。返回 (记录, 动作)，动作是 created / updated。"""
        key = record.file_unique_id
        if not key:
            raise ValueError("file_unique_id 不能为空")
        with self._lock:
            existing = self.stickers.get(key)
            if existing:
                stored = StickerRecord.from_dict(existing)
                stored.file_id = record.file_id or stored.file_id      # file_id 可能变化
                stored.emoji = record.emoji or stored.emoji
                stored.set_name = record.set_name or stored.set_name
                stored.file_size = record.file_size or stored.file_size
                stored.width = record.width or stored.width
                stored.height = record.height or stored.height
                stored.seen_count = int(stored.seen_count) + 1
                stored.last_seen_at = record.last_seen_at
                merged_tags = sorted(set(stored.tags) | set(record.tags))
                stored.tags = merged_tags
                if record.visual_description and not stored.visual_description:
                    stored.visual_description = record.visual_description
                self.stickers[key] = stored.to_dict()
                action = "updated"
            else:
                self.stickers[key] = record.to_dict()
                action = "created"
            self._trim()
        self.save()
        return StickerRecord.from_dict(self.stickers[key]), action

    def _trim(self) -> None:
        """只按"最少使用 + 最久未出现"淘汰，绝不动偏好里的贴纸。"""
        if len(self.stickers) <= self.max_stickers:
            return
        protected = set(self.preferences) if self.preferences else set()
        candidates = [
            (key, item) for key, item in self.stickers.items() if key not in protected
        ]
        candidates.sort(key=lambda kv: (int(kv[1].get("seen_count", 1)), str(kv[1].get("last_seen_at", ""))))
        for key, _ in candidates[: len(self.stickers) - self.max_stickers]:
            self.stickers.pop(key, None)

    # ── 查询 ────────────────────────────────────────────────────────
    def get(self, file_unique_id: str) -> StickerRecord | None:
        with self._lock:
            item = self.stickers.get(str(file_unique_id))
        return StickerRecord.from_dict(item) if item else None

    def all(self) -> list[StickerRecord]:
        with self._lock:
            return [StickerRecord.from_dict(item) for item in self.stickers.values()]

    def count(self) -> int:
        with self._lock:
            return len(self.stickers)

    # ── Sticker Set 缓存 ────────────────────────────────────────────
    def has_set(self, set_name: str) -> bool:
        with self._lock:
            return str(set_name) in self.sets

    def cache_set(self, set_name: str, payload: dict) -> None:
        if not set_name:
            return
        with self._lock:
            self.sets[str(set_name)] = {
                "title": str((payload or {}).get("title", "")),
                "sticker_type": str((payload or {}).get("sticker_type", "")),
                "count": len((payload or {}).get("stickers") or []),
                "cached_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            }
            if len(self.sets) > self.max_sets:
                for key in sorted(self.sets)[: len(self.sets) - self.max_sets]:
                    self.sets.pop(key, None)
        self.save()

    def set_info(self, set_name: str) -> dict:
        with self._lock:
            return dict(self.sets.get(str(set_name)) or {})

    # ── 偏好（只提供接口，不自动推断）──────────────────────────────
    def set_preference(self, file_unique_id: str, value: str) -> bool:
        if value not in ("positive", "negative", "neutral"):
            raise ValueError("偏好只能是 positive / negative / neutral")
        key = str(file_unique_id)
        with self._lock:
            if key not in self.stickers:
                return False
            self.preferences[key] = value
        self.save_preferences()
        return True

    def preference(self, file_unique_id: str) -> str:
        with self._lock:
            return self.preferences.get(str(file_unique_id), "")

    def tags(self) -> dict:
        counts: dict[str, int] = {}
        for item in self.stickers.values():
            for tag in item.get("tags") or []:
                counts[str(tag)] = counts.get(str(tag), 0) + 1
        return counts
