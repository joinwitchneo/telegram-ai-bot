"""Proactive Engine（Phase 6）：从 Character Life / Memory / Topic / Relationship 里
生成"可以主动聊什么"的候选，并算出分数与理由。

它只回答两个问题：
    1. 现在有哪些值得找用户的理由？（Candidate）
    2. 哪个理由最成立？（排序）

它**不**决定能不能发（那是 Scheduler），也**不**生成消息（那复用 Phase 5）。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from proactive.proactive_score import gap_feature, recent_chat_feature, score_candidate

REASONS = (
    "unfinished_topic", "important_event", "character_thought",
    "topic_revival", "interaction_gap", "shared_experience",
)

REASON_LABELS = {
    "unfinished_topic": "之前没聊完的事",
    "important_event": "用户说过的重要事情",
    "character_thought": "她自己突然想到的念头",
    "topic_revival": "沉寂话题有了新由头",
    "interaction_gap": "确实有段时间没聊了",
    "shared_experience": "共同经历",
}

EVENT_KEYWORDS = (
    "考试", "面试", "开会", "体检", "出差", "答辩", "搬家", "生日", "约定",
    "截止", "复试", "笔试", "报名", "交材料", "复查",
)

LIFE_REASON_MAP = {
    # 没聊完的念头本来就属于"未完成话题"，这样它才够格触发一次主动
    "unfinished_thought": "unfinished_topic",
    "unfinished_topic": "unfinished_topic",
    "important_event": "important_event",
    "interest": "character_thought",
    "mood_note": "character_thought",
    "setting": "character_thought",
}


def _parse_time(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class Candidate:
    id: str
    topic: str
    reason: str
    score: float
    hint: str
    source: str = ""
    memory_ids: list[str] = field(default_factory=list)
    life_id: str = ""
    features: dict = field(default_factory=dict)
    parts: dict = field(default_factory=dict)
    special: bool = False
    priority: int = 0
    expires_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "reason": self.reason,
            "score": self.score,
            "hint": self.hint,
            "source": self.source,
            "memory_ids": list(self.memory_ids),
            "life_id": self.life_id,
            "special": self.special,
            "expires_at": self.expires_at,
            "features": dict(self.features),
        }


class ProactiveHistory:
    """记录主动消息有没有被回应，并给出"这个话题最近聊过没有"。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.entries: list[dict] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                self.entries = [item for item in loaded if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError):
            self.entries = []

    def _save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.entries[-200:], ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入主动历史失败：%s", exc)

    def record(self, *, candidate: Candidate, message: str, now: datetime.datetime) -> dict:
        entry = {
            "ts": now.isoformat(timespec="seconds"),
            "candidate_id": candidate.id,
            "topic": candidate.topic,
            "reason": candidate.reason,
            "score": candidate.score,
            "message": message[:200],
            "replied": False,
            "replied_at": "",
        }
        with self._lock:
            self.entries.append(entry)
        self._save()
        return entry

    def mark_replied(self, *, now: datetime.datetime, within_hours: float = 12.0) -> int:
        """用户回来了：把最近的未回复主动记为已回复。"""
        marked = 0
        with self._lock:
            for entry in reversed(self.entries):
                if entry.get("replied"):
                    continue
                sent = _parse_time(entry.get("ts", ""))
                if sent is None:
                    continue
                if (now - sent).total_seconds() > within_hours * 3600:
                    break
                entry["replied"] = True
                entry["replied_at"] = now.isoformat(timespec="seconds")
                marked += 1
            if marked:
                self._save()
        return marked

    def last_unreplied(self) -> dict | None:
        for entry in reversed(self.entries):
            if not entry.get("replied"):
                return dict(entry)
        return None

    def recent_topics(self, *, hours: float, now: datetime.datetime) -> set[str]:
        cutoff = now - datetime.timedelta(hours=float(hours))
        topics = set()
        for entry in self.entries:
            sent = _parse_time(entry.get("ts", ""))
            if sent is None or sent < cutoff:
                continue
            topics.add(str(entry.get("topic", "")))
        return topics

    def stats(self) -> dict:
        sent = len(self.entries)
        replied = sum(1 for item in self.entries if item.get("replied"))
        return {
            "sent": sent,
            "replied": replied,
            "reply_rate": round(replied / sent, 3) if sent else 0.0,
        }


class ProactiveEngine:
    def __init__(
        self,
        *,
        life=None,
        memory_store=None,
        topics=None,
        history: ProactiveHistory | None = None,
        weights: dict | None = None,
        topic_cooldown_hours: float = 72.0,
        memory_min_age_hours: float = 2.0,
        gap_full_hours: float = 48.0,
        recent_chat_window_minutes: float = 45.0,
        min_heat_for_gap: float = 0.35,
    ) -> None:
        self.life = life
        self.memory_store = memory_store
        self.topics = topics
        self.history = history
        self.weights = dict(weights or {})
        self.topic_cooldown_hours = float(topic_cooldown_hours)
        self.memory_min_age_hours = float(memory_min_age_hours)
        self.gap_full_hours = float(gap_full_hours)
        self.recent_chat_window_minutes = float(recent_chat_window_minutes)
        self.min_heat_for_gap = float(min_heat_for_gap)

    # ── 对外 ────────────────────────────────────────────────────────
    def build_candidates(
        self,
        *,
        signals: dict | None = None,
        last_user_at: datetime.datetime | None = None,
        sent_today: int = 0,
        daily_limit: int = 3,
        last_sent_at: datetime.datetime | None = None,
        min_interval_minutes: float = 90.0,
        nonresponse_streak: int = 0,
        nonresponse_limit: int = 2,
        now: datetime.datetime | None = None,
    ) -> list[Candidate]:
        moment = now or datetime.datetime.now()
        signals = signals or {}
        emotion = signals.get("emotion") or {}
        relationship = signals.get("relationship") or {}
        heat = float(relationship.get("interaction_heat", 0.2) or 0.2)
        shared = float(relationship.get("shared_experience", 0.02) or 0.02)
        intimacy = float(relationship.get("intimacy", 0.05) or 0.05)
        fatigue = float(emotion.get("fatigue", 0.3) or 0.3)

        gap_hours = 0.0
        if last_user_at is not None:
            gap_hours = max(0.0, (moment - last_user_at).total_seconds() / 3600)

        frequency = 0.0
        if last_sent_at is not None:
            since_last = (moment - last_sent_at).total_seconds() / 60.0
            if since_last < min_interval_minutes:
                frequency = 1.0
        frequency = max(frequency, min(1.0, sent_today / max(1, daily_limit)))

        base_context = {
            # 不知道用户上次什么时候说话时，不按"刚聊过"处理
            "recent_chat_penalty": (
                recent_chat_feature(
                    gap_hours * 60, window_minutes=self.recent_chat_window_minutes
                )
                if last_user_at is not None
                else 0.0
            ),
            "proactive_frequency_penalty": round(frequency, 4),
            "user_nonresponse_penalty": min(1.0, nonresponse_streak / max(1, nonresponse_limit)),
            "fatigue_penalty": fatigue,
            "relationship_heat": heat,
            "shared_experience": shared,
            "interaction_gap": gap_feature(gap_hours, full_hours=self.gap_full_hours),
        }

        blocked_topics = (
            self.history.recent_topics(hours=self.topic_cooldown_hours, now=moment)
            if self.history is not None
            else set()
        )
        candidates: list[Candidate] = []
        candidates.extend(self._from_life(signals=signals, base=base_context, now=moment))
        candidates.extend(self._from_memory(signals=signals, base=base_context, now=moment))
        candidates.extend(self._from_topics(signals=signals, base=base_context, now=moment))
        if gap_hours >= self.gap_full_hours and heat >= self.min_heat_for_gap:
            candidates.append(
                self._make(
                    key="gap",
                    topic="interaction_gap",
                    reason="interaction_gap",
                    hint=f"已经 {int(gap_hours)} 小时没聊了，你想起来问问他在忙什么",
                    base=base_context,
                    boost=min(1.0, gap_hours / max(1.0, self.gap_full_hours)),
                )
            )

        # 同一个话题最近已经主动聊过 → 这次不再提（特殊事件也不例外）
        kept = [item for item in candidates if item.topic not in blocked_topics]
        kept.sort(key=lambda item: (-item.score, -item.priority, item.id))
        return kept

    def best(self, candidates: list[Candidate]) -> Candidate | None:
        return candidates[0] if candidates else None

    # ── 各路来源 ────────────────────────────────────────────────────
    def _from_life(self, *, signals, base, now) -> list[Candidate]:
        if self.life is None:
            return []
        result: list[Candidate] = []
        for entry in self.life.active():
            reason = LIFE_REASON_MAP.get(str(entry.get("type")), "character_thought")
            importance = float(entry.get("importance", 0.5))
            # 强度只看这件事本身有多重要；confidence 只用来判断可信度（不重复打折）
            strength = max(0.0, min(1.0, importance))
            special = reason == "important_event" and str(entry.get("source")) == "CONFIRMED"
            result.append(
                self._make(
                    key=str(entry.get("id", "")),
                    # 用内容而不是 id 当话题键：重置存档后不会和旧记录撞车
                    topic=f"life:{str(entry.get('content', ''))[:24]}",
                    reason=reason,
                    hint=f"用户之前说过「{entry.get('content', '')}」，你现在想知道这件事后来怎么样了",
                    base=base,
                    boost=strength,
                    life_id=str(entry.get("id", "")),
                    memory_ids=[str(tag) for tag in entry.get("tags", []) if str(tag).startswith("mem_")],
                    special=special,
                    priority=2 if special else 1,
                    expires_at=str(entry.get("expires_at", "")),
                )
            )
        return result

    def _from_memory(self, *, signals, base, now) -> list[Candidate]:
        if self.memory_store is None:
            return []
        result: list[Candidate] = []
        for memory in self.memory_store.list():
            created = _parse_time(memory.get("created_at", ""))
            if created is not None:
                age_hours = (now - created).total_seconds() / 3600
                if age_hours < self.memory_min_age_hours:
                    continue
            content = str(memory.get("content", ""))
            if not content:
                continue
            importance = float(memory.get("importance", 0.5))
            if importance < 0.55:
                continue
            memory_type = str(memory.get("type", ""))
            if any(word in content for word in EVENT_KEYWORDS):
                reason = "important_event"
                hint = f"你想起来用户之前说过：{content}"
            elif memory_type in ("topic", "preference", "agreement", "shared_event", "relationship_event"):
                reason = "shared_experience" if memory_type in ("shared_event", "relationship_event") else "unfinished_topic"
                hint = f"你还记着这件事，想问问他后来怎么样了：{content}"
            else:
                continue
            result.append(
                self._make(
                    key=str(memory.get("id", "")),
                    topic=str(memory.get("id", "")),
                    reason=reason,
                    hint=hint,
                    base=base,
                    boost=importance,
                    memory_ids=[str(memory.get("id", ""))],
                    special=reason == "important_event" and bool(memory.get("protected")),
                    priority=1,
                )
            )
        return result

    def _from_topics(self, *, signals, base, now) -> list[Candidate]:
        if self.topics is None:
            return []
        result: list[Candidate] = []
        for topic in self.topics.list():
            if str(topic.get("status")) != "DORMANT":
                continue
            # DORMANT 不能自己复活：必须有"新的理由"——之后产生的新记忆
            revival = self._revival_source(topic, now=now)
            if revival is None:
                continue
            result.append(
                self._make(
                    key=str(topic.get("id", "")),
                    topic=str(topic.get("id", "")),
                    reason="topic_revival",
                    hint=f"你看到相关的消息，又想起之前聊过的「{topic.get('topic', '')}」：{revival}",
                    base=base,
                    boost=float(topic.get("importance", 0.5)),
                    memory_ids=list(topic.get("source_memory_ids", [])),
                    priority=0,
                )
            )
        return result

    def _revival_source(self, topic: dict, *, now: datetime.datetime) -> str | None:
        if self.memory_store is None:
            return None
        try:
            last_active = datetime.datetime.fromisoformat(str(topic.get("last_active_at")))
        except ValueError:
            return None
        keywords = [str(word) for word in topic.get("keywords", []) if str(word)]
        if not keywords:
            return None
        for memory in self.memory_store.list():
            created = _parse_time(memory.get("created_at", ""))
            if created is None or created < last_active:
                continue
            content = str(memory.get("content", ""))
            if any(word and word in content for word in keywords):
                return content[:60]
        return None

    # ── 组装 ────────────────────────────────────────────────────────
    def _make(
        self,
        *,
        key: str,
        topic: str,
        reason: str,
        hint: str,
        base: dict,
        boost: float,
        life_id: str = "",
        memory_ids: list[str] | None = None,
        special: bool = False,
        priority: int = 0,
        expires_at: str = "",
        source: str = "",
    ) -> Candidate:
        features = dict(base)
        features[reason] = max(float(features.get(reason, 0.0)), max(0.0, min(1.0, boost)))
        score, parts = score_candidate(features, self.weights)
        if not source:
            source = "life" if life_id else ("topic" if reason == "topic_revival" else "memory")
        return Candidate(
            id=str(key),
            topic=topic,
            reason=reason,
            score=score,
            hint=hint,
            source=source,
            memory_ids=list(memory_ids or []),
            life_id=life_id,
            features=features,
            parts=parts,
            special=special,
            priority=priority,
            expires_at=expires_at,
        )
