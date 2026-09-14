"""结构化事件日志（Phase 6 §二十四）。

目的：以后必须能回答"夕颜刚才为什么主动找我"。
每条事件都带 timestamp / trace_id / cycle_id / thought_id，落盘成 jsonl，只追加。
这是旁路观察，**绝不参与决策**（决策只看 Core 的状态与配置）。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from pathlib import Path

# 与设计文档保持一致的事件名
EVENT_KINDS = (
    "checkpoint_started",
    "thought_updated",
    "thought_created",
    "thought_decayed",
    "thought_merged",
    "thought_activated",
    "threshold_evaluated",
    "cognitive_triggered",
    "cognitive_skipped",
    "cognitive_result",
    "decision_made",
    "action_requested",
    "action_validated",
    "action_rejected",
    "action_executed",
    "outcome_received",
    "state_changed",
)

MAX_FIELD_CHARS = 400


class TraceLog:
    def __init__(self, path: Path, *, keep: int = 4000) -> None:
        self.path = Path(path)
        self.keep = max(200, int(keep))
        self._lock = threading.RLock()
        self._count = self._count_lines()

    # ── 写入 ────────────────────────────────────────────────────
    def record(self, kind: str, *, trace_id: str = "", cycle_id: str = "",
               thought_id: str = "", action_id: str = "", **fields) -> dict:
        entry = {
            "kind": str(kind),
            "trace_id": str(trace_id),
            "cycle_id": str(cycle_id),
            "thought_id": str(thought_id),
            "action_id": str(action_id),
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "fields": {k: _trim(v) for k, v in fields.items()},
        }
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._count += 1
            except OSError as exc:
                logging.warning("[v3] 写 trace 失败：%s", exc)
        return entry

    # ── 读取 ────────────────────────────────────────────────────
    def _count_lines(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def recent(self, limit: int = 20, *, kind: str = "") -> list:
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
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if kind and item.get("kind") != kind:
                        continue
                    items.append(item)
        except OSError:
            return []
        return items[-max(1, int(limit)):]

    def by_trace(self, trace_id: str, *, limit: int = 60) -> list:
        if not trace_id:
            return []
        return [item for item in self.recent(limit=max(1, int(limit)) * 4)
                if item.get("trace_id") == trace_id][-max(1, int(limit)):]

    def count(self) -> int:
        with self._lock:
            return self._count


def _trim(value):
    if isinstance(value, str):
        return value[:MAX_FIELD_CHARS]
    if isinstance(value, dict):
        return {str(k): _trim(v) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_trim(v) for v in list(value)[:20]]
    return value
