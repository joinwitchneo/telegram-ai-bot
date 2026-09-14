"""RewardEngine。

Reward = 0.50*objective + 0.20*behavioral + 0.15*continuity + 0.15*llm_eval
其中：
  - objective 来自确定性信号（是否获得新信息 / 是否推进了未完成念头）
  - llm_eval 默认关闭（V3_REWARD_LLM_ENABLED=false），关闭时其权重按比例并入前三项
  - 同一 topic 短时间内重复触发时，llm_eval 贡献递减并设上限，杜绝自我奖励正反馈
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

BASE_WEIGHTS = {"objective": 0.50, "behavioral": 0.20, "continuity": 0.15, "llm_eval": 0.15}
LLM_EVAL_TOTAL_CAP = 0.15


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass
class RewardBreakdown:
    reward: float = 0.0
    components: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)
    expected: float = 0.0
    actual: float = 0.0
    prediction_error: float = 0.0
    llm_eval_used: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class RewardEngine:
    def __init__(
        self,
        *,
        llm_eval_enabled: bool = False,
        weights: dict | None = None,
        llm_eval_cap: float = LLM_EVAL_TOTAL_CAP,
    ) -> None:
        self.llm_eval_enabled = bool(llm_eval_enabled)
        self.weights = {**BASE_WEIGHTS, **(weights or {})}
        self.llm_eval_cap = max(0.0, float(llm_eval_cap))

    def effective_weights(self) -> dict:
        """LLM 评分关闭时，把它的权重按比例并入前三项（objective 仍是主导）。"""
        weights = dict(self.weights)
        if not self.llm_eval_enabled:
            spare = float(weights.pop("llm_eval", 0.0))
            holders = ("objective", "behavioral", "continuity")
            total = sum(float(weights.get(name, 0.0)) for name in holders) or 1.0
            for name in holders:
                weights[name] = round(float(weights.get(name, 0.0)) + spare * float(weights.get(name, 0.0)) / total, 4)
            weights["llm_eval"] = 0.0
        return weights

    def evaluate(
        self,
        *,
        information_gain: float = 0.0,
        unfinished_progress: float = 0.0,
        behavioral_signal: float = 0.0,
        continuity_signal: float = 0.0,
        llm_eval_raw: float | None = None,
        topic_repeat_count: int = 1,
        expected_reward: float = 0.0,
    ) -> RewardBreakdown:
        objective = clamp(0.7 * float(information_gain) + 0.3 * float(unfinished_progress))
        behavioral = clamp(behavioral_signal)
        continuity = clamp(continuity_signal, 0.0, 1.0)
        components = {"objective": round(objective, 4), "behavioral": round(behavioral, 4),
                      "continuity": round(continuity, 4), "llm_eval": 0.0}

        used_llm = self.llm_eval_enabled and llm_eval_raw is not None
        if used_llm:
            decay = 1.0 / max(1, int(topic_repeat_count))
            components["llm_eval"] = round(clamp(float(llm_eval_raw)) * decay, 4)
            if components["llm_eval"] * self.weights.get("llm_eval", 0.0) > self.llm_eval_cap:
                components["llm_eval"] = round(self.llm_eval_cap / max(1e-6, self.weights["llm_eval"]), 4)

        weights = self.effective_weights()
        reward = sum(components.get(name, 0.0) * float(weight) for name, weight in weights.items())
        actual = clamp(reward)
        return RewardBreakdown(
            reward=round(actual, 4),
            components=components,
            weights=weights,
            expected=round(clamp(expected_reward), 4),
            actual=round(actual, 4),
            prediction_error=round(actual - clamp(expected_reward), 4),
            llm_eval_used=used_llm,
            reason="objective 主导" if not used_llm else "objective + 受限 llm_eval",
        )
