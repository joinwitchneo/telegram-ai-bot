"""Token 用量记录：按类别落盘 + 按日聚合。

只写日志与 data/usage.json，不向 Telegram 暴露（V2 方案明确要求）。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

CATEGORIES = ("chat", "summary", "extraction", "proactive", "tool", "retry", "rule")


def _empty_day() -> dict:
    return {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "cache_hit": 0,
        "cache_miss": 0,
        "retries": 0,
        "fallbacks": 0,
        "by_category": {},
    }


class UsageLogger:
    def __init__(self, path: Path, enabled: bool = True, keep_recent: int = 200) -> None:
        self.path = path
        self.enabled = enabled
        self.keep_recent = max(10, keep_recent)
        self._lock = threading.Lock()
        self.data: dict = {"days": {}, "recent": []}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data = {"days": loaded.get("days", {}), "recent": loaded.get("recent", [])}
            except (OSError, json.JSONDecodeError):
                pass

    # ── 写入 ────────────────────────────────────────────────────────
    def record(
        self,
        *,
        category: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        latency_ms: int = 0,
        fallback_from: str = "",
        retry: bool = False,
        tier: str = "",
        request_id: str = "",
        note: str = "",
    ) -> dict:
        if not self.enabled:
            return {}
        category = category if category in CATEGORIES else "chat"
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "model": model,
            "tier": tier,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cached_tokens": int(cached_tokens),
            "latency_ms": int(latency_ms),
            "fallback_from": fallback_from,
            "retry": bool(retry),
            "request_id": request_id,
            "note": note,
        }
        day = entry["ts"][:10]
        with self._lock:
            bucket = self.data["days"].setdefault(day, _empty_day())
            bucket["requests"] += 1
            bucket["input_tokens"] += entry["input_tokens"]
            bucket["output_tokens"] += entry["output_tokens"]
            bucket["cached_tokens"] += entry["cached_tokens"]
            if entry["cached_tokens"] > 0:
                bucket["cache_hit"] += 1
            else:
                bucket["cache_miss"] += 1
            if entry["retry"]:
                bucket["retries"] += 1
            if entry["fallback_from"]:
                bucket["fallbacks"] += 1
            cat = bucket["by_category"].setdefault(category, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})
            cat["requests"] += 1
            cat["input_tokens"] += entry["input_tokens"]
            cat["output_tokens"] += entry["output_tokens"]
            cat["cached_tokens"] += entry["cached_tokens"]
            self.data["recent"].append(entry)
            del self.data["recent"][: -self.keep_recent]
        self.save()
        logging.info(
            "[usage] %s %s in=%d out=%d cached=%d %sms%s",
            category,
            entry["model"] or "-",
            entry["input_tokens"],
            entry["output_tokens"],
            entry["cached_tokens"],
            entry["latency_ms"],
            f" fallback_from={fallback_from}" if fallback_from else "",
        )
        return entry

    # ── 读取 ────────────────────────────────────────────────────────
    def day(self, day: str | None = None) -> dict:
        day = day or datetime.date.today().isoformat()
        with self._lock:
            bucket = self.data["days"].get(day)
            return json.loads(json.dumps(bucket)) if bucket else _empty_day()

    def summary(self, days: int = 7) -> dict:
        """近 N 天汇总，含 cache 命中率与平均用量。"""
        today = datetime.date.today()
        window = [(today - datetime.timedelta(days=i)).isoformat() for i in range(days)]
        total = _empty_day()
        with self._lock:
            for day in window:
                bucket = self.data["days"].get(day)
                if not bucket:
                    continue
                for key in ("requests", "input_tokens", "output_tokens", "cached_tokens", "cache_hit", "cache_miss", "retries", "fallbacks"):
                    total[key] += int(bucket.get(key, 0))
                for cat, values in bucket.get("by_category", {}).items():
                    target = total["by_category"].setdefault(cat, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})
                    for key in ("requests", "input_tokens", "output_tokens", "cached_tokens"):
                        target[key] += int(values.get(key, 0))
        requests = max(1, total["requests"])
        total["avg_input_tokens"] = round(total["input_tokens"] / requests)
        total["avg_output_tokens"] = round(total["output_tokens"] / requests)
        total["avg_cached_tokens"] = round(total["cached_tokens"] / requests)
        total["cache_hit_rate"] = round(total["cache_hit"] / requests, 3)
        total["days"] = days
        return total

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return json.loads(json.dumps(self.data["recent"][-limit:]))

    def record_local(self, category: str, tokens: int, note: str = "") -> None:
        """记录"没有调用模型"的本地处理（用于对比省下多少）。"""
        if not self.enabled:
            return
        with self._lock:
            day = datetime.date.today().isoformat()
            bucket = self.data["days"].setdefault(day, _empty_day())
            cat = bucket["by_category"].setdefault(category, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})
            cat["requests"] += 1
            cat["cached_tokens"] += 0
        self.save()
        logging.info("[usage] %s 本地处理，未调用模型（省下约 %d tokens）%s", category, tokens, note)

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("could not save usage log: %s", exc)
