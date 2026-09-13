"""Self Model 与 Memory / Emotion / Relationship 的事件接口（Phase 8）。

只做一件事：真实发生的事 -> 可能让夕颜对自己的理解发生一点变化。
不编造经历、不为了"深刻"强行思考、不产生没来源的情绪。
"""

from __future__ import annotations

import datetime
import logging

from self_model.store import THOUGHT_TRIGGERS

# 哪些事件值得让她"想一想"（全部来自真实发生的事）
RELEVANT_EVENTS = ("UserMessageReceived", "MemoryCreated", "RelationshipChanged")


class SelfModelBridge:
    def __init__(self, store, *, bus=None, enabled: bool = True, cooldown_minutes: float = 30.0) -> None:
        self.store = store
        self.bus = bus
        self.enabled = bool(enabled)
        self.cooldown = max(0.0, float(cooldown_minutes)) * 60
        self._last_at: datetime.datetime | None = None
        if bus is not None:
            for name in RELEVANT_EVENTS:
                bus.subscribe(name, self._on_event)

    # ── 事件 ────────────────────────────────────────────────────────
    def _on_event(self, event) -> None:
        if not self.enabled:
            return
        try:
            if event.name == "UserMessageReceived":
                self.observe_text(str(event.payload.get("text", "")), origin="用户说到")
            elif event.name == "MemoryCreated":
                kind = str(event.payload.get("type", ""))
                if kind in ("relationship_event", "shared_event", "emotion_event"):
                    self.store.record_growth(
                        f"发生了一件被记下来的事（{kind}），我对自己的理解可能会因此有点变化",
                        origin="MemoryCreated",
                    )
        except Exception as exc:  # noqa: BLE001 - 自我模型出问题不能影响聊天
            logging.warning("Self Model 事件处理失败：%s", exc)

    # ── 从真实话语里找"值得想一想"的点 ─────────────────────────────
    def observe_text(self, text: str, *, origin: str = "", now: datetime.datetime | None = None) -> list[str]:
        raw = (text or "").strip()
        if not self.enabled or not raw:
            return []
        moment = now or datetime.datetime.now()
        if self._last_at is not None and (moment - self._last_at).total_seconds() < self.cooldown:
            return []
        lowered = raw.lower()
        hits: list[str] = []
        for keywords, question in THOUGHT_TRIGGERS.items():
            if any(word in lowered for word in keywords):
                self.store.add_thought(question, origin=origin or raw[:40], now=moment)
                hits.append(question)
        if hits:
            self._last_at = moment
        return hits
