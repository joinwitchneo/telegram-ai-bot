"""ObservationRecorder：把聊天写成 jsonl 观测。

铁律：**只存已经存在的数据**（用户原话、她实际发出的文本摘录、已有 MessagePlan 元数据、
情绪/关系模块已经算出的 delta）。这里绝不调用 LLM，也不做"摘要"。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from pathlib import Path

MAX_FIELD_CHARS = 240


class ObservationRecorder:
    def __init__(self, path: Path, *, keep: int = 500) -> None:
        self.path = Path(path)
        self.keep = max(50, int(keep))
        self.archive_path = self.path.with_name("observations_archive.jsonl")
        self._lock = threading.RLock()
        self._count = self._count_lines()

    # ── 写入 ────────────────────────────────────────────────────────
    def append(
        self,
        *,
        user_message: str = "",
        bot_response_excerpt: str = "",
        response_mode: str = "",
        message_count: int = 0,
        emotion_delta: dict | None = None,
        relationship_delta: dict | None = None,
        chat_id: int | str = "",
        cycle_id: str = "",
        now: datetime.datetime | None = None,
    ) -> dict:
        entry = {
            "user_message": str(user_message or "")[:MAX_FIELD_CHARS],
            "bot_response_excerpt": str(bot_response_excerpt or "")[:MAX_FIELD_CHARS],
            "response_mode": str(response_mode or ""),
            "message_count": int(message_count or 0),
            "emotion_delta": dict(emotion_delta or {}),
            "relationship_delta": dict(relationship_delta or {}),
            "chat_id": str(chat_id),
            "cycle_id": str(cycle_id),
            "consumed": False,
            "timestamp": (now or datetime.datetime.now()).isoformat(timespec="seconds"),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._count += 1
            if self._count > self.keep:
                self._rotate()
        return entry

    def _rotate(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            half = len(lines) // 2
            with open(self.archive_path, "a", encoding="utf-8") as handle:
                for line in lines[:half]:
                    handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            from core.atomic_io import atomic_write_text

            atomic_write_text(self.path, "\n".join(lines[half:]) + ("\n" if lines[half:] else ""))
            self._count = len(lines) - half
            logging.info("[v3] observations 轮转：归档 %d 条", half)
        except OSError as exc:
            logging.warning("[v3] observations 轮转失败：%s", exc)

    # ── 读取 ────────────────────────────────────────────────────────
    def _count_lines(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def recent(self, limit: int = 8, *, unconsumed_only: bool = False) -> list:
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
                    if unconsumed_only and item.get("consumed"):
                        continue
                    items.append(item)
        except OSError:
            return []
        return items[-max(1, int(limit)) :]

    def mark_consumed(self, cycle_id: str, *, limit: int = 0) -> int:
        """把本轮读过的观测标记为已消费（避免同一素材被反复"发现"）。

        limit>0 时只标记最后 limit 条未消费的记录（配合"每轮只处理最近几条"）。
        """
        if not self.path.is_file():
            return 0
        from core.atomic_io import atomic_write_text

        marked = 0
        with self._lock:
            lines = []
            parsed = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            pending = [index for index, item in enumerate(parsed) if not item.get("consumed")]
            targets = set(pending[-int(limit):]) if limit and int(limit) > 0 else set(pending)
            for index, item in enumerate(parsed):
                if index in targets:
                    item["consumed"] = True
                    item["consumed_by"] = cycle_id
                    marked += 1
                lines.append(json.dumps(item, ensure_ascii=False))
            try:
                atomic_write_text(self.path, "\n".join(lines) + ("\n" if lines else ""))
            except OSError:
                return 0
        return marked

    def count(self) -> int:
        with self._lock:
            return self._count
