"""Character Life（Phase 6）：她"在想什么"，而不是"她今天假装干了什么"。

三层可信度：
    CONFIRMED 1.0 —— 系统确定的事实（时间、日期、用户明确说过、已确认设定）
    INTERNAL  0.8 —— 角色内部状态（念头、兴趣、没聊完的事）
    FICTIONAL 0.6 —— 世界观/角色扮演允许的背景（"她在学某样东西"）

硬规则：
1. 不为了"活人感"每天生成随机日记；没有值得记录的事就什么都不写；
2. FICTIONAL **不允许**编造成"已发生的现实经历"（"我今天下午去了咖啡店"会被拒绝）；
3. 这里的条目是短期状态，**不写入 Memory**（Memory 只收长期有价值的信息）。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

SOURCES = ("CONFIRMED", "INTERNAL", "FICTIONAL")
SOURCE_CONFIDENCE = {"CONFIRMED": 1.0, "INTERNAL": 0.8, "FICTIONAL": 0.6}

STATUSES = ("ACTIVE", "COMPLETED", "EXPIRED", "ARCHIVED")

TYPES = (
    "unfinished_thought",   # 还想知道某件事的结果
    "unfinished_topic",     # 没聊完的话题
    "important_event",      # 考试/面试/约定等有日期的事
    "interest",             # 她对某个话题产生了兴趣
    "mood_note",            # 关于用户状态的短期挂念（不是她的日记）
    "setting",              # 角色设定层面的背景
)

# 这些句式属于"编造现实经历"，FICTIONAL 一律不允许
FORBIDDEN_FICTIONAL = (
    "我今天去", "我刚刚去", "我刚才去", "我昨天去", "我去了", "我去买", "我买了",
    "我吃了", "我喝了", "我逛了", "我在咖啡", "我下班", "我去上班", "我出门",
    "我今天", "我昨天", "我刚刚", "我刚才", "我早上", "我晚上",
)


class CharacterLife:
    def __init__(self, path: Path, *, now_fn=None, max_entries: int = 80) -> None:
        self.path = path
        self._now = now_fn or datetime.datetime.now
        self.max_entries = max(2, int(max_entries))
        self._lock = threading.RLock()
        self.data: list[dict] = []
        self._load()

    # ── 存档 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                self.data = [item for item in loaded if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            self.data = []

    def _save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入角色生活失败：%s", exc)

    def _next_id(self) -> str:
        biggest = 0
        for item in self.data:
            try:
                biggest = max(biggest, int(str(item.get("id", "")).split("_")[-1]))
            except ValueError:
                continue
        return f"life_{biggest + 1:06d}"

    # ── 写入 ────────────────────────────────────────────────────────
    def add(
        self,
        type: str,
        content: str,
        *,
        source: str = "INTERNAL",
        confidence: float | None = None,
        importance: float = 0.5,
        ttl_hours: float | None = None,
        expires_at: str | None = None,
        tags: list[str] | None = None,
        reason: str = "",
        now: datetime.datetime | None = None,
    ) -> dict:
        moment = now or self._now()
        kind = type if type in TYPES else "unfinished_thought"
        origin = source if source in SOURCES else "INTERNAL"
        text = (content or "").strip()
        if not text:
            raise ValueError("内容不能为空")
        if origin == "FICTIONAL" and any(word in text for word in FORBIDDEN_FICTIONAL):
            raise ValueError("虚构来源不允许编造已经发生的现实经历")

        expiry = ""
        if expires_at:
            expiry = str(expires_at)
        elif ttl_hours:
            expiry = (moment + datetime.timedelta(hours=float(ttl_hours))).isoformat(timespec="seconds")

        record = {
            "id": self._next_id(),
            "type": kind,
            "content": text[:120],
            "source": origin,
            "confidence": round(
                float(SOURCE_CONFIDENCE[origin] if confidence is None else confidence), 3
            ),
            "created_at": moment.isoformat(timespec="seconds"),
            "expires_at": expiry,
            "status": "ACTIVE",
            "importance": max(0.0, min(1.0, float(importance))),
            "tags": sorted({str(tag) for tag in (tags or []) if str(tag).strip()}),
            "reason": str(reason)[:40],
            "updated_at": moment.isoformat(timespec="seconds"),
        }
        with self._lock:
            self.data.append(record)
            self._trim()
        self._save()
        return dict(record)

    def _trim(self) -> None:
        """太多条目时，先丢最旧的非重要条目。"""
        if len(self.data) <= self.max_entries:
            return
        ordered = sorted(
            self.data,
            key=lambda item: (float(item.get("importance", 0.5)), str(item.get("created_at", ""))),
        )
        drop = len(self.data) - self.max_entries
        keep = set(id(item) for item in ordered[drop:])
        self.data = [item for item in self.data if id(item) in keep]

    # ── 读取 ────────────────────────────────────────────────────────
    def get(self, life_id: str) -> dict | None:
        with self._lock:
            for item in self.data:
                if item.get("id") == life_id:
                    return dict(item)
        return None

    def active(self, *, type: str | None = None, source: str | None = None) -> list[dict]:
        self.expire_due()
        with self._lock:
            items = [dict(item) for item in self.data if item.get("status") == "ACTIVE"]
        if type:
            items = [item for item in items if item.get("type") == type]
        if source:
            items = [item for item in items if item.get("source") == source]
        return items

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self.data]

    # ── 状态流转 ────────────────────────────────────────────────────
    def set_status(self, life_id: str, status: str, *, now: datetime.datetime | None = None) -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"未知状态：{status}")
        moment = now or self._now()
        with self._lock:
            for item in self.data:
                if item.get("id") != life_id:
                    continue
                item["status"] = status
                item["updated_at"] = moment.isoformat(timespec="seconds")
                self._save()
                return dict(item)
        return None

    def complete(self, life_id: str, **kwargs) -> dict | None:
        return self.set_status(life_id, "COMPLETED", **kwargs)

    def archive(self, life_id: str, **kwargs) -> dict | None:
        return self.set_status(life_id, "ARCHIVED", **kwargs)

    def update(self, life_id: str, **fields) -> dict | None:
        allowed = {"content", "importance", "confidence", "expires_at", "tags", "reason", "type", "source"}
        with self._lock:
            for item in self.data:
                if item.get("id") != life_id:
                    continue
                for key, value in fields.items():
                    if key in allowed:
                        item[key] = value
                item["updated_at"] = self._now().isoformat(timespec="seconds")
                self._save()
                return dict(item)
        return None

    def expire_due(self, *, now: datetime.datetime | None = None) -> int:
        moment = now or self._now()
        moved = 0
        with self._lock:
            for item in self.data:
                if item.get("status") != "ACTIVE":
                    continue
                expiry = str(item.get("expires_at") or "")
                if not expiry:
                    continue
                try:
                    due = datetime.datetime.fromisoformat(expiry)
                except ValueError:
                    continue
                if moment >= due:
                    item["status"] = "EXPIRED"
                    item["updated_at"] = moment.isoformat(timespec="seconds")
                    moved += 1
            if moved:
                self._save()
        return moved

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        sources: dict[str, int] = {}
        for item in self.all():
            counts[str(item.get("status"))] = counts.get(str(item.get("status")), 0) + 1
            sources[str(item.get("source"))] = sources.get(str(item.get("source")), 0) + 1
        return {"total": len(self.data), "by_status": counts, "by_source": sources}

    def reset(self) -> None:
        with self._lock:
            self.data = []
        self._save()
