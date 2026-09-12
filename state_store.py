"""每个用户独立的会话状态（情绪 / 关系 / 最近回复短语）。

用 JSON 持久化，结构上留了以后换 SQLite 的余地：所有读写都走这里，
别的模块不直接碰文件。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

import emotion as emotion_mod


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {"chats": {}}
        if path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {"chats": {}}
        self.data.setdefault("chats", {})

    # ── 基础 ────────────────────────────────────────────────────────
    def get(self, chat_id: int) -> dict:
        """取出（必要时创建）某个用户的状态。"""
        with self._lock:
            chat = self.data["chats"].setdefault(str(chat_id), {})
            chat.setdefault("emotion", emotion_mod.EmotionEngine.new_state())
            chat.setdefault(
                "relationship",
                {
                    "level": 0.05,
                    "familiarity": 0.05,
                    "trust": 0.20,
                    "dependence": 0.10,      # 依赖 / 粘人程度（会随互动慢慢长）
                    "playfulness": 0.20,     # 玩闹程度：越高越敢开玩笑、越敢损
                    "recent_conflicts": 0,
                    "shared_memories": [],
                    "turns": 0,
                },
            )
            chat.setdefault("recent_phrases", [])
            chat.setdefault("last_message_at", 0.0)
            chat.setdefault("last_bot_message_at", 0.0)
            chat.setdefault("last_proactive_at", 0.0)
            chat.setdefault("cold_streak", 0)
            chat.setdefault("proactive_streak", 0)
            # 未完成话题（OpenTopic）：话题 / 状态 / 创建时间 / 重要性 / 上次问的时间
            chat.setdefault("open_topics", [])
            # 情绪余波：还没消化的不痛快，之后自然提起
            chat.setdefault("residues", [])
            # 聊天阶段：IDLE / STARTING / CASUAL / DEEP / PLAYFUL / CONFLICT / ENDING / COOLDOWN
            chat.setdefault("phase", "IDLE")
            chat.setdefault("phase_since", 0.0)
            chat.setdefault("cooldown_until", 0.0)
            # 最近用过的口癖/短语，用来避免重复
            chat.setdefault("recent_tokens", [])
            # 内部念头：她想找他的理由（不是直接发出去的消息）
            chat.setdefault("thoughts", [])
            chat.setdefault("thoughts_generated_at", 0.0)
            # 旧版数据结构兼容：老的 followup_topics（纯字符串）迁移进 open_topics
            legacy = chat.pop("followup_topics", None)
            if legacy:
                for item in legacy:
                    text = str(item).strip()
                    if text and not any(t.get("topic") == text for t in chat["open_topics"]):
                        chat["open_topics"].append(
                            {
                                "topic": text[:80],
                                "status": "waiting",
                                "created_at": datetime.datetime.now().timestamp(),
                                "importance": 0.6,
                                "asked_at": 0.0,
                                "asked_count": 0,
                            }
                        )
            chat.setdefault(
                "user_style",
                {
                    "samples": 0, "avg_length": 0.0, "avg_count": 1.0,
                    "emoji_rate": 0.0, "punct_rate": 0.5,
                    "energy_rate": 0.0, "burst_rate": 0.0,
                },
            )
            return chat

    # ── 用户聊天风格（长期适应，文档第二/六节）────────────────────
    def update_user_style(self, chat_id: int, style: dict) -> dict:
        chat = self.get(chat_id)
        current = chat["user_style"]
        samples = int(current.get("samples", 0))
        alpha = 0.35 if samples < 5 else 0.15

        def ema(key: str, value: float) -> None:
            current[key] = (1 - alpha) * float(current.get(key, 0.0)) + alpha * float(value)

        ema("avg_length", style.get("average_length", 0))
        ema("avg_count", style.get("message_count", 1))
        ema("emoji_rate", 1.0 if style.get("emoji_usage") else 0.0)
        ema("punct_rate", 0.0 if style.get("punctuation_style") == "sparse" else 1.0)
        ema("energy_rate", 1.0 if style.get("energy") == "high" else 0.0)
        ema("burst_rate", 1.0 if style.get("burst_style") else 0.0)
        current["samples"] = samples + 1
        return current

    def user_style(self, chat_id: int) -> dict:
        return self.get(chat_id).get("user_style", {})

    def style_brief(self, chat_id: int) -> str:
        """把这个用户长期的聊天习惯描述成一句话（样本不足时返回空）。"""
        style = self.user_style(chat_id)
        if not style or int(style.get("samples", 0)) < 3:
            return ""
        parts: list[str] = []
        avg_length = float(style.get("avg_length", 0))
        if avg_length <= 6:
            parts.append("他习惯发很短的消息")
        elif avg_length >= 60:
            parts.append("他习惯发长消息")
        if float(style.get("burst_rate", 0)) >= 0.5:
            parts.append("他经常连着发几条")
        emoji_rate = float(style.get("emoji_rate", 0))
        if emoji_rate < 0.1:
            parts.append("他基本不用 emoji")
        elif emoji_rate > 0.5:
            parts.append("他爱用 emoji")
        if float(style.get("punct_rate", 1)) < 0.3:
            parts.append("他很少打标点")
        return "、".join(parts)

    # ── 主动消息节流（文档第十四节：不回复就不再发）────────────────
    def proactive_streak(self, chat_id: int) -> int:
        return int(self.get(chat_id).get("proactive_streak", 0) or 0)

    def bump_proactive_streak(self, chat_id: int) -> int:
        chat = self.get(chat_id)
        chat["proactive_streak"] = self.proactive_streak(chat_id) + 1
        return chat["proactive_streak"]

    def reset_proactive_streak(self, chat_id: int) -> None:
        self.get(chat_id)["proactive_streak"] = 0

    # ── 依赖度（恋爱 / 粘人倾向）──────────────────────────────────
    def bump_dependence(self, chat_id: int, amount: float) -> float:
        chat = self.get(chat_id)
        rel = chat["relationship"]
        rel["dependence"] = max(0.0, min(1.0, float(rel.get("dependence", 0.1)) + amount))
        return rel["dependence"]

    def dependence(self, chat_id: int) -> float:
        return float(self.get(chat_id)["relationship"].get("dependence", 0.1))

    def bump_playfulness(self, chat_id: int, amount: float) -> float:
        rel = self.get(chat_id)["relationship"]
        rel["playfulness"] = max(0.0, min(1.0, float(rel.get("playfulness", 0.2)) + amount))
        return rel["playfulness"]

    def playfulness(self, chat_id: int) -> float:
        return float(self.get(chat_id)["relationship"].get("playfulness", 0.2))

    # ── 内部念头（写给主动聊天用的"想找他的理由"）────────────────
    def add_thought(self, chat_id: int, kind: str, text: str) -> None:
        text = (text or "").strip()[:60]
        if not text:
            return
        chat = self.get(chat_id)
        store = chat["thoughts"]
        if any(item.get("thought") == text for item in store):
            return
        store.append(
            {
                "type": (kind or "curiosity")[:20],
                "thought": text,
                "created_at": datetime.datetime.now().timestamp(),
            }
        )
        del store[:-5]

    def thoughts(self, chat_id: int) -> list[dict]:
        chat = self.get(chat_id)
        now = datetime.datetime.now().timestamp()
        kept = [t for t in chat["thoughts"] if (now - float(t.get("created_at", now))) < 3 * 86400]
        chat["thoughts"] = kept
        return [dict(t) for t in kept]

    def pop_thought(self, chat_id: int) -> dict | None:
        """取一个念头用掉（优先"想他"和"没聊完的事"）。"""
        store = self.thoughts(chat_id)
        if not store:
            return None
        priority = {"unfinished": 0, "missing": 1, "curiosity": 2, "playful": 3, "complaint": 4}
        store.sort(key=lambda t: priority.get(str(t.get("type")), 9))
        chosen = store[0]
        chat = self.get(chat_id)
        chat["thoughts"] = [t for t in chat["thoughts"] if t.get("thought") != chosen.get("thought")]
        return chosen

    # ── 未完成话题 OpenTopic ───────────────────────────────────────
    def add_open_topics(self, chat_id: int, topics: list[str], importance: float = 0.6) -> int:
        chat = self.get(chat_id)
        store = chat["open_topics"]
        added = 0
        now = datetime.datetime.now().timestamp()
        for topic in topics:
            text = (topic or "").strip()[:80]
            if not text:
                continue
            if any(item.get("topic") == text for item in store):
                continue
            store.append(
                {
                    "topic": text,
                    "status": "waiting",
                    "created_at": now,
                    "importance": max(0.0, min(1.0, importance)),
                    "asked_at": 0.0,
                    "asked_count": 0,
                }
            )
            added += 1
        del store[:-12]
        return added

    def pick_open_topic(self, chat_id: int) -> dict | None:
        """挑一个最值得主动问起的话题：重要、没问过、没过期。"""
        self.prune_open_topics(chat_id)
        candidates = [t for t in self.get(chat_id)["open_topics"] if t.get("status") != "resolved"]
        if not candidates:
            return None
        now = datetime.datetime.now().timestamp()
        candidates.sort(
            key=lambda t: (
                -float(t.get("importance", 0.5)),
                int(t.get("asked_count", 0)),
                -float(t.get("created_at", 0)),
            )
        )
        fresh = [t for t in candidates if (now - float(t.get("asked_at", 0) or 0)) > 6 * 3600]
        return (fresh or candidates)[0]

    def mark_topic_asked(self, chat_id: int, topic: str) -> None:
        for item in self.get(chat_id)["open_topics"]:
            if item.get("topic") == topic:
                item["asked_at"] = datetime.datetime.now().timestamp()
                item["asked_count"] = int(item.get("asked_count", 0)) + 1
                item["status"] = "asked"

    def resolve_asked_topics(self, chat_id: int) -> int:
        """用户回话之后，把刚问过的话题标记为已了结。"""
        resolved = 0
        for item in self.get(chat_id)["open_topics"]:
            if item.get("status") == "asked":
                item["status"] = "resolved"
                resolved += 1
        return resolved

    def prune_open_topics(self, chat_id: int) -> None:
        """丢过期和问太多次都没结果的话题。"""
        chat = self.get(chat_id)
        now = datetime.datetime.now().timestamp()
        kept = []
        for item in chat["open_topics"]:
            age_days = (now - float(item.get("created_at", now))) / 86400
            if age_days > 14:
                continue
            if int(item.get("asked_count", 0)) >= 3 and item.get("status") != "resolved":
                continue
            if item.get("status") == "resolved":
                continue
            kept.append(item)
        chat["open_topics"] = kept

    # 兼容旧接口
    def add_followup_topics(self, chat_id: int, topics: list[str]) -> None:
        self.add_open_topics(chat_id, topics)

    def pop_followup_topic(self, chat_id: int) -> str:
        topic = self.pick_open_topic(chat_id)
        if not topic:
            return ""
        self.mark_topic_asked(chat_id, str(topic.get("topic", "")))
        return str(topic.get("topic", ""))

    # ── 情绪余波 ───────────────────────────────────────────────────
    def add_residue(self, chat_id: int, reason: str, kind: str, intensity: float) -> None:
        chat = self.get(chat_id)
        store = chat["residues"]
        reason = (reason or "").strip()[:60]
        if not reason:
            return
        store.append(
            {
                "reason": reason,
                "kind": kind,
                "intensity": max(0.0, min(1.0, intensity)),
                "created_at": datetime.datetime.now().timestamp(),
                "turns": 0,
                "used": False,
            }
        )
        del store[:-5]

    def pending_residue(self, chat_id: int, min_turns: int = 2) -> dict | None:
        """还没消化、也还没提起过的情绪余波。"""
        now = datetime.datetime.now().timestamp()
        for item in self.get(chat_id)["residues"]:
            if item.get("used"):
                continue
            if (now - float(item.get("created_at", now))) > 24 * 3600:
                item["used"] = True
                continue
            if int(item.get("turns", 0)) < min_turns:
                continue
            return item
        return None

    def tick_residues(self, chat_id: int) -> None:
        for item in self.get(chat_id)["residues"]:
            item["turns"] = int(item.get("turns", 0)) + 1

    def mark_residue_used(self, chat_id: int, reason: str) -> None:
        for item in self.get(chat_id)["residues"]:
            if item.get("reason") == reason:
                item["used"] = True

    # ── 聊天阶段 ───────────────────────────────────────────────────
    def set_phase(self, chat_id: int, phase: str, cooldown_hours: float = 0.0) -> None:
        chat = self.get(chat_id)
        chat["phase"] = phase
        chat["phase_since"] = datetime.datetime.now().timestamp()
        if cooldown_hours > 0:
            chat["cooldown_until"] = datetime.datetime.now().timestamp() + cooldown_hours * 3600

    def phase(self, chat_id: int) -> str:
        chat = self.get(chat_id)
        if float(chat.get("cooldown_until", 0) or 0) > datetime.datetime.now().timestamp():
            return "COOLDOWN"
        return str(chat.get("phase", "IDLE"))

    def in_cooldown(self, chat_id: int) -> bool:
        return float(self.get(chat_id).get("cooldown_until", 0) or 0) > datetime.datetime.now().timestamp()

    # ── 口癖 / 短语频率 ────────────────────────────────────────────
    def note_tokens(self, chat_id: int, tokens: list[str]) -> None:
        chat = self.get(chat_id)
        store = chat["recent_tokens"]
        store.extend(tokens)
        del store[:-40]

    def recent_tokens(self, chat_id: int, count: int = 8) -> list[str]:
        return list(self.get(chat_id)["recent_tokens"])[-count:]

    def overused_tokens(self, chat_id: int, window: int = 8, threshold: int = 3) -> list[str]:
        """最近 window 条回复里出现次数过多的口癖，本轮别再用了。"""
        recent = self.get(chat_id)["recent_tokens"][-window:]
        counts: dict[str, int] = {}
        for token in recent:
            counts[token] = counts.get(token, 0) + 1
        return [token for token, count in counts.items() if count >= threshold]

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save conversation states: %s", exc)

    # ── 时间戳 ──────────────────────────────────────────────────────
    def note_incoming(self, chat_id: int) -> float:
        now = datetime.datetime.now().timestamp()
        self.get(chat_id)["last_message_at"] = now
        return now

    def note_outgoing(self, chat_id: int) -> float:
        now = datetime.datetime.now().timestamp()
        self.get(chat_id)["last_bot_message_at"] = now
        return now

    def note_proactive(self, chat_id: int) -> None:
        self.get(chat_id)["last_proactive_at"] = datetime.datetime.now().timestamp()

    def minutes_since_last_message(self, chat_id: int) -> float:
        last = float(self.get(chat_id).get("last_message_at") or 0.0)
        if last <= 0:
            return 0.0
        return max(0.0, (datetime.datetime.now().timestamp() - last) / 60.0)

    # ── 关系 ────────────────────────────────────────────────────────
    def update_relationship(
        self,
        chat_id: int,
        delta: float = 0.0,
        *,
        shared_memory: str | None = None,
        conflict: bool = False,
    ) -> dict:
        chat = self.get(chat_id)
        rel = chat["relationship"]
        rel["turns"] = int(rel.get("turns", 0)) + 1
        if delta:
            rel["level"] = max(0.0, min(1.0, float(rel.get("level", 0.05)) + delta))
            rel["familiarity"] = max(0.0, min(1.0, float(rel.get("familiarity", 0.05)) + delta * 0.8))
        if conflict:
            rel["recent_conflicts"] = int(rel.get("recent_conflicts", 0)) + 1
            rel["trust"] = max(0.0, float(rel.get("trust", 0.2)) - 0.02)
        if shared_memory:
            memories = rel.setdefault("shared_memories", [])
            if shared_memory not in memories:
                memories.append(shared_memory)
                rel["level"] = max(0.0, min(1.0, float(rel.get("level", 0.05)) + 0.02))
            del memories[:-20]  # 最多留 20 条共同经历
        return rel

    def relationship_label(self, level: float) -> str:
        if level < 0.20:
            return "基本陌生"
        if level < 0.40:
            return "认识"
        if level < 0.60:
            return "熟悉"
        if level < 0.80:
            return "很熟"
        return "高度亲近"

    def relationship_describe(self, chat_id: int) -> str:
        rel = self.get(chat_id)["relationship"]
        level = float(rel.get("level", 0.05))
        parts = [f"关系等级 {level:.2f}（{self.relationship_label(level)}）"]
        conflicts = int(rel.get("recent_conflicts", 0))
        if conflicts:
            parts.append(f"最近有过 {conflicts} 次不愉快")
        shared = rel.get("shared_memories") or []
        if shared:
            parts.append("共同经历：" + "；".join(shared[-3:]))
        return "；".join(parts)

    # ── 回复短语（防模板化）────────────────────────────────────────
    def remember_phrase(self, chat_id: int, phrase: str) -> None:
        phrase = (phrase or "").strip()
        if not phrase:
            return
        chat = self.get(chat_id)
        phrases = chat.setdefault("recent_phrases", [])
        phrases.append(phrase[:40])
        del phrases[:-15]

    def phrase_repeated(self, chat_id: int, phrase: str) -> bool:
        """最近说过几乎一样的话（开头相同也算）。"""
        phrase = (phrase or "").strip()
        if len(phrase) < 4:
            return False
        recent = self.get(chat_id).get("recent_phrases", [])
        head = phrase[:8]
        return any(item == phrase or (len(item) >= 4 and item[:8] == head) for item in recent)

    def recent_phrases(self, chat_id: int, count: int = 6) -> list[str]:
        return list(self.get(chat_id).get("recent_phrases", []))[-count:]
