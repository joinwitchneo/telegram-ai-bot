"""WakeResult 与唤醒历史（Phase 6 §十/§十三）。"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class WakeResult:
    wake_id: str
    reason: str
    wake_score: float = 0.0
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    thought_ids: list = field(default_factory=list)
    interest_ids: list = field(default_factory=list)
    pending_intention_ids: list = field(default_factory=list)
    threshold_reached: bool = False
    cognition_ran: bool = False
    decision: str = ""
    decision_reason: str = ""
    would_action: str = ""
    action_id: str = ""
    action_status: str = ""
    simulated: bool = False
    motivation: float = 0.0
    expected_reward: float = 0.0
    trace_id: str = ""
    cycle_id: str = ""
    mode: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class WakeLog:
    """唤醒历史（jsonl）——`/whyawake` 与 `/wakereasons` 的数据来源。"""

    def __init__(self, path: Path, *, keep: int = 500) -> None:
        self.path = Path(path)
        self.keep = max(50, int(keep))
        self._lock = threading.RLock()
        self._count = self._count_lines()

    def record(self, result: WakeResult) -> dict:
        entry = result.to_dict()
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._count += 1
            except OSError as exc:
                logging.warning("[v3] 写 wake 记录失败：%s", exc)
        return entry

    def recent(self, limit: int = 5) -> list:
        if not self.path.is_file():
            return []
        items = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return items[-max(1, int(limit)):]

    def _count_lines(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def count(self) -> int:
        with self._lock:
            return self._count

    def summary(self, *, limit: int = 50) -> dict:
        """按原因统计（用于 `/wakereasons`）。"""
        rows = self.recent(limit=limit)
        counts: dict = {}
        for item in rows:
            reason = str(item.get("reason", "?"))
            bucket = counts.setdefault(reason, {"wakes": 0, "cognition": 0, "messages": 0})
            bucket["wakes"] += 1
            if item.get("cognition_ran"):
                bucket["cognition"] += 1
            if item.get("action_status") in ("SENT", "SIMULATED"):
                bucket["messages"] += 1
        return {"total": len(rows), "by_reason": counts}


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")
