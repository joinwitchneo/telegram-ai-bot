"""CognitiveContextBuilder（Phase 6 §十五）。

达到阈值时才组装上下文，而且只放**相关**的东西：触发念头 + 少量相关念头 +
少量观测 + 内部状态 + 最近结果 + 允许的行动。不把全部历史塞给 LLM。
"""

from __future__ import annotations

from v3.cognition.base import CognitiveContext


class CognitiveContextBuilder:
    def __init__(self, *, thoughts=None, observations=None, outcomes=None,
                 interests=None, available_actions=None, mode_getter=None,
                 max_related: int = 3, max_observations: int = 6,
                 max_outcomes: int = 3) -> None:
        self.thoughts = thoughts
        self.observations = observations
        self.outcomes = outcomes
        self.interests = interests
        self.available_actions = available_actions or (lambda: [])
        self.mode_getter = mode_getter or (lambda: "")
        self.max_related = max(0, int(max_related))
        self.max_observations = max(0, int(max_observations))
        self.max_outcomes = max(0, int(max_outcomes))

    def build(self, *, candidate, trace_id: str = "", cycle_id: str = "",
              environment: dict | None = None, now=None) -> CognitiveContext:
        candidate = dict(candidate or {})
        thought_id = str(candidate.get("thought_id", ""))
        topic = str(candidate.get("topic", ""))
        return CognitiveContext(
            trace_id=trace_id, cycle_id=cycle_id, candidate=candidate,
            related_thoughts=self._related(thought_id, topic),
            memories=[],                      # V3 目前不接记忆器官（留接口）
            internal_state=self._internal_state(),
            recent_outcomes=self._recent_outcomes(),
            available_actions=list(self.available_actions() or []),
            environment=dict(environment or {}),
            observations=self._observations(),
        )

    # ── 取材（全部有上限，避免上下文膨胀）──────────────────────────
    def _related(self, thought_id: str, topic: str) -> list:
        if self.thoughts is None or self.max_related <= 0:
            return []
        items = []
        for thought in self.thoughts.all():
            if thought.id == thought_id or thought.is_closed():
                continue
            same_topic = topic and thought.topic == topic
            linked = thought_id and thought_id in (thought.related_ids or [])
            if not (same_topic or linked):
                continue
            items.append({"thought_id": thought.id, "content": thought.content[:100],
                          "topic": thought.topic, "state": thought.lifecycle_state,
                          "trigger_score": thought.trigger_score})
        items.sort(key=lambda item: -float(item.get("trigger_score") or 0.0))
        return items[: self.max_related]

    def _observations(self) -> list:
        if self.observations is None or self.max_observations <= 0:
            return []
        rows = self.observations.recent(limit=self.max_observations, unconsumed_only=True)
        if not rows:
            rows = self.observations.recent(limit=self.max_observations)
        return [{"user_message": str(item.get("user_message", ""))[:120],
                 "bot_response_excerpt": str(item.get("bot_response_excerpt", ""))[:120],
                 "timestamp": item.get("timestamp", "")} for item in rows]

    def _internal_state(self) -> dict:
        state = {"mode": self.mode_getter()}
        if self.interests is not None:
            top = self.interests.top(limit=3)
            state["top_interests"] = [
                f"{item.topic}(好感{item.attraction:.2f}/好奇{item.curiosity:.2f})" for item in top
            ]
        if self.thoughts is not None:
            stats = self.thoughts.stats()
            state["thoughts"] = stats.get("total", 0)
            state["unfinished"] = stats.get("unfinished", 0)
        return state

    def _recent_outcomes(self) -> list:
        if self.outcomes is None or self.max_outcomes <= 0:
            return []
        try:
            return list(self.outcomes.recent(limit=self.max_outcomes))
        except Exception:  # noqa: BLE001 - 观察失败不能影响认知
            return []
