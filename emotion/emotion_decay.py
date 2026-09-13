"""情绪衰减：向基线指数回归，不同维度速度不同。

公式：value = baseline + (value - baseline) * exp(-rate * hours)
这样"偏离越大回落越快"，且永远不会越过基线。
"""

from __future__ import annotations

import math

# 每小时衰减率（越大回得越快）；0 表示几乎不动
DEFAULT_RATES = {
    "excitement": 0.25,
    "embarrassment": 0.15,
    "joy": 0.12,
    "anger": 0.10,
    "fatigue": 0.10,
    "calm": 0.08,
    "anxiety": 0.07,
    "interest": 0.06,
    "sadness": 0.05,
    "loneliness": 0.03,
    "affection": 0.02,
    "trust": 0.005,
}


class EmotionDecay:
    def __init__(self, baseline: dict, rates: dict | None = None, max_hours: float = 24 * 30) -> None:
        self.baseline = dict(baseline)
        self.rates = {**DEFAULT_RATES, **(rates or {})}
        self.max_hours = float(max_hours)

    def factor(self, name: str, hours: float) -> float:
        rate = max(0.0, float(self.rates.get(name, 0.05)))
        hours = min(max(0.0, float(hours)), self.max_hours)
        return math.exp(-rate * hours)

    def decay(self, emotions: dict, hours: float) -> dict:
        """返回衰减后的新状态（不修改入参）。"""
        if hours <= 0:
            return dict(emotions)
        result = {}
        for name, value in emotions.items():
            base = float(self.baseline.get(name, 0.5))
            factor = self.factor(name, hours)
            result[name] = base + (float(value) - base) * factor
        return result

    def deviation(self, emotions: dict) -> float:
        """当前状态偏离基线多少（用于判断"情绪是否已经平复"）。"""
        return round(
            sum(abs(float(value) - float(self.baseline.get(name, 0.5))) for name, value in emotions.items()), 4
        )
