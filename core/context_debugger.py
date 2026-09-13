"""Context Debugger：旁路观察每轮上下文（Phase 2）。

两条硬规则：
1. Debug 数据**只读不写回**——它绝不参与下一轮 Context 组装；
2. Snapshot 默认关闭，避免长期运行堆满磁盘。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path


class ContextDebugger:
    def __init__(
        self,
        *,
        debug_dir: Path,
        enabled: bool = True,
        snapshot_enabled: bool = False,
        keep_recent: int = 50,
    ) -> None:
        self.debug_dir = debug_dir
        self.enabled = enabled
        self.snapshot_enabled = snapshot_enabled
        self.keep_recent = max(5, int(keep_recent))
        self._lock = threading.Lock()
        self._recent: list[dict] = []

    # ── 记录 ────────────────────────────────────────────────────────
    def log(self, record: dict) -> dict:
        """记录一轮 Context 的关键指标（写日志 + 内存环形缓冲）。"""
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            **record,
        }
        with self._lock:
            self._recent.append(entry)
            del self._recent[: -self.keep_recent]
        if self.enabled:
            tokens = entry.get("tokens", {}) or {}
            logging.info(
                "[context] %s mode=%s model=%s L0=%s L1=%s L2=%s L3=%s L4=%s total=%s history=%s policy=%s",
                entry.get("request_id", "-"),
                entry.get("mode", "-"),
                entry.get("model", "-"),
                tokens.get("L0", 0),
                tokens.get("L1", 0),
                tokens.get("L2", 0),
                tokens.get("L3", 0),
                tokens.get("L4", 0),
                entry.get("total_context_tokens", 0),
                entry.get("history_count", 0),
                entry.get("policy_reason", ""),
            )
        return entry

    # ── 快照 ────────────────────────────────────────────────────────
    def snapshot(self, request_id: str, payload: dict) -> Path | None:
        """调试模式开启时，把完整上下文落盘，便于离线回放对比。"""
        if not self.snapshot_enabled:
            return None
        day = datetime.date.today().isoformat()
        target_dir = self.debug_dir / day
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{request_id}.json"
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            logging.info("[context] snapshot 已保存：%s", path.name)
            return path
        except OSError as exc:
            logging.warning("snapshot 写入失败：%s", exc)
            return None

    # ── 读取（只用于排查，不参与组装）──────────────────────────────
    def recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return json.loads(json.dumps(self._recent[-limit:]))

    def last(self) -> dict | None:
        with self._lock:
            return json.loads(json.dumps(self._recent[-1])) if self._recent else None

    def stats(self) -> dict:
        with self._lock:
            return {
                "records": len(self._recent),
                "enabled": self.enabled,
                "snapshot_enabled": self.snapshot_enabled,
            }
