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
            return chat

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
