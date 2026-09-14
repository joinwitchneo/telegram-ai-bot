"""ContinuityStore：seed / cycle 记录 / 最近一次认知 的持久化。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_text
from v3.continuity.models import ContinuitySeed, CycleRecord

MAX_CYCLES = 300


class ContinuityStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.seed_path = self.dir / "seed.json"
        self.cycles_path = self.dir / "cycles.json"
        self._lock = threading.RLock()
        self.seed: dict = {}
        self.cycles: list = []
        self._load()

    def _load(self) -> None:
        try:
            if self.seed_path.is_file():
                self.seed = json.loads(self.seed_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] seed.json 损坏，从空开始")
        try:
            if self.cycles_path.is_file():
                self.cycles = json.loads(self.cycles_path.read_text(encoding="utf-8")) or []
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] cycles.json 损坏，从空开始")

    # ── seed ────────────────────────────────────────────────────────
    def save_seed(self, seed: ContinuitySeed) -> None:
        with self._lock:
            self.seed = seed.to_dict()
        try:
            atomic_write_text(self.seed_path, json.dumps(self.seed, ensure_ascii=False, indent=1))
        except OSError as exc:
            logging.warning("[v3] 写 seed 失败：%s", exc)

    def load_seed(self) -> ContinuitySeed | None:
        with self._lock:
            return ContinuitySeed.from_dict(self.seed) if self.seed else None

    # ── cycle 记录 ──────────────────────────────────────────────────
    def append_cycle(self, record: CycleRecord) -> None:
        with self._lock:
            self.cycles.append(record.to_dict())
            self.cycles = self.cycles[-MAX_CYCLES:]
            snapshot = json.dumps(self.cycles, ensure_ascii=False, indent=1)
        try:
            atomic_write_text(self.cycles_path, snapshot)
        except OSError as exc:
            logging.warning("[v3] 写 cycles 失败：%s", exc)

    def last_cycle(self) -> dict:
        with self._lock:
            return dict(self.cycles[-1]) if self.cycles else {}

    def recent_cycles(self, limit: int = 5) -> list:
        with self._lock:
            return [dict(item) for item in self.cycles[-max(1, int(limit)) :]]

    def stats(self) -> dict:
        with self._lock:
            return {"cycles": len(self.cycles), "has_seed": bool(self.seed)}
