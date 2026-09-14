"""唤醒规则（Python 硬约束，LLM 只能建议）。"""

from __future__ import annotations

import datetime

WAKE_HIGH = "high"
WAKE_NORMAL = "normal"
WAKE_LOW = "low"
PRIORITY_ORDER = (WAKE_HIGH, WAKE_NORMAL, WAKE_LOW)


def clamp_priority(priority: str, *, has_unfinished: bool, has_gain: bool, no_action: bool) -> str:
    """NoAction 不允许 High；有未完成念头/新信息才能到 Normal，否则 Low。"""
    value = str(priority or WAKE_NORMAL).lower()
    if value not in PRIORITY_ORDER:
        value = WAKE_NORMAL
    if no_action and value == WAKE_HIGH:
        value = WAKE_NORMAL if (has_unfinished or has_gain) else WAKE_LOW
    if value == WAKE_NORMAL and not (has_unfinished or has_gain):
        value = WAKE_LOW
    return value


def plan_next_wake(
    *,
    now: datetime.datetime,
    min_interval_minutes: int,
    max_silence_hours: int,
    hint_minutes: int | None = None,
    priority: str = WAKE_NORMAL,
    last_wake_at: datetime.datetime | None = None,
    has_unfinished: bool = False,
    has_gain: bool = False,
    no_action: bool = False,
) -> dict:
    """返回 {earliest_at, priority, reason} —— 所有时间都受硬下限钳位。"""
    floor = now + datetime.timedelta(minutes=max(1, int(min_interval_minutes)))
    if last_wake_at is not None:
        floor = max(floor, last_wake_at + datetime.timedelta(minutes=max(1, int(min_interval_minutes))))
    silence_deadline = now + datetime.timedelta(hours=max(1, int(max_silence_hours)))

    proposed = floor
    if hint_minutes:
        proposed = max(floor, now + datetime.timedelta(minutes=max(1, int(hint_minutes))))
    if proposed > silence_deadline:
        proposed = silence_deadline
        priority = WAKE_LOW
    priority = clamp_priority(priority, has_unfinished=has_unfinished, has_gain=has_gain,
                              no_action=no_action)
    return {
        "earliest_at": proposed.isoformat(timespec="seconds"),
        "priority": priority,
        "reason": "continuity_seed",
        "clamped_to_min_interval": proposed == floor,
    }
