"""ThoughtLifecycle：状态流转与归档（有生命周期，不无限膨胀）。"""

from __future__ import annotations

import datetime

from v3.thought.models import state_for_status
from v3.thought.store import ThoughtStore

EPHEMERAL_TTL_DAYS = 30


class ThoughtLifecycle:
    def __init__(self, store: ThoughtStore, *, ttl_days: int = EPHEMERAL_TTL_DAYS) -> None:
        self.store = store
        self.ttl_days = max(1, int(ttl_days))

    def run(self, *, now: datetime.datetime | None = None) -> dict:
        archived = self.store.archive_expired(days=self.ttl_days, now=now)
        return {"archived": archived, "total": self.store.count()}

    def promote(self, thought_id: str, *, status: str | None = None) -> bool:
        """把瞬时念头提升为 candidate/active（unfinished 或反复出现才提升）。

        Phase 6 起 lifecycle_state 是规范状态，`status` 只是它的兼容镜像，
        所以这里写 set_state()，由模型统一同步旧字段。
        """
        thought = self.store.get(thought_id)
        if thought is None:
            return False
        if status is None:
            status = "active" if (thought.is_unfinished or thought.seen_count >= 2) else "candidate"
        thought.set_state(state_for_status(status))
        thought.last_seen_at = datetime.datetime.now().isoformat(timespec="seconds")
        self.store.items[thought.id] = thought.to_dict()
        self.store.save()
        return True
