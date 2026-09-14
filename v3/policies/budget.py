"""ActionBudget（Phase 6 §二十二）。

预算不是为了"像人"，而是为了限制资源消耗、防止行动失控。全部配置化，落盘可查。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.atomic_io import atomic_write_text


def _today() -> str:
    return datetime.date.today().isoformat()


@dataclass
class ActionBudgetState:
    date: str = field(default_factory=_today)
    messages_sent: int = 0
    last_message_at: str = ""
    last_cognitive_at: str = ""
    cognitive_calls: int = 0


class ActionBudget:
    def __init__(
        self,
        path: Path,
        *,
        daily_message_limit: int = 3,
        message_cooldown_minutes: int = 90,
        cognitive_cooldown_minutes: int = 10,
        daily_cognitive_limit: int = 12,
    ) -> None:
        self.path = Path(path)
        self.daily_message_limit = max(0, int(daily_message_limit))
        self.message_cooldown_minutes = max(0, int(message_cooldown_minutes))
        self.cognitive_cooldown_minutes = max(0, int(cognitive_cooldown_minutes))
        self.daily_cognitive_limit = max(0, int(daily_cognitive_limit))
        self._lock = threading.RLock()
        self.data = ActionBudgetState()
        self._load()

    # ── 读写 ────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("date") == _today():
                known = set(ActionBudgetState.__dataclass_fields__)
                self.data = ActionBudgetState(**{k: v for k, v in loaded.items() if k in known})
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] action_budget.json 损坏，从零开始")

    def save(self) -> None:
        with self._lock:
            snapshot = json.dumps(asdict(self.data), ensure_ascii=False, indent=1)
        try:
            atomic_write_text(self.path, snapshot)
        except OSError as exc:
            logging.warning("[v3] 写 action_budget 失败：%s", exc)

    def _roll_day(self) -> None:
        if self.data.date != _today():
            self.data = ActionBudgetState()

    # ── 判定 ────────────────────────────────────────────────────
    def can_message(self, *, now: datetime.datetime | None = None) -> tuple[bool, str]:
        moment = now or datetime.datetime.now()
        with self._lock:
            self._roll_day()
            if self.daily_message_limit <= 0:
                return False, "主动消息未开启（每日上限 0）"
            if self.data.messages_sent >= self.daily_message_limit:
                return False, f"今天主动消息已达上限 {self.daily_message_limit}"
            if self.data.last_message_at and self.message_cooldown_minutes > 0:
                try:
                    last = datetime.datetime.fromisoformat(self.data.last_message_at)
                except ValueError:
                    last = None
                if last is not None:
                    waited = (moment - last).total_seconds() / 60.0
                    if waited < self.message_cooldown_minutes:
                        return False, (f"距离上次主动消息只过了 {int(waited)} 分钟"
                                       f"（冷却 {self.message_cooldown_minutes} 分钟）")
            return True, ""

    def can_cognize(self, *, now: datetime.datetime | None = None) -> tuple[bool, str]:
        moment = now or datetime.datetime.now()
        with self._lock:
            self._roll_day()
            if self.data.cognitive_calls >= self.daily_cognitive_limit:
                return False, f"今日深思次数已达上限 {self.daily_cognitive_limit}"
            if self.data.last_cognitive_at and self.cognitive_cooldown_minutes > 0:
                try:
                    last = datetime.datetime.fromisoformat(self.data.last_cognitive_at)
                except ValueError:
                    last = None
                if last is not None:
                    waited = (moment - last).total_seconds() / 60.0
                    if waited < self.cognitive_cooldown_minutes:
                        return False, f"距离上次深思只过了 {int(waited)} 分钟"
            return True, ""

    # ── 记账 ────────────────────────────────────────────────────
    def note_message(self, *, now: datetime.datetime | None = None) -> None:
        moment = now or datetime.datetime.now()
        with self._lock:
            self._roll_day()
            self.data.messages_sent += 1
            self.data.last_message_at = moment.isoformat(timespec="seconds")
        self.save()

    def note_cognitive(self, *, now: datetime.datetime | None = None) -> None:
        moment = now or datetime.datetime.now()
        with self._lock:
            self._roll_day()
            self.data.cognitive_calls += 1
            self.data.last_cognitive_at = moment.isoformat(timespec="seconds")
        self.save()

    def snapshot(self) -> dict:
        with self._lock:
            self._roll_day()
            return {
                **asdict(self.data),
                "limits": {
                    "daily_message_limit": self.daily_message_limit,
                    "message_cooldown_minutes": self.message_cooldown_minutes,
                    "daily_cognitive_limit": self.daily_cognitive_limit,
                    "cognitive_cooldown_minutes": self.cognitive_cooldown_minutes,
                },
            }
