"""LifeCycle：Phase 6 的自主行为闭环（§一/§三十/§三十四）。

    Environment -> Observation -> Thought Dynamics -> Checkpoint -> Threshold
        -> (未达阈值) 什么都不做，状态继续演化
        -> (达到阈值) Cognitive Provider -> Proposal -> Decision
           -> ActionValidator -> Action -> Outcome -> Feedback -> 下一轮

三条不可动摇的原则：
  1. LLM 只是"提议者/认知器官"：它的输出必须经 validator 收窄，再由
     DecisionEngine（纯 Python）决定做什么；LLM 不能直接触发行动。
  2. 检查点便宜、LLM 昂贵：每个检查点只做 Python 计算；只有达到认知阈值
     才可能调用一次模型（还受预算与冷却限制）。
  3. 行动接口化：Core 只产生 ActionRequest，交给 ActionProvider；
     Telegram 只是 environments 里的一个实现。
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from v3.actions.base import MESSAGE, NO_ACTION, ActionRequest, Outcome, STATUS_REJECTED
from v3.continuity.models import ContinuitySeed, CycleRecord
from v3.continuity.rules import WAKE_NORMAL, plan_next_wake
from v3.thought.models import STATE_ACTIVE, Thought
from v3.thought.novelty import compute_information_gain, compute_novelty
from v3.wake.wake_reason import AUTONOMOUS_REASONS


@dataclass
class LifeCycleRecord:
    cycle_id: str
    trace_id: str
    trigger: str
    mode: str
    wake_reason: str = ""
    ran: bool = True
    skip_reason: str = ""
    threshold_reached: bool = False     # 检查点判定"够格深思"
    cognition_ran: bool = False         # 认知器官真的被调用了
    checkpoint: dict = field(default_factory=dict)
    cognitive: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    feedback: dict = field(default_factory=dict)
    thoughts_created: int = 0
    thoughts_rejected: int = 0
    interests: list = field(default_factory=list)
    llm_calls: int = 0
    info_gain: float = 0.0
    reason_code: str = ""
    latency_ms: int = 0
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class LifeCycle:
    def __init__(
        self,
        *,
        runtime=None,
        budget=None,
        checkpoint,
        context_builder,
        provider,
        decision_engine,
        registry=None,
        validator=None,
        action_budget=None,
        outcomes=None,
        feedback=None,
        thoughts=None,
        dynamics=None,
        interests=None,
        interest_engine=None,
        continuity=None,
        journal=None,
        wake_queue=None,
        trace=None,
        observations=None,
        former=None,
        environment=None,
        chat_id=None,
        log_path=None,
        keep: int = 300,
        now_fn=None,
    ) -> None:
        self.runtime = runtime
        self.budget = budget
        self.checkpoint = checkpoint
        self.context_builder = context_builder
        self.provider = provider
        self.decision_engine = decision_engine
        self.registry = registry
        self.validator = validator
        self.action_budget = action_budget
        self.outcomes = outcomes
        self.feedback = feedback
        self.thoughts = thoughts
        self.dynamics = dynamics
        self.interests = interests
        self.interest_engine = interest_engine
        self.continuity = continuity
        self.journal = journal
        self.wake_queue = wake_queue
        self.trace = trace
        self.observations = observations
        self.former = former
        self.environment = environment
        self.chat_id = chat_id
        self._now = now_fn or datetime.datetime.now
        self._counter = int(continuity.stats().get("cycles", 0)) if continuity is not None else 0
        self._lock = __import__("threading").RLock()
        self._records: list = []
        self.keep = max(20, int(keep))
        self.log_path = log_path
        if self.log_path is not None:
            self._records = self._read_log()[-self.keep:]

    # ── 主循环 ──────────────────────────────────────────────────
    def run(self, *, trigger: str = "scheduler", wake_reason: str = "",
            now: datetime.datetime | None = None) -> dict:
        moment = now or self._now()
        started = time.time()
        mode = self.runtime.mode() if self.runtime is not None else "observe"

        if self.budget is not None:
            allowed, why = self.budget.can_cycle()
            if not allowed:
                logging.info("[v3] 生命循环被预算拒绝：%s", why)
                self._trace("cognitive_skipped", reason="budget_exhausted", detail=why,
                            phase="cycle")
                return {"ran": False, "skip_reason": why, "reason_code": "budget_exhausted",
                        "mode": mode}
            self.budget.spend_cycle()
            self.budget.begin_chain()

        self._counter += 1
        cycle_id = f"cy_{moment.strftime('%Y%m%d%H%M%S')}_{self._counter:04d}"
        record = LifeCycleRecord(
            cycle_id=cycle_id, trace_id="", trigger=trigger, mode=mode,
            wake_reason=str(wake_reason or ""),
            created_at=moment.isoformat(timespec="seconds"),
        )

        # 0) 观测 → 念头形成（Phase 6 §一 的廉价一步，纯 Python，0 token）
        formed = []
        if self.former is not None:
            formed = self.former.form(now=moment, cycle_id=cycle_id)
            for thought in formed:
                self._trace("thought_created", trace_id="", cycle_id=cycle_id,
                            thought_id=thought.id, content=thought.content,
                            topic=thought.topic, source="observation")
            record.thoughts_created = len(formed)
            self._update_interests(formed, record, cycle_id=cycle_id)

        # 1) 检查点（便宜、确定性、绝不调模型）
        checkpoint = self.checkpoint.tick(
            now=moment, interaction_gap_hours=self._interaction_gap_hours(moment))
        record.trace_id = checkpoint.trace_id
        record.threshold_reached = bool(checkpoint.triggered)
        record.checkpoint = {"checkpoint_id": checkpoint.checkpoint_id,
                             "band": checkpoint.band, "triggered": checkpoint.triggered,
                             "top": checkpoint.top, "reason": checkpoint.reason,
                             "evaluated": checkpoint.evaluated}

        if not checkpoint.triggered or not checkpoint.top:
            record.skip_reason = checkpoint.reason or "没有达到认知阈值的念头"
            record.reason_code = "threshold_not_met"
            record.decision = {"action": NO_ACTION, "reason": record.skip_reason,
                               "thought_id": (checkpoint.top or {}).get("thought_id", "")}
            self._finish(record, moment, started, decision_action=NO_ACTION)
            return self._result(record, checkpoint)

        # 2) 认知（昂贵）：预算 + 冷却 + 接口调用
        if self.provider is None or not self.provider.available():
            record.skip_reason = "没有可用的认知器官"
            record.reason_code = "no_provider"
            record.decision = {"action": NO_ACTION, "reason": record.skip_reason}
            self._finish(record, moment, started, decision_action=NO_ACTION)
            return self._result(record, checkpoint)
        if self.action_budget is not None:
            allowed, why = self.action_budget.can_cognize(now=moment)
            if not allowed:
                record.skip_reason = why
                record.reason_code = "cooldown" if "分钟" in why else "budget_exhausted"
                record.decision = {"action": NO_ACTION, "reason": why}
                self._trace("cognitive_skipped", trace_id=checkpoint.trace_id,
                            reason=record.reason_code, detail=why)
                self._finish(record, moment, started, decision_action=NO_ACTION)
                return self._result(record, checkpoint)

        context = self.context_builder.build(
            candidate=checkpoint.top, trace_id=checkpoint.trace_id, cycle_id=cycle_id,
            environment=self._environment_state(), now=moment,
        )
        result = self.provider.process(context)
        record.cognition_ran = True
        if self.action_budget is not None:
            self.action_budget.note_cognitive(now=moment)
        record.cognitive = result.to_dict()
        record.llm_calls = int(result.llm_calls or 0)
        self._trace("cognitive_result", trace_id=checkpoint.trace_id, cycle_id=cycle_id,
                    thought_id=checkpoint.top.get("thought_id", ""),
                    ok=result.ok, intention=result.intention.to_dict(),
                    error=result.error, rejected=result.rejected)

        candidate_thought = None
        if self.thoughts is not None:
            candidate_thought = self.thoughts.get(checkpoint.top.get("thought_id", ""))

        # 3) 应用提案（Python 裁决：hint 只按比例融合）
        if candidate_thought is not None and self.dynamics is not None:
            changed = self.dynamics.apply_hints(candidate_thought, result.thought_update)
            self.thoughts.update(candidate_thought)
            if changed:
                self._trace("thought_updated", trace_id=checkpoint.trace_id,
                            thought_id=candidate_thought.id, changes=changed)
        source = ("autonomous_wake"
                  if record.wake_reason in AUTONOMOUS_REASONS else "cognition")
        created = self._create_thoughts(result, cycle_id=cycle_id, moment=moment,
                                        trace_id=checkpoint.trace_id, source=source)
        record.thoughts_created += len(created)
        record.thoughts_rejected = len(result.rejected)
        self._update_interests(created, record, cycle_id=cycle_id)

        # 4) 决策（纯 Python；LLM 说了不算）
        decision = self.decision_engine.decide(
            candidate=checkpoint.top, result=result,
            availability=self._availability(now=moment), now=moment)
        record.decision = decision.to_dict()
        self._trace("decision_made", trace_id=checkpoint.trace_id, cycle_id=cycle_id,
                    thought_id=decision.thought_id, action=decision.action,
                    reason=decision.reason, would_action=decision.would_action,
                    trigger_score=decision.trigger_score, confidence=decision.confidence)

        # 5) 行动 + 校验 + 结果
        outcome = self._maybe_act(decision, record, cycle_id=cycle_id, moment=moment)
        self._track_pending_intention(decision, candidate_thought, cycle_id=cycle_id)

        # 6) 反馈
        if outcome is not None and self.feedback is not None:
            expected = float(candidate_thought.activation) if candidate_thought else 0.5
            record.feedback = self.feedback.apply(
                outcome=outcome, thought=candidate_thought, trace_id=checkpoint.trace_id,
                expected=expected)
            if self.thoughts is not None and candidate_thought is not None:
                self.thoughts.update(candidate_thought)

        self._finish(record, moment, started, decision_action=decision.action)
        return self._result(record, checkpoint)

    # ── 行动 ────────────────────────────────────────────────────
    def _maybe_act(self, decision, record: LifeCycleRecord, *, cycle_id: str,
                   moment: datetime.datetime):
        if decision.action == NO_ACTION or self.registry is None:
            return None
        action_id = f"act_{moment.strftime('%Y%m%d%H%M%S')}_{decision.thought_id or 'x'}"
        why = {
            "trigger": ("autonomous_wake" if record.wake_reason in AUTONOMOUS_REASONS
                        else (record.wake_reason or "cognition")),
            "wake_reason": record.wake_reason,
            "thought_ids": [decision.thought_id] if decision.thought_id else [],
            "topic": (record.checkpoint.get("top", {}) or {}).get("topic", ""),
            "motivation": (record.checkpoint.get("top", {}) or {}).get("motivation", 0.0),
            "expected_reward": (record.checkpoint.get("top", {}) or {}).get("trigger_score", 0.0),
            "intention_confidence": decision.confidence,
            "reason": decision.reason,
            "decision": decision.action,
        }
        request = ActionRequest(
            action_id=action_id, kind=decision.action,
            payload={"content": decision.content, "chat_id": self.chat_id, "why": why},
            thought_id=decision.thought_id, trace_id=decision.trace_id, cycle_id=cycle_id,
            reason=decision.reason,
        )
        record.action = request.to_dict()
        self._trace("action_requested", trace_id=request.trace_id, action_id=action_id,
                    cycle_id=cycle_id, thought_id=request.thought_id,
                    action_kind=request.kind,
                    payload=request.payload, reason=request.reason)

        verdict = None
        if self.validator is not None:
            verdict = self.validator.check(request, now=moment)
            self._trace("action_validated" if verdict.ok else "action_rejected",
                        trace_id=request.trace_id, action_id=action_id,
                        action_kind=request.kind, ok=verdict.ok, reason=verdict.reason)
        elif request.is_outward:
            verdict = type("V", (), {"ok": False, "reason": "没有 ActionValidator"})()

        if verdict is not None and not verdict.ok:
            outcome = Outcome(action_id=action_id, kind=request.kind, status=STATUS_REJECTED,
                              error=verdict.reason, trace_id=request.trace_id)
        else:
            outcome = self.registry.execute(request)
            outcome.trace_id = request.trace_id
            self._trace("action_executed", trace_id=request.trace_id, action_id=action_id,
                        action_kind=outcome.kind, status=outcome.status,
                        simulated=outcome.simulated,
                        error=outcome.error)
            if outcome.kind == MESSAGE and outcome.executed and self.action_budget is not None:
                self.action_budget.note_message(now=moment)
        if self.outcomes is not None:
            self.outcomes.record(outcome)
        record.outcome = outcome.to_dict()
        self._trace("outcome_received", trace_id=request.trace_id, action_id=action_id,
                    status=outcome.status, simulated=outcome.simulated)
        return outcome

    def _track_pending_intention(self, decision, candidate_thought, *, cycle_id: str) -> None:
        """想说话但没能说出去（冷却/预算/档位）→ 记为"未兑现的意向"，下次唤醒继续。"""
        if candidate_thought is None or self.thoughts is None:
            return
        metadata = dict(candidate_thought.metadata or {})
        intended = str(getattr(decision, "would_action", "") or "") == MESSAGE
        executed = str(getattr(decision, "action", "") or "") == MESSAGE
        if intended and not executed:
            metadata["pending_intention"] = {
                "kind": MESSAGE, "content": str(getattr(decision, "content", ""))[:200],
                "reason": str(getattr(decision, "reason", ""))[:200],
                "cycle_id": cycle_id,
            }
            candidate_thought.metadata = metadata
            self.thoughts.update(candidate_thought)
            self._trace("state_changed", cycle_id=cycle_id, thought_id=candidate_thought.id,
                        change="pending_intention_saved",
                        detail=metadata["pending_intention"])
        elif executed and metadata.get("pending_intention"):
            metadata.pop("pending_intention", None)
            candidate_thought.metadata = metadata
            self.thoughts.update(candidate_thought)

    def _create_thoughts(self, result, *, cycle_id: str, moment: datetime.datetime,
                         trace_id: str, source: str = "cognition") -> list:
        if self.thoughts is None:
            return []
        created = []
        recent = self.thoughts.recent_contents(limit=20)
        for item in result.new_thoughts or []:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            topic = str(item.get("topic", "") or content[:6])
            importance = float(item.get("importance_hint", 0.5) or 0.5)
            novelty = compute_novelty(content, recent_contents=recent)
            gain = compute_information_gain(content, recent_contents=recent,
                                            topic_seen_count=1, observation_is_new=True)
            thought = Thought(
                id="", type=item.get("type", "observation"), content=content,
                evidence=list(item.get("evidence") or []), source=source,
                topic=topic, is_unfinished=bool(item.get("is_unfinished")),
                novelty=novelty, information_gain=gain,
                score=round(0.6 * gain + 0.4 * novelty, 4),
                importance=max(0.0, min(1.0, importance)),
                curiosity=float(item.get("curiosity_hint", 0.5) or 0.5),
                urgency=float(item.get("urgency_hint", 0.0) or 0.0),
                confidence=float(item.get("confidence_hint", 0.5) or 0.5),
                valence=float(item.get("valence_hint", 0.0) or 0.0),
                relationship_relevance=float(item.get("relationship_relevance_hint", 0.0) or 0.0),
                activation=0.5, persistence=0.5,
                created_at=moment.isoformat(timespec="seconds"),
                last_seen_at=moment.isoformat(timespec="seconds"),
                updated_at=moment.isoformat(timespec="seconds"),
                cycle_id=cycle_id,
            )
            thought.set_state(STATE_ACTIVE if thought.is_unfinished else thought.lifecycle_state)
            saved, action = self.thoughts.add(thought)
            if action == "created":
                created.append(saved)
                self._trace("thought_created", trace_id=trace_id, cycle_id=cycle_id,
                            thought_id=saved.id, content=saved.content, topic=saved.topic,
                            importance=saved.importance, novelty=novelty, info_gain=gain)
        return created

    # ── 收尾：连续性 seed + 下一次唤醒 + 记录 ────────────────────
    def _finish(self, record: LifeCycleRecord, moment: datetime.datetime, started: float,
                *, decision_action: str) -> None:
        record.latency_ms = int((time.time() - started) * 1000)
        seed = None
        if self.continuity is not None:
            unfinished = self.thoughts.unfinished(limit=5) if self.thoughts is not None else []
            seed = ContinuitySeed(
                seed_id=f"seed_{record.cycle_id}", cycle_id=record.cycle_id,
                unfinished_thought_ids=[t.id for t in unfinished],
                active_topic=(record.checkpoint.get("top", {}) or {}).get("topic", ""),
                hint=(record.checkpoint.get("top", {}) or {}).get("content", ""),
                created_at=moment.isoformat(timespec="seconds"),
            )
            self.continuity.save_seed(seed)
        if self.wake_queue is not None:
            plan = plan_next_wake(
                now=moment,
                min_interval_minutes=int(self.runtime.limit_int("V3_MIN_WAKE_INTERVAL_MINUTES", 60))
                if self.runtime else 60,
                max_silence_hours=int(self.runtime.limit_int("V3_MAX_SILENCE_HOURS", 48))
                if self.runtime else 48,
                priority=WAKE_NORMAL,
                has_unfinished=bool(seed.unfinished_thought_ids) if seed else False,
                has_gain=record.info_gain >= 0.2,
                no_action=(decision_action == NO_ACTION),
            )
            max_attempts = int(self.runtime.limit_int("V3_MAX_ATTEMPTS", 3)) if self.runtime else 3
            self.wake_queue.enqueue(reason="life_cycle", earliest_at=plan["earliest_at"],
                                    priority=plan["priority"], cycle_id=record.cycle_id,
                                    max_attempts=max_attempts)
        if self.continuity is not None:
            self.continuity.append_cycle(CycleRecord(
                cycle_id=record.cycle_id, trigger=record.trigger, mode=record.mode,
                thoughts_created=record.thoughts_created,
                thoughts_rejected=record.thoughts_rejected,
                info_gain=record.info_gain,
                decision=record.decision or {"action": NO_ACTION, "reason": record.skip_reason},
                llm_calls=record.llm_calls, skip_reason=record.skip_reason,
                latency_ms=record.latency_ms, created_at=record.created_at,
            ))
        self._store_record(record)

    def _store_record(self, record: LifeCycleRecord) -> None:
        with self._lock:
            self._records.append(record.to_dict())
            self._records = self._records[-self.keep:]
            snapshot = list(self._records)
        if self.log_path is None:
            return
        try:
            import json
            from core.atomic_io import atomic_write_text

            atomic_write_text(self.log_path,
                              "\n".join(json.dumps(item, ensure_ascii=False) for item in snapshot) + "\n")
        except OSError as exc:
            logging.warning("[v3] 写 life_cycle 记录失败：%s", exc)

    def _read_log(self) -> list:
        """读盘上的记录（跨越 bot 进程与 cycle_runner 进程）。"""
        if self.log_path is None or not Path(self.log_path).is_file():
            return []
        try:
            import json
            return [json.loads(line) for line in
                    Path(self.log_path).read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            return []

    # ── 辅助 ────────────────────────────────────────────────────
    def _update_interests(self, thoughts, record: LifeCycleRecord, *, cycle_id: str) -> None:
        """新念头落地时更新对应话题的兴趣（限步长 + 边际衰减，规则在 InterestEngine 里）。"""
        if self.interest_engine is None or not thoughts:
            return
        valence = self._valence_signal(self.observations)
        for thought in thoughts:
            topic = thought.topic or (thought.content[:6] if thought.content else "")
            if not topic:
                continue
            try:
                _, detail = self.interest_engine.update(
                    topic, information_gain=float(thought.information_gain or 0.0),
                    valence_signal=valence, evidence_delta=1,
                )
            except Exception as exc:  # noqa: BLE001 - 兴趣更新失败不能中断生命循环
                logging.warning("[v3] 兴趣更新失败：%s", exc)
                continue
            record.interests.append(detail)
            self._trace("state_changed", trace_id="", cycle_id=cycle_id,
                        thought_id=thought.id, change="interest_updated", detail=detail)

    @staticmethod
    def _valence_signal(observations) -> float:
        """从最近观测的情绪 delta 里取一个正负号（和 Phase 5 的规则一致）。"""
        if observations is None:
            return 0.0
        try:
            rows = observations.recent(limit=5)
        except Exception:  # noqa: BLE001
            return 0.0
        positive = 0.0
        negative = 0.0
        for item in rows or []:
            for key, value in (item.get("emotion_delta") or {}).items():
                try:
                    delta = float(value)
                except (TypeError, ValueError):
                    continue
                if key in ("joy", "interest", "affection", "trust", "excitement", "calm") and delta > 0:
                    positive += delta
                elif key in ("anger", "sadness", "anxiety", "loneliness") and delta > 0:
                    negative += delta
        if positive == 0 and negative == 0:
            return 0.0
        return max(-1.0, min(1.0, (positive - negative) / max(0.1, positive + negative)))

    def _result(self, record: LifeCycleRecord, checkpoint) -> dict:
        return {
            "ran": True, "mode": record.mode, "cycle_id": record.cycle_id,
            "trace_id": record.trace_id,
            "triggered": record.cognition_ran,
            "threshold_reached": record.threshold_reached,
            "skip_reason": record.skip_reason, "reason_code": record.reason_code,
            "cycle": record.to_dict(),
            "next_wake": {"earliest_at": self._last_wake_at()},
        }

    def _last_wake_at(self) -> str:
        if self.wake_queue is None:
            return ""
        items = [str(item.get("earliest_at", "")) for item in self.wake_queue.all()
                 if item.get("status") == "PENDING"]
        return min(items) if items else ""

    def _interaction_gap_hours(self, moment: datetime.datetime) -> float:
        if self.observations is None:
            return 0.0
        recent = self.observations.recent(limit=1)
        if not recent:
            return 0.0
        try:
            last = datetime.datetime.fromisoformat(str(recent[-1].get("timestamp", "")))
        except ValueError:
            return 0.0
        return max(0.0, (moment - last).total_seconds() / 3600.0)

    def _environment_state(self) -> dict:
        if self.environment is None:
            return {"name": "none", "available": False}
        return {"name": getattr(self.environment, "name", "unknown"),
                "available": bool(self.environment.available()),
                "mode": self.runtime.mode() if self.runtime is not None else "observe"}

    def _availability(self, *, now: datetime.datetime | None = None) -> dict:
        provider = self.registry.get(MESSAGE) if self.registry is not None else None
        env_ok = True
        if self.environment is not None:
            env_ok = bool(self.environment.available())
        available = bool(provider is not None and provider.available() and env_ok)
        reason = "" if available else "消息行动或环境不可用"
        # 冷却/预算也是"能不能说话"的条件（Phase 6 §九：Cooldown/Budget 必须 PASS）
        if available and self.action_budget is not None:
            allowed, why = self.action_budget.can_message(now=now)
            if not allowed:
                available, reason = False, why
        return {"message_available": available,
                "message_reason": reason,
                "actions": list(self.registry.kinds()) if self.registry is not None else []}

    def _trace(self, kind: str, *, trace_id: str = "", **fields) -> None:
        if self.trace is None:
            return
        try:
            self.trace.record(kind, trace_id=trace_id, **fields)
        except Exception as exc:  # noqa: BLE001 - 观察失败不能影响生命循环
            logging.debug("[v3] trace 记录失败（忽略）：%s", exc)

    # ── 只读查询（给命令用）─────────────────────────────────────
    def recent(self, limit: int = 5) -> list:
        """最近的生命循环记录（优先读盘，这样能同时看到 cycle_runner 跑的那些）。"""
        rows = self._read_log() or list(self._records)
        return [dict(item) for item in rows[-max(1, int(limit)):]]

    def by_action(self, action_id: str) -> dict:
        if not action_id:
            return {}
        for item in reversed(self.recent(limit=self.keep)):
            if str((item.get("action") or {}).get("action_id", "")) == str(action_id):
                return item
        return {}
