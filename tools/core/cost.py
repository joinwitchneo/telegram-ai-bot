"""Tool 成本台账：把"这次花了什么"记清楚，避免出现隐藏成本。

分三类：
    llm_tokens   主/本地模型 token
    http_requests 免费网络请求次数
    paid_calls   付费 AI API 调用次数（应该极少）
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path


def _empty_day() -> dict:
    return {
        "calls": 0,
        "by_tool": {},
        "llm_tokens": 0,
        "http_requests": 0,
        "paid_calls": 0,
        "cache_hits": 0,
        "errors": 0,
    }


class CostTracker:
    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self._lock = threading.RLock()
        self.days: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("days"), dict):
                self.days = loaded["days"]
        except (OSError, json.JSONDecodeError):
            self.days = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            with self._lock:
                payload = json.dumps({"days": self.days}, ensure_ascii=False, indent=1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(payload, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入工具成本失败：%s", exc)

    def record(
        self,
        tool: str,
        *,
        mode: str = "LOCAL",
        llm_tokens: int = 0,
        http_requests: int = 0,
        cache_hit: bool = False,
        error: bool = False,
    ) -> dict:
        if not self.enabled:
            return {}
        today = datetime.date.today().isoformat()
        with self._lock:
            day = self.days.setdefault(today, _empty_day())
            day["calls"] = int(day.get("calls", 0)) + 1
            day["by_tool"][tool] = int(day["by_tool"].get(tool, 0)) + 1
            day["llm_tokens"] = int(day.get("llm_tokens", 0)) + int(llm_tokens)
            day["http_requests"] = int(day.get("http_requests", 0)) + int(http_requests)
            if mode == "PAID_API":
                day["paid_calls"] = int(day.get("paid_calls", 0)) + 1
            if cache_hit:
                day["cache_hits"] = int(day.get("cache_hits", 0)) + 1
            if error:
                day["errors"] = int(day.get("errors", 0)) + 1
            snapshot = dict(day)
        self._save()
        return snapshot

    def day(self, when: str | None = None) -> dict:
        key = when or datetime.date.today().isoformat()
        with self._lock:
            return dict(self.days.get(key, _empty_day()))

    def summary(self) -> dict:
        with self._lock:
            days = {key: dict(value) for key, value in self.days.items()}
        return {
            "days": len(days),
            "calls": sum(int(day.get("calls", 0)) for day in days.values()),
            "llm_tokens": sum(int(day.get("llm_tokens", 0)) for day in days.values()),
            "http_requests": sum(int(day.get("http_requests", 0)) for day in days.values()),
            "paid_calls": sum(int(day.get("paid_calls", 0)) for day in days.values()),
            "cache_hits": sum(int(day.get("cache_hits", 0)) for day in days.values()),
            "errors": sum(int(day.get("errors", 0)) for day in days.values()),
        }
