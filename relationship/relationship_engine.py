"""关系引擎：五维状态，事件驱动 + 时间衰减 + 上下限。

与 Emotion 完全解耦：只订阅 Event Bus 白名单事件，不调用情绪模块。
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import threading
from pathlib import Path

from relationship import relationship_events as events_mod

ALLOWED_SOURCES = ("UserMessageReceived", "UserReturned", "ConversationEnded")


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class RelationshipEngine:
    def __init__(
        self,
        path: Path,
        *,
        bus=None,
        baseline: dict | None = None,
        rates: dict | None = None,
        inertia: float = 0.35,
        event_mapper=None,
    ) -> None:
        self.path = path
        self.bus = bus
        self.baseline = {**events_mod.BASELINE, **(baseline or {})}
        self.rates = {**events_mod.DECAY_RATES, **(rates or {})}
        self.inertia = min(0.95, max(0.0, float(inertia)))
        self.event_mapper = event_mapper or self._default_mapper
        self._lock = threading.RLock()
        self.state = self._load()
        self._subscribe()

    @staticmethod
    def _default_mapper(text: str) -> list[str]:
        from emotion.emotion_events import map_text_to_events

        return map_text_to_events(text)

    # ── 状态 ────────────────────────────────────────────────────────
    def _fresh_state(self) -> dict:
        return {
            "dimensions": dict(self.baseline),
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "turns": 0,
        }

    def _load(self) -> dict:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("dimensions"), dict):
                    merged = dict(self.baseline)
                    for name, value in data["dimensions"].items():
                        if name in self.baseline:
                            merged[name] = _clamp(value)
                    data["dimensions"] = merged
                    data.setdefault("turns", 0)
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return self._fresh_state()

    def dimensions(self) -> dict:
        with self._lock:
            return dict(self.state.get("dimensions", {}))

    def value(self, name: str) -> float:
        return float(self.dimensions().get(name, self.baseline.get(name, 0.0)))

    def reset(self) -> dict:
        with self._lock:
            self.state = self._fresh_state()
        self._save()
        return self.dimensions()

    # ── 事件 ────────────────────────────────────────────────────────
    def apply_event(self, name: str, weight: float = 1.0, *, now: datetime.datetime | None = None) -> dict:
        deltas = events_mod.EVENT_DELTAS.get(name)
        if not deltas:
            return {}
        with self._lock:
            current = self.state.setdefault("dimensions", dict(self.baseline))
            changed: dict[str, float] = {}
            for dimension, delta in deltas.items():
                if dimension not in current:
                    continue
                old = float(current[dimension])
                target = _clamp(old + float(delta) * float(weight))
                new = _clamp(old * self.inertia + target * (1 - self.inertia))
                if abs(new - old) > 1e-9:
                    current[dimension] = round(new, 6)
                    changed[dimension] = round(new - old, 6)
            self.state["turns"] = int(self.state.get("turns", 0)) + 1
            self.state["updated_at"] = (now or datetime.datetime.now()).isoformat(timespec="seconds")
        self._save()
        self._publish_changed(changed)
        return changed

    def apply_events(self, names: list[str], *, now: datetime.datetime | None = None) -> dict:
        merged: dict[str, float] = {}
        for name in names:
            for dimension, delta in self.apply_event(name, now=now).items():
                merged[dimension] = round(merged.get(dimension, 0.0) + delta, 6)
        return merged

    def tick(self, *, now: datetime.datetime | None = None, hours: float | None = None) -> dict:
        moment = now or datetime.datetime.now()
        if hours is None:
            hours = self.hours_since(moment)
        with self._lock:
            current = self.state.setdefault("dimensions", dict(self.baseline))
            moved: dict[str, float] = {}
            for name, value in current.items():
                rate = max(0.0, float(self.rates.get(name, 0.0)))
                if rate <= 0:
                    continue
                base = float(self.baseline.get(name, 0.0))
                factor = math.exp(-rate * min(hours, 24 * 30))
                new = _clamp(base + (float(value) - base) * factor)
                if abs(new - float(value)) > 1e-9:
                    current[name] = round(new, 6)
                    moved[name] = round(new - float(value), 6)
            self.state["updated_at"] = moment.isoformat(timespec="seconds")
        if moved:
            self._save()
        return moved

    def hours_since(self, now: datetime.datetime | None = None) -> float:
        moment = now or datetime.datetime.now()
        try:
            last = datetime.datetime.fromisoformat(str(self.state.get("updated_at")))
        except ValueError:
            return 0.0
        return max(0.0, (moment - last).total_seconds() / 3600)

    # ── 输出给 L2 ───────────────────────────────────────────────────
    def describe(self) -> str:
        return events_mod.describe(self.dimensions())

    def behavior_hints(self) -> list[str]:
        return events_mod.behavior_hints(self.dimensions())

    def is_close(self, threshold: float = 0.5) -> bool:
        return self.value("intimacy") >= threshold and self.value("familiarity") >= threshold

    # ── Event Bus ───────────────────────────────────────────────────
    def _subscribe(self) -> None:
        if self.bus is None:
            return
        for name in ALLOWED_SOURCES:
            self.bus.subscribe(name, self._on_event)

    def _on_event(self, event) -> None:
        if event.name == "UserMessageReceived":
            text = str(event.payload.get("text", ""))
            self.tick()
            self.apply_events(self.event_mapper(text))
        elif event.name == "UserReturned":
            self.tick()
            self.apply_event("USER_RETURNS")
        elif event.name == "ConversationEnded":
            self.tick()

    def _publish_changed(self, changed: dict) -> None:
        if self.bus is not None and changed:
            self.bus.publish(
                "RelationshipChanged",
                dimensions=sorted(changed.keys()),
                deltas={key: value for key, value in changed.items()},
            )

    def _save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.state, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("保存关系状态失败：%s", exc)
