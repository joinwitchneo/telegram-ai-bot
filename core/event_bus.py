"""事件总线：只负责"把事件告诉订阅者"，不含任何业务逻辑。

设计约束（来自 V2 方案）：
- 模块之间不直接互相调用，状态传播统一走这里；
- EventBus 不判断情绪、不改记忆、不决定是否主动聊天——那是各模块自己的事；
- 订阅者出错不影响其他订阅者，也不会打断发布者。
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

EVENT_NAMES = (
    "UserMessageReceived",
    "TopicUpdated",
    "MemoryCreated",
    "MemoryRecalled",
    "EmotionChanged",
    "RelationshipChanged",
    "ConversationEnded",
    "ProactiveTriggered",
    "UserReturned",
    # V3：只读通知（发布方在 V3 内；V2 侧只多发布一个 BotResponseSent）
    "BotResponseSent",
    "v3:thought_created",
    "v3:interest_updated",
    "v3:experience_committed",
    "v3:cycle_completed",
)

_counter = itertools.count(1)


@dataclass
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: f"evt_{next(_counter):06d}")


class EventBus:
    """极简发布/订阅实现。线程安全，订阅者异常只记日志。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: dict[str, list[Callable[[Event], None]]] = {}
        self._published = 0
        self._delivered = 0
        self._failed = 0

    def subscribe(self, event_name: str, handler: Callable[[Event], None]) -> Callable[[], bool]:
        """订阅事件，返回一个"取消订阅"的函数。"""
        self._validate(event_name)
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        with self._lock:
            self._subscribers.setdefault(event_name, []).append(handler)
        return lambda: self.unsubscribe(event_name, handler)

    def unsubscribe(self, event_name: str, handler: Callable[[Event], None]) -> bool:
        self._validate(event_name)
        with self._lock:
            handlers = self._subscribers.get(event_name, [])
            if handler in handlers:
                handlers.remove(handler)
                return True
        return False

    def subscriber_count(self, event_name: str) -> int:
        self._validate(event_name)
        with self._lock:
            return len(self._subscribers.get(event_name, []))

    def clear(self, event_name: str | None = None) -> None:
        with self._lock:
            if event_name is None:
                self._subscribers.clear()
            else:
                self._subscribers.pop(event_name, None)

    def publish(self, event: Event | str, **payload: Any) -> int:
        """发布事件，返回成功送达的订阅者数量。"""
        if isinstance(event, str):
            event = Event(name=event, payload=dict(payload))
        elif payload:
            event.payload = {**event.payload, **payload}
        self._validate(event.name)
        with self._lock:
            handlers = list(self._subscribers.get(event.name, []))
            self._published += 1
        delivered = 0
        for handler in handlers:
            try:
                handler(event)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - 订阅者出错不能影响别人
                self._failed += 1
                logging.warning("event handler failed (%s): %s", event.name, exc)
        with self._lock:
            self._delivered += delivered
        return delivered

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "published": self._published,
                "delivered": self._delivered,
                "failed": self._failed,
                "topics": len(self._subscribers),
            }

    @staticmethod
    def _validate(event_name: str) -> None:
        if event_name not in EVENT_NAMES:
            raise ValueError(f"未定义的事件名：{event_name}")
