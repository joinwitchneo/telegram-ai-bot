"""Interest 数据模型（所有数值由 Python clamp，LLM 输出只作候选）。"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


@dataclass
class TopicInterest:
    topic: str
    attraction: float = 0.5
    curiosity: float = 0.5
    valence: float = 0.0
    exploration_cost: float = 0.3
    evidence: int = 0
    updates: int = 1
    last_updated: str = field(default_factory=lambda: datetime.datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        item = asdict(self)
        item["attraction"] = round(clamp(self.attraction, 0.0, 1.0), 4)
        item["curiosity"] = round(clamp(self.curiosity, 0.0, 1.0), 4)
        item["valence"] = round(clamp(self.valence, -1.0, 1.0), 4)
        item["exploration_cost"] = round(clamp(self.exploration_cost, 0.0, 1.0), 4)
        return item

    @classmethod
    def from_dict(cls, data: dict) -> "TopicInterest":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})
