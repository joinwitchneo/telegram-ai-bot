"""ContinuitySeed / CycleRecord。"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclass
class ContinuitySeed:
    seed_id: str = ""
    cycle_id: str = ""
    unfinished_thought_ids: list = field(default_factory=list)
    active_topic: str = ""
    next_wake_hint: str = ""
    hint: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ContinuitySeed":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class CycleRecord:
    cycle_id: str = ""
    trigger: str = ""
    mode: str = ""
    thoughts_created: int = 0
    thoughts_rejected: int = 0
    info_gain: float = 0.0
    motivation: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    reward: dict = field(default_factory=dict)
    interest_changes: list = field(default_factory=list)
    llm_calls: int = 0
    skip_reason: str = ""
    latency_ms: int = 0
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)
