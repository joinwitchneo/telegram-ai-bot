"""角色的第二本世界书：把聊天内容定期浓缩成"记忆"，并定期整理，控制 token 占用。"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path
from typing import Callable


class MemoryBook:
    def __init__(
        self,
        path: Path,
        ask: Callable[[list[dict]], str],
        summary_every: int = 6,
        inject_limit: int = 2500,
        condense_at: int = 25,
    ) -> None:
        self.path = path
        self.ask = ask
        self.summary_every = max(2, summary_every)
        self.inject_limit = max(500, inject_limit)
        self.condense_at = max(5, condense_at)
        self._lock = threading.Lock()
        self.data: dict = {"chats": {}}
        if path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {"chats": {}}
        self.data.setdefault("chats", {})
        self.data.setdefault("users", {})

    def touch_user(self, chat_id: int, user_info: dict | None = None) -> None:
        """登记用户身份（昵称/用户名/最近活跃），方便区分不同的{user}。"""
        with self._lock:
            users = self.data.setdefault("users", {})
            key = str(chat_id)
            record = users.setdefault(key, {})
            user_info = user_info or {}
            if user_info.get("first_name"):
                record["name"] = user_info["first_name"]
            if user_info.get("username"):
                record["username"] = user_info["username"]
            record.setdefault("first_seen", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
            record["last_seen"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.save()

    def _chat(self, chat_id: int) -> dict:
        chat = self.data["chats"].setdefault(
            str(chat_id),
            {"entries": [], "long_term": "", "turn_count": 0, "last_summary_turn": 0},
        )
        chat.setdefault("entries", [])
        chat.setdefault("long_term", "")
        chat.setdefault("turn_count", 0)
        chat.setdefault("last_summary_turn", 0)
        return chat

    def add_note(self, chat_id: int, text: str) -> None:
        """手动记一条记忆（比如刚发生的重要事件）。"""
        with self._lock:
            chat = self._chat(chat_id)
            chat["entries"].append(
                {"ts": datetime.datetime.now().strftime("%m-%d %H:%M"), "text": text}
            )
        self.save()

    def maybe_summarize(self, chat_id: int, history: list[dict]) -> None:
        """每隔 N 轮对话，把最近的聊天浓缩成一条简短记忆。"""
        with self._lock:
            chat = self._chat(chat_id)
            chat["turn_count"] += 1
            if chat["turn_count"] - chat["last_summary_turn"] < self.summary_every:
                return
            recent = [dict(t) for t in history[- self.summary_every * 2:]]
        if not recent:
            return
        try:
            summary = self._summarize(recent)
        except Exception as exc:  # noqa: BLE001 - 总结失败不影响聊天
            logging.warning("memory summary failed: %s", exc)
            return
        if not summary:
            return
        with self._lock:
            chat = self._chat(chat_id)
            chat["last_summary_turn"] = chat["turn_count"]
            chat["entries"].append(
                {"ts": datetime.datetime.now().strftime("%m-%d %H:%M"), "text": summary}
            )
            condensed = self._maybe_condense_locked(chat)
        self.save()
        logging.info("memory book: chat %s summarized (entries=%d)", chat_id, len(chat["entries"]))
        if condensed:
            logging.info("memory book: chat %s condensed", chat_id)

    def _summarize(self, recent: list[dict]) -> str:
        lines = []
        for item in recent:
            role = "对方" if item.get("role") == "user" else "角色"
            content = (item.get("content") or "").strip()
            if not content or content.startswith("[") or content.startswith("（"):
                continue
            lines.append(f"{role}：{content[:120]}")
        if not lines:
            return ""
        log_text = "\n".join(lines[-20:])
        prompt = (
            "以下是角色和对方最近的一段聊天记录。\n"
            "请用角色的视角，只总结**关键部分**，浓缩成 1~3 句\"角色会记住的事\"：\n"
            "- 只保留：对方的重要近况（身体/工作/行程变化）、明确说过的约定或承诺、"
            "重要日子（生日/节日/提醒事项）、角色重要的情绪转折或心事。\n"
            "- 丢掉：寒暄、打招呼、表情包、日常琐碎闲聊、重复或客套的话。\n"
            "每条不超过 40 字；用简体中文；不要加序号标题；没有值得记的就只写最关键的。\n\n"
            f"{log_text}"
        )
        return self.ask([{"role": "user", "content": prompt}]).strip()

    def _maybe_condense_locked(self, chat: dict) -> bool:
        """记忆太多时，把最旧的一半合并进长期记忆，保持注入内容精简。"""
        entries = chat.get("entries", [])
        if len(entries) <= self.condense_at:
            return False
        old = entries[: len(entries) // 2]
        keep = entries[len(entries) // 2:]
        old_text = "\n".join(f"- {e.get('text', '')}" for e in old)
        long_term = chat.get("long_term", "")
        prompt = (
            "以下是角色的旧记忆条目和旧的长期记忆。\n"
            "请把它们合并成一段更简洁的\"长期记忆\"：只保留关键事实、关系、约定和长期状态，"
            "丢掉已经过时或琐碎的细节；用简体中文，200 字以内，一段话。\n\n"
            f"旧的长期记忆：{long_term or '（无）'}\n\n旧条目：\n{old_text}"
        )
        try:
            merged = self.ask([{"role": "user", "content": prompt}]).strip()
        except Exception as exc:  # noqa: BLE001
            logging.warning("memory condense failed: %s", exc)
            return False
        if not merged:
            return False
        chat["long_term"] = merged
        chat["entries"] = keep
        return True

    def injection(self, chat_id: int, limit: int | None = None) -> str:
        """生成注入系统提示词的精简记忆文本（省 token）。"""
        with self._lock:
            chat = self._chat(chat_id)
            entries = [dict(e) for e in chat.get("entries", [])]
            long_term = chat.get("long_term", "")
        limit = limit or self.inject_limit
        parts: list[str] = []
        if long_term:
            parts.append(f"【长期记忆】{long_term}")
        for e in entries[-15:]:
            parts.append(f"- {e.get('ts', '')}：{e.get('text', '')}")
        text = "\n".join(parts)
        if len(text) <= limit:
            return text
        # 超限：从旧条目开始丢，保留长期记忆和最新条目
        while len(text) > limit and len(parts) > 1:
            parts.pop(1)
            text = "\n".join(parts)
        return text[:limit]

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save memory book: %s", exc)
