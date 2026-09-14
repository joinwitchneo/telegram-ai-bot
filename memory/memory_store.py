"""结构化记忆仓库：CRUD + 去重 + 冲突失效 + 温度标记。

设计要点（Phase 3 规范）：
- 每条记忆有稳定唯一 ID，内容更新不会产生新对象；
- 明显重复的同类事实 → 更新已有记忆，不无限堆积；
- 被新信息推翻的旧记忆标记 superseded_by 后失效，不物理删除；
- protected 记忆不会被普通衰减归档。
"""

from __future__ import annotations

from core.atomic_io import atomic_write_text

import datetime
import json
import logging
import threading
from pathlib import Path

TYPES = (
    "fact", "preference", "habit", "agreement",
    "shared_event", "relationship_event", "topic", "emotion_event",
)
TEMPERATURES = ("HOT", "WARM", "COLD", "ARCHIVED")

# 表示"情况变了"的标记词
CHANGE_MARKERS = (
    "不再", "改成", "改了", "现在是", "以后都", "已经换", "取消了", "不学了", "不玩了",
    "不喜欢了", "决定不", "戒了", "换成",
)


def _bigrams(text: str) -> set[str]:
    cleaned = "".join(text.split())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def similarity(left: str, right: str) -> float:
    """字符二元组 Jaccard 相似度（中英文都适用，不需要 embedding）。"""
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def looks_like_change(new_text: str, old_text: str) -> bool:
    """新信息是否在推翻旧信息。"""
    if any(marker in new_text for marker in CHANGE_MARKERS):
        return True
    # 明显对立：喜欢 / 不喜欢
    pairs = (("喜欢", "不喜欢"), ("会", "不会"), ("能", "不能"), ("在学", "不学"))
    for positive, negative in pairs:
        if (positive in old_text and negative in new_text) or (negative in old_text and positive in new_text):
            return True
    return False


class MemoryStore:
    def __init__(self, path: Path, *, dedup_threshold: float = 0.55) -> None:
        self.path = path
        self.dedup_threshold = dedup_threshold
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
                self._counter = max(self._counter, int(str(item.get("id", "mem_0")).split("_")[-1]))
            except ValueError:
                continue

    # ── 基础 CRUD ──────────────────────────────────────────────────
    def _next_id(self) -> str:
        self._counter += 1
        return f"mem_{self._counter:06d}"

    def create(
        self,
        *,
        type: str,
        content: str,
        tags: list[str] | None = None,
        importance: float = 0.5,
        emotion_weight: float = 0.0,
        protected: bool = False,
        source: str = "",
        temperature: str = "WARM",
    ) -> tuple[dict, str]:
        """写入一条记忆。返回 (记录, 动作)，动作是 created / updated / superseded。"""
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        memory_type = type if type in TYPES else "fact"
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            for existing in self.data:
                if not self._active(existing) or existing.get("type") != memory_type:
                    continue
                score = similarity(content, str(existing.get("content", "")))
                # 先判断"是否推翻了旧信息"：改写/反转必须优先于去重
                if looks_like_change(content, str(existing.get("content", ""))) and score >= 0.2:
                    new_id = self._next_id()
                    existing["superseded_by"] = new_id
                    record = self._build(
                        new_id, memory_type, content, tags, importance, emotion_weight,
                        protected, source, temperature, now,
                    )
                    self.data.append(record)
                    self._save()
                    return record, "superseded"
                if score >= self.dedup_threshold:
                    # 同一事实：更新已有记忆，不新建
                    if len(content) > len(str(existing.get("content", ""))):
                        existing["content"] = content
                    merged_tags = sorted(set(existing.get("tags", [])) | set(tags or []))
                    existing["tags"] = merged_tags
                    existing["importance"] = min(1.0, float(existing.get("importance", 0.5)) + 0.05)
                    if protected:
                        existing["protected"] = True
                    existing["last_updated_at"] = now
                    self._save()
                    return existing, "updated"
            record = self._build(
                self._next_id(), memory_type, content, tags, importance, emotion_weight,
                protected, source, temperature, now,
            )
            self.data.append(record)
            self._save()
            return record, "created"

    @staticmethod
    def _build(
        memory_id: str, memory_type: str, content: str, tags: list[str] | None,
        importance: float, emotion_weight: float, protected: bool, source: str,
        temperature: str, now: str,
    ) -> dict:
        return {
            "id": memory_id,
            "type": memory_type,
            "content": content,
            "tags": sorted({tag for tag in (tags or []) if tag}),
            "importance": max(0.0, min(1.0, float(importance))),
            "created_at": now,
            "last_used_at": "",
            "last_updated_at": now,
            "use_count": 0,
            "emotion_weight": max(0.0, min(1.0, float(emotion_weight))),
            "protected": bool(protected),
            "temperature": temperature if temperature in TEMPERATURES else "WARM",
            "superseded_by": "",
            "source": source,
        }

    def get(self, memory_id: str) -> dict | None:
        with self._lock:
            for item in self.data:
                if item.get("id") == memory_id:
                    return dict(item)
        return None

    def update(self, memory_id: str, **fields) -> dict | None:
        with self._lock:
            for item in self.data:
                if item.get("id") != memory_id:
                    continue
                for key, value in fields.items():
                    if key in ("id",):
                        continue
                    item[key] = value
                item["last_updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                self._save()
                return dict(item)
        return None

    def delete(self, memory_id: str) -> bool:
        """软删除：标记归档，保留历史（不物理删除重要记忆）。"""
        result = self.update(memory_id, temperature="ARCHIVED", deleted=True)
        return result is not None

    def purge(self, memory_id: str) -> bool:
        """物理删除（只给管理/测试用）。"""
        with self._lock:
            before = len(self.data)
            self.data = [item for item in self.data if item.get("id") != memory_id]
            removed = len(self.data) != before
            if removed:
                self._save()
        return removed

    def list(self, *, include_inactive: bool = False, type: str | None = None, temperature: str | None = None) -> list[dict]:
        with self._lock:
            items = []
            for item in self.data:
                if not include_inactive and not self._active(item):
                    continue
                if type and item.get("type") != type:
                    continue
                if temperature and item.get("temperature") != temperature:
                    continue
                items.append(dict(item))
        return items

    # ── 状态标记 ───────────────────────────────────────────────────
    @staticmethod
    def _active(memory: dict) -> bool:
        return not memory.get("superseded_by") and not memory.get("deleted")

    def mark_used(self, memory_id: str, now: datetime.datetime | None = None) -> dict | None:
        moment = (now or datetime.datetime.now()).isoformat(timespec="seconds")
        with self._lock:
            for item in self.data:
                if item.get("id") != memory_id:
                    continue
                item["use_count"] = int(item.get("use_count", 0)) + 1
                item["last_used_at"] = moment
                if item.get("temperature") == "COLD" and not item.get("protected"):
                    item["temperature"] = "WARM"      # 被用到了就回温
                self._save()
                return dict(item)
        return None

    def protect(self, memory_id: str) -> dict | None:
        return self.update(memory_id, protected=True)

    def archive(self, memory_id: str) -> dict | None:
        return self.update(memory_id, temperature="ARCHIVED")

    def set_temperature(self, memory_id: str, temperature: str) -> dict | None:
        if temperature not in TEMPERATURES:
            raise ValueError(f"未知温度：{temperature}")
        return self.update(memory_id, temperature=temperature)

    # ── 统计与持久化 ───────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            active = [item for item in self.data if self._active(item)]
            by_temperature: dict[str, int] = {}
            for item in active:
                key = str(item.get("temperature", "WARM"))
                by_temperature[key] = by_temperature.get(key, 0) + 1
            return {
                "total": len(self.data),
                "active": len(active),
                "superseded": len([item for item in self.data if item.get("superseded_by")]),
                "protected": len([item for item in active if item.get("protected")]),
                "by_temperature": by_temperature,
            }

    def _save(self) -> None:
        snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("保存记忆失败：%s", exc)
