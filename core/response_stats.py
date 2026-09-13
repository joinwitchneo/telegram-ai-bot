"""回复行为统计（Phase 5）：只落盘，不参与决策、不注入 Prompt。

用来回答"她最近回得多长、拆几条、什么时候选择不回"这类问题。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path


def _empty_day() -> dict:
    return {
        "replies": 0,
        "silences": 0,
        "messages_total": 0,
        "retries": 0,
        "fallbacks": 0,
        "stickers": 0,
        "score_sum": 0.0,
        "pause_sum": 0.0,
        "pause_samples": 0,
        "silence_reasons": {},
        "by_mode": {},
        "by_length": {},
        "by_tone": {},
    }


def _bump(bucket: dict, key: str, amount: int = 1) -> None:
    if not key:
        return
    bucket[key] = int(bucket.get(key, 0)) + int(amount)


class ResponseStats:
    def __init__(self, path: Path, enabled: bool = True) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self.data: dict = {"days": {}}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("days"), dict):
                    self.data = {"days": loaded["days"]}
            except (OSError, json.JSONDecodeError):
                pass

    def record(
        self,
        *,
        mode: str = "",
        length: str = "",
        tone: str = "",
        score: float = 0.0,
        reply_message_count: int = 0,
        replied: bool = True,
        silence_reason: str = "",
        retry: bool = False,
        fallback: bool = False,
        sticker: bool = False,
        pause_avg: float = 0.0,
    ) -> dict:
        if not self.enabled:
            return {}
        today = datetime.date.today().isoformat()
        with self._lock:
            day = self.data["days"].setdefault(today, _empty_day())
            if replied:
                day["replies"] = int(day.get("replies", 0)) + 1
                day["messages_total"] = int(day.get("messages_total", 0)) + int(reply_message_count)
                _bump(day["by_mode"], str(mode).upper())
                _bump(day["by_length"], str(length).upper())
                _bump(day["by_tone"], str(tone).upper())
                day["score_sum"] = round(float(day.get("score_sum", 0.0)) + float(score), 4)
                if pause_avg > 0:
                    day["pause_sum"] = round(float(day.get("pause_sum", 0.0)) + float(pause_avg), 3)
                    day["pause_samples"] = int(day.get("pause_samples", 0)) + 1
            else:
                day["silences"] = int(day.get("silences", 0)) + 1
                _bump(day["silence_reasons"], silence_reason or "unknown")
            if retry:
                day["retries"] = int(day.get("retries", 0)) + 1
            if fallback:
                day["fallbacks"] = int(day.get("fallbacks", 0)) + 1
            if sticker:
                day["stickers"] = int(day.get("stickers", 0)) + 1
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入回复统计失败：%s", exc)
        return day

    def day(self, when: str | None = None) -> dict:
        key = when or datetime.date.today().isoformat()
        with self._lock:
            return dict(self.data["days"].get(key, _empty_day()))

    def summary(self) -> dict:
        with self._lock:
            days = {key: dict(value) for key, value in self.data.get("days", {}).items()}
        replies = sum(int(day.get("replies", 0)) for day in days.values())
        silences = sum(int(day.get("silences", 0)) for day in days.values())
        messages = sum(int(day.get("messages_total", 0)) for day in days.values())
        samples = sum(int(day.get("pause_samples", 0)) for day in days.values())
        pauses = sum(float(day.get("pause_sum", 0.0)) for day in days.values())
        return {
            "days": len(days),
            "replies": replies,
            "silences": silences,
            "messages_total": messages,
            "avg_messages_per_reply": round(messages / replies, 2) if replies else 0.0,
            "avg_pause": round(pauses / samples, 2) if samples else 0.0,
            "retries": sum(int(day.get("retries", 0)) for day in days.values()),
            "fallbacks": sum(int(day.get("fallbacks", 0)) for day in days.values()),
            "stickers": sum(int(day.get("stickers", 0)) for day in days.values()),
        }
