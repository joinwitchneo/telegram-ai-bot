"""Token 估算与预算控制。

职责：估算 token、记账当日用量、判断是否超预算、按优先级裁剪上下文。
裁剪函数是纯函数，方便单测（Phase 2 的 Context Manager 会调用它）。
"""

from __future__ import annotations

import datetime
import math

CJK_RANGE = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0xAC00, 0xD7AF))


def estimate_tokens(text: str) -> int:
    """粗略估算 token：中文约 1.5 字/token，其它约 4 字符/token，另加固定开销。"""
    if not text:
        return 0
    cjk = 0
    for char in text:
        code = ord(char)
        if any(low <= code <= high for low, high in CJK_RANGE):
            cjk += 1
    other = max(0, len(text) - cjk)
    return int(math.ceil(cjk / 1.5 + other / 4)) + 4


def estimate_messages(messages: list[dict]) -> int:
    """估算一整组消息的 token（含每条的角色开销）。"""
    total = 0
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else str(message)
        total += estimate_tokens(str(content)) + 4
    return total


class TokenBudget:
    """当日预算记账与降档判定。"""

    LEVEL_OK = "ok"
    LEVEL_SOFT = "soft"   # 超软阈值：降档（strong→main→cheap）
    LEVEL_HARD = "hard"   # 超硬阈值：只做规则回复

    def __init__(
        self,
        *,
        max_context_tokens: int = 6000,
        max_output_tokens: int = 400,
        daily_token_budget: int = 300000,
        soft_ratio: float = 0.8,
        hard_ratio: float = 0.95,
    ) -> None:
        self.max_context_tokens = max(500, int(max_context_tokens))
        self.max_output_tokens = max(50, int(max_output_tokens))
        self.daily_token_budget = max(1000, int(daily_token_budget))
        self.soft_ratio = min(max(soft_ratio, 0.1), 0.99)
        self.hard_ratio = min(max(hard_ratio, self.soft_ratio), 1.0)
        self._used = 0
        self._used_on = datetime.date.today().isoformat()

    # ── 记账 ────────────────────────────────────────────────────────
    def _roll_day(self) -> None:
        today = datetime.date.today().isoformat()
        if today != self._used_on:
            self._used_on = today
            self._used = 0

    def record(self, tokens: int) -> int:
        self._roll_day()
        self._used += max(0, int(tokens))
        return self._used

    def used_today(self) -> int:
        self._roll_day()
        return self._used

    def ratio(self) -> float:
        return self.used_today() / self.daily_token_budget

    def level(self) -> str:
        ratio = self.ratio()
        if ratio >= self.hard_ratio:
            return self.LEVEL_HARD
        if ratio >= self.soft_ratio:
            return self.LEVEL_SOFT
        return self.LEVEL_OK

    def state(self) -> dict:
        return {
            "used": self.used_today(),
            "limit": self.daily_token_budget,
            "ratio": round(self.ratio(), 4),
            "level": self.level(),
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
        }

    def can_call(self, estimated_input: int, estimated_output: int = 0) -> tuple[bool, str]:
        """这一轮还能不能调模型。"""
        level = self.level()
        if level == self.LEVEL_HARD:
            return False, "当日预算已达硬上限，本轮只做规则回复"
        total = estimated_input + estimated_output
        if estimated_input > self.max_context_tokens:
            return True, f"输入超上下文上限（{estimated_input}>{self.max_context_tokens}），需要裁剪"
        if self.used_today() + total > self.daily_token_budget:
            return False, "加上本轮预计用量会超日预算"
        return True, ""


def trim_blocks(blocks: list[dict], max_tokens: int) -> tuple[list[dict], list[str]]:
    """按优先级裁剪上下文块。

    blocks: [{"name": str, "tokens": int, "priority": int, "text": str}]
    priority 越小越重要：0 = 永不裁剪（L0/L1/硬规则/protected），
    1 = 次要状态/风格，2 = 低价值记忆，3 = COLD 记忆，4 = 无关历史。
    返回 (保留的块, 被裁掉的块名)。
    """
    total = sum(int(b.get("tokens", 0)) for b in blocks)
    if total <= max_tokens:
        return list(blocks), []
    kept = list(blocks)
    dropped: list[str] = []
    order = sorted(
        [b for b in kept if int(b.get("priority", 0)) > 0],
        key=lambda b: (-int(b.get("priority", 0)), -int(b.get("tokens", 0))),
    )
    for block in order:
        if total <= max_tokens:
            break
        if block in kept:
            kept.remove(block)
            dropped.append(str(block.get("name", "?")))
            total -= int(block.get("tokens", 0))
    return kept, dropped
