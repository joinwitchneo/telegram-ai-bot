"""唤醒策略（Phase 6 §六/§二十）：阈值、上限、权重，全部配置化。"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_WEIGHTS = {
    "urgency": 0.25,
    "unfinished": 0.20,
    "curiosity": 0.15,
    "motivation": 0.15,
    "novelty": 0.10,
    "continuity": 0.10,
    "relationship_relevance": 0.05,
}


def clamp(value, low=0.0, high=1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


@dataclass
class WakePolicy:
    wake_threshold: float = 0.55            # 自主唤醒门槛
    daily_wake_limit: int = 12              # 每天最多自主唤醒几次（≠ LLM 次数）
    min_gap_minutes: int = 30               # 两次自主唤醒之间的最小间隔
    per_thought_cooldown_minutes: int = 120  # 同一个念头多久内不重复唤醒
    curiosity_floor: float = 0.60           # 好奇心候选的下限
    relationship_floor: float = 0.60
    time_recheck_score: float = 0.20        # 单纯"到点看看"的分数（永远不够触发）
    pending_intention_bonus: float = 0.15   # 上次想做却没做的意向
    ignored_penalty: float = 0.05           # 被忽略过的念头略微降权
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @classmethod
    def from_runtime(cls, runtime) -> "WakePolicy":
        if runtime is None:
            return cls()
        weights = {
            "urgency": runtime.limit("V3_WAKE_WEIGHT_URGENCY", 0.25),
            "unfinished": runtime.limit("V3_WAKE_WEIGHT_UNFINISHED", 0.20),
            "curiosity": runtime.limit("V3_WAKE_WEIGHT_CURIOSITY", 0.15),
            "motivation": runtime.limit("V3_WAKE_WEIGHT_MOTIVATION", 0.15),
            "novelty": runtime.limit("V3_WAKE_WEIGHT_NOVELTY", 0.10),
            "continuity": runtime.limit("V3_WAKE_WEIGHT_CONTINUITY", 0.10),
            "relationship_relevance": runtime.limit("V3_WAKE_WEIGHT_RELATIONSHIP", 0.05),
        }
        return cls(
            wake_threshold=runtime.limit("V3_WAKE_THRESHOLD", 0.55),
            daily_wake_limit=runtime.limit_int("V3_AUTONOMOUS_WAKE_PER_DAY", 12),
            min_gap_minutes=runtime.limit_int("V3_WAKE_MIN_GAP_MINUTES", 30),
            per_thought_cooldown_minutes=runtime.limit_int("V3_WAKE_THOUGHT_COOLDOWN_MINUTES", 120),
            curiosity_floor=runtime.limit("V3_WAKE_CURIOSITY_FLOOR", 0.60),
            relationship_floor=runtime.limit("V3_WAKE_RELATIONSHIP_FLOOR", 0.60),
            time_recheck_score=runtime.limit("V3_WAKE_TIME_RECHECK_SCORE", 0.20),
            pending_intention_bonus=runtime.limit("V3_WAKE_PENDING_INTENTION_BONUS", 0.15),
            ignored_penalty=runtime.limit("V3_WAKE_IGNORED_PENALTY", 0.05),
            weights=weights,
        )

    def to_dict(self) -> dict:
        return {
            "wake_threshold": self.wake_threshold,
            "daily_wake_limit": self.daily_wake_limit,
            "min_gap_minutes": self.min_gap_minutes,
            "per_thought_cooldown_minutes": self.per_thought_cooldown_minutes,
            "time_recheck_score": self.time_recheck_score,
            "pending_intention_bonus": self.pending_intention_bonus,
            "weights": dict(self.weights),
        }
