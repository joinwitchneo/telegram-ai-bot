"""MotivationEngine：把念头与兴趣折算成"现在值不值得想/做"。

information_gain 是乘子：反复想同一件毫无新意的事，动机自然被压低。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass
class MotivationState:
    desire: float = 0.0
    curiosity: float = 0.0
    unfinished_pull: float = 0.0
    information_gain: float = 0.0
    interaction_gap_hours: float = 0.0
    score: float = 0.0
    reason: str = ""
    drivers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class MotivationEngine:
    def __init__(self, *, gap_full_hours: float = 24.0, weights: dict | None = None) -> None:
        self.gap_full_hours = max(1.0, float(gap_full_hours))
        self.weights = {
            "desire": 0.30, "curiosity": 0.25, "unfinished": 0.25, "info_gain": 0.20,
            **(weights or {}),
        }

    def evaluate(
        self,
        *,
        thoughts: list,
        interests: list,
        interaction_gap_hours: float = 0.0,
    ) -> MotivationState:
        unfinished = [t for t in (thoughts or []) if getattr(t, "is_unfinished", False)]
        if not thoughts and not interests:
            return MotivationState(reason="没有念头也没有兴趣")

        best = None
        for thought in thoughts or []:
            gain = float(getattr(thought, "information_gain", 0.0) or 0.0)
            novelty = float(getattr(thought, "novelty", 0.0) or 0.0)
            topic_interest = next((i for i in (interests or []) if i.topic and i.topic == getattr(thought, "topic", "")), None)
            desire = clamp((topic_interest.attraction if topic_interest else 0.5) * (0.5 + 0.5 * novelty))
            curiosity = clamp((topic_interest.curiosity if topic_interest else 0.5) * (0.5 + 0.5 * gain))
            pull = 1.0 if getattr(thought, "is_unfinished", False) else 0.0
            score = (
                desire * self.weights["desire"]
                + curiosity * self.weights["curiosity"]
                + pull * self.weights["unfinished"]
                + gain * self.weights["info_gain"]
            )
            if best is None or score > best.score:
                best = MotivationState(
                    desire=round(desire, 4),
                    curiosity=round(curiosity, 4),
                    unfinished_pull=pull,
                    information_gain=gain,
                    interaction_gap_hours=round(float(interaction_gap_hours or 0.0), 2),
                    score=round(clamp(score), 4),
                    reason=f"topic={getattr(thought, 'topic', '') or '-'} gain={gain}",
                    drivers={"thought_id": getattr(thought, "id", ""), "unfinished": bool(pull)},
                )
        if best is None:
            return MotivationState(reason="没有可评估的念头")

        # 久未互动时给一点点"想起他"的推力（不改变方向，只做微调）
        gap_ratio = clamp(float(interaction_gap_hours or 0.0) / self.gap_full_hours)
        if gap_ratio > 0:
            best.score = round(clamp(best.score + 0.05 * gap_ratio), 4)
            best.drivers["gap_bonus"] = round(0.05 * gap_ratio, 4)
        return best
