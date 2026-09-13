"""消息渲染：把一段文本变成"像真人一样发出去的多条消息"。

这里承担 V2 明确的一条硬约束：**MessagePlan 由 Python 最终裁决**。
模型给多少条、多长停顿都不算数，一律经 clamp_plan() 夹进允许范围。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from style.message_splitter import render_messages


@dataclass
class RenderLimits:
    max_messages: int = 4
    min_pause: float = 0.5
    max_pause: float = 8.0
    max_chars: int = 800
    max_sticker_probability: float = 0.35
    cps_min: float = 2.5
    cps_max: float = 4.5
    # Phase 5：按回复长度档限制单条消息长度
    max_chars_by_length: dict = field(
        default_factory=lambda: {"ULTRA_SHORT": 30, "SHORT": 60, "NORMAL": 200, "LONG": 800}
    )

    def cap_for(self, length: str | None) -> int:
        key = str(length or "NORMAL").upper()
        value = self.max_chars_by_length.get(key, self.max_chars)
        return max(10, min(int(value), int(self.max_chars)))


def split_messages(text: str, max_messages: int = 4, max_chars: int = 800) -> list[str]:
    """兼容入口：真正的拆分规则在 style/message_splitter.render_messages。"""
    raw = (text or "").strip()
    if not raw:
        return []
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    if len(lines) > 1:
        messages = lines
    else:
        messages = render_messages(raw, target_count=max_messages, max_messages=max_messages)
    messages = messages or [raw]
    result: list[str] = []
    for line in messages:
        while len(line) > max_chars:
            result.append(line[:max_chars])
            line = line[max_chars:]
        if line:
            result.append(line)
    if len(result) > max_messages:
        head = result[: max_messages - 1]
        tail = "".join(result[max_messages - 1 :])
        result = head + [tail[:max_chars]]
    return result


def typing_seconds(text: str, limits: RenderLimits) -> float:
    """按字数估算"打完这条要多久"。"""
    chars = len(re.sub(r"\s", "", text or "")) or 1
    cps = random.uniform(max(0.5, limits.cps_min), max(limits.cps_min, limits.cps_max))
    return max(0.8, min(12.0, chars / cps))


def clamp_plan(plan: dict, limits: RenderLimits) -> dict:
    """把模型给的 MessagePlan 夹进允许范围（不可信模型输出）。"""
    plan = dict(plan or {})
    count = int(plan.get("message_count", len(plan.get("messages", [])) or 1) or 1)
    count = max(1, min(count, limits.max_messages))
    length = str(plan.get("length", "") or "").upper()
    raw_pause = plan.get("pause", 1.0)
    if isinstance(raw_pause, (list, tuple)) and len(raw_pause) >= 2:
        try:
            low, high = float(raw_pause[0]), float(raw_pause[1])
        except (TypeError, ValueError):
            low, high = 1.0, 1.0
        low, high = (low, high) if low <= high else (high, low)
        pause = [
            round(max(limits.min_pause, min(low, limits.max_pause)), 2),
            round(max(limits.min_pause, min(high, limits.max_pause)), 2),
        ]
    else:
        try:
            pause = float(raw_pause or 1.0)
        except (TypeError, ValueError):
            pause = 1.0
        pause = round(max(limits.min_pause, min(pause, limits.max_pause)), 2)
    sticker_probability = float(plan.get("sticker_probability", 0.0) or 0.0)
    sticker_probability = max(0.0, min(sticker_probability, limits.max_sticker_probability))
    cap = limits.cap_for(length)
    messages = [str(m)[:cap] for m in plan.get("messages", []) if str(m).strip()]
    if len(messages) > count:
        head = messages[: count - 1]
        messages = head + ["".join(messages[count - 1 :])[:cap]]
    plan.update(
        {
            "message_count": count,
            "pause": pause,
            "sticker_probability": round(sticker_probability, 3),
            "messages": messages,
            "clamped": True,
        }
    )
    return plan


class MessageRenderer:
    def __init__(
        self,
        send: Callable[[int, str], None],
        typing: Callable[[int], None] | None = None,
        limits: RenderLimits | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.send = send
        self.typing = typing
        self.limits = limits or RenderLimits()
        self.sleep = sleep

    def render(
        self,
        chat_id: int,
        text_or_plan: str | dict,
        *,
        interrupt_check: Callable[[], bool] | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        """按条发送，返回真正发出去的内容列表。

        dry_run=True 时只计算"会发什么"，不发送、不等待、不显示输入状态（测试用）。
        """
        if isinstance(text_or_plan, dict):
            plan = clamp_plan(text_or_plan, self.limits)
            length = str(plan.get("length", "") or "").upper()
            cap = self.limits.cap_for(length)
            messages = plan.get("messages") or split_messages(
                str(text_or_plan.get("content", "")), self.limits.max_messages, cap
            )
            pause_value = plan.get("pause", 1.0)
        else:
            messages = split_messages(str(text_or_plan), self.limits.max_messages, self.limits.max_chars)
            pause_value = 1.0
        if dry_run:
            return list(messages)

        sent: list[str] = []
        for index, message in enumerate(messages):
            if interrupt_check is not None and interrupt_check():
                break
            if self.typing is not None:
                try:
                    self.typing(chat_id)
                except Exception:  # noqa: BLE001 - 输入状态只是锦上添花
                    pass
            self.sleep(typing_seconds(message, self.limits))
            self.send(chat_id, message)
            sent.append(message)
            if index < len(messages) - 1:
                self.sleep(self._gap(pause_value))
        return sent

    def _gap(self, pause_value: object) -> float:
        """条与条之间的停顿：只在 Planner 给的区间内微扰，不做全局随机。"""
        if isinstance(pause_value, (list, tuple)) and len(pause_value) >= 2:
            try:
                low, high = float(pause_value[0]), float(pause_value[1])
            except (TypeError, ValueError):
                low, high = self.limits.min_pause, self.limits.min_pause * 2
        elif isinstance(pause_value, (int, float)):
            low = high = float(pause_value)
        else:
            low = high = 1.0
        low = max(self.limits.min_pause, min(low, self.limits.max_pause))
        high = max(low, min(high, self.limits.max_pause))
        base = random.uniform(low, high)
        jitter = random.uniform(0.85, 1.15)
        return max(self.limits.min_pause, min(base * jitter, self.limits.max_pause))
