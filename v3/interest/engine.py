"""InterestEngine：单次步长受限 + 边际收益衰减 => 增量单调递减、总量收敛。"""

from __future__ import annotations

from v3.interest.models import TopicInterest, clamp

MAX_STEP = 0.05          # 单次事件对任何字段的最大变化
GAIN_TO_STEP = 0.25      # information_gain -> 步长
MARGINAL_DECAY = 0.5     # 同一主题反复更新时步长按此比例衰减
MAX_ATTRACTION_STEP_TOTAL = 0.10   # 同一主题累计最多推高这么多（防止"什么都越来越喜欢"）


class InterestEngine:
    def __init__(
        self,
        store,
        *,
        max_step: float = MAX_STEP,
        gain_to_step: float = GAIN_TO_STEP,
        marginal_decay: float = MARGINAL_DECAY,
        total_cap: float = MAX_ATTRACTION_STEP_TOTAL,
    ) -> None:
        self.store = store
        self.max_step = max(0.001, float(max_step))
        self.gain_to_step = max(0.0, float(gain_to_step))
        self.marginal_decay = clamp(marginal_decay, 0.05, 0.99)
        self.total_cap = max(0.0, float(total_cap))

    def step_for(self, *, information_gain: float, updates: int) -> float:
        """第 updates 次同主题更新的步长：随次数指数衰减。"""
        base = min(self.max_step, max(0.0, float(information_gain)) * self.gain_to_step)
        return round(base * (self.marginal_decay ** max(0, int(updates) - 1)), 6)

    def update(
        self,
        topic: str,
        *,
        information_gain: float = 0.0,
        valence_signal: float = 0.0,
        exploration_cost: float = 0.0,
        evidence_delta: int = 1,
        is_new_observation: bool = True,
    ) -> tuple[TopicInterest, dict]:
        """一次 Experience 只能带来有限步长；返回 (新状态, 本次变化明细)。"""
        topic = (topic or "").strip()[:40]
        # 新话题从 updates=0 起步：第一次更新不应该被当成"第二次"而先衰减一半
        current = self.store.get(topic) or TopicInterest(topic=topic, updates=0)
        updates = int(current.updates) + 1
        step = self.step_for(information_gain=information_gain, updates=updates)
        if not is_new_observation:
            step *= 0.5

        attraction_delta = min(step, max(0.0, self.total_cap - max(0.0, current.attraction - 0.5)))
        curiosity_delta = step * 0.6
        valence_delta = clamp(float(valence_signal), -1.0, 1.0) * step

        if valence_delta < 0:
            # 负向：降低 attraction，但允许 curiosity 独立变化（"讨厌但仍然好奇"）
            attraction_delta = max(-self.max_step, valence_delta)

        updated = TopicInterest(
            topic=topic,
            attraction=clamp(current.attraction + attraction_delta, 0.0, 1.0),
            curiosity=clamp(current.curiosity + curiosity_delta, 0.0, 1.0),
            valence=clamp(current.valence + valence_delta, -1.0, 1.0),
            exploration_cost=clamp(current.exploration_cost + float(exploration_cost), 0.0, 1.0),
            evidence=int(current.evidence) + int(evidence_delta),
            updates=updates,
        )
        saved = self.store.upsert(updated)
        detail = {
            "topic": topic,
            "step": round(step, 6),
            "attraction_delta": round(saved.attraction - current.attraction, 6),
            "curiosity_delta": round(saved.curiosity - current.curiosity, 6),
            "valence_delta": round(saved.valence - current.valence, 6),
            "updates": updates,
        }
        return saved, detail

    def snapshot(self, *, limit: int = 8) -> list[dict]:
        return [item.to_dict() for item in self.store.top(limit=limit)]
