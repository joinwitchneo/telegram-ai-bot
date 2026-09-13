"""话题生命周期：NEW → ACTIVE → DEEP → COOLDOWN → DORMANT（可复活）。

重要约束：Topic 只**记录状态**，不主动找用户聊。
"要不要主动复活某个话题"是 Phase 6 Proactive Engine 的决定，这里不做。
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import threading
from pathlib import Path

STATUSES = ("NEW", "ACTIVE", "DEEP", "COOLDOWN", "DORMANT")


def _keywords(text: str, limit: int = 6) -> list[str]:
    raw = (text or "").strip().lower()
    words = re.findall(r"[a-z0-9_]{2,}", raw)
    cjk = re.findall(r"[\u4e00-\u9fff]", raw)
    pairs = [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]
    ordered: list[str] = []
    for token in words + pairs + cjk:
        if token and token not in ordered:
            ordered.append(token)
    return ordered[:limit]


class TopicMemory:
    def __init__(
        self,
        path: Path,
        *,
        cooldown_days: int = 3,
        dormant_days: int = 14,
        deep_touches: int = 4,
        event_bus=None,
    ) -> None:
        self.path = path
        self.cooldown_days = max(1, int(cooldown_days))
        self.dormant_days = max(self.cooldown_days, int(dormant_days))
        self.deep_touches = max(2, int(deep_touches))
        self.bus = event_bus
        self._lock = threading.RLock()
        self.data: list[dict] = []
        self._counter = 0
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    self.data = loaded
            except (OSError, json.JSONDecodeError):
                self.data = []
        for item in self.data:
            try:
                self._counter = max(self._counter, int(str(item.get("id", "topic_0")).split("_")[-1]))
            except ValueError:
                continue

    # ── 查询 ────────────────────────────────────────────────────────
    def find(self, text: str) -> dict | None:
        """按关键词找已存在的话题（用于复活判断）。"""
        tokens = set(_keywords(text, limit=8))
        best, best_score = None, 0.0
        for topic in self.data:
            topic_tokens = set(topic.get("keywords", [])) | set(_keywords(str(topic.get("topic", ""))))
            if not topic_tokens or not tokens:
                continue
            matched = tokens & topic_tokens
            overlap = len(matched) / max(1, min(len(tokens), len(topic_tokens)))
            # 命中一个"长词"（≥2 字/英文单词）就算相关，避免中文分词稀疏导致漏召回
            strong_hit = any(len(token) >= 2 and token in matched for token in matched)
            score = overlap if not strong_hit else max(overlap, 0.5)
            if score > best_score:
                best, best_score = topic, score
        threshold = 0.34 if not tokens else 0.34
        return best if best_score >= threshold else None

    def list(self, status: str | None = None) -> list[dict]:
        with self._lock:
            items = [dict(item) for item in self.data]
        if status:
            items = [item for item in items if item.get("status") == status]
        return items

    def get(self, topic_id: str) -> dict | None:
        with self._lock:
            for item in self.data:
                if item.get("id") == topic_id:
                    return dict(item)
        return None

    # ── 写入 / 状态流转 ────────────────────────────────────────────
    def upsert(
        self,
        topic: str,
        *,
        memory_id: str | None = None,
        importance: float = 0.5,
        now: datetime.datetime | None = None,
    ) -> tuple[dict, str]:
        """提到一个话题：新建（NEW）或复活（DORMANT → ACTIVE）。"""
        moment = now or datetime.datetime.now()
        topic = (topic or "").strip()[:40]
        if not topic:
            raise ValueError("话题不能为空")
        with self._lock:
            existing = self.find(topic)
            if existing is not None:
                previous = str(existing.get("status"))
                existing["touches"] = int(existing.get("touches", 0)) + 1
                existing["last_active_at"] = moment.isoformat(timespec="seconds")
                if previous in ("DORMANT", "COOLDOWN", "NEW"):
                    existing["status"] = "ACTIVE"
                elif existing["touches"] >= self.deep_touches:
                    existing["status"] = "DEEP"
                existing["keywords"] = sorted(set(existing.get("keywords", [])) | set(_keywords(topic, 8)))
                if memory_id:
                    sources = existing.setdefault("source_memory_ids", [])
                    if memory_id not in sources:
                        sources.append(memory_id)
                existing["importance"] = max(float(existing.get("importance", 0.5)), float(importance))
                self._save()
                action = "revived" if previous in ("DORMANT", "COOLDOWN") else "updated"
                self._publish(existing, action)
                return dict(existing), action
            record = {
                "id": self._next_id(),
                "topic": topic,
                "keywords": _keywords(topic, 8),
                "status": "NEW",
                "created_at": moment.isoformat(timespec="seconds"),
                "last_active_at": moment.isoformat(timespec="seconds"),
                "importance": max(0.0, min(1.0, float(importance))),
                "user_interest": 0.5,
                "char_interest": 0.5,
                "touches": 1,
                "source_memory_ids": [memory_id] if memory_id else [],
            }
            self.data.append(record)
            self._save()
        self._publish(record, "created")
        return dict(record), "created"

    def set_status(self, topic_id: str, status: str) -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"未知状态：{status}")
        with self._lock:
            for item in self.data:
                if item.get("id") != topic_id:
                    continue
                item["status"] = status
                self._save()
                return dict(item)
        return None

    def decay(self, *, now: datetime.datetime | None = None) -> dict:
        """按沉寂时间降级：ACTIVE/DEEP → COOLDOWN → DORMANT。"""
        moment = now or datetime.datetime.now()
        moved = 0
        with self._lock:
            for item in self.data:
                try:
                    last = datetime.datetime.fromisoformat(str(item.get("last_active_at")))
                except ValueError:
                    continue
                days = (moment - last).total_seconds() / 86400
                status = str(item.get("status"))
                if days >= self.dormant_days and status != "DORMANT":
                    item["status"] = "DORMANT"
                    moved += 1
                elif days >= self.cooldown_days and status in ("ACTIVE", "DEEP", "NEW"):
                    item["status"] = "COOLDOWN"
                    moved += 1
            if moved:
                self._save()
        return {"moved": moved, "by_status": self._count_by_status()}

    def _count_by_status(self) -> dict:
        counts: dict[str, int] = {}
        for item in self.data:
            key = str(item.get("status"))
            counts[key] = counts.get(key, 0) + 1
        return counts

    def stats(self) -> dict:
        with self._lock:
            return {"total": len(self.data), "by_status": self._count_by_status()}

    # ── 内部 ────────────────────────────────────────────────────────
    def _next_id(self) -> str:
        self._counter += 1
        return f"topic_{self._counter:05d}"

    def _publish(self, topic: dict, action: str) -> None:
        if self.bus is not None:
            self.bus.publish("TopicUpdated", topic=topic.get("topic"), status=topic.get("status"), action=action)

    def _save(self) -> None:
        snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("保存话题失败：%s", exc)
