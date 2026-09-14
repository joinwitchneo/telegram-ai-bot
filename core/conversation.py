"""对话编排：Debounce → LLM Policy → Planner → Context → LLM → Validator → Renderer。

Phase 2 起，上下文组装交给 ContextManager（五层结构 + 模式 + 预算裁剪），
Phase 5 起，回复形态由 ResponsePlanner 决定、ResponseValidator 复核、
ConsistencyChecker 检查 AI 味（最多重试一次），本模块只负责流程编排与状态记录。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from core import llm_policy
from core.context_manager import ContextManager, detect_mode
from core.event_bus import EventBus


class Conversation:
    def __init__(
        self,
        *,
        llm_client,
        renderer,
        context_manager: ContextManager | None = None,
        token_budget=None,
        usage_logger=None,
        event_bus: EventBus | None = None,
        debugger=None,
        state_provider: Callable[[], dict] | None = None,
        memory_provider: Callable[[str, list[dict]], list[dict]] | None = None,
        after_reply: Callable[[int, str, list[dict]], None] | None = None,
        planner=None,
        validator=None,
        checker=None,
        style_provider: Callable[[int], dict] | None = None,
        signal_provider: Callable[[], dict] | None = None,
        memory_by_id: Callable[[list[str]], list[dict]] | None = None,
        response_stats=None,
        max_retry: int = 1,
        dry_run: bool = False,
        static_prefix: str = "",
        history_limit: int = 12,
        debounce_single: float = 1.0,
        debounce_multi: float = 2.0,
        timer_factory=None,
    ) -> None:
        self.llm = llm_client
        self.renderer = renderer
        self.budget = token_budget
        self.usage = usage_logger
        self.bus = event_bus or EventBus()
        self.debugger = debugger
        self.state_provider = state_provider
        self.memory_provider = memory_provider
        self.after_reply = after_reply
        self.planner = planner
        self.validator = validator
        self.checker = checker
        # 主动消息不允许复述"理由原话"，所以用更严格的阈值单独检查
        self.proactive_checker = self._build_proactive_checker(checker)
        self.style_provider = style_provider
        self.signal_provider = signal_provider
        self.memory_by_id = memory_by_id
        self.response_stats = response_stats
        self.max_retry = max(0, int(max_retry))
        self.dry_run = bool(dry_run)
        # 没传 ContextManager 时，用 static_prefix 兜底（Phase 1 兼容路径）
        self.context = context_manager or ContextManager(
            contract=static_prefix, persona="", token_budget=token_budget
        )
        self.history_limit = max(2, int(history_limit))
        self.debounce_single = max(0.3, float(debounce_single))
        self.debounce_multi = max(self.debounce_single, float(debounce_multi))
        self._timer_factory = timer_factory or (
            lambda delay, fn, args: threading.Timer(delay, fn, args=args)
        )
        self._lock = threading.RLock()
        self._pending: dict[int, list[str]] = {}
        self._timers: dict[int, object] = {}
        self._history: dict[int, list[dict]] = {}
        self._last_active: dict[int, float] = {}
        self._reply_streak: dict[int, int] = {}
        self._last_built = None
        self._last_ai_flavor: list[str] = []

    # ── 历史（Phase 3 会由 Memory 层接管短期存储）──────────────────
    def history(self, chat_id: int) -> list[dict]:
        with self._lock:
            return list(self._history.get(chat_id, []))

    def remember(self, chat_id: int, role: str, content: str) -> None:
        with self._lock:
            items = self._history.setdefault(chat_id, [])
            items.append({"role": role, "content": content[:2000]})
            del items[: -self.history_limit]

    def clear_history(self, chat_id: int) -> None:
        with self._lock:
            self._history.pop(chat_id, None)

    # ── Debounce ───────────────────────────────────────────────────
    def handle_text(self, chat_id: int, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"accepted": False}
        self.bus.publish("UserMessageReceived", chat_id=chat_id, text=text)
        with self._lock:
            buffer = self._pending.setdefault(chat_id, [])
            buffer.append(text)
            timer = self._timers.pop(chat_id, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:  # noqa: BLE001
                    pass
            window = self.debounce_single if len(buffer) == 1 else self.debounce_multi
            new_timer = self._timer_factory(window, self.flush, (chat_id,))
            try:
                new_timer.daemon = True
            except Exception:  # noqa: BLE001
                pass
            self._timers[chat_id] = new_timer
            new_timer.start()
            return {"accepted": True, "pending": len(buffer), "window": window}

    def flush(self, chat_id: int) -> dict:
        with self._lock:
            parts = self._pending.pop(chat_id, [])
            self._timers.pop(chat_id, None)
        if not parts:
            return {"processed": False}
        return self.process(chat_id, "\n".join(part for part in parts if part).strip())

    # ── 核心流程 ───────────────────────────────────────────────────
    def process(self, chat_id: int, text: str) -> dict:
        history_before = self.history(chat_id)
        mode = detect_mode(text, history_before)
        gap_minutes = self._gap_minutes(chat_id)
        self._last_active[chat_id] = time.time()
        style_state: dict = {}
        signals: dict = {}
        plan = None
        if self.planner is not None:
            signals = self.signal_provider() if self.signal_provider else {}
            if self.style_provider is not None:
                try:
                    style_state = dict(self.style_provider(chat_id) or {})
                except Exception as exc:  # noqa: BLE001 - 风格失败不能阻断聊天
                    logging.warning("读取用户风格失败：%s", exc)
                    style_state = {}
            plan = self.planner.plan(
                text,
                mode=mode,
                signals=signals,
                style=style_state,
                gap_minutes=gap_minutes,
                consecutive_replies=self._reply_streak.get(chat_id, 0),
                last_user_text=self._last_user_text(chat_id),
            )
            if not plan.should_reply:
                self._reply_streak[chat_id] = 0
                if self.response_stats is not None:
                    self.response_stats.record(
                        mode=mode, replied=False, silence_reason=plan.reason, score=plan.score
                    )
                if self.usage is not None:
                    self.usage.record_local("rule", 0, note=f"planner:{plan.reason}")
                self.bus.publish("ConversationEnded", chat_id=chat_id, sent=0, mode=mode, skipped=True)
                return {
                    "processed": True,
                    "skipped": True,
                    "reason": plan.reason,
                    "plan": plan.to_plan_dict(),
                    "mode": mode,
                    "sent": [],
                }

        budget_state = self.budget.state() if self.budget is not None else None
        decision = llm_policy.decide(text, category="chat", budget_state=budget_state)
        state = dict(self.state_provider() or {}) if self.state_provider else {}
        if style_state:
            state.update({key: value for key, value in style_state.items() if value})
        if plan is not None and plan.tone_hint():
            state["tone"] = "；".join(
                part for part in (plan.length_hint(), plan.tone_hint()) if part
            )
        memories = self.memory_provider(text, history_before) if self.memory_provider else None
        memory_info = [
            {"id": str(block.get("id", "")), "score": block.get("score", 0.0),
             "parts": block.get("parts", {})}
            for block in (memories or [])
        ]
        if memory_info and self.bus is not None:
            self.bus.publish(
                "MemoryRecalled",
                ids=[item["id"] for item in memory_info],
                scores=[item["score"] for item in memory_info],
            )
        built = self.context.build(
            user_text=text, history=history_before, state=state or None, memory_blocks=memories, mode=mode
        )
        self._last_built = built
        logging.info("[policy] %s", decision.as_log())

        if not decision.use_llm:
            if self.usage is not None:
                self.usage.record_local("rule", built.stats["total_context_tokens"], note=decision.reason)
            if self.response_stats is not None:
                self.response_stats.record(
                    mode=built.mode, replied=False, silence_reason="rule_path",
                    score=float(plan.score) if plan is not None else 0.0,
                )
            self._log_context(built, decision, model="", usage=None, request_id="", memory_info=memory_info)
            return {
                "processed": True,
                "use_llm": False,
                "decision": decision,
                "mode": built.mode,
                "context": built.stats,
                "sent": [],
            }

        result = self.llm.chat(
            built.messages,
            tier=decision.tier,
            category=decision.category,
            max_tokens=getattr(self.budget, "max_output_tokens", None),
        )
        if not result.ok:
            logging.warning("[conversation] LLM 调用失败：%s", result.error)
            self._log_context(
                built, decision, model="", usage=None, request_id=result.request_id, memory_info=memory_info
            )
            fallback_sent = self._send_fallback(chat_id, mode=mode, plan=plan)
            if fallback_sent:
                self.remember(chat_id, "user", text)
                self.remember(chat_id, "assistant", "\n".join(fallback_sent))
                self._reply_streak[chat_id] = self._reply_streak.get(chat_id, 0) + 1
                self.bus.publish("ConversationEnded", chat_id=chat_id, sent=len(fallback_sent), mode=mode)
                return {
                    "processed": True,
                    "use_llm": True,
                    "ok": False,
                    "error": result.error,
                    "decision": decision,
                    "mode": built.mode,
                    "context": built.stats,
                    "fallback_used": True,
                    "sent": fallback_sent,
                }
            return {
                "processed": True,
                "use_llm": True,
                "ok": False,
                "error": result.error,
                "decision": decision,
                "mode": built.mode,
                "context": built.stats,
            }

        result, retry_used, retry_failed = self._maybe_retry(
            result, built=built, decision=decision, text=text, mode=mode,
            signals=signals, memories=memories, plan=plan,
        )

        validated = None
        if self.validator is not None:
            if retry_failed:
                validated = self.validator.fallback("ai_flavor")
            else:
                candidate = self.validator.parse_candidate(result.content)
                merged = self._merge_plan(plan, candidate.plan)
                validated = self.validator.validate(
                    candidate.messages,
                    merged,
                    sticker_allowed=bool(plan.sticker_allowed) if plan is not None else False,
                    max_count=int(plan.message_count) if plan is not None else None,
                )
            sent = self.renderer.render(
                chat_id,
                validated.to_render_dict(),
                interrupt_check=lambda: self.pending_count(chat_id) > 0,
                dry_run=self.dry_run,
            )
        else:
            sent = self.renderer.render(chat_id, result.content, dry_run=self.dry_run)

        self.remember(chat_id, "user", text)
        if sent:
            self.remember(chat_id, "assistant", "\n".join(sent))
        # V3 观测用：只把"已经存在的数据"发出去（实际发送文本 + 已有计划信息），不调模型
        if self.bus is not None:
            try:
                self.bus.publish(
                    "BotResponseSent",
                    chat_id=chat_id,
                    excerpt="\n".join(sent)[:240],
                    response_mode=built.mode,
                    message_count=len(sent),
                    plan=(plan.to_plan_dict() if plan is not None else {}),
                )
            except Exception as exc:  # noqa: BLE001 - 观测失败不能影响聊天
                logging.debug("[v3] 发布 BotResponseSent 失败：%s", exc)
        self._reply_streak[chat_id] = self._reply_streak.get(chat_id, 0) + 1
        fallback_used = bool(validated is not None and validated.fallback_used)
        if self.response_stats is not None and plan is not None:
            pause = (validated.plan.get("pause") if validated is not None else None) or [0, 0]
            self.response_stats.record(
                mode=built.mode,
                length=plan.length,
                tone=plan.tone,
                score=plan.score,
                reply_message_count=len(sent),
                replied=True,
                retry=retry_used,
                fallback=fallback_used,
                sticker=bool(validated.plan.get("sticker")) if validated is not None else False,
                pause_avg=(float(pause[0]) + float(pause[1])) / 2 if len(pause) >= 2 else 0.0,
            )
        self._log_context(
            built, decision, model=result.model, usage=result, request_id=result.request_id,
            memory_info=memory_info,
        )
        self.bus.publish("ConversationEnded", chat_id=chat_id, sent=len(sent), mode=built.mode)
        if self.after_reply is not None:
            try:
                self.after_reply(chat_id, text, self.history(chat_id))
            except Exception as exc:  # noqa: BLE001 - 后置处理不影响回复
                logging.warning("after_reply 钩子失败：%s", exc)
        return {
            "processed": True,
            "use_llm": True,
            "ok": True,
            "decision": decision,
            "mode": built.mode,
            "context": built.stats,
            "tier": result.tier,
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cached_tokens": result.cached_tokens,
            "fallback_from": result.fallback_from,
            "retry_used": retry_used,
            "fallback_used": fallback_used,
            "ai_flavor": list(self._last_ai_flavor),
            "plan": (
                {**plan.to_plan_dict(), **(validated.plan if validated is not None else {})}
                if plan is not None
                else None
            ),
            "issues": validated.issues if validated is not None else [],
            "sent": sent,
        }

    # ── Phase 5：计划合并 / 重试 / 兜底 ────────────────────────────
    # ── Phase 6：主动消息（复用 Phase 5 的计划/校验/渲染）───────────
    # ── Phase 7：感知内容（图片 / 语音 / 视频 / 文档 / 网页）────────
    @staticmethod
    def perception_instruction(source_type: str, summary: str, *, ok: bool = True) -> str:
        label = {
            "image": "一张图片", "voice": "一条语音", "video": "一段视频",
            "document": "一个文件", "url": "一个网页链接", "sticker": "一个表情包",
        }.get(str(source_type), "一条内容")
        if not ok:
            # 视觉事实边界：拿不到内容时，绝不允许她"看见"
            return (
                f"【本地感知结果】用户刚发来{label}，但你**没有获得任何视觉/听觉内容**。\n"
                f"系统状态：{summary}\n"
                "硬规则：不要描述这条内容里有什么；不要说“我看到了/我听到/图上/照片里/画面中”；"
                "不要根据文件名、用户后续的描述或者常识去猜内容；只能老实说你这边看不到，请他讲讲。"
            )
        return (
            f"【本地感知结果】用户刚发来{label}。你这边（本地、没有上传到任何云）看到的是：\n{summary}\n"
            "用你自己的语气回应这件事本身，别解释你是怎么看到的、别像工具播报、别罗列字段；"
            "只能依据上面的内容说事，没看到的细节不要编；如果上面说本地没能力识别，就老实说没看到，让他讲讲。"
        )

    def perceive(self, chat_id: int, *, source_type: str, summary: str,
                 user_text: str = "", mode: str = "CASUAL", perception_ok: bool = True) -> dict:
        """把感知层的结果送进同一条回复管线（Planner → LLM → Validator → Renderer）。"""
        history_before = self.history(chat_id)
        mode = detect_mode(user_text or summary, history_before)
        signals = self.signal_provider() if self.signal_provider else {}
        style_state: dict = {}
        if self.style_provider is not None:
            try:
                style_state = dict(self.style_provider(chat_id) or {})
            except Exception as exc:  # noqa: BLE001
                logging.warning("感知回复读取风格失败：%s", exc)

        if self.planner is not None:
            plan = self.planner.plan(
                summary or user_text, mode=mode, signals=signals, style=style_state,
                gap_minutes=self._gap_minutes(chat_id),
                consecutive_replies=self._reply_streak.get(chat_id, 0),
            )
        else:
            plan = None
        if plan is not None and not plan.should_reply:
            plan.should_reply = True       # 用户主动发来的东西一定要有回应
            plan.reason = "perception_always_reply"

        state = dict(self.state_provider() or {}) if self.state_provider else {}
        if style_state:
            state.update({key: value for key, value in style_state.items() if value})
        if plan is not None and plan.tone_hint():
            state["tone"] = "；".join(part for part in (plan.length_hint(), plan.tone_hint()) if part)
        # 给 AI 味检查用的边界信号：本轮到底有没有拿到感知内容
        state["perception_ok"] = bool(perception_ok)

        instruction = self.perception_instruction(source_type, summary, ok=perception_ok)
        built = self.context.build(
            user_text=user_text,
            history=history_before,
            state=state or None,
            memory_blocks=None,
            mode=mode,
            extra_system=instruction,
        )
        # 关键断言：感知结果必须真的进了最终 messages，否则就是 Context 注入失败
        marker = str(instruction)[:24]
        instruction_in_messages = any(
            marker in str(message.get("content", "")) for message in built.messages
        )
        logging.info(
            "[context][vision] injected=%s instruction_len=%d",
            instruction_in_messages, len(instruction),
        )
        logging.info(
            "[context][vision] content=%s",
            str(instruction).replace("\n", " ")[:200],
        )
        logging.info(
            "[perception] ok=%s instruction_len=%d 进入messages=%s",
            perception_ok, len(instruction), instruction_in_messages,
        )
        if not instruction_in_messages:
            logging.error("[perception] 感知结果没有进入最终 messages（Context 注入失败）")
        decision = llm_policy.decide(
            summary or user_text, category="chat",
            budget_state=self.budget.state() if self.budget is not None else None,
        )
        if not decision.use_llm:
            if self.usage is not None:
                self.usage.record_local("rule", built.stats["total_context_tokens"], note=decision.reason)
            return {
                "processed": True, "use_llm": False, "decision": decision,
                "mode": mode, "sent": [], "summary": summary,
            }

        result = self.llm.chat(
            built.messages, tier=decision.tier, category="chat",
            max_tokens=getattr(self.budget, "max_output_tokens", None),
        )
        if not result.ok:
            logging.warning("[perceive] LLM 调用失败：%s", result.error)
            return {"processed": True, "ok": False, "error": result.error, "sent": []}

        result, retry_used, retry_failed = self._maybe_retry(
            result, built=built, decision=decision, text=summary, mode=mode,
            signals=signals, memories=[], plan=plan,
        )
        validated = None
        if self.validator is not None:
            if retry_failed:
                validated = self.validator.fallback("ai_flavor")
            else:
                candidate = self.validator.parse_candidate(result.content)
                merged = self._merge_plan(plan, candidate.plan)
                validated = self.validator.validate(
                    candidate.messages, merged,
                    sticker_allowed=bool(plan.sticker_allowed) if plan is not None else False,
                    max_count=int(plan.message_count) if plan is not None else None,
                )
            sent = self.renderer.render(
                chat_id, validated.to_render_dict(),
                interrupt_check=lambda: self.pending_count(chat_id) > 0,
                dry_run=self.dry_run,
            )
        else:
            sent = self.renderer.render(chat_id, result.content, dry_run=self.dry_run)

        note = f"[{source_type}] {str(summary)[:200]}"
        self.remember(chat_id, "user", user_text or note)
        if sent:
            self.remember(chat_id, "assistant", "\n".join(sent))
        self._reply_streak[chat_id] = self._reply_streak.get(chat_id, 0) + 1
        self._log_context(
            built, decision, model=result.model, usage=result, request_id=result.request_id, memory_info=[]
        )
        self.bus.publish("ConversationEnded", chat_id=chat_id, sent=len(sent), mode=mode)
        return {
            "processed": True, "ok": True, "mode": mode, "sent": sent,
            "retry_used": retry_used, "summary": summary,
            "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
            "cached_tokens": result.cached_tokens,
            "plan": (validated.plan if validated is not None else (plan.to_plan_dict() if plan else None)),
        }

    @staticmethod
    def _build_proactive_checker(checker):
        if checker is None:
            return None
        try:
            from personality.consistency_checker import ConsistencyChecker

            return ConsistencyChecker(
                max_severity=min(float(getattr(checker, "max_severity", 0.5)), 0.40),
                enabled=bool(getattr(checker, "enabled", True)),
            )
        except Exception:  # noqa: BLE001
            return checker

    @staticmethod
    def proactive_instruction(candidate) -> str:
        """主动消息的系统指令：理由只给模型自己看，绝不允许解释。"""
        hint = str(getattr(candidate, "hint", "") or "").strip() or "你突然想起他了"
        return (
            "【主动消息】现在没有新消息，你想主动找他说句话。\n"
            f"你想到的理由（只给你自己看）：{hint}\n"
            "要求：像突然想起来一样自然，直接说你想说的，短一点；"
            "不要解释你为什么现在发消息，不要说“好久没聊”“我注意到”“最近怎么样”这类话；"
            "不要编造你今天去了哪里、做了什么；不要问候式客套；"
            "尤其不要把上面那句理由原话、或者对方说过的那句话，直接抄一遍发出去。"
        )

    def proactive(self, chat_id: int, *, candidate, mode: str = "CASUAL") -> dict:
        """主动消息：同样走 Planner → Context → LLM → Validator → Renderer。"""
        if self.planner is None or self.validator is None:
            return {"sent": [], "cancelled": True, "reason": "no_pipeline"}
        history_before = self.history(chat_id)
        signals = self.signal_provider() if self.signal_provider else {}
        style_state: dict = {}
        if self.style_provider is not None:
            try:
                style_state = dict(self.style_provider(chat_id) or {})
            except Exception as exc:  # noqa: BLE001
                logging.warning("主动消息读取风格失败：%s", exc)

        plan = self.planner.plan_proactive(
            candidate=candidate, signals=signals, style=style_state, mode=mode
        )
        state = dict(self.state_provider() or {}) if self.state_provider else {}
        if style_state:
            state.update({key: value for key, value in style_state.items() if value})
        state["tone"] = "；".join(part for part in (plan.length_hint(), plan.tone_hint()) if part)

        memory_ids = list(getattr(candidate, "memory_ids", []) or [])
        memories = []
        if memory_ids and self.memory_by_id is not None:
            try:
                memories = self.memory_by_id(memory_ids)
            except Exception as exc:  # noqa: BLE001
                logging.warning("主动消息读取记忆失败：%s", exc)

        built = self.context.build(
            user_text="",
            history=history_before,
            state=state or None,
            memory_blocks=memories,
            mode=mode,
            extra_system=self.proactive_instruction(candidate),
        )
        decision = llm_policy.decide(
            str(getattr(candidate, "hint", "") or ""),
            category="proactive",
            budget_state=self.budget.state() if self.budget is not None else None,
        )
        if not decision.use_llm:
            if self.usage is not None:
                self.usage.record_local("rule", 0, note=f"proactive:{decision.reason}")
            return {
                "sent": [], "cancelled": True, "reason": decision.reason,
                "candidate": getattr(candidate, "to_dict", lambda: {})(), "plan": plan.to_plan_dict(),
            }

        result = self.llm.chat(
            built.messages,
            tier=decision.tier,
            category="proactive",
            max_tokens=getattr(self.budget, "max_output_tokens", None),
        )
        if not result.ok:
            logging.warning("[proactive] 模型调用失败：%s", result.error)
            return {"sent": [], "cancelled": True, "reason": "llm_failed", "error": result.error}

        result, retry_used, retry_failed = self._maybe_retry(
            result, built=built, decision=decision, text=str(getattr(candidate, "hint", "") or ""),
            mode=mode, signals=signals, memories=memories, plan=plan,
            checker=self.proactive_checker,
        )
        if retry_failed:
            # 主动消息宁可这一次不发，也不要发一句明显不对味的兜底话
            return {"sent": [], "cancelled": True, "reason": "ai_flavor", "retry_used": True}

        candidate_messages = self.validator.parse_candidate(result.content)
        merged = self._merge_plan(plan, candidate_messages.plan)
        validated = self.validator.validate(
            candidate_messages.messages,
            merged,
            sticker_allowed=plan.sticker_allowed,
            max_count=int(plan.message_count),
        )
        if validated.fallback_used:
            return {"sent": [], "cancelled": True, "reason": "empty_reply"}
        if self.pending_count(chat_id) > 0:
            # 用户这会儿刚发消息，主动消息就不发了
            return {"sent": [], "cancelled": True, "reason": "user_active"}

        sent = self.renderer.render(
            chat_id,
            validated.to_render_dict(),
            interrupt_check=lambda: self.pending_count(chat_id) > 0,
            dry_run=self.dry_run,
        )
        if sent:
            self.remember(chat_id, "assistant", "\n".join(sent))
        self._log_context(
            built, decision, model=result.model, usage=result, request_id=result.request_id,
            memory_info=[{"id": str(mid), "score": 0.0, "parts": {}} for mid in memory_ids],
        )
        return {
            "sent": sent,
            "candidate": getattr(candidate, "to_dict", lambda: {})(),
            "plan": {**plan.to_plan_dict(), **validated.plan},
            "retry_used": retry_used,
            "mode": mode,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cached_tokens": result.cached_tokens,
        }

    @staticmethod
    def _merge_plan(plan, proposed: dict | None) -> dict:
        """Python 的计划是硬上限；模型只能提议语气，条数由内容和计划共同裁决。"""
        base = dict(plan.to_plan_dict()) if plan is not None else {}
        proposed = dict(proposed or {})
        for key in ("length", "tone", "reply_mode", "pause"):
            if key in proposed:
                base[key] = proposed[key]
        base["sticker"] = bool(proposed.get("sticker", base.get("sticker", False)))
        return base

    def _maybe_retry(self, result, *, built, decision, text, mode, signals, memories, plan, checker=None):
        """AI 味检查不过 → 最多重试一次；重试仍不过 → 交给兜底。

        返回 (使用的回复结果, 是否重试过, 是否重试后仍然不合格)。
        """
        checker = checker or self.checker
        if checker is None or self.validator is None or self.max_retry <= 0:
            return result, False, False
        candidate = self.validator.parse_candidate(result.content)
        check_state = {"signals": signals} if signals else None
        check = checker.check(
            candidate.messages, user_text=text, mode=mode, state=check_state,
            memories=memories, plan=plan,
        )
        if check.ok:
            self._last_ai_flavor = []
            return result, False, False
        self._last_ai_flavor = list(check.codes)
        logging.info("[checker] AI 味过重 %s，重试一次", check.codes)
        correction = checker.correction_instruction(check.issues)
        retry_messages = list(built.messages) + [{"role": "system", "content": correction}]
        retry = self.llm.chat(
            retry_messages,
            tier=getattr(decision, "tier", "main"),
            category="retry",
            max_tokens=getattr(self.budget, "max_output_tokens", None),
        )
        if not retry.ok:
            return result, True, True
        retry_candidate = self.validator.parse_candidate(retry.content)
        recheck = checker.check(
            retry_candidate.messages, user_text=text, mode=mode, state=check_state,
            memories=memories, plan=plan,
        )
        if recheck.ok:
            return retry, True, False
        return (retry, True, True) if recheck.severity < check.severity else (result, True, True)

    def _send_fallback(self, chat_id: int, *, mode: str, plan) -> list[str]:
        """模型失败/超时时，至少说一句人话，不能凭空消失。"""
        if self.validator is None:
            return []
        fallback = self.validator.fallback("llm_failed")
        sent = self.renderer.render(
            chat_id, fallback.to_render_dict(), dry_run=self.dry_run
        )
        if self.response_stats is not None:
            self.response_stats.record(
                mode=mode, length="SHORT", tone="NEUTRAL",
                score=float(getattr(plan, "score", 0.0) or 0.0),
                reply_message_count=len(sent), replied=True, fallback=True,
            )
        return sent

    # ── Context Debugger（旁路，不回流）────────────────────────────
    def _log_context(self, built, decision, *, model: str, usage, request_id: str, memory_info=None) -> None:
        if self.debugger is None:
            return
        tokens = built.stats.get("tokens", {})
        memory_info = memory_info or []
        record = {
            "request_id": request_id or f"ctx_{int(time.time() * 1000)}",
            "model": model,
            "mode": built.mode,
            "tokens": tokens,
            "total_context_tokens": built.stats.get("total_context_tokens", 0),
            "memory_ids": [item["id"] for item in memory_info],
            "retrieved_memory_ids": [item["id"] for item in memory_info],
            "retrieved_memory_scores": [item["score"] for item in memory_info],
            "selected_memory_ids": [item["id"] for item in memory_info],
            "memory_parts": [item.get("parts", {}) for item in memory_info],
            "topic": "",
            "emotion": "",
            "relationship": "",
            "history_count": built.stats.get("history_count", 0),
            "policy_reason": decision.reason,
            "budget_state": (self.budget.state() if self.budget is not None else {}),
            "fallback_from": getattr(usage, "fallback_from", "") if usage else "",
        }
        self.debugger.log(record)
        self.debugger.snapshot(
            record["request_id"],
            {
                "meta": record,
                "L0": self.context.contract,
                "L1": self.context.persona,
                "L2": next((b.text for b in built.blocks if b.name == "L2"), ""),
                "L3": next((b.text for b in built.blocks if b.name == "L3"), ""),
                "L4": [m for m in built.messages if m.get("role") != "system"],
                "messages": built.messages,
            },
        )

    # ── 调试与测试用 ───────────────────────────────────────────────
    def _gap_minutes(self, chat_id: int) -> float:
        last = self._last_active.get(chat_id)
        if not last:
            return 0.0
        return max(0.0, (time.time() - last) / 60.0)

    def _last_user_text(self, chat_id: int) -> str:
        for item in reversed(self.history(chat_id)):
            if item.get("role") == "user":
                return str(item.get("content", ""))
        return ""

    def pending_count(self, chat_id: int) -> int:
        with self._lock:
            return len(self._pending.get(chat_id, []))

    def last_context(self) -> dict | None:
        return self._last_built.stats if self._last_built is not None else None

    def stats(self) -> dict:
        with self._lock:
            return {
                "chats": len(self._history),
                "pending_chats": len(self._pending),
                "reply_streaks": dict(self._reply_streak),
                "events": self.bus.stats(),
                "debugger": self.debugger.stats() if self.debugger else {},
            }
