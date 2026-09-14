"""CognitiveCycle：一次完整的内在认知（wake -> recall -> think -> decide -> seed -> sleep）。

铁律：
  - 全程不向用户发送任何消息（结果只进 inner_journal）；
  - LLM 只在 thought / 可选 evaluator 处调用，且先过 Policy + V3 预算；
  - 所有状态变更都经 Validator，未通过的直接丢弃。
"""

from __future__ import annotations

import datetime
import logging
import time

from v3.continuity.models import ContinuitySeed, CycleRecord
from v3.continuity.rules import WAKE_NORMAL, plan_next_wake
from v3.thought.models import Thought
from v3.thought.novelty import compute_information_gain, compute_novelty


class CognitiveCycle:
    def __init__(
        self,
        *,
        runtime=None,
        budget,
        thoughts,
        lifecycle,
        generator,
        interests,
        interest_engine,
        motivation_engine,
        decision_engine,
        reward_engine,
        continuity,
        observations,
        journal,
        wake_queue=None,
        bus=None,
        now_fn=None,
    ) -> None:
        self.runtime = runtime
        self.budget = budget
        self.thoughts = thoughts
        self.lifecycle = lifecycle
        self.generator = generator
        self.interests = interests
        self.interest_engine = interest_engine
        self.motivation_engine = motivation_engine
        self.decision_engine = decision_engine
        self.reward_engine = reward_engine
        self.continuity = continuity
        self.observations = observations
        self.journal = journal
        self.wake_queue = wake_queue
        self.bus = bus
        self._now = now_fn or datetime.datetime.now
        self._counter = int(continuity.stats().get("cycles", 0))

    # ── 单次循环 ────────────────────────────────────────────────────
    def run(self, *, trigger: str = "scheduler", now: datetime.datetime | None = None) -> dict:
        moment = now or self._now()
        started = time.time()
        mode = self.runtime.mode() if self.runtime is not None else "observe"

        allowed, why = self.budget.can_cycle()
        if not allowed:
            logging.info("[v3] cycle 被预算拒绝：%s", why)
            return {"ran": False, "skip_reason": why, "mode": mode}
        self.budget.spend_cycle()
        self.budget.begin_chain()   # 链长是"单轮"限制：每轮开始时复位

        self._counter += 1
        cycle_id = f"cy_{moment.strftime('%Y%m%d%H%M%S')}_{self._counter:04d}"
        seed_before = self.continuity.load_seed()
        record = CycleRecord(cycle_id=cycle_id, trigger=trigger, mode=mode)

        # 1. recall：未完成念头（跨周期连续性）
        unfinished_before = self.thoughts.unfinished(limit=5)
        # 2. perceive：最近未消费的观测（读过即标记，避免同一素材被反复"发现"）
        observations = self.observations.recent(limit=8, unconsumed_only=True)
        has_new_observations = bool(observations)
        if not observations:
            observations = self.observations.recent(limit=8)

        # 3. think：LLM 生成候选（受 Policy + 预算 + 链长），未通过的丢弃
        llm_before = int(self.budget.data.get("llm_calls", 0))
        accepted, note, rejected_count = self.generator.generate(observations)
        record.llm_calls = max(0, int(self.budget.data.get("llm_calls", 0)) - llm_before)
        logging.info("[v3] thought 生成：%s", note)
        if has_new_observations:
            self.observations.mark_consumed(cycle_id)

        created: list[Thought] = []
        for candidate in accepted:
            topic = str(candidate.get("topic") or "").strip()[:40]
            existing_interest = self.interests.get(topic) if topic else None
            seen = int(existing_interest.evidence) if existing_interest else 0
            content = candidate["content"]
            novelty = compute_novelty(content, recent_contents=self.thoughts.recent_contents(limit=20))
            gain = compute_information_gain(
                content,
                recent_contents=self.thoughts.recent_contents(limit=20),
                topic_seen_count=max(1, seen + 1),
                observation_is_new=True,
            )
            thought = Thought(
                id="",
                type=candidate["type"],
                content=content,
                evidence=candidate["evidence"],
                source="conversation",
                status="ephemeral",
                is_unfinished=bool(candidate.get("is_unfinished")),
                novelty=novelty,
                information_gain=gain,
                score=round(0.6 * gain + 0.4 * novelty, 4),
                topic=topic or content[:6],
                created_at=moment.isoformat(timespec="seconds"),
                last_seen_at=moment.isoformat(timespec="seconds"),
                cycle_id=cycle_id,
            )
            saved, action = self.thoughts.add(thought)
            if saved.is_unfinished or saved.seen_count >= 2:
                self.lifecycle.promote(saved.id)
                saved = self.thoughts.get(saved.id) or saved
            if action == "created":
                created.append(saved)
                if self.bus is not None:
                    try:
                        self.bus.publish("v3:thought_created", thought_id=saved.id, type=saved.type,
                                         content=saved.content, evidence=list(saved.evidence),
                                         created_at=saved.created_at, is_unfinished=saved.is_unfinished)
                    except Exception:  # noqa: BLE001
                        logging.debug("[v3] 事件发布失败（忽略）")

        record.thoughts_created = len(created)
        record.thoughts_rejected = int(rejected_count) + max(0, len(accepted) - len(created))

        # 4. 兴趣更新（步长受限 + 边际衰减）；正负由观测里的情绪 delta 决定
        valence_signal = self._valence_signal(observations)
        for thought in created:
            _, detail = self.interest_engine.update(
                thought.topic or thought.content[:6],
                information_gain=thought.information_gain,
                valence_signal=valence_signal,
                evidence_delta=1,
            )
            record.interest_changes.append(detail)
            if self.bus is not None:
                try:
                    self.bus.publish("v3:interest_updated", topic=detail["topic"], old_state={},
                                     new_state=detail, trigger_cycle_id=cycle_id)
                except Exception:  # noqa: BLE001
                    logging.debug("[v3] 事件发布失败（忽略）")

        # 5. motivation（纯 Python）：先看这一轮新产生的念头，没有新念头才回落到召回的旧念头
        #    —— 否则一个很久以前的高分念头会永远把动机顶在高位，NoAction 就再也不会出现
        active = self.thoughts.recall(limit=5)
        gap_hours = self._interaction_gap_hours(moment)
        motivation = self.motivation_engine.evaluate(
            thoughts=created or active, interests=self.interests.all(), interaction_gap_hours=gap_hours
        )
        record.motivation = motivation.to_dict()

        # 6. decision（MVP 只有 NoAction / JOURNAL_NOTE）
        best_thought = (created or active or [None])[0]
        decision = self.decision_engine.decide(motivation=motivation, thought=best_thought)
        record.decision = decision.to_dict()
        logging.info("[v3] decision=%s reason=%s", decision.action, decision.reason)

        # 7. reward + prediction error（objective 主导；LLM 评分默认关闭）
        info_gain_avg = round(
            sum(t.information_gain for t in created) / len(created), 4
        ) if created else 0.0
        unfinished_progress = 1.0 if (unfinished_before and created) else 0.0
        behavioral = self._behavioral_signal(observations)
        continuity_signal = 0.8 if (seed_before and seed_before.unfinished_thought_ids) else 0.4
        # 期望基线取中立值，而不是"话题当前好感"：
        # 否则好感越高、预测误差越负，正向对话反而会把兴趣压回去（自相矛盾）。
        # 用中立基线后，拿不到新信息的轮次自然为负、有新信息的轮次为正，兴趣才会收敛而不是振荡。
        expected = 0.5
        reward = self.reward_engine.evaluate(
            information_gain=info_gain_avg,
            unfinished_progress=unfinished_progress,
            behavioral_signal=behavioral,
            continuity_signal=continuity_signal,
            topic_repeat_count=max(1, len(created)),
            expected_reward=expected,
        )
        record.reward = reward.to_dict()
        record.info_gain = info_gain_avg

        # 8. 预测误差回灌兴趣（仍然限步长）
        if best_thought is not None and getattr(best_thought, "topic", ""):
            _, detail = self.interest_engine.update(
                best_thought.topic,
                information_gain=abs(reward.prediction_error),
                valence_signal=1.0 if reward.prediction_error >= 0 else -1.0,
                evidence_delta=0,
                is_new_observation=False,
            )
            record.interest_changes.append(detail)

        # 9. inner_journal（唯一的输出通道）
        self.journal.write(
            cycle_id=cycle_id,
            kind=decision.action.lower(),
            content=decision.content or decision.reason,
            meta={
                "trigger": trigger,
                "mode": mode,
                "motivation": motivation.score,
                "info_gain": info_gain_avg,
                "reward": reward.reward,
                "prediction_error": reward.prediction_error,
                "thoughts": [t.content for t in created][:3],
            },
            now=moment,
        )

        # 10. continuity seed + 下一次唤醒（硬下限钳位；NoAction 不许 High）
        unfinished_after = self.thoughts.unfinished(limit=5)
        seed = ContinuitySeed(
            seed_id=f"seed_{cycle_id}",
            cycle_id=cycle_id,
            unfinished_thought_ids=[t.id for t in unfinished_after],
            active_topic=(created[0].topic if created else (active[0].topic if active else "")),
            next_wake_hint="",
            hint=(created[0].content if created else ""),
            created_at=moment.isoformat(timespec="seconds"),
        )
        self.continuity.save_seed(seed)

        plan = plan_next_wake(
            now=moment,
            min_interval_minutes=int(self.runtime.limit_int("V3_MIN_WAKE_INTERVAL_MINUTES", 60)) if self.runtime else 60,
            max_silence_hours=int(self.runtime.limit_int("V3_MAX_SILENCE_HOURS", 48)) if self.runtime else 48,
            priority=WAKE_NORMAL,
            has_unfinished=bool(unfinished_after),
            has_gain=info_gain_avg >= 0.2,
            no_action=(decision.action == "NO_ACTION"),
        )
        if self.wake_queue is not None:
            max_attempts = int(self.runtime.limit_int("V3_MAX_ATTEMPTS", 3)) if self.runtime else 3
            self.wake_queue.enqueue(reason="continuity_seed", earliest_at=plan["earliest_at"],
                                    priority=plan["priority"], cycle_id=cycle_id,
                                    max_attempts=max_attempts)
        record.latency_ms = int((time.time() - started) * 1000)
        self.continuity.append_cycle(record)
        if self.bus is not None:
            try:
                self.bus.publish("v3:cycle_completed", cycle_id=cycle_id, info_gain=info_gain_avg,
                                 actions_taken=0, seed_saved=True,
                                 next_wake_hint=plan["earliest_at"])
            except Exception:  # noqa: BLE001
                logging.debug("[v3] 事件发布失败（忽略）")
        return {"ran": True, "cycle": record.to_dict(), "next_wake": plan, "seed": seed.to_dict()}

    # ── 辅助信号（全部确定性，不调模型）────────────────────────────
    @staticmethod
    def _valence_signal(observations: list) -> float:
        positive = 0.0
        negative = 0.0
        for item in observations or []:
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

    @staticmethod
    def _behavioral_signal(observations: list) -> float:
        if not observations:
            return 0.0
        lengths = [len(str(item.get("user_message", ""))) for item in observations]
        avg = sum(lengths) / max(1, len(lengths))
        return max(0.0, min(1.0, avg / 40.0))

    def _interaction_gap_hours(self, moment: datetime.datetime) -> float:
        recent = self.observations.recent(limit=1)
        if not recent:
            return 0.0
        try:
            last = datetime.datetime.fromisoformat(str(recent[-1].get("timestamp", "")))
        except ValueError:
            return 0.0
        return max(0.0, (moment - last).total_seconds() / 3600.0)
