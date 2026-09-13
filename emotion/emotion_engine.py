"""情绪引擎：状态保存、事件更新、惯性平滑、时间衰减。

严格约束（Phase 4）：
- 不随机：同样的状态 + 同样的事件 = 同样的结果；
- 有惯性：单条消息不会让情绪跳变；
- 有边界：永远夹在 0.0 ~ 1.0；
- 不直接调用 Relationship —— 只能通过 Event Bus 发布 EmotionChanged。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

from emotion import emotion_events as events_mod
from emotion.emotion_decay import EmotionDecay

# 允许订阅的事件白名单（不订阅 EmotionChanged / RelationshipChanged，避免反馈循环）
ALLOWED_SOURCES = ("UserMessageReceived", "UserReturned", "ConversationEnded")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


class EmotionEngine:
    def __init__(
        self,
        path: Path,
        *,
        bus=None,
        baseline: dict | None = None,
        rates: dict | None = None,
        inertia: float = 0.6,
        event_mapper=None,
    ) -> None:
        self.path = path
        self.bus = bus
        self.baseline = {**events_mod.BASELINE, **(baseline or {})}
        self.decay = EmotionDecay(self.baseline, rates)
        self.inertia = min(0.95, max(0.0, float(inertia)))
        self.event_mapper = event_mapper or events_mod.map_text_to_events
        self._lock = threading.RLock()
        self._last_changed: list[dict] = []
        self.state = self._load()
        self._subscribe()

    # ── 状态读写 ────────────────────────────────────────────────────
    def _fresh_state(self) -> dict:
        return {
            "emotions": dict(self.baseline),
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    def _load(self) -> dict:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("emotions"), dict):
                    merged = dict(self.baseline)
                    for name, value in data["emotions"].items():
                        if name in self.baseline:
                            merged[name] = _clamp(value)
                    data["emotions"] = merged
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return self._fresh_state()

    def emotions(self) -> dict:
        with self._lock:
            return dict(self.state.get("emotions", {}))

    def value(self, name: str) -> float:
        return float(self.emotions().get(name, self.baseline.get(name, 0.5)))

    def reset(self) -> dict:
        with self._lock:
            self.state = self._fresh_state()
        self._save()
        return self.emotions()

    # ── 事件驱动 ────────────────────────────────────────────────────
    def apply_event(self, name: str, weight: float = 1.0, *, now: datetime.datetime | None = None) -> dict:
        """按事件更新情绪（带惯性平滑）。返回本次实际变化量。"""
        deltas = events_mod.EVENT_DELTAS.get(name)
        if not deltas:
            return {}
        with self._lock:
            current = self.state.setdefault("emotions", dict(self.baseline))
            changed: dict[str, float] = {}
            for dimension, delta in deltas.items():
                if dimension not in current:
                    continue
                old = float(current[dimension])
                target = _clamp(old + float(delta) * float(weight))
                # 惯性：新值 = 旧值 × 惯性 + 目标值 × (1 - 惯性)
                new = _clamp(old * self.inertia + target * (1 - self.inertia))
                if abs(new - old) > 1e-9:
                    current[dimension] = round(new, 6)
                    changed[dimension] = round(new - old, 6)
            self.state["updated_at"] = (now or datetime.datetime.now()).isoformat(timespec="seconds")
            self._last_changed = [
                {"dimension": key, "delta": value} for key, value in changed.items()
            ]
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
        """按时间推进衰减；不传 hours 就按 updated_at 计算。"""
        moment = now or datetime.datetime.now()
        if hours is None:
            hours = self.hours_since(moment)
        with self._lock:
            current = self.state.setdefault("emotions", dict(self.baseline))
            decayed = self.decay.decay(current, hours)
            moved = {
                name: round(decayed[name] - float(current[name]), 6)
                for name in current
                if abs(decayed[name] - float(current[name])) > 1e-9
            }
            self.state["emotions"] = {name: round(_clamp(value), 6) for name, value in decayed.items()}
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

    # ── 给 L2 用的自然语言 ─────────────────────────────────────────
    def describe(self) -> str:
        return events_mod.summarise_emotion(self.emotions())

    def behavior_hints(self) -> list[str]:
        state = self.emotions()
        hints: list[str] = []
        if state.get("joy", 0.5) >= 0.65:
            hints.append("可以更轻松、多开点玩笑")
        if state.get("fatigue", 0.3) >= 0.6:
            hints.append("回复短一些、少展开")
        if state.get("anger", 0.1) >= 0.4:
            hints.append("语气可以冲一点、话少")
        if state.get("sadness", 0.1) >= 0.4:
            hints.append("语气淡一点，不要强行热络")
        if state.get("interest", 0.5) >= 0.65:
            hints.append("可以顺着话题多聊两句")
        if state.get("loneliness", 0.2) >= 0.5:
            hints.append("可以表现得想让他多陪一会儿（傲娇式）")
        if state.get("embarrassment", 0.1) >= 0.4:
            hints.append("可以有点嘴硬、转移话题")
        return hints[:3]          # 只保留最相关的几条，省 token

    def dominant(self) -> tuple[str, float]:
        state = self.emotions()
        best = ("calm", 0.0)
        for name, value in state.items():
            deviation = abs(float(value) - float(self.baseline.get(name, 0.5)))
            if deviation > best[1]:
                best = (name, round(deviation, 4))
        return best

    # ── Event Bus ───────────────────────────────────────────────────
    def _subscribe(self) -> None:
        if self.bus is None:
            return
        for name in ALLOWED_SOURCES:
            self.bus.subscribe(name, self._on_event)

    def _on_event(self, event) -> None:
        """只处理白名单事件；EmotionChanged/RelationshipChanged 不会被订阅。"""
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
                "EmotionChanged",
                dimensions=sorted(changed.keys()),
                deltas={key: value for key, value in changed.items()},
                dominant=self.dominant()[0],
            )

    def _save(self) -> None:
        with self._lock:
            snapshot = json.dumps(self.state, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("保存情绪状态失败：%s", exc)
