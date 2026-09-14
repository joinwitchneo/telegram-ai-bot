"""V3 硬预算：日计数在 Python 层，LLM 无法修改。

每次调用前先扣减后执行；日切自动归零；同时受全局 DAILY_TOKEN_BUDGET 约束。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

from core.atomic_io import atomic_write_text


def _today() -> str:
    return datetime.date.today().isoformat()


def _empty() -> dict:
    return {"date": _today(), "llm_calls": 0, "cycles": 0, "actions": 0}


class V3Budget:
    def __init__(
        self,
        path: Path,
        *,
        llm_per_day: int = 6,
        cycles_per_day: int = 6,
        actions_per_day: int = 8,
        max_chain: int = 3,
        global_budget=None,
    ) -> None:
        self.path = Path(path)
        self.llm_per_day = max(0, int(llm_per_day))
        self.cycles_per_day = max(0, int(cycles_per_day))
        self.actions_per_day = max(0, int(actions_per_day))
        self.max_chain = max(1, int(max_chain))
        self.global_budget = global_budget
        self._lock = threading.RLock()
        self.chain_calls = 0          # 本次认知周期内的 LLM 调用数（进程内，不持久化）
        self.data = _empty()
        self._load()

    # ── 读写 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("date") == _today():
                self.data = {**_empty(), **loaded}
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] budget.json 损坏，从零开始")

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            atomic_write_text(self.path, snapshot)
        except OSError as exc:
            logging.warning("[v3] 写 budget.json 失败：%s", exc)

    def _roll_day(self) -> None:
        if self.data.get("date") != _today():
            self.data = _empty()

    # ── 判定 ────────────────────────────────────────────────────────
    def begin_chain(self) -> None:
        """一轮认知开始时清零链内计数：链长是"单轮"限制，不是日限制。"""
        with self._lock:
            self.chain_calls = 0

    def global_level(self) -> str:
        if self.global_budget is None:
            return "ok"
        try:
            return str(self.global_budget.level())
        except Exception:  # noqa: BLE001
            return "ok"

    def can_cycle(self) -> tuple[bool, str]:
        with self._lock:
            self._roll_day()
            if self.global_level() != "ok":
                return False, f"全局预算处于 {self.global_level()}，V3 拒绝运行"
            if int(self.data.get("cycles", 0)) >= self.cycles_per_day:
                return False, f"今日 cycle 已达上限 {self.cycles_per_day}"
            return True, ""

    def can_llm(self) -> tuple[bool, str]:
        with self._lock:
            self._roll_day()
            if self.global_level() != "ok":
                return False, f"全局预算处于 {self.global_level()}，V3 拒绝调用模型"
            if int(self.data.get("llm_calls", 0)) >= self.llm_per_day:
                return False, f"今日自主 LLM 调用已达上限 {self.llm_per_day}"
            if self.chain_calls >= self.max_chain:
                return False, f"本轮认知链长已达上限 {self.max_chain}"
            return True, ""

    def can_action(self) -> tuple[bool, str]:
        with self._lock:
            self._roll_day()
            if int(self.data.get("actions", 0)) >= self.actions_per_day:
                return False, f"今日 Action 已达上限 {self.actions_per_day}"
            return True, ""

    # ── 扣减 ────────────────────────────────────────────────────────
    def spend_cycle(self) -> None:
        with self._lock:
            self._roll_day()
            self.data["cycles"] = int(self.data.get("cycles", 0)) + 1
        self.save()

    def spend_llm(self, n: int = 1) -> None:
        with self._lock:
            self._roll_day()
            step = max(0, int(n))
            self.data["llm_calls"] = int(self.data.get("llm_calls", 0)) + step
            self.chain_calls += step
        self.save()

    def spend_action(self) -> None:
        with self._lock:
            self._roll_day()
            self.data["actions"] = int(self.data.get("actions", 0)) + 1
        self.save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day()
            return {
                **dict(self.data),
                "chain_calls": self.chain_calls,
                "limits": {
                    "llm_calls": self.llm_per_day,
                    "cycles": self.cycles_per_day,
                    "actions": self.actions_per_day,
                    "max_chain": self.max_chain,
                },
                "global_level": self.global_level(),
            }
