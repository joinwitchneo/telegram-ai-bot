"""DecisionEngine：Desire != Action。MVP 的合法动作只有两个。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

NO_ACTION = "NO_ACTION"
JOURNAL_NOTE = "JOURNAL_NOTE"

MOTIVATION_THRESHOLD = 0.55     # 低于此分数直接 NoAction


@dataclass
class Decision:
    action: str = NO_ACTION
    reason: str = ""
    content: str = ""
    motivation_score: float = 0.0
    would_action: str = ""      # observe 档下"本来会做什么"

    def to_dict(self) -> dict:
        return asdict(self)


class DecisionEngine:
    def __init__(self, *, threshold: float = MOTIVATION_THRESHOLD, mode_getter=None) -> None:
        self.threshold = float(threshold)
        self._mode_getter = mode_getter or (lambda: "observe")

    def decide(self, *, motivation, thought=None) -> Decision:
        mode = str(self._mode_getter() or "observe").lower()
        if motivation is None:
            return Decision(reason="没有动机状态")
        score = float(getattr(motivation, "score", 0.0) or 0.0)
        if score < self.threshold:
            return Decision(action=NO_ACTION, reason=f"动机 {score} 低于阈值 {self.threshold}",
                            motivation_score=score)
        content = str(getattr(thought, "content", "") or "").strip()
        if not content:
            return Decision(action=NO_ACTION, reason="没有可记录的念头", motivation_score=score)

        if mode == "observe":
            # MVP 的观察档：只记录"本来会写一条日志"，不产生任何对外行为
            return Decision(
                action=JOURNAL_NOTE, reason="观察档：只记 inner_journal（不执行任何对外动作）",
                content=content, motivation_score=score, would_action=JOURNAL_NOTE,
            )
        return Decision(action=JOURNAL_NOTE, reason="记录一条内在日志", content=content,
                        motivation_score=score)
