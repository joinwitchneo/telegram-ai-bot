"""角色的第二本世界书：把聊天内容定期浓缩成"记忆"，并定期检查、整理，控制 token 占用。

三层结构：
1. entries：一条条带时间戳的短期记忆（每隔几轮自动总结出来的）；
2. long_term：把过旧的条目合并成的长期记忆（一段话，稳定保留）；
3. maintenance()：定期检查——补齐漏掉的总结、压缩过长的记忆、必要时整体重建。
"""

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
        rebuild_at: int = 8000,
        keep_entries: int = 10,
    ) -> None:
        self.path = path
        self.ask = ask
        self.summary_every = max(2, summary_every)
        self.inject_limit = max(500, inject_limit)
        self.condense_at = max(5, condense_at)
        self.rebuild_at = max(2000, rebuild_at)
        self.keep_entries = max(3, keep_entries)
        self._lock = threading.Lock()
        self.data: dict = {"chats": {}}
        if path.is_file():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {"chats": {}}
        self.data.setdefault("chats", {})
        self.data.setdefault("users", {})

    # ── 基础 ────────────────────────────────────────────────────────
    def touch_user(self, chat_id: int, user_info: dict | None = None) -> None:
        """登记用户身份（昵称/用户名/最近活跃），方便区分不同的用户。"""
        with self._lock:
            users = self.data.setdefault("users", {})
            record = users.setdefault(str(chat_id), {})
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
        """手动记一条记忆（比如刚发生的重要事件、待办变化）。"""
        with self._lock:
            chat = self._chat(chat_id)
            chat["entries"].append(
                {
                    "ts": datetime.datetime.now().strftime("%m-%d %H:%M"),
                    "text": text,
                    "kind": "event",
                    "importance": 0.6,
                    "active": True,
                }
            )
        self.save()

    # ── 结构化记忆（带类型与重要性）──────────────────────────────────
    def add_entry(
        self,
        chat_id: int,
        text: str,
        kind: str = "fact",
        importance: float = 0.5,
        confidence: float = 0.8,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            chat = self._chat(chat_id)
            entries = chat["entries"]
            for item in entries:
                if item.get("text") == text and item.get("active", True):
                    return
            entries.append(
                {
                    "ts": datetime.datetime.now().strftime("%m-%d %H:%M"),
                    "text": text,
                    "kind": kind,
                    "importance": max(0.0, min(1.0, float(importance))),
                    "confidence": max(0.0, min(1.0, float(confidence))),
                    "active": True,
                }
            )
        self.save()

    def supersede(self, chat_id: int, keyword: str) -> int:
        """新信息推翻旧信息：把含关键词的旧条目标记为失效。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return 0
        changed = 0
        with self._lock:
            chat = self._chat(chat_id)
            for item in chat["entries"]:
                if item.get("active", True) and keyword in str(item.get("text", "")):
                    item["active"] = False
                    changed += 1
        if changed:
            self.save()
        return changed

    def active_entries(self, chat_id: int, min_importance: float = 0.0) -> list[dict]:
        with self._lock:
            chat = self._chat(chat_id)
            return [
                dict(e)
                for e in chat.get("entries", [])
                if e.get("active", True) and float(e.get("importance", 0.5)) >= min_importance
            ]

    EXTRACT_PROMPT = (
        "以下是角色和用户刚才的对话。请只挑出**值得长期记住**的信息，输出 JSON 数组；"
        "没有任何值得记的就输出 []。格式：\n"
        '[{"content": "用户喜欢玩某游戏", "type": "preference", "importance": 0.72, "replaces": ""}]\n'
        "type 只能是：fact（长期事实：名字/职业/住哪/身体状况）、preference（长期喜好与讨厌）、"
        "commitment（约定或让他记住的事）、experience（重要共同经历）、relation（关系事件）。\n"
        "importance 是 0~1 的重要性，低于 0.25 的不要输出。\n"
        "不要记录：寒暄、表情、一次性的心情、闲聊废话、天气、当前时间。\n"
        "如果新信息推翻了旧信息（比如“现在不喜欢了”），把旧信息的关键词填进 replaces。\n"
        "只输出 JSON，不要解释。\n\n对话：\n{CONVERSATION}"
    )

    def extract_candidates(self, chat_id: int, user_text: str, assistant_text: str) -> int:
        """让模型挑出值得长期记住的内容，交给 Python 决定怎么存。"""
        conversation = f"用户：{user_text[:400]}\n角色：{assistant_text[:400]}"
        try:
            raw = self.ask(
                [{"role": "user", "content": self.EXTRACT_PROMPT.replace("{CONVERSATION}", conversation)}]
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("memory extraction failed: %s", exc)
            return 0
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end <= start:
            return 0
        try:
            items = json.loads(raw[start : end + 1])
        except (ValueError, TypeError):
            return 0
        if not isinstance(items, list):
            return 0
        stored = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            importance = float(item.get("importance", 0.0) or 0.0)
            if not content or importance < 0.25:
                continue
            replaces = str(item.get("replaces", "") or "").strip()
            if replaces:
                self.supersede(chat_id, replaces)
            self.add_entry(
                chat_id,
                content,
                kind=str(item.get("type", "fact"))[:20],
                importance=importance,
                confidence=float(item.get("confidence", 0.8) or 0.8),
            )
            stored += 1
        if stored:
            logging.info("memory book: chat %s stored %d structured memories", chat_id, stored)
        return stored

    # ── 总结 ────────────────────────────────────────────────────────
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
            chat["last_maintenance"] = datetime.datetime.now().isoformat(timespec="seconds")
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
            "以下是角色和用户最近的一段聊天记录。\n"
            "请用角色的视角，只总结**关键部分**，浓缩成 1~3 句\"角色会记住的事\"：\n"
            "- 只保留：用户的重要近况（身体/工作/行程/心情变化）、明确说过的约定或承诺、"
            "重要日子与待办（生日/节日/提醒/没做完的事）、用户明确表达过的偏好和忌讳。\n"
            "- 丢掉：寒暄、打招呼、表情包、日常琐碎闲聊、重复或客套的话。\n"
            "每条不超过 40 字；用简体中文；不要加序号标题；没有值得记的就返回空。\n\n"
            f"{log_text}"
        )
        return self.ask([{"role": "user", "content": prompt}]).strip()

    # ── 定期检查与更新 ──────────────────────────────────────────────
    def maintenance(
        self,
        chat_id: int,
        history: list[dict],
        min_interval_hours: float = 6.0,
    ) -> dict:
        """定期检查记忆书：补齐漏掉的总结、压缩过长的记忆、必要时整体重建。

        返回本次做了什么，方便记日志。默认同一会话 6 小时内只真正跑一次。"""
        now = datetime.datetime.now()
        with self._lock:
            chat = self._chat(chat_id)
            last = chat.get("last_maintenance")
            if last:
                try:
                    if (now - datetime.datetime.fromisoformat(last)).total_seconds() < min_interval_hours * 3600:
                        return {"skipped": True}
                except ValueError:
                    pass
            pending = max(0, chat["turn_count"] - chat["last_summary_turn"])
            entries = list(chat.get("entries", []))
            long_term = chat.get("long_term", "")
        report = {"summarized": False, "condensed": False, "rebuilt": False, "pending_turns": pending}

        # 1) 还有没被总结的对话 → 补一条
        if pending > 0 and history:
            recent = [dict(t) for t in history[-max(4, pending * 2):]]
            try:
                summary = self._summarize(recent)
            except Exception as exc:  # noqa: BLE001
                logging.warning("memory maintenance summarize failed: %s", exc)
                summary = ""
            if summary:
                with self._lock:
                    chat = self._chat(chat_id)
                    chat["entries"].append({"ts": now.strftime("%m-%d %H:%M"), "text": summary})
                    chat["last_summary_turn"] = chat["turn_count"]
                report["summarized"] = True

        # 2) 记忆条数过多 → 压缩进长期记忆
        with self._lock:
            chat = self._chat(chat_id)
            if len(chat.get("entries", [])) > self.condense_at:
                report["condensed"] = self._maybe_condense_locked(chat)
            total_chars = len(chat.get("long_term", "")) + sum(
                len(e.get("text", "")) for e in chat.get("entries", [])
            )
            # 3) 整体太大 → 连长期记忆一起重建，只保留最近几条
            if total_chars > self.rebuild_at:
                report["rebuilt"] = self._rebuild_locked(chat)
            chat["last_maintenance"] = now.isoformat(timespec="seconds")
        self.save()
        logging.info(
            "memory maintenance: chat %s summarized=%s condensed=%s rebuilt=%s pending=%s",
            chat_id,
            report["summarized"],
            report["condensed"],
            report["rebuilt"],
            pending,
        )
        return report

    def _maybe_condense_locked(self, chat: dict) -> bool:
        """记忆太多时，把最旧的一半合并进长期记忆，保持注入内容精简。"""
        chat["entries"] = [e for e in chat.get("entries", []) if e.get("active", True)]
        entries = chat.get("entries", [])
        if len(entries) <= self.condense_at:
            return False
        old = entries[: len(entries) // 2]
        keep = entries[len(entries) // 2:]
        old_text = "\n".join(f"- {e.get('text', '')}" for e in old)
        long_term = chat.get("long_term", "")
        prompt = (
            "以下是角色的旧记忆条目和旧的长期记忆。\n"
            "请把它们合并成一段更简洁的\"长期记忆\"：只保留关键事实、关系、约定、"
            "长期有效的偏好和还在进行中的事情，丢掉已经过时或琐碎的细节；"
            "用简体中文，250 字以内，一段话，不要分点。\n\n"
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

    def _rebuild_locked(self, chat: dict) -> bool:
        """整体重建：把全部记忆重新压成一段长期记忆，只留最近几条短期记忆。"""
        entries = chat.get("entries", [])
        if not entries:
            return False
        text = "\n".join(f"- {e.get('ts', '')}：{e.get('text', '')}" for e in entries)
        prompt = (
            "以下是角色记住的全部内容（旧的长期记忆 + 全部条目）。\n"
            "请重写成一段 300 字以内的\"长期记忆\"，只保留：用户的基本情况和长期偏好、"
            "重要约定、长期有效的安排、还没做完的事、重要的关系与日子。"
            "过时的、一次性的细节全部丢掉。用简体中文，一段话，不要分点。\n\n"
            f"旧长期记忆：{chat.get('long_term', '') or '（无）'}\n\n全部条目：\n{text}"
        )
        try:
            merged = self.ask([{"role": "user", "content": prompt}]).strip()
        except Exception as exc:  # noqa: BLE001
            logging.warning("memory rebuild failed: %s", exc)
            return False
        if not merged:
            return False
        chat["long_term"] = merged
        chat["entries"] = entries[-self.keep_entries:]
        return True

    # ── 读取 ────────────────────────────────────────────────────────
    def injection(self, chat_id: int, limit: int | None = None) -> str:
        """生成注入系统提示词的精简记忆文本（省 token，但保证关键内容一定带上）。"""
        with self._lock:
            chat = self._chat(chat_id)
            entries = [dict(e) for e in chat.get("entries", []) if e.get("active", True)]
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
        # 超限：先丢中间的旧条目，长期记忆和最近几条一定保留
        while len(text) > limit and len(parts) > 6:
            parts.pop(1)
            text = "\n".join(parts)
        return text[:limit]

    def memory_text(self, chat_id: int) -> str:
        """给 /memory 用的完整展示文本（含长期记忆和全部条目）。"""
        with self._lock:
            chat = self._chat(chat_id)
            entries = [dict(e) for e in chat.get("entries", []) if e.get("active", True)]
            long_term = chat.get("long_term", "")
            last = chat.get("last_maintenance", "")
        parts: list[str] = []
        if long_term:
            parts.append(f"【长期记忆】{long_term}")
        for e in entries:
            parts.append(f"- {e.get('ts', '')}：{e.get('text', '')}")
        if not parts:
            return ""
        if last:
            parts.append(f"（上次整理：{last[:16].replace('T', ' ')}）")
        return "\n".join(parts)

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save memory book: %s", exc)
