"""Sticker 索引：确定性查找（无 Embedding、无向量库）。"""

from __future__ import annotations

from collections import defaultdict

from sticker.models import StickerRecord


class StickerIndex:
    def __init__(self, store) -> None:
        self.store = store
        self._by_emoji: dict[str, list[str]] = {}
        self._by_set: dict[str, list[str]] = {}
        self._by_tag: dict[str, list[str]] = {}
        self._built = 0

    # ── 建索引 ──────────────────────────────────────────────────────
    def build(self) -> dict:
        by_emoji: dict[str, list[str]] = defaultdict(list)
        by_set: dict[str, list[str]] = defaultdict(list)
        by_tag: dict[str, list[str]] = defaultdict(list)
        records = self.store.all()
        for record in records:
            if record.emoji:
                by_emoji[record.emoji].append(record.file_unique_id)
            if record.set_name:
                by_set[record.set_name].append(record.file_unique_id)
            for tag in record.tags or []:
                by_tag[str(tag)].append(record.file_unique_id)
        self._by_emoji = {key: sorted(value) for key, value in by_emoji.items()}
        self._by_set = {key: sorted(value) for key, value in by_set.items()}
        self._by_tag = {key: sorted(value) for key, value in by_tag.items()}
        self._built = len(records)
        return {"stickers": self._built, "emoji": len(self._by_emoji),
                "sets": len(self._by_set), "tags": len(self._by_tag)}

    def ensure_fresh(self) -> None:
        """数据量变了就重建（O(n)，几千条量级完全够用）。"""
        if self._built != self.store.count():
            self.build()

    # ── 查询 ────────────────────────────────────────────────────────
    def by_emoji(self, emoji: str) -> list[StickerRecord]:
        self.ensure_fresh()
        keys = self._by_emoji.get(str(emoji), [])
        return [item for item in (self.store.get(key) for key in keys) if item]

    def by_set(self, set_name: str) -> list[StickerRecord]:
        self.ensure_fresh()
        keys = self._by_set.get(str(set_name), [])
        return [item for item in (self.store.get(key) for key in keys) if item]

    def by_tag(self, tag: str) -> list[StickerRecord]:
        self.ensure_fresh()
        keys = self._by_tag.get(str(tag), [])
        return [item for item in (self.store.get(key) for key in keys) if item]

    def recent(self, limit: int = 10) -> list[StickerRecord]:
        records = self.store.all()
        records.sort(key=lambda item: str(item.last_seen_at), reverse=True)
        return records[: max(1, int(limit))]

    def stats(self) -> dict:
        self.ensure_fresh()
        return {
            "stickers": self._built,
            "emoji_kinds": len(self._by_emoji),
            "sets": len(self._by_set),
            "tags": sorted(self._by_tag)[:10],
        }
