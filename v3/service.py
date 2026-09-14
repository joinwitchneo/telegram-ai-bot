"""V3Service：给 Telegram 命令和 cycle_runner 用的统一门面（读状态 / 提交请求）。

Phase 6.14 起，运行入口是：
    V3Service -> LifeCycle -> Checkpoint -> ThoughtDynamics -> Cognition
              -> Decision -> Action -> Outcome -> Feedback -> Continuity

Phase 5 的 CognitiveCycle 保留为**兼容/回滚实现**（老测试仍然覆盖它），
但默认运行路径不再调用它。
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path

from core.llm_client import LLMClient
from core.token_budget import TokenBudget
from core.usage_logger import UsageLogger
from v3.actions.base import MESSAGE, ActionRegistry
from v3.actions.journal import JournalAction
from v3.actions.message import MessageAction
from v3.budget import V3Budget
from v3.checkpoint.engine import CheckpointEngine
from v3.checkpoint.threshold import Thresholds
from v3.cognition.context import CognitiveContextBuilder
from v3.cognition.deepseek import DeepSeekCognitiveProvider
from v3.config import RuntimeConfig
from v3.continuity.cycle import CognitiveCycle
from v3.continuity.store import ContinuityStore
from v3.decision.engine import DecisionEngine
from v3.environments.telegram import TelegramEnvironment
from v3.interest.engine import InterestEngine
from v3.interest.store import InterestStore
from v3.journal import InnerJournal
from v3.life_cycle import LifeCycle
from v3.motivation.engine import MotivationEngine
from v3.observation.recorder import ObservationRecorder
from v3.outcome import FeedbackEngine, OutcomeStore
from v3.policies.budget import ActionBudget
from v3.policies.decision import ActionDecisionEngine
from v3.policies.validator import ActionValidator
from v3.reward.engine import RewardEngine
from v3.scheduler.queue import WakeQueue
from v3.thought.dynamics import ThoughtDynamics
from v3.thought.generator import ThoughtGenerator
from v3.thought.former import ThoughtFormer
from v3.thought.lifecycle import ThoughtLifecycle
from v3.thought.store import ThoughtStore
from v3.thought.validator import ThoughtValidator
from v3.trace import TraceLog
from v3.wake.wake_manager import WakeManager
from v3.wake.wake_policy import WakePolicy
from v3.wake.wake_result import WakeResult


class V3Service:
    """一次性装配 V3 全部组件；bot 常驻进程与 cycle_runner 各自建一份。

    environment_sender：把"怎么发消息"注入进来（bot 进程传入它自己的发送方法）。
    不传的话（例如 cycle_runner 子进程）对外行动会诚实地降级成"不可用"。
    """

    def __init__(self, config, *, base_dir: Path, client=None, budget_global=None,
                 environment_sender=None, chat_id=None) -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self.runtime = RuntimeConfig(config, base_dir=self.base_dir)
        self.runtime.ensure_dirs()
        paths = self.runtime.paths()

        self.global_budget = budget_global
        self.budget = V3Budget(
            paths["budget"],
            llm_per_day=self.runtime.limit_int("V3_LLM_CALLS_PER_DAY", 6),
            cycles_per_day=self.runtime.limit_int("V3_CYCLES_PER_DAY", 6),
            actions_per_day=self.runtime.limit_int("V3_ACTIONS_PER_DAY", 8),
            max_chain=self.runtime.limit_int("V3_MAX_CHAIN", 3),
            global_budget=budget_global,
        )
        self.thoughts = ThoughtStore(paths["thoughts"] / "thoughts.json")
        self.validator = ThoughtValidator()
        self.lifecycle = ThoughtLifecycle(self.thoughts)
        self.interests = InterestStore(paths["interests"] / "interests.json")
        self.interest_engine = InterestEngine(self.interests)
        self.motivation_engine = MotivationEngine()
        self.decision_engine = DecisionEngine(mode_getter=self.runtime.mode)
        self.reward_engine = RewardEngine(
            llm_eval_enabled=config.get_bool("V3_REWARD_LLM_ENABLED", False),
            llm_eval_cap=self.runtime.limit("V3_REWARD_LLM_EVAL_CAP", 0.15),
        )
        self.continuity = ContinuityStore(paths["continuity"])
        self.observations = ObservationRecorder(
            paths["observations"], keep=self.runtime.limit_int("V3_OBSERVATION_KEEP", 500)
        )
        self.journal = InnerJournal(paths["journal"] / "inner_journal.jsonl")
        self.wake_queue = WakeQueue(
            paths["continuity"] / "wake_queue.json",
            default_max_attempts=self.runtime.limit_int("V3_MAX_ATTEMPTS", 3),
        )
        self.client = client

        # ── Phase 6：观察 / 认知 / 行动 / 反馈 ──────────────────────
        self.trace = TraceLog(paths["data"] / "events.jsonl",
                              keep=self.runtime.limit_int("V3_TRACE_KEEP", 4000))
        self.dynamics = ThoughtDynamics.from_runtime(self.runtime)
        self.thresholds = Thresholds.from_runtime(self.runtime)
        self.outcomes = OutcomeStore(paths["data"] / "outcomes.jsonl",
                                     keep=self.runtime.limit_int("V3_OUTCOME_KEEP", 500))
        self.action_budget = ActionBudget(
            paths["data"] / "action_budget.json",
            daily_message_limit=self.runtime.limit_int("V3_ACTION_DAILY_MESSAGE_LIMIT", 3),
            message_cooldown_minutes=self.runtime.limit_int("V3_ACTION_MESSAGE_COOLDOWN_MINUTES", 90),
            cognitive_cooldown_minutes=self.runtime.limit_int("V3_ACTION_COGNITIVE_COOLDOWN_MINUTES", 10),
            daily_cognitive_limit=self.runtime.limit_int("V3_ACTION_DAILY_COGNITIVE_LIMIT", 12),
        )
        self.environment = TelegramEnvironment(
            sender=environment_sender,
            dry_run=(self.runtime.mode() == "dry_run"),
        )
        self.registry = ActionRegistry()
        self.registry.register(JournalAction(journal=self.journal))
        self.registry.register(MessageAction(environment=self.environment, chat_id=chat_id))
        self.action_validator = ActionValidator(
            registry=self.registry, budget=self.action_budget, environment=self.environment,
            mode_getter=self.runtime.mode,
            max_message_chars=self.runtime.limit_int("V3_ACTION_MAX_MESSAGE_CHARS", 800),
        )
        self.checkpoint = CheckpointEngine(
            thoughts=self.thoughts, dynamics=self.dynamics, thresholds=self.thresholds,
            motivation_engine=self.motivation_engine, interests=self.interests,
            trace=self.trace, log_path=paths["data"] / "checkpoints.jsonl",
            keep=self.runtime.limit_int("V3_CHECKPOINT_KEEP", 500),
        )
        self.context_builder = CognitiveContextBuilder(
            thoughts=self.thoughts, observations=self.observations, outcomes=self.outcomes,
            interests=self.interests, available_actions=self.registry.kinds,
            mode_getter=self.runtime.mode,
        )
        self.provider = DeepSeekCognitiveProvider(
            client=client, budget=self.budget, validator=self.validator,
            max_new_thoughts=self.runtime.limit_int("V3_COGNITION_MAX_NEW_THOUGHTS", 3),
        )
        self.action_decision = ActionDecisionEngine(
            mode_getter=self.runtime.mode, action_threshold=self.thresholds.action,
            min_confidence=self.runtime.limit("V3_ACTION_MIN_CONFIDENCE", 0.60),
        )
        self.feedback = FeedbackEngine(
            dynamics=self.dynamics, reward_engine=self.reward_engine,
            interests=self.interests, interest_engine=self.interest_engine, trace=self.trace,
        )
        self.former = ThoughtFormer(
            thoughts=self.thoughts, observations=self.observations, interests=self.interests,
            max_per_tick=self.runtime.limit_int("V3_THOUGHT_FORM_MAX_PER_TICK", 5),
        )
        self.wake_manager = WakeManager(
            thoughts=self.thoughts, interests=self.interests, continuity=self.continuity,
            wake_queue=self.wake_queue, policy=WakePolicy.from_runtime(self.runtime),
            trace=self.trace, state_path=paths["data"] / "wake_state.json",
            log_path=paths["data"] / "wakes.jsonl", observations=self.observations,
        )
        self.life = LifeCycle(
            runtime=self.runtime, budget=self.budget, checkpoint=self.checkpoint,
            context_builder=self.context_builder, provider=self.provider,
            decision_engine=self.action_decision, registry=self.registry,
            validator=self.action_validator, action_budget=self.action_budget,
            outcomes=self.outcomes, feedback=self.feedback, thoughts=self.thoughts,
            dynamics=self.dynamics, interests=self.interests,
            interest_engine=self.interest_engine, continuity=self.continuity,
            journal=self.journal, wake_queue=self.wake_queue, trace=self.trace,
            observations=self.observations, former=self.former,
            environment=self.environment, chat_id=chat_id,
            log_path=paths["data"] / "life_cycle.jsonl",
            keep=self.runtime.limit_int("V3_LIFE_CYCLE_KEEP", 300),
        )

        # ── Phase 5 兼容层（默认路径不再使用，保留以备回滚）──────────
        self.generator = ThoughtGenerator(client=client, budget=self.budget, validator=self.validator)
        self.cycle = CognitiveCycle(
            runtime=self.runtime, budget=self.budget, thoughts=self.thoughts,
            lifecycle=self.lifecycle, generator=self.generator, interests=self.interests,
            interest_engine=self.interest_engine, motivation_engine=self.motivation_engine,
            decision_engine=self.decision_engine, reward_engine=self.reward_engine,
            continuity=self.continuity, observations=self.observations, journal=self.journal,
            wake_queue=self.wake_queue,
        )

    # ── 请求（写操作必须回到正轨）───────────────────────────────────
    def run_cycle(self, *, trigger: str = "manual",
                  now: datetime.datetime | None = None,
                  wake_reason: str = "") -> dict:
        """Phase 6 起唯一的运行入口：人工 /cycle 与调度唤醒都走 LifeCycle。"""
        # 档位可以随时被 /mode 改，所以每次跑之前重新判定 dry_run
        self.environment.dry_run = self.runtime.mode() == "dry_run"
        return self.life.run(trigger=trigger, wake_reason=wake_reason, now=now)

    def record_wake_result(self, *, wake_id: str, reason: str, result: dict,
                           started_at: str = "") -> dict:
        """把一轮唤醒的结果写进唤醒历史（`/whyawake` 用）。"""
        cycle = (result or {}).get("cycle") or {}
        checkpoint = cycle.get("checkpoint") or {}
        top = checkpoint.get("top") or {}
        decision = cycle.get("decision") or {}
        action = cycle.get("action") or {}
        outcome = cycle.get("outcome") or {}
        row = WakeResult(
            wake_id=str(wake_id), reason=str(reason), started_at=str(started_at),
            wake_score=float(top.get("trigger_score") or 0.0),
            thought_ids=[str(top.get("thought_id"))] if top.get("thought_id") else [],
            interest_ids=[str(top.get("topic"))] if top.get("topic") else [],
            threshold_reached=bool(result.get("threshold_reached")),
            cognition_ran=bool(result.get("triggered")),
            decision=str(decision.get("action", "")),
            decision_reason=str(decision.get("reason", "")),
            would_action=str(decision.get("would_action", "")),
            action_id=str(action.get("action_id", "")),
            action_status=str(outcome.get("status", "")),
            simulated=bool(outcome.get("simulated")),
            motivation=float(top.get("motivation") or 0.0),
            expected_reward=float(decision.get("trigger_score") or 0.0),
            trace_id=str(result.get("trace_id", "")), cycle_id=str(result.get("cycle_id", "")),
            mode=str(result.get("mode", "")),
            error=str(result.get("skip_reason", "") or ""),
        )
        return self.wake_manager.record_result(row)

    # ── 自主唤醒（给 Scheduler / 命令用）─────────────────────────
    def wake_status(self, *, now: datetime.datetime | None = None) -> dict:
        return self.wake_manager.status(now=now)

    def wake_preview(self, *, now: datetime.datetime | None = None) -> dict:
        decision = self.wake_manager.evaluate(now=now)
        return decision.to_dict()

    def whyawake(self) -> dict:
        return self.wake_manager.last()

    def wake_reasons(self, limit: int = 50) -> dict:
        return self.wake_manager.log.summary(limit=limit) if self.wake_manager.log else {}

    def run_waketest(self, kind: str, *, now: datetime.datetime | None = None,
                     immediate: bool = False) -> dict:
        """受控的内部唤醒场景：只塑造内部状态，然后走正常唤醒链路。

        绝不设置后门：照样过预算 / 阈值 / 校验 / 档位 / 幂等。
        """
        moment = now or datetime.datetime.now()
        kind = (kind or "").strip().lower()
        thought = self._make_waketest_thought(kind, moment=moment)
        if thought is None:
            return {"ok": False, "error": f"不认识 /waketest 的场景：{kind}",
                    "usage": "unfinished｜curiosity｜intention｜relationship"}
        reason_map = {"unfinished": "UNFINISHED_THOUGHT", "curiosity": "CURIOSITY",
                      "intention": "INTENTION_DUE", "relationship": "RELATIONSHIP_EVENT"}
        reason = reason_map.get(kind, "UNFINISHED_THOUGHT")
        if not immediate:
            queued = self.wake_manager.request_wake(
                reason=reason,
                candidate={"thought_id": thought.id, "topic": thought.topic,
                           "reason": reason, "content": thought.content},
                now=moment, wake_score=0.0,
            )
            return {"ok": True, "thought_id": thought.id, "reason": reason,
                    "queued": queued, "immediate": False}
        result = self.run_cycle(trigger=f"waketest:{kind}", wake_reason=reason, now=moment)
        self.record_wake_result(wake_id=f"waketest-{kind}", reason=reason, result=result)
        return {"ok": True, "thought_id": thought.id, "reason": reason,
                "result": result, "immediate": True}

    def _make_waketest_thought(self, kind: str, *, moment: datetime.datetime):
        from v3.thought.models import Thought

        presets = {
            "unfinished": dict(content="（测试）我有件事还没想完，想继续想想",
                               type="unfinished", is_unfinished=True, urgency=0.90,
                               importance=0.95, motivation=0.0, novelty=0.85,
                               curiosity=0.90, activation=0.95, persistence=0.90,
                               information_gain=0.70, relationship_relevance=0.85),
            "curiosity": dict(content="（测试）我很好奇这件事后来怎么样了",
                              type="curiosity", is_unfinished=True, urgency=0.90,
                              importance=0.92, novelty=0.85, curiosity=0.95,
                              activation=0.90, persistence=0.85,
                              information_gain=0.70, relationship_relevance=0.80),
            "intention": dict(content="（测试）我上次想跟他说一句但忍住了",
                              type="desire", is_unfinished=True, urgency=0.90,
                              importance=0.95, novelty=0.70, curiosity=0.85,
                              activation=0.95, persistence=0.90,
                              information_gain=0.65, relationship_relevance=0.90),
            "relationship": dict(content="（测试）这跟我们之间的事有关，我想确认一下",
                                 type="reflection", is_unfinished=True, urgency=0.90,
                                 importance=0.95, novelty=0.70, curiosity=0.85,
                                 activation=0.95, persistence=0.90,
                                 information_gain=0.65, relationship_relevance=0.95),
        }
        preset = presets.get(kind)
        if preset is None:
            return None
        preset = dict(preset)
        preset["topic"] = f"waketest-{kind}"
        thought = Thought(
            id="", evidence=["/waketest 构造的内部场景"], source="waketest",
            created_at=moment.isoformat(timespec="seconds"),
            last_seen_at=moment.isoformat(timespec="seconds"),
            updated_at=moment.isoformat(timespec="seconds"),
            metadata={"waketest": kind,
                      "pending_intention": ({"kind": "MESSAGE", "content": ""}
                                            if kind == "intention" else None)},
            **preset,
        )
        saved, _ = self.thoughts.add(thought)
        return saved

    def set_mode(self, mode: str, *, actor: str = "") -> bool:
        ok = self.runtime.set_runtime_mode(mode, actor=actor)
        if ok:
            self.environment.dry_run = self.runtime.mode() == "dry_run"
        return ok

    # ── 只读状态（给命令）───────────────────────────────────────────
    def status(self) -> dict:
        checkpoints = self.checkpoint.stats()
        return {
            "runtime": self.runtime.mode_report(),
            "budget": self.budget.snapshot(),
            "queue": self.wake_queue.stats(),
            "thoughts": self.thoughts.stats(),
            "interests": self.interests.stats(),
            "journal": self.journal.count(),
            "observations": self.observations.count(),
            "continuity": self.continuity.stats(),
            "phase6": {
                "thresholds": self.thresholds.to_dict(),
                "checkpoints": checkpoints.get("checkpoints", 0),
                "triggered": checkpoints.get("triggered", 0),
                "band": (checkpoints.get("last") or {}).get("band", ""),
                "actions": self.action_budget.snapshot(),
                "environment": {
                    "name": self.environment.name,
                    "available": self.environment.available(),
                    "dry_run": self.environment.dry_run,
                },
                "actions_available": self.registry.kinds(),
                "actions_executable": self.effective_actions(),
                "trace": self.trace.count(),
                "outcomes": self.outcomes.count(),
                "wake": self.wake_manager.status(),
            },
        }

    def effective_actions(self) -> list:
        """当前档位下**真的允许执行**的行动（observe 档永远不含对外行动）。"""
        mode = self.runtime.mode()
        rows = []
        for item in self.registry.describe():
            if not item.get("available"):
                continue
            if mode in ("observe", "off", "") and item.get("kind") == MESSAGE:
                continue
            rows.append(str(item.get("kind")))
        return rows

    def wakequeue_lines(self) -> list:
        rows = []
        for item in self.wake_queue.all()[-10:]:
            rows.append(f"{item.get('id')} {item.get('status')} {item.get('priority')} "
                        f"{item.get('earliest_at')} attempts={item.get('attempts')}/{item.get('max_attempts')} "
                        f"reason={item.get('reason')}")
        return rows

    def thought_lines(self, limit: int = 8) -> list:
        rows = []
        for thought in sorted(self.thoughts.all(), key=lambda t: str(t.last_seen_at), reverse=True)[:limit]:
            flag = "未完成" if thought.is_unfinished else thought.status
            rows.append(f"{thought.id} [{flag}] ({thought.topic or '-'}) {thought.content} "
                        f"trigger={thought.trigger_score} act={thought.activation} "
                        f"age={thought.age} seen={thought.seen_count}")
        return rows

    def thought_detail(self, thought_id: str) -> list:
        """单个念头的完整快照（只给可解释字段，绝不含思维链或原始模型输出）。"""
        thought = self.thoughts.get(thought_id)
        if thought is None:
            return []
        interest = self.interests.get(thought.topic) if thought.topic else None
        if interest is None:
            interest_text = "（这个话题还没有兴趣记录）"
        else:
            interest_text = (f"好感={interest.attraction:.2f} 好奇={interest.curiosity:.2f} "
                             f"倾向={interest.valence:+.2f}")
        return [
            f"念头 {thought.id}",
            "────────────",
            f"内容：{thought.content}",
            f"类型：{thought.type}　生命周期：{thought.lifecycle_state}（旧状态 {thought.status}）",
            f"话题：{thought.topic or '-'}　未完成：{'是' if thought.is_unfinished else '否'}",
            f"score={thought.score}　importance={thought.importance}　urgency={thought.urgency}",
            f"activation={thought.activation}　persistence={thought.persistence}　"
            f"novelty={thought.novelty}　信息增益={thought.information_gain}",
            f"motivation={thought.motivation}　trigger_score={thought.trigger_score}",
            f"兴趣：{interest_text}",
            f"年龄={thought.age}（检查点数）　被激活={thought.times_activated}　"
            f"被忽略={thought.times_ignored}　已行动={thought.times_acted}",
            f"最近结果：{thought.last_outcome or '-'}　冷却到：{thought.cooldown_until or '-'}",
            f"来源：{thought.source}　cycle={thought.cycle_id or '-'}",
            f"创建：{thought.created_at}　更新：{thought.updated_at}　"
            f"上次激活：{thought.last_activated_at or '-'}",
            f"证据：{'；'.join(str(item) for item in thought.evidence) or '-'}",
            f"关联念头：{'、'.join(thought.related_ids) or '-'}",
        ]

    def interest_lines(self, limit: int = 8) -> list:
        rows = []
        for item in self.interests.top(limit=limit):
            rows.append(f"{item.topic}  好感={item.attraction:.2f}  好奇={item.curiosity:.2f}  "
                        f"倾向={item.valence:+.2f}  证据={item.evidence}")
        return rows

    def journal_lines(self, limit: int = 5) -> list:
        rows = []
        for item in self.journal.recent(limit=limit):
            rows.append(f"{item.get('timestamp')} [{item.get('kind')}] {item.get('content')}")
        return rows

    def checkpoint_lines(self, limit: int = 5) -> list:
        """最近检查点：重点回答"为什么这一刻没有调用 LLM"。"""
        records = self.checkpoint.recent(limit=limit)
        if not records:
            return []
        cycles = {str(item.get("trace_id", "")): item
                  for item in self.life.recent(limit=self.life.keep)}
        rows = []
        for item in records:
            top = item.get("top") or {}
            cycle = cycles.get(str(item.get("trace_id", "")), {})
            decision = (cycle.get("decision") or {}).get("action", "")
            rows.append(
                f"{item.get('created_at')} {item.get('checkpoint_id')}　"
                f"念头 {item.get('evaluated')}（活跃 {item.get('active_count')}）　"
                f"最高触发分 {top.get('trigger_score', 0)}　档位 {item.get('band') or '-'}"
            )
            tail = f"   认知：{'已触发' if item.get('triggered') else '未触发'}（{item.get('reason')}）"
            if decision:
                tail += f"　决策：{decision}"
            rows.append(tail)
        return rows

    def trigger_lines(self, limit: int = 8) -> list:
        """当前触发候选，并明确区分 candidate / eligible / blocked。"""
        records = self.checkpoint.recent(limit=1)
        if not records:
            return ["还没有跑过检查点：先用 /cycle 跑一轮。"]
        last = records[-1]
        can_cognize, why = self.action_budget.can_cognize()
        can_message, msg_why = self.action_budget.can_message()
        rows = [f"最近检查点 {last.get('checkpoint_id')}（{last.get('created_at')}）",
                f"档位={self.runtime.mode()}　阈值={self.thresholds.to_dict()}"]
        for item in (last.get("candidates") or [])[:limit]:
            score = float(item.get("trigger_score") or 0.0)
            if score >= self.thresholds.cognitive:
                status = "eligible（够格深思）" if can_cognize else f"blocked（{why}）"
            elif score >= self.thresholds.attention:
                status = "candidate（高关注，还不够深思）"
            else:
                status = "candidate（观察中）"
            components = item.get("components", {}) or {}
            rows.append(f"· {item.get('thought_id')} {str(item.get('content', ''))[:40]}")
            rows.append(f"    trigger={score} 档位={item.get('band')} 状态={status}")
            rows.append(
                f"    importance={components.get('importance')} urgency={components.get('urgency')}"
                f" novelty={components.get('novelty')}"
                f" relationship={components.get('relationship_relevance')}"
                f" persistence={components.get('persistence')}"
            )
        if not can_message:
            rows.append(f"（对外消息当前不可用：{msg_why}）")
        return rows

    def action_lines(self, limit: int = 8) -> list:
        """最近的行动：observe 档明确显示 WOULD_ACTION 而不是 EXECUTED。"""
        outcomes = self.outcomes.recent(limit=limit)
        if not outcomes:
            return ["还没有任何行动记录（检查点可能一直没到阈值）。"]
        rows = []
        for item in outcomes:
            action_id = str(item.get("action_id", ""))
            cycle = self.life.by_action(action_id)
            decision = cycle.get("decision") or {}
            rows.append(
                f"{item.get('created_at')} {action_id}　类型={item.get('kind')}　"
                f"状态={item.get('status')}　模式={cycle.get('mode') or '-'}"
            )
            extra = ""
            if decision.get("would_action"):
                extra += f"　本来会做：{decision['would_action']}"
            if item.get("error"):
                extra += f"　错误：{item['error']}"
            if item.get("simulated"):
                extra += "　（模拟发送）"
            rows.append(f"   决策原因：{decision.get('reason') or '-'}{extra}")
        return rows

    def why(self, action_id: str = "") -> dict:
        """可解释链：优先解释某个 action，否则解释最近一轮。"""
        cycle = self.life.by_action(action_id) if action_id else {}
        if not cycle:
            recent = self.life.recent(limit=1)
            cycle = recent[-1] if recent else {}
        trace_id = str(cycle.get("trace_id", ""))
        return {
            "cycle": cycle,
            "trace": self.trace.by_trace(trace_id) if trace_id else [],
            "found": bool(cycle),
        }
