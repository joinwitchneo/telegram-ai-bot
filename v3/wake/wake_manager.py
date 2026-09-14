"""WakeManager（Phase 6 §三/§六/§七）：判断"要不要自己醒来"，并把唤醒排进队列。

它**不**做这些事：调 LLM、直接发消息、修改 Thought/Interest、绕过预算。
它只做：读内部状态 → 算唤醒分 → 决定 WAKE/SKIP → 入队（走统一入口）。

自己的状态（每日计数、每个念头的唤醒冷却）写在 `wake_state.json` 里，
所以 Scheduler 进程也**不需要**去改念头文件。
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.atomic_io import atomic_write_text
from v3.wake.wake_context import build_wake_context
from v3.wake.wake_policy import WakePolicy, clamp
from v3.wake.wake_reason import (
    CONTINUITY,
    CURIOSITY,
    INTENTION_DUE,
    INTEREST_DECAY_CHECK,
    RELATIONSHIP_EVENT,
    UNFINISHED_THOUGHT,
    can_lead_to_message,
    normalize_reason,
)
from v3.wake.wake_result import WakeLog, WakeResult

WAKE = "WAKE"
SKIP = "SKIP"


def _parse_time(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class WakeCandidate:
    reason: str
    wake_score: float
    thought_id: str = ""
    topic: str = ""
    content: str = ""
    eligible: bool = False
    blocked_by: str = ""
    components: dict = field(default_factory=dict)
    can_message: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WakeDecision:
    decision: str = SKIP
    reason: str = ""
    wake_score: float = 0.0
    candidate: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)
    blocked_by: str = ""
    context: dict = field(default_factory=dict)
    created: bool = False
    task: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class WakeManager:
    def __init__(
        self,
        *,
        thoughts=None,
        interests=None,
        continuity=None,
        wake_queue=None,
        policy: WakePolicy | None = None,
        trace=None,
        state_path: Path | None = None,
        log_path: Path | None = None,
        observations=None,
        now_fn=None,
    ) -> None:
        self.thoughts = thoughts
        self.interests = interests
        self.continuity = continuity
        self.wake_queue = wake_queue
        self.policy = policy or WakePolicy()
        self.trace = trace
        self.observations = observations
        self.log = WakeLog(log_path) if log_path else None
        self.state_path = Path(state_path) if state_path else None
        self._now = now_fn or datetime.datetime.now
        self._lock = threading.RLock()
        self.state: dict = {"date": "", "wakes_today": 0, "last_autonomous_at": "",
                            "thought_last_wake_at": {}}
        self._load_state()

    # ── 状态文件（只属于唤醒模块自己）────────────────────────────
    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.state.update(loaded)
        except (OSError, json.JSONDecodeError):
            logging.warning("[v3] wake_state.json 损坏，从零开始")

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            atomic_write_text(self.state_path,
                              json.dumps(self.state, ensure_ascii=False, indent=1))
        except OSError as exc:
            logging.warning("[v3] 写 wake_state 失败：%s", exc)

    def _roll_day(self, moment: datetime.datetime) -> None:
        today = moment.date().isoformat()
        if self.state.get("date") != today:
            self.state["date"] = today
            self.state["wakes_today"] = 0

    # ── 候选与评分（纯 Python，§六）──────────────────────────────
    def candidates(self, *, now: datetime.datetime | None = None) -> list:
        moment = now or self._now()
        seed_ids = []
        if self.continuity is not None:
            try:
                seed = self.continuity.load_seed()
                seed_ids = list(getattr(seed, "unfinished_thought_ids", []) or [])
            except Exception:  # noqa: BLE001
                seed_ids = []
        rows = []
        if self.thoughts is not None:
            try:
                rows = [t for t in self.thoughts.all() if not t.is_closed()]
            except Exception:  # noqa: BLE001
                rows = []
        result: list[WakeCandidate] = []
        for thought in rows:
            reasons = []
            if (thought.metadata or {}).get("pending_intention"):
                reasons.append(INTENTION_DUE)
            if thought.is_unfinished:
                reasons.append(UNFINISHED_THOUGHT)
            if float(thought.curiosity or 0.0) >= self.policy.curiosity_floor:
                reasons.append(CURIOSITY)
            if thought.id in seed_ids:
                reasons.append(CONTINUITY)
            if float(thought.relationship_relevance or 0.0) >= self.policy.relationship_floor:
                reasons.append(RELATIONSHIP_EVENT)
            if not reasons:
                continue
            for reason in reasons:
                result.append(self._score(thought, reason, seed_ids=seed_ids, now=moment))
        result.append(WakeCandidate(
            reason=INTEREST_DECAY_CHECK, wake_score=self.policy.time_recheck_score,
            topic="", content="（到点看看内部状态）",
            can_message=can_lead_to_message(INTEREST_DECAY_CHECK),
            components={"note": "时间只提供唤醒机会，本身永远不足以促成消息"},
        ))
        result.sort(key=lambda item: (-item.wake_score, item.thought_id))
        return result

    def _score(self, thought, reason: str, *, seed_ids: list, now: datetime.datetime) -> WakeCandidate:
        weights = self.policy.weights
        components = {
            "urgency": clamp(thought.urgency),
            "unfinished": 1.0 if thought.is_unfinished else 0.0,
            "curiosity": clamp(thought.curiosity),
            "motivation": clamp(thought.motivation),
            "novelty": clamp(thought.novelty),
            "continuity": 1.0 if thought.id in seed_ids else 0.0,
            "relationship_relevance": clamp(thought.relationship_relevance),
        }
        total = sum(components[name] * float(weights.get(name, 0.0)) for name in components)
        weight_sum = sum(float(weights.get(name, 0.0)) for name in components) or 1.0
        score = total / weight_sum
        if (thought.metadata or {}).get("pending_intention"):
            score += self.policy.pending_intention_bonus
        score -= self.policy.ignored_penalty * min(1.0, int(thought.times_ignored or 0) / 3.0)
        score = clamp(score)

        blocked = ""
        last = _parse_time((self.state.get("thought_last_wake_at") or {}).get(thought.id, ""))
        if last is not None:
            waited = (now - last).total_seconds() / 60.0
            if waited < self.policy.per_thought_cooldown_minutes:
                blocked = (f"这个念头 {int(waited)} 分钟前刚唤醒过"
                           f"（冷却 {self.policy.per_thought_cooldown_minutes} 分钟）")
        return WakeCandidate(
            reason=normalize_reason(reason), wake_score=round(score, 4),
            thought_id=thought.id, topic=thought.topic, content=str(thought.content)[:80],
            eligible=(not blocked and score >= self.policy.wake_threshold),
            blocked_by=blocked,
            components={k: round(v, 4) for k, v in components.items()},
            can_message=can_lead_to_message(reason),
        )

    # ── 决策与入队（§七）────────────────────────────────────────
    def evaluate(self, *, now: datetime.datetime | None = None) -> WakeDecision:
        moment = now or self._now()
        with self._lock:
            self._roll_day(moment)
            candidates = self.candidates(now=moment)
            best = candidates[0] if candidates else None
            decision = WakeDecision(
                decision=SKIP, candidates=[c.to_dict() for c in candidates[:5]],
                candidate=best.to_dict() if best else {}, wake_score=best.wake_score if best else 0.0,
            )
            if best is None:
                decision.reason = "没有可评估的候选"
                return decision
            decision.reason = f"最高候选来自 {best.reason}（{best.wake_score}）"
            if best.blocked_by:
                decision.blocked_by = best.blocked_by
                return decision
            if best.wake_score < self.policy.wake_threshold:
                decision.blocked_by = (f"唤醒分 {best.wake_score} 低于门槛 "
                                       f"{self.policy.wake_threshold}")
                return decision
            if int(self.state.get("wakes_today", 0)) >= self.policy.daily_wake_limit:
                decision.blocked_by = (f"今天自主唤醒已达上限 "
                                       f"{self.policy.daily_wake_limit} 次")
                return decision
            last = _parse_time(self.state.get("last_autonomous_at", ""))
            if last is not None:
                waited = (moment - last).total_seconds() / 60.0
                if waited < self.policy.min_gap_minutes:
                    decision.blocked_by = (f"距上次自主唤醒只过了 {int(waited)} 分钟"
                                           f"（最小间隔 {self.policy.min_gap_minutes} 分钟）")
                    return decision
            decision.decision = WAKE
            return decision

    def tick(self, *, now: datetime.datetime | None = None,
             queue_busy: bool = False) -> dict:
        """Scheduler 每轮调用：没有排队任务时，看看要不要自己醒来。"""
        moment = now or self._now()
        if queue_busy:
            return {"created": False, "decision": SKIP, "reason": "队列里已有待执行任务"}
        decision = self.evaluate(now=moment)
        if decision.decision != WAKE:
            self._trace("cognitive_skipped", reason="wake_skip",
                        detail=decision.blocked_by or decision.reason,
                        candidate=decision.candidate)
            return {"created": False, "decision": SKIP, "reason": decision.reason,
                    "blocked_by": decision.blocked_by, "candidates": decision.candidates}
        return self.request_wake(reason=decision.candidate.get("reason", ""),
                                 candidate=decision.candidate, now=moment,
                                 wake_score=decision.wake_score)

    def request_wake(self, *, reason: str, candidate: dict | None = None,
                     now: datetime.datetime | None = None, wake_score: float = 0.0) -> dict:
        """把唤醒排进 wake_queue（所有唤醒的统一入口）。"""
        moment = now or self._now()
        candidate = dict(candidate or {})
        with self._lock:
            self._roll_day(moment)
        if self.wake_queue is None:
            return {"created": False, "reason": "没有队列"}
        context = build_wake_context(
            wake_id="", reason=reason, thoughts=self.thoughts, interests=self.interests,
            continuity_seed=(self.continuity.load_seed() if self.continuity else None),
            budget={"wakes_today": self.state.get("wakes_today", 0),
                    "daily_wake_limit": self.policy.daily_wake_limit},
            now=moment, extra={"wake_score": wake_score, "candidate": candidate},
        )
        task = self.wake_queue.enqueue(
            reason=f"autonomous:{reason}", earliest_at=moment.isoformat(timespec="seconds"),
            priority="normal", cycle_id="",
        )
        if not task:
            return {"created": False, "reason": "入队失败（队列被占用）"}
        with self._lock:
            self.state["wakes_today"] = int(self.state.get("wakes_today", 0)) + 1
            self.state["last_autonomous_at"] = moment.isoformat(timespec="seconds")
            if candidate.get("thought_id"):
                mapping = dict(self.state.get("thought_last_wake_at") or {})
                mapping[candidate["thought_id"]] = moment.isoformat(timespec="seconds")
                self.state["thought_last_wake_at"] = mapping
            self._save_state()
        self._trace("checkpoint_started", reason="autonomous_wake", detail=reason,
                    candidate=candidate, task_id=task.get("id"), wake_score=wake_score)
        return {"created": True, "decision": WAKE, "reason": reason, "task": task,
                "wake_score": wake_score, "context": context,
                "candidates": [candidate] if candidate else []}

    # ── 结果回填（cycle_runner 执行完一轮后调用）────────────────
    def record_result(self, result: WakeResult) -> dict:
        if not result.created_at:
            result.created_at = result.started_at or (self._now()).isoformat(timespec="seconds")
        if not result.finished_at:
            result.finished_at = (self._now()).isoformat(timespec="seconds")
        if self.log is None:
            return result.to_dict()
        return self.log.record(result)

    # ── 只读查询 ────────────────────────────────────────────────
    def status(self, *, now: datetime.datetime | None = None) -> dict:
        moment = now or self._now()
        with self._lock:
            self._roll_day(moment)
            return {
                "policy": self.policy.to_dict(),
                "wakes_today": int(self.state.get("wakes_today", 0)),
                "daily_wake_limit": self.policy.daily_wake_limit,
                "last_autonomous_at": self.state.get("last_autonomous_at", ""),
                "history": self.log.count() if self.log else 0,
                "reasons": self.log.summary() if self.log else {"total": 0, "by_reason": {}},
            }

    def recent(self, limit: int = 5) -> list:
        return self.log.recent(limit=limit) if self.log else []

    def last(self) -> dict:
        rows = self.recent(limit=1)
        return rows[-1] if rows else {}

    def _trace(self, kind: str, *, trace_id: str = "", **fields) -> None:
        if self.trace is None:
            return
        try:
            self.trace.record(kind, trace_id=trace_id, **fields)
        except Exception as exc:  # noqa: BLE001 - 观察失败不能影响唤醒判断
            logging.debug("[v3] wake trace 失败（忽略）：%s", exc)
