"""ActionDecisionEngine（Phase 6 §十七/§二十/§二十一）。

**Python 是最终决策者**：LLM 只给出 Proposal（intention），这里结合
候选念头的触发分、意图置信度、环境可用性、预算与档位，决定到底做什么。
允许（而且经常）输出 NO_ACTION —— 这不是失败，是自主系统的正常状态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from v3.actions.base import JOURNAL, MESSAGE, NO_ACTION


@dataclass
class ActionDecision:
    action: str = NO_ACTION
    reason: str = ""
    content: str = ""
    confidence: float = 0.0
    trigger_score: float = 0.0
    thought_id: str = ""
    trace_id: str = ""
    would_action: str = ""
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class ActionDecisionEngine:
    def __init__(
        self,
        *,
        mode_getter=None,
        action_threshold: float = 0.80,
        min_confidence: float = 0.60,
        allow_message: bool = True,
    ) -> None:
        self.mode_getter = mode_getter or (lambda: "observe")
        self.action_threshold = float(action_threshold)
        self.min_confidence = float(min_confidence)
        self.allow_message = bool(allow_message)

    def decide(self, *, candidate, result, availability=None, now=None) -> ActionDecision:
        availability = dict(availability or {})
        candidate = dict(candidate or {})
        thought_id = str(candidate.get("thought_id", ""))
        score = float(candidate.get("trigger_score") or 0.0)
        trace_id = str(getattr(result, "trace_id", "") or candidate.get("trace_id", ""))
        mode = str(self.mode_getter() or "observe").lower()

        if not getattr(result, "ok", False):
            return ActionDecision(
                action=NO_ACTION, reason=f"认知未产出可用提案：{getattr(result, 'error', '')}",
                trigger_score=score, thought_id=thought_id, trace_id=trace_id,
            )

        intention = getattr(result, "intention", None)
        kind = str(getattr(intention, "type", "none") or "none").lower()
        confidence = float(getattr(intention, "confidence", 0.0) or 0.0)
        content = str(getattr(intention, "content", "") or "").strip()

        if kind == "none":
            return ActionDecision(action=NO_ACTION, reason="认知器官认为现在不必行动",
                                  confidence=confidence, trigger_score=score,
                                  thought_id=thought_id, trace_id=trace_id)

        # 1) 想说话：必须同时满足"念头够重要 + 够自信 + 对外行动可用"
        if kind == "message":
            if not self.allow_message:
                return ActionDecision(action=JOURNAL, reason="对外消息当前被策略关闭，退化为记日志",
                                      content=content, confidence=confidence, trigger_score=score,
                                      thought_id=thought_id, trace_id=trace_id,
                                      would_action=MESSAGE)
            if score < self.action_threshold:
                return ActionDecision(
                    action=JOURNAL, reason=f"触发分 {score} 低于行动阈值 {self.action_threshold}，"
                                           "只记日志",
                    content=content, confidence=confidence, trigger_score=score,
                    thought_id=thought_id, trace_id=trace_id, would_action=MESSAGE)
            if confidence < self.min_confidence:
                return ActionDecision(
                    action=JOURNAL, reason=f"意图置信度 {confidence} 低于 {self.min_confidence}，"
                                           "只记日志",
                    content=content, confidence=confidence, trigger_score=score,
                    thought_id=thought_id, trace_id=trace_id, would_action=MESSAGE)
            if not availability.get("message_available", True):
                return ActionDecision(
                    action=JOURNAL, reason=f"对外行动不可用：{availability.get('message_reason', '')}",
                    content=content, confidence=confidence, trigger_score=score,
                    thought_id=thought_id, trace_id=trace_id, would_action=MESSAGE)
            if mode in ("observe", "off", ""):
                return ActionDecision(
                    action=JOURNAL, reason="观察档：只记录本来会发的话，不真正发送",
                    content=content, confidence=confidence, trigger_score=score,
                    thought_id=thought_id, trace_id=trace_id, would_action=MESSAGE)
            return ActionDecision(action=MESSAGE, reason=str(getattr(intention, "reason", ""))[:200],
                                  content=content, confidence=confidence, trigger_score=score,
                                  thought_id=thought_id, trace_id=trace_id)

        # 2) 只是想记点什么
        if kind == "journal":
            if not content:
                return ActionDecision(action=NO_ACTION, reason="打算记录却没给内容",
                                      confidence=confidence, trigger_score=score,
                                      thought_id=thought_id, trace_id=trace_id)
            return ActionDecision(action=JOURNAL, reason=str(getattr(intention, "reason", ""))[:200],
                                  content=content, confidence=confidence, trigger_score=score,
                                  thought_id=thought_id, trace_id=trace_id)

        return ActionDecision(action=NO_ACTION, reason=f"未知意图 {kind}",
                              confidence=confidence, trigger_score=score,
                              thought_id=thought_id, trace_id=trace_id)
