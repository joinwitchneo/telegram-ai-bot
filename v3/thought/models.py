"""Thought 数据模型（Phase 6 版本）。

分层（Phase 6 §五/§六）：
  - LLM 只能**提议**：content / topic / importance_hint / curiosity_hint /
    novelty_hint / confidence_hint / valence_hint / possible_intention
  - Core 负责**计算**：age / activation / persistence / motivation / trigger_score /
    cooldown / decay / lifecycle_state / times_*

兼容性：`status` 保留为 Phase 0-5 的旧状态（ephemeral/candidate/active/...），
Phase 6 的 `lifecycle_state` 是新的规范状态，两者通过映射表保持一致，
这样旧数据文件、旧命令、旧测试都不需要改。
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field

THOUGHT_TYPES = (
    "observation", "question", "curiosity", "evaluation",
    "desire", "reflection", "unfinished",
)

# ── Phase 0-5 的旧状态（保留，不删）────────────────────────────────
THOUGHT_STATUSES = ("ephemeral", "candidate", "active", "revisited", "reinforced", "pattern")
PERSIST_STATUSES = ("active", "revisited", "reinforced", "pattern")

# ── Phase 6 的生命周期状态（§七）───────────────────────────────────
STATE_BORN = "BORN"
STATE_ACTIVE = "ACTIVE"
STATE_INCUBATING = "INCUBATING"
STATE_TRIGGERED = "TRIGGERED"
STATE_PROCESSING = "PROCESSING"
STATE_ACTION_PENDING = "ACTION_PENDING"
STATE_ACTED = "ACTED"
STATE_RESOLVED = "RESOLVED"
STATE_DECAYING = "DECAYING"
STATE_ARCHIVED = "ARCHIVED"
STATE_MERGED = "MERGED"

LIFECYCLE_STATES = (
    STATE_BORN, STATE_ACTIVE, STATE_INCUBATING, STATE_TRIGGERED, STATE_PROCESSING,
    STATE_ACTION_PENDING, STATE_ACTED, STATE_RESOLVED, STATE_DECAYING,
    STATE_ARCHIVED, STATE_MERGED,
)

# 旧状态 -> Phase 6 状态（读旧数据文件时用）
STATE_BY_STATUS = {
    "ephemeral": STATE_BORN,
    "candidate": STATE_INCUBATING,
    "active": STATE_ACTIVE,
    "revisited": STATE_ACTIVE,
    "reinforced": STATE_ACTED,
    "pattern": STATE_RESOLVED,
}

# Phase 6 状态 -> 旧状态（写回时同步，旧命令/旧测试继续可读）
STATUS_BY_STATE = {
    STATE_BORN: "ephemeral",
    STATE_ACTIVE: "active",
    STATE_INCUBATING: "candidate",
    STATE_TRIGGERED: "active",
    STATE_PROCESSING: "active",
    STATE_ACTION_PENDING: "active",
    STATE_ACTED: "reinforced",
    STATE_RESOLVED: "pattern",
    STATE_DECAYING: "candidate",
    STATE_ARCHIVED: "ephemeral",
    STATE_MERGED: "pattern",
}

# 这些状态已经"结束"，不再参与检查点评估
CLOSED_STATES = (STATE_ARCHIVED, STATE_MERGED, STATE_RESOLVED)


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def status_for_state(state: str) -> str:
    return STATUS_BY_STATE.get(str(state or "").upper(), "ephemeral")


def state_for_status(status: str) -> str:
    return STATE_BY_STATUS.get(str(status or "").lower(), STATE_BORN)


@dataclass
class Thought:
    # ── 身份与内容 ─────────────────────────────────────────────────
    id: str
    type: str = "observation"
    content: str = ""
    evidence: list = field(default_factory=list)
    source: str = "conversation"
    topic: str = ""

    # ── 兼容层：Phase 0-5 旧状态（由 lifecycle_state 同步）──────────
    status: str = "ephemeral"

    # ── Phase 6 生命周期 ──────────────────────────────────────────
    lifecycle_state: str = ""
    is_unfinished: bool = False

    # ── 时间与演化（Core 计算）────────────────────────────────────
    age: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = ""
    last_seen_at: str = field(default_factory=now_iso)
    last_activated_at: str = ""
    cooldown_until: str = ""

    # ── 内容质量（LLM 可提议数值，但由 Core 落定）──────────────────
    novelty: float = 0.0
    information_gain: float = 0.0
    score: float = 0.0

    # ── Phase 6 认知变量（Core 计算）──────────────────────────────
    importance: float = 0.5
    curiosity: float = 0.5
    urgency: float = 0.0
    confidence: float = 0.5
    valence: float = 0.0
    motivation: float = 0.0
    activation: float = 0.5
    persistence: float = 0.5
    relationship_relevance: float = 0.0
    environment_relevance: float = 0.0
    trigger_score: float = 0.0

    # ── 关系与经历 ────────────────────────────────────────────────
    parent_ids: list = field(default_factory=list)
    related_ids: list = field(default_factory=list)
    times_activated: int = 0
    times_ignored: int = 0
    times_acted: int = 0
    last_outcome: str = ""

    # ── 归属与杂项 ────────────────────────────────────────────────
    cycle_id: str = ""
    seen_count: int = 1
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.lifecycle_state:
            self.lifecycle_state = state_for_status(self.status)
        else:
            self.lifecycle_state = str(self.lifecycle_state).upper()
        # 旧 status 与 Phase 6 状态始终一致（旧命令/旧测试读 status）
        if self.status not in THOUGHT_STATUSES:
            self.status = "ephemeral"
        self.status = status_for_state(self.lifecycle_state)
        if not self.updated_at:
            self.updated_at = self.created_at

    # ── 序列化 ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Thought":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})

    # ── 便捷判定 ──────────────────────────────────────────────────
    def is_persistent(self) -> bool:
        """保持 Phase 0-5 的语义不变（决定归档/淘汰时用）。"""
        return self.status in PERSIST_STATUSES or self.is_unfinished

    def is_closed(self) -> bool:
        return self.lifecycle_state in CLOSED_STATES

    def set_state(self, state: str) -> None:
        self.lifecycle_state = str(state).upper()
        self.status = status_for_state(self.lifecycle_state)
