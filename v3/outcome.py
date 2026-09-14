"""Outcome 记录与反馈（Phase 6 §二十三）。

行动执行后必须留下 Outcome，并把它反馈回念头/兴趣/动机：
    Action -> Outcome -> Feedback -> State Change -> Future Behavior
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from v3.actions.base import Outcome


class OutcomeStore:
    def __init__(self, path: Path, *, keep: int = 500) -> None:
        self.path = Path(path)
        self.keep = max(50, int(keep))
        self._lock = threading.RLock()
        self._count = self._count_lines()

    def record(self, outcome: Outcome) -> dict:
        entry = outcome.to_dict()
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._count += 1
            except OSError as exc:
                logging.warning("[v3] 写 outcome 失败：%s", exc)
        return entry

    def recent(self, limit: int = 5) -> list:
        if not self.path.is_file():
            return []
        items = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return items[-max(1, int(limit)):]

    def _count_lines(self) -> int:
        if not self.path.is_file():
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def count(self) -> int:
        with self._lock:
            return self._count


class FeedbackEngine:
    """把 Outcome 折回内部状态。全部确定性计算，不调模型。"""

    def __init__(self, *, dynamics=None, reward_engine=None, interests=None,
                 interest_engine=None, trace=None) -> None:
        self.dynamics = dynamics
        self.reward_engine = reward_engine
        self.interests = interests
        self.interest_engine = interest_engine
        self.trace = trace

    def apply(self, *, outcome: Outcome, thought=None, trace_id: str = "",
              expected: float = 0.5) -> dict:
        detail: dict = {"action_id": outcome.action_id, "kind": outcome.kind,
                        "status": outcome.status, "changes": {}}
        positive = outcome.executed and not outcome.error
        if thought is not None and self.dynamics is not None:
            detail["changes"]["thought"] = self.dynamics.apply_outcome(
                thought, status=outcome.status, positive=positive, kind=outcome.kind)

        if self.reward_engine is not None and thought is not None:
            reward = self.reward_engine.evaluate(
                information_gain=float(getattr(thought, "information_gain", 0.0) or 0.0),
                unfinished_progress=1.0 if outcome.executed else 0.0,
                behavioral_signal=0.5 if outcome.executed else 0.0,
                continuity_signal=1.0 if outcome.executed else 0.3,
                topic_repeat_count=1,
                expected_reward=expected,
            )
            detail["reward"] = reward.to_dict()
            if self.interest_engine is not None and getattr(thought, "topic", ""):
                _, interest_detail = self.interest_engine.update(
                    thought.topic,
                    information_gain=abs(float(reward.prediction_error)),
                    valence_signal=1.0 if reward.prediction_error >= 0 else -1.0,
                    evidence_delta=0,
                    is_new_observation=False,
                )
                detail["changes"]["interest"] = interest_detail

        if self.trace is not None:
            try:
                # 注意：不能把 detail 里的 "kind" 直接展开（会撞上 record(kind=...)）
                payload = {
                    "action_kind": detail.get("kind"),
                    "status": detail.get("status"),
                    "changes": detail.get("changes"),
                }
                if "reward" in detail:
                    payload["reward"] = detail["reward"]
                self.trace.record("state_changed", trace_id=trace_id,
                                  thought_id=getattr(thought, "id", ""),
                                  action_id=outcome.action_id, **payload)
            except Exception:  # noqa: BLE001 - 观察失败不能影响生命循环
                logging.debug("[v3] trace 记录失败（忽略）")
        return detail
