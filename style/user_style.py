"""User Style Profile（Phase 5）：慢慢适应对方的聊天习惯。

设计要点：
- 用 EMA（指数移动平均）而不是全历史平均，用户风格变了不会半年后还停在旧风格；
- 长期特征（标点、emoji、提问率）用慢 alpha，短期特征（长度、连发）用快 alpha；
- 只做统计，不做人格判断；不复制用户，只提供"参考"。
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import threading
from pathlib import Path

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
PUNCT_RE = re.compile(r"[。！？!?，,、；;：:~～]")
LATIN_RE = re.compile(r"[A-Za-z0-9_]{3,}")

SHORT_MESSAGE_CHARS = 12
LONG_MESSAGE_CHARS = 60
BURST_GAP_SECONDS = 120


def _ema(old: float, observation: float, alpha: float) -> float:
    return round(float(old) * (1 - alpha) + float(observation) * alpha, 6)


class UserStyle:
    def __init__(
        self,
        path: Path,
        *,
        alpha: float = 0.10,
        alpha_slow: float = 0.03,
        max_common_words: int = 12,
        recent_lengths: int = 50,
        min_messages: int = 5,
    ) -> None:
        self.path = path
        self.alpha = min(0.9, max(0.01, float(alpha)))
        self.alpha_slow = min(0.9, max(0.005, float(alpha_slow)))
        self.max_common_words = max(3, int(max_common_words))
        self.recent_lengths = max(5, int(recent_lengths))
        self.min_messages = max(1, int(min_messages))
        self._lock = threading.RLock()
        self.data: dict = {"chats": {}}
        self._load()

    # ── 存档 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("chats"), dict):
                self.data = loaded
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入用户风格失败：%s", exc)

    @staticmethod
    def _fresh() -> dict:
        return {
            "messages": 0,
            "average_message_length": 0.0,
            "short_message_ratio": 0.0,
            "long_message_ratio": 0.0,
            "emoji_frequency": 0.0,
            "question_frequency": 0.0,
            "punctuation_ratio": 0.0,
            "consecutive_message_count": 0.0,
            "reply_interval": 0.0,
            "active_hours": [0] * 24,
            "common_words": {},
            "recent_lengths": [],
            "last_message_at": "",
            "updated_at": "",
        }

    # ── 观察一条用户消息 ────────────────────────────────────────────
    def observe(self, chat_id: int | str, text: str, *, now: datetime.datetime | None = None) -> dict:
        raw = (text or "").strip()
        if not raw:
            return self.profile(chat_id)
        moment = now or datetime.datetime.now()
        length = len(raw)
        cjk = len(CJK_RE.findall(raw))
        has_emoji = bool(EMOJI_RE.search(raw))
        has_punct = bool(PUNCT_RE.search(raw))
        looks_question = ("?" in raw) or ("？" in raw)

        with self._lock:
            record = self.data["chats"].setdefault(str(chat_id), self._fresh())
            record["messages"] = int(record.get("messages", 0)) + 1

            record["average_message_length"] = _ema(
                record.get("average_message_length", 0.0), length, self.alpha
            )
            record["short_message_ratio"] = _ema(
                record.get("short_message_ratio", 0.0),
                1.0 if length <= SHORT_MESSAGE_CHARS else 0.0,
                self.alpha_slow,
            )
            record["long_message_ratio"] = _ema(
                record.get("long_message_ratio", 0.0),
                1.0 if length >= LONG_MESSAGE_CHARS else 0.0,
                self.alpha_slow,
            )
            record["emoji_frequency"] = _ema(
                record.get("emoji_frequency", 0.0), 1.0 if has_emoji else 0.0, self.alpha_slow
            )
            record["question_frequency"] = _ema(
                record.get("question_frequency", 0.0), 1.0 if looks_question else 0.0, self.alpha_slow
            )
            record["punctuation_ratio"] = _ema(
                record.get("punctuation_ratio", 0.0), 1.0 if has_punct else 0.0, self.alpha_slow
            )

            previous = self._parse_time(record.get("last_message_at", ""))
            gap = (moment - previous).total_seconds() if previous else None
            if gap is not None and 0 <= gap <= BURST_GAP_SECONDS:
                run = float(record.get("_burst_run", 0.0)) + 1
            else:
                run = 1.0
            record["_burst_run"] = run
            record["consecutive_message_count"] = _ema(
                record.get("consecutive_message_count", 0.0), run, self.alpha
            )
            if gap is not None and 0 < gap <= 6 * 3600:
                record["reply_interval"] = _ema(record.get("reply_interval", gap), gap, self.alpha)

            hours = record.get("active_hours") or [0] * 24
            if len(hours) != 24:
                hours = [0] * 24
            hours[moment.hour] = int(hours[moment.hour]) + 1
            record["active_hours"] = hours

            lengths = record.get("recent_lengths") or []
            lengths.append(length)
            record["recent_lengths"] = lengths[-self.recent_lengths :]

            self._observe_words(record, raw, cjk)
            record["last_message_at"] = moment.isoformat(timespec="seconds")
            record["updated_at"] = moment.isoformat(timespec="seconds")
        self.save()
        return self.profile(chat_id)

    def _observe_words(self, record: dict, raw: str, cjk: int) -> None:
        words = record.get("common_words") or {}
        if not isinstance(words, dict):
            words = {}
        for token in LATIN_RE.findall(raw):
            key = token.lower()
            words[key] = int(words.get(key, 0)) + 1
        chinese = CJK_RE.findall(raw)
        if cjk >= 4:
            for index in range(len(chinese) - 1):
                key = chinese[index] + chinese[index + 1]
                words[key] = int(words.get(key, 0)) + 1
        if len(words) > self.max_common_words * 4:
            ranked = sorted(words.items(), key=lambda item: (-item[1], item[0]))
            words = dict(ranked[: self.max_common_words * 2])
        record["common_words"] = words

    @staticmethod
    def _parse_time(value: str) -> datetime.datetime | None:
        try:
            return datetime.datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None

    # ── 读取 ────────────────────────────────────────────────────────
    def profile(self, chat_id: int | str) -> dict:
        with self._lock:
            record = dict(self.data["chats"].get(str(chat_id)) or self._fresh())
        lengths = list(record.get("recent_lengths") or [])
        if lengths:
            ordered = sorted(lengths)
            middle = len(ordered) // 2
            median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        else:
            median = 0.0
        words = record.get("common_words") or {}
        top_words = [word for word, _ in sorted(words.items(), key=lambda item: (-item[1], item[0]))[:6]]
        hours = list(record.get("active_hours") or [0] * 24)
        active_hours = [index for index, count in enumerate(hours) if count > 0]
        profile = {
            "messages": int(record.get("messages", 0)),
            "average_message_length": round(float(record.get("average_message_length", 0.0)), 1),
            "median_message_length": round(float(median), 1),
            "short_message_ratio": round(float(record.get("short_message_ratio", 0.0)), 3),
            "long_message_ratio": round(float(record.get("long_message_ratio", 0.0)), 3),
            "emoji_frequency": round(float(record.get("emoji_frequency", 0.0)), 3),
            "question_frequency": round(float(record.get("question_frequency", 0.0)), 3),
            "punctuation_ratio": round(float(record.get("punctuation_ratio", 0.0)), 3),
            "consecutive_message_count": round(float(record.get("consecutive_message_count", 0.0)), 2),
            "reply_interval": round(float(record.get("reply_interval", 0.0)), 1),
            "active_hours": active_hours,
            "common_words": top_words,
            "ready": int(record.get("messages", 0)) >= self.min_messages,
        }
        profile["punctuation_style"] = self._punctuation_label(profile["punctuation_ratio"])
        return profile

    @staticmethod
    def _punctuation_label(ratio: float) -> str:
        if ratio <= 0.25:
            return "极少标点"
        if ratio >= 0.7:
            return "标点完整"
        return "标点适中"

    def reset(self, chat_id: int | str | None = None) -> None:
        with self._lock:
            if chat_id is None:
                self.data = {"chats": {}}
            else:
                self.data["chats"].pop(str(chat_id), None)
        self.save()
