"""CheckpointEngine（Phase 6 §十/§十三）。

检查点必须**便宜、确定性、可测试**：只做 Python 计算，绝不调用 LLM。
LLM 只可能在检查点"判定达到认知阈值"之后，由 LifeCycle 拉起。

每个检查点做四件事：
  1. 推进所有活跃念头的动力学（age/activation/persistence/触发分）；
  2. 用 MotivationEngine 刷新每个候选念头的 motivation（复用既有实现）；
  3. 用 Thresholds 把触发分分档；
  4. 选出候选（按触发分排序）并落一条 checkpoint 记录 + trace 事件。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from v3.checkpoint.threshold import BAND_COGNITIVE, Thresholds
from v3.thought.models import STATE_ACTIVE, STATE_BORN, STATE_INCUBATING, STATE_DECAYING
from v3.trace import TraceLog

MAX_CANDIDATES = 5


def _new_trace_id(moment: datetime.datetime) -> str:
    return f"tr_{moment.strftime('%Y%m%d%H%M%S%f')[:20]}"


@dataclass
class Candidate:
    thought_id: str
    topic: str
    content: str
    trigger_score: float
    band: str
    state: str
    activation: float
    motivation: float
    components: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckpointRecord:
    checkpoint_id: str
    trace_id: str
    created_at: str
    evaluated: int = 0
    active_count: int = 0
    thresholds: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)
    top: dict = field(default_factory=dict)
    band: str = ""
    triggered: bool = False
    reason: str = ""
    llm_calls: int = 0
    updates: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class CheckpointEngine:
    def __init__(
        self,
        *,
        thoughts,
        dynamics,
        thresholds: Thresholds | None = None,
        motivation_engine=None,
        interests=None,
        trace: TraceLog | None = None,
        log_path: Path | None = None,
        keep: int = 500,
        now_fn=None,
        max_candidates: int = MAX_CANDIDATES,
    ) -> None:
        self.thoughts = thoughts
        self.dynamics = dynamics
        self.thresholds = thresholds or Thresholds()
        self.motivation_engine = motivation_engine
        self.interests = interests
        self.trace = trace
        self.log_path = Path(log_path) if log_path else None
        self.keep = max(20, int(keep))
        self._now = now_fn or datetime.datetime.now
        self.max_candidates = max(1, int(max_candidates))
        self._lock = threading.RLock()
        self._counter = 0

    # ── 主入口 ──────────────────────────────────────────────────
    def tick(self, *, now: datetime.datetime | None = None,
             interaction_gap_hours: float = 0.0) -> CheckpointRecord:
        moment = now or self._now()
        self._counter += 1
        checkpoint_id = f"cp_{moment.strftime('%Y%m%d%H%M%S')}_{self._counter:04d}"
        trace_id = _new_trace_id(moment)
        record = CheckpointRecord(
            checkpoint_id=checkpoint_id, trace_id=trace_id,
            created_at=moment.isoformat(timespec="seconds"),
            thresholds=self.thresholds.to_dict(),
        )
        self._trace("checkpoint_started", trace_id=trace_id,
                    checkpoint_id=checkpoint_id, thresholds=record.thresholds)

        interests = self.interests.all() if self.interests is not None else []
        active = [t for t in self.thoughts.all() if not t.is_closed()]
        record.evaluated = len(active)
        record.active_count = sum(
            1 for t in active if t.lifecycle_state in (STATE_ACTIVE, STATE_INCUBATING))

        candidates: list[Candidate] = []
        for thought in active:
            change = self.dynamics.tick(thought, now=moment)
            record.updates.append(change)
            if change["state"] == STATE_DECAYING:
                self._trace("thought_decayed", trace_id=trace_id, thought_id=thought.id,
                            activation=change["after"]["activation"], age=change["age"])
            if self.motivation_engine is not None:
                state = self.motivation_engine.evaluate(
                    thoughts=[thought], interests=interests,
                    interaction_gap_hours=interaction_gap_hours,
                )
                thought.motivation = round(float(getattr(state, "score", 0.0) or 0.0), 4)
            score, components = self.dynamics.trigger_score(thought)
            thought.trigger_score = score
            band = self.thresholds.band(score)
            if thought.lifecycle_state in (STATE_BORN, STATE_ACTIVE) and band != "ignore":
                thought.set_state(STATE_INCUBATING if band == "incubating" else STATE_ACTIVE)
            candidates.append(Candidate(
                thought_id=thought.id, topic=thought.topic, content=thought.content[:120],
                trigger_score=score, band=band, state=thought.lifecycle_state,
                activation=round(thought.activation, 4),
                motivation=round(thought.motivation, 4), components=components,
            ))

        # 动力学改动必须写回：否则"状态随时间演化"只是内存里的幻觉
        self.thoughts.update_many(active)
        candidates.sort(key=lambda c: (-c.trigger_score, c.thought_id))
        record.candidates = [c.to_dict() for c in candidates[: self.max_candidates]]
        top = candidates[0] if candidates else None
        if top is not None:
            record.top = top.to_dict()
            record.band = top.band
            record.triggered = top.band == BAND_COGNITIVE
            record.reason = (f"念头 {top.thought_id} 触发分 {top.trigger_score} "
                             f"达到 {record.band}")
        else:
            record.reason = "没有可评估的念头"

        self._trace("threshold_evaluated", trace_id=trace_id,
                    evaluated=record.evaluated, band=record.band,
                    top=record.top, thresholds=record.thresholds)
        if record.triggered:
            self._trace("cognitive_triggered", trace_id=trace_id,
                        thought_id=record.top.get("thought_id", ""),
                        trigger_score=record.top.get("trigger_score", 0.0))
        else:
            self._trace("cognitive_skipped", trace_id=trace_id,
                        reason=record.reason, band=record.band)
        self._append(record)
        return record

    # ── 落盘与事件 ───────────────────────────────────────────────
    def _append(self, record: CheckpointRecord) -> None:
        if self.log_path is None:
            return
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                existing = self._read_log()
                existing.append(record.to_dict())
                payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in existing[-self.keep:])
                from core.atomic_io import atomic_write_text

                atomic_write_text(self.log_path, payload + "\n")
            except OSError as exc:
                logging.warning("[v3] 写 checkpoints 失败：%s", exc)

    def _read_log(self) -> list:
        if self.log_path is None or not self.log_path.is_file():
            return []
        try:
            return [json.loads(line) for line in
                    self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    def recent(self, limit: int = 5) -> list:
        return self._read_log()[-max(1, int(limit)):]

    def _trace(self, kind: str, *, trace_id: str = "", **fields) -> None:
        if self.trace is None:
            return
        try:
            self.trace.record(kind, trace_id=trace_id, **fields)
        except Exception as exc:  # noqa: BLE001 - 观察失败不能影响生命循环
            logging.debug("[v3] trace 记录失败（忽略）：%s", exc)

    def stats(self) -> dict:
        items = self._read_log()
        triggered = sum(1 for item in items if item.get("triggered"))
        return {"checkpoints": len(items), "triggered": triggered,
                "last": items[-1] if items else {}}
