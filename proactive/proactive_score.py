"""Proactive Score（Phase 6）：给"为什么现在找用户"打分。

全部正负项归一化到 0~1，权重可配置。纯函数、无随机、0 token。

    score =   unfinished_topic + relationship_heat + interaction_gap
            + important_event + character_thought + shared_experience + topic_revival
            - recent_chat_penalty - proactive_frequency_penalty
            - user_nonresponse_penalty - fatigue_penalty
"""

from __future__ import annotations

DEFAULT_WEIGHTS = {
    # 正项
    "unfinished_topic": 0.35,
    "important_event": 0.35,
    "interaction_gap": 0.20,
    "character_thought": 0.22,
    "relationship_heat": 0.15,
    "topic_revival": 0.10,
    "shared_experience": 0.10,
    # 负项
    "recent_chat_penalty": 0.35,
    "proactive_frequency_penalty": 0.25,
    "user_nonresponse_penalty": 0.30,
    "fatigue_penalty": 0.10,
}

POSITIVE = (
    "unfinished_topic", "important_event", "interaction_gap",
    "character_thought", "relationship_heat", "topic_revival", "shared_experience",
)
NEGATIVE = (
    "recent_chat_penalty", "proactive_frequency_penalty",
    "user_nonresponse_penalty", "fatigue_penalty",
)

FEATURES = POSITIVE + NEGATIVE


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def normalize_features(features: dict | None) -> dict:
    """缺省 0，所有值夹到 0~1，非数字当 0。"""
    result: dict[str, float] = {}
    for name in FEATURES:
        try:
            value = float((features or {}).get(name, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        result[name] = round(_clamp(value), 6)
    return result


def score_candidate(features: dict | None, weights: dict | None = None) -> tuple[float, dict]:
    """返回 (总分, 每项贡献)，方便 Debug"为什么现在找她"。"""
    table = {**DEFAULT_WEIGHTS, **(weights or {})}
    values = normalize_features(features)
    parts: dict[str, float] = {}
    total = 0.0
    for name in POSITIVE:
        contribution = round(values[name] * float(table.get(name, 0.0)), 6)
        parts[name] = contribution
        total += contribution
    for name in NEGATIVE:
        contribution = round(-values[name] * float(table.get(name, 0.0)), 6)
        parts[name] = contribution
        total += contribution
    return round(_clamp(total), 4), parts


def gap_feature(gap_hours: float, *, full_hours: float = 48.0) -> float:
    """多久没聊 → 0~1；不足 6 小时基本为 0，超过 full_hours 记满分。"""
    if gap_hours <= 6:
        return 0.0
    if gap_hours >= full_hours:
        return 1.0
    return round((gap_hours - 6) / max(1.0, full_hours - 6), 4)


def recent_chat_feature(minutes_since_last_user: float, *, window_minutes: float = 45.0) -> float:
    """刚聊完就别打扰：窗口内接近 1。"""
    if minutes_since_last_user >= window_minutes:
        return 0.0
    return round(1.0 - max(0.0, minutes_since_last_user) / max(1.0, window_minutes), 4)
