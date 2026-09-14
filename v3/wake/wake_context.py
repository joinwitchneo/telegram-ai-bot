"""WakeContext（Phase 6 §五）：只装"这次唤醒真正相关"的状态，不塞全部历史。"""

from __future__ import annotations

import datetime


def build_wake_context(
    *,
    wake_id: str,
    reason: str,
    thoughts=None,
    interests=None,
    continuity_seed=None,
    last_user_at: str = "",
    cooldowns: dict | None = None,
    budget: dict | None = None,
    now: datetime.datetime | None = None,
    extra: dict | None = None,
) -> dict:
    moment = now or datetime.datetime.now()
    active_thoughts = []
    candidates: dict = {}
    if thoughts is not None:
        try:
            rows = [t for t in thoughts.all() if not t.is_closed()]
        except Exception:  # noqa: BLE001
            rows = []
        rows.sort(key=lambda t: -(t.trigger_score or 0.0))
        active_thoughts = [t.id for t in rows[:5]]
        candidates = {
            t.id: {
                "topic": t.topic, "state": t.lifecycle_state,
                "trigger_score": t.trigger_score, "urgency": t.urgency,
                "curiosity": t.curiosity, "motivation": t.motivation,
                "is_unfinished": t.is_unfinished,
                "pending_intention": bool((t.metadata or {}).get("pending_intention")),
            }
            for t in rows[:5]
        }
    active_interests = []
    if interests is not None:
        try:
            active_interests = [item.topic for item in interests.top(limit=5)]
        except Exception:  # noqa: BLE001
            active_interests = []
    seed = continuity_seed.to_dict() if hasattr(continuity_seed, "to_dict") else {}
    return {
        "wake_id": str(wake_id),
        "reason": str(reason),
        "timestamp": moment.isoformat(timespec="seconds"),
        "continuity_seed_ids": list(seed.get("unfinished_thought_ids") or []),
        "active_thought_ids": active_thoughts,
        "thought_summaries": candidates,
        "active_interest_ids": active_interests,
        "pending_intention_ids": [
            tid for tid, item in candidates.items() if item.get("pending_intention")
        ],
        "last_user_interaction": str(last_user_at or ""),
        "cooldowns": dict(cooldowns or {}),
        "budget": dict(budget or {}),
        "extra": dict(extra or {}),
    }
