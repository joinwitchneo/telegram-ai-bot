"""记忆温度衰减：HOT / WARM / COLD / ARCHIVED。

规则（Phase 2/3 定稿）：
- HOT：最近 hot_days 天内用过，或 7 天内用过 ≥3 次
- WARM：重要性 ≥0.5 且 warm_days 内用过
- COLD：超过 warm_days 没用
- ARCHIVED：超过 archive_days 没用且重要性 <0.4，且**非 protected**
- protected 记忆永远不会被普通衰减归档（最低只降到 COLD）
"""

from __future__ import annotations

import datetime


class MemoryDecay:
    def __init__(
        self,
        store,
        *,
        hot_days: int = 3,
        warm_days: int = 30,
        archive_days: int = 90,
        archive_importance_below: float = 0.4,
        hot_use_count: int = 3,
        hot_window_days: int = 7,
    ) -> None:
        self.store = store
        self.hot_days = max(1, int(hot_days))
        self.warm_days = max(self.hot_days, int(warm_days))
        self.archive_days = max(self.warm_days, int(archive_days))
        self.archive_importance_below = float(archive_importance_below)
        self.hot_use_count = max(1, int(hot_use_count))
        self.hot_window_days = max(1, int(hot_window_days))

    def evaluate(self, memory: dict, now: datetime.datetime) -> str:
        """计算这条记忆当前应该处于的温度。"""
        protected = bool(memory.get("protected"))
        last_used = self._parse(memory.get("last_used_at")) or self._parse(memory.get("created_at"))
        if last_used is None:
            return "WARM"
        days = max(0.0, (now - last_used).total_seconds() / 86400)
        use_count = int(memory.get("use_count", 0))

        if days <= self.hot_days or (use_count >= self.hot_use_count and days <= self.hot_window_days):
            return "HOT"
        if days <= self.warm_days and float(memory.get("importance", 0.5)) >= 0.5:
            return "WARM"
        if days <= self.archive_days:
            return "WARM" if protected else "COLD"
        if protected:
            return "COLD"
        if float(memory.get("importance", 0.5)) < self.archive_importance_below:
            return "ARCHIVED"
        return "WARM"

    def apply(self, *, now: datetime.datetime | None = None) -> dict:
        """全量评估一次温度，返回变更统计。"""
        moment = now or datetime.datetime.now()
        changed = {"HOT": 0, "WARM": 0, "COLD": 0, "ARCHIVED": 0}
        moved = 0
        for memory in self.store.list():
            target = self.evaluate(memory, moment)
            if target != memory.get("temperature"):
                self.store.set_temperature(str(memory.get("id")), target)
                changed[target] += 1
                moved += 1
        return {"evaluated": len(self.store.list()), "moved": moved, "by_temperature": changed}

    @staticmethod
    def _parse(value) -> datetime.datetime | None:
        text = str(value or "")
        if not text:
            return None
        try:
            return datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
