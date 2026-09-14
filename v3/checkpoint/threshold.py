"""阈值系统（Phase 6 §十一）。数值一律来自配置，不写死在代码里。"""

from __future__ import annotations

from dataclasses import dataclass

BAND_IGNORE = "ignore"
BAND_INCUBATING = "incubating"
BAND_ATTENTION = "attention"
BAND_COGNITIVE = "cognitive"


@dataclass
class Thresholds:
    incubation: float = 0.30
    attention: float = 0.60
    cognitive: float = 0.80
    action: float = 0.80

    @classmethod
    def from_runtime(cls, runtime) -> "Thresholds":
        if runtime is None:
            return cls()
        return cls(
            incubation=runtime.limit("V3_THRESHOLD_INCUBATION", 0.30),
            attention=runtime.limit("V3_THRESHOLD_ATTENTION", 0.60),
            cognitive=runtime.limit("V3_THRESHOLD_COGNITIVE", 0.80),
            action=runtime.limit("V3_THRESHOLD_ACTION", 0.80),
        )

    def band(self, score: float) -> str:
        value = float(score)
        if value >= self.cognitive:
            return BAND_COGNITIVE
        if value >= self.attention:
            return BAND_ATTENTION
        if value >= self.incubation:
            return BAND_INCUBATING
        return BAND_IGNORE

    def to_dict(self) -> dict:
        return {
            "incubation": self.incubation, "attention": self.attention,
            "cognitive": self.cognitive, "action": self.action,
        }
