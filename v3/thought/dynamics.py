"""念头动力学（Phase 6 §三/§六/§八/§九/§十二）。

全部是纯 Python、确定性、可测试的计算。LLM 不参与任何打分。

职责：
  - tick()：随时间/关联演化一个念头（age/activation/persistence/curiosity/urgency）
  - trigger_score()：透明公式 + 可配置权重 + 可复现（返回分数与分项）
  - activate()/reinforce()：被事件或相关念头触发时提热度
  - merge()/archive()：合并与归档
  - link()：念头之间的关系（related / supports / conflicts）
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from v3.thought.models import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_DECAYING,
    STATE_MERGED,
    Thought,
    now_iso,
)

LINK_KINDS = ("related", "supports", "conflicts")

DEFAULT_TRIGGER_WEIGHTS = {
    "importance": 0.25,
    "motivation": 0.25,
    "urgency": 0.15,
    "curiosity": 0.10,
    "novelty": 0.10,
    "relationship_relevance": 0.10,
    "persistence": 0.05,
}


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass
class DynamicsConfig:
    """全部可由 config.env 覆盖（Phase 6 §八：规则要能调参）。"""

    base_decay: float = 0.08            # 每个检查点的基础衰减
    importance_shield: float = 0.7      # importance 越高衰减越慢（0=不保护，1=完全不衰减）
    persistence_gain: float = 0.03      # 每次被激活带来的抗衰减增长
    curiosity_decay: float = 0.04
    urgency_decay: float = 0.06
    # 没想完的事会越来越"惦记"（时间压力），这是防止系统永久睡死的核心机制之一
    unfinished_urgency_gain: float = 0.03
    # LLM 的 hint 只是建议：Core 按这个比例融合，而不是直接采纳
    hint_blend: float = 0.5
    archive_below: float = 0.05         # activation 低于此值且久未动 → 归档
    archive_age_ticks: int = 48
    related_activation: float = 0.15    # 相关念头出现时的激活增量
    weights: dict = field(default_factory=lambda: dict(DEFAULT_TRIGGER_WEIGHTS))


class ThoughtDynamics:
    def __init__(self, *, config: DynamicsConfig | None = None) -> None:
        self.config = config or DynamicsConfig()

    @classmethod
    def from_runtime(cls, runtime) -> "ThoughtDynamics":
        """从 RuntimeConfig 读取全部参数（代码默认值 → config → env 覆盖）。"""
        if runtime is None:
            return cls()
        weights = {
            "importance": runtime.limit("V3_WEIGHT_IMPORTANCE", 0.25),
            "motivation": runtime.limit("V3_WEIGHT_MOTIVATION", 0.25),
            "urgency": runtime.limit("V3_WEIGHT_URGENCY", 0.15),
            "curiosity": runtime.limit("V3_WEIGHT_CURIOSITY", 0.10),
            "novelty": runtime.limit("V3_WEIGHT_NOVELTY", 0.10),
            "relationship_relevance": runtime.limit("V3_WEIGHT_RELATIONSHIP", 0.10),
            "persistence": runtime.limit("V3_WEIGHT_PERSISTENCE", 0.05),
        }
        return cls(config=DynamicsConfig(
            base_decay=runtime.limit("V3_DECAY_BASE", 0.08),
            importance_shield=runtime.limit("V3_DECAY_IMPORTANCE_SHIELD", 0.7),
            persistence_gain=runtime.limit("V3_DECAY_PERSISTENCE_GAIN", 0.03),
            curiosity_decay=runtime.limit("V3_DECAY_CURIOSITY", 0.04),
            urgency_decay=runtime.limit("V3_DECAY_URGENCY", 0.06),
            unfinished_urgency_gain=runtime.limit("V3_UNFINISHED_URGENCY_GAIN", 0.03),
            hint_blend=runtime.limit("V3_UNFINISHED_HINT_BLEND", 0.5),
            archive_below=runtime.limit("V3_TRIGGER_ARCHIVE_BELOW", 0.05),
            archive_age_ticks=runtime.limit_int("V3_TRIGGER_ARCHIVE_AGE_TICKS", 48),
            related_activation=runtime.limit("V3_TRIGGER_RELATED_ACTIVATION", 0.15),
            weights=weights,
        ))

    # ── 触发分（透明公式，§十二）───────────────────────────────────
    def trigger_score(self, thought: Thought) -> tuple[float, dict]:
        weights = self.config.weights
        components = {
            "importance": clamp(thought.importance),
            "motivation": clamp(thought.motivation),
            "urgency": clamp(thought.urgency),
            "curiosity": clamp(thought.curiosity),
            "novelty": clamp(thought.novelty),
            "relationship_relevance": clamp(thought.relationship_relevance),
            "persistence": clamp(thought.persistence),
        }
        total = sum(components[name] * float(weights.get(name, 0.0)) for name in components)
        weight_sum = sum(float(weights.get(name, 0.0)) for name in components) or 1.0
        score = clamp(total / weight_sum)
        return round(score, 4), {k: round(v, 4) for k, v in components.items()}

    def score(self, thought: Thought) -> float:
        return self.trigger_score(thought)[0]

    # ── 演化 ─────────────────────────────────────────────────────
    def tick(self, thought: Thought, *, now: datetime.datetime | None = None) -> dict:
        """推进一个时间步：越重要衰减越慢；长期没被碰过的会掉到 DECAYING。"""
        cfg = self.config
        before = {
            "activation": round(thought.activation, 4),
            "persistence": round(thought.persistence, 4),
            "curiosity": round(thought.curiosity, 4),
            "urgency": round(thought.urgency, 4),
        }
        thought.age = int(thought.age) + 1
        shield = 1.0 - cfg.importance_shield * clamp(thought.importance)
        decay = max(0.0, cfg.base_decay * shield) * (1.0 - 0.5 * clamp(thought.persistence))
        thought.activation = clamp(thought.activation - decay)
        thought.curiosity = clamp(thought.curiosity - cfg.curiosity_decay * shield)
        if thought.is_unfinished:
            thought.urgency = clamp(thought.urgency + cfg.unfinished_urgency_gain)
        else:
            thought.urgency = clamp(thought.urgency - cfg.urgency_decay)
        thought.updated_at = (now or datetime.datetime.now()).isoformat(timespec="seconds")

        if not thought.is_closed():
            if thought.activation <= cfg.archive_below and not thought.is_unfinished:
                thought.set_state(STATE_DECAYING)
            elif thought.activation > cfg.archive_below and thought.lifecycle_state == STATE_DECAYING:
                thought.set_state(STATE_ACTIVE)

        thought.trigger_score = self.score(thought)
        return {
            "thought_id": thought.id,
            "age": thought.age,
            "before": before,
            "after": {
                "activation": round(thought.activation, 4),
                "persistence": round(thought.persistence, 4),
                "curiosity": round(thought.curiosity, 4),
                "urgency": round(thought.urgency, 4),
            },
            "state": thought.lifecycle_state,
        }

    def activate(self, thought: Thought, *, strength: float = 0.2,
                 now: datetime.datetime | None = None) -> dict:
        """被事件/相关念头触发：提热度、提持续性、记一次激活。"""
        cfg = self.config
        gain = clamp(strength)
        thought.activation = clamp(thought.activation + gain)
        thought.persistence = clamp(thought.persistence + cfg.persistence_gain)
        thought.times_activated = int(thought.times_activated) + 1
        thought.last_activated_at = (now or datetime.datetime.now()).isoformat(timespec="seconds")
        thought.updated_at = thought.last_activated_at
        if thought.lifecycle_state in (STATE_DECAYING,):
            thought.set_state(STATE_ACTIVE)
        thought.trigger_score = self.score(thought)
        return {"thought_id": thought.id, "activation": round(thought.activation, 4),
                "times_activated": thought.times_activated, "state": thought.lifecycle_state}

    def note_ignored(self, thought: Thought, *, reason: str = "") -> dict:
        thought.times_ignored = int(thought.times_ignored) + 1
        thought.last_outcome = reason or "ignored"
        thought.persistence = clamp(thought.persistence - 0.01)
        thought.updated_at = now_iso()
        return {"thought_id": thought.id, "times_ignored": thought.times_ignored}

    def apply_hints(self, thought: Thought, update: dict) -> dict:
        """把认知器官的 hint 融合进 Core 状态（LLM 提议，Core 决定采纳多少）。"""
        changed: dict = {}
        blend = clamp(self.config.hint_blend, 0.0, 1.0)
        for key, value in dict(update or {}).items():
            if not hasattr(thought, key):
                continue
            old = float(getattr(thought, key) or 0.0)
            new = clamp(old + blend * (clamp(value) - old))
            setattr(thought, key, round(new, 4))
            changed[key] = {"from": round(old, 4), "hint": round(clamp(value), 4),
                            "to": round(new, 4)}
        if changed:
            thought.updated_at = now_iso()
            thought.trigger_score = self.score(thought)
        return changed

    def apply_outcome(self, thought: Thought, *, status: str, positive: bool,
                      kind: str = "") -> dict:
        """行动结果回到念头上（Phase 6 §二十三）。"""
        thought.last_outcome = str(status)
        thought.updated_at = now_iso()
        if str(kind).upper() == "JOURNAL":
            # 写一条内在日志不算"对外行动"：只轻轻提一点热度，不涨 times_acted
            thought.activation = clamp(thought.activation + 0.03)
            thought.trigger_score = self.score(thought)
            return {"thought_id": thought.id, "status": status, "kind": kind,
                    "activation": round(thought.activation, 4),
                    "times_acted": thought.times_acted,
                    "times_ignored": thought.times_ignored}
        if status.upper() in ("SENT", "SIMULATED", "EXECUTED"):
            thought.times_acted = int(thought.times_acted) + 1
            boost = 0.12 if positive else -0.06
            thought.activation = clamp(thought.activation + boost)
            thought.motivation = clamp(thought.motivation + (0.05 if positive else -0.05))
        else:
            thought.times_ignored = int(thought.times_ignored) + 1
            thought.activation = clamp(thought.activation - 0.05)
        thought.trigger_score = self.score(thought)
        return {"thought_id": thought.id, "status": status,
                "activation": round(thought.activation, 4),
                "times_acted": thought.times_acted, "times_ignored": thought.times_ignored}

    def merge(self, primary: Thought, other: Thought, *, now: datetime.datetime | None = None) -> Thought:
        """把 `other` 并进 `primary`（§九：允许 MERGED -> ARCHIVED）。"""
        primary.content = primary.content or other.content
        primary.evidence = list(dict.fromkeys(list(primary.evidence) + list(other.evidence)))[:8]
        primary.importance = clamp(max(primary.importance, other.importance))
        primary.activation = clamp(primary.activation + 0.5 * other.activation)
        primary.persistence = clamp(max(primary.persistence, other.persistence))
        primary.related_ids = list(dict.fromkeys(
            [rid for rid in primary.related_ids + other.related_ids if rid != primary.id]
        ))[:12]
        primary.times_activated = int(primary.times_activated) + int(other.times_activated)
        primary.seen_count = int(primary.seen_count) + int(other.seen_count)
        primary.updated_at = (now or datetime.datetime.now()).isoformat(timespec="seconds")
        other.set_state(STATE_MERGED)
        other.metadata = {**dict(other.metadata or {}), "merged_into": primary.id}
        return primary

    def archive(self, thought: Thought, *, reason: str = "", now: datetime.datetime | None = None) -> dict:
        thought.set_state(STATE_ARCHIVED)
        thought.metadata = {**dict(thought.metadata or {}), "archived_reason": reason}
        thought.updated_at = (now or datetime.datetime.now()).isoformat(timespec="seconds")
        return {"thought_id": thought.id, "state": thought.lifecycle_state, "reason": reason}

    # ── 关系（§九）────────────────────────────────────────────────
    def link(self, left: Thought, right: Thought, *, kind: str = "related",
             activate: bool = True, now: datetime.datetime | None = None) -> dict:
        if kind not in LINK_KINDS:
            kind = "related"
        left.related_ids = list(dict.fromkeys(list(left.related_ids) + [right.id]))[:12]
        right.related_ids = list(dict.fromkeys(list(right.related_ids) + [left.id]))[:12]
        links = dict(left.metadata.get("links") or {})
        links[right.id] = kind
        left.metadata = {**dict(left.metadata or {}), "links": links}
        reverse = dict(right.metadata.get("links") or {})
        reverse[left.id] = kind
        right.metadata = {**dict(right.metadata or {}), "links": reverse}
        result = {"left": left.id, "right": right.id, "kind": kind}
        if activate:
            boost = self.config.related_activation * (1.25 if kind == "supports" else 1.0)
            self.activate(right, strength=boost, now=now)
            result["right_activation"] = round(right.activation, 4)
        return result
