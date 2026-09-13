"""工具缓存：同样的查询短时间内不重复打网络（天气 10 分钟、地理编码 1 天…）。"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path


class TTLCache:
    def __init__(self, path: Path | None = None, *, enabled: bool = True, max_entries: int = 500) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.max_entries = max(10, int(max_entries))
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0
        self._load()

    # ── 读写 ────────────────────────────────────────────────────────
    def get(self, key: str):
        if not self.enabled:
            return None
        with self._lock:
            item = self._items.get(str(key))
            if not item:
                self.misses += 1
                return None
            if float(item.get("expires_at", 0)) <= time.time():
                self._items.pop(str(key), None)
                self.misses += 1
                return None
            self.hits += 1
            return item.get("value")

    def set(self, key: str, value, ttl: float) -> None:
        if not self.enabled or ttl <= 0:
            return
        with self._lock:
            self._items[str(key)] = {"value": value, "expires_at": time.time() + float(ttl)}
            if len(self._items) > self.max_entries:
                ordered = sorted(self._items.items(), key=lambda kv: float(kv[1].get("expires_at", 0)))
                for stale_key, _ in ordered[: len(self._items) - self.max_entries]:
                    self._items.pop(stale_key, None)

    def clear(self) -> int:
        with self._lock:
            count = len(self._items)
            self._items.clear()
        return count

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._items),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }

    # ── 落盘 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            items = loaded.get("items") if isinstance(loaded, dict) else None
            if isinstance(items, dict):
                now = time.time()
                self._items = {
                    key: value for key, value in items.items()
                    if isinstance(value, dict) and float(value.get("expires_at", 0)) > now
                }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._items = {}

    def save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                payload = json.dumps({"items": self._items}, ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入工具缓存失败：%s", exc)
