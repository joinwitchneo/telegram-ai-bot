"""小游戏：骰子、抛硬币、猜拳、猜数字。0 API、0 token。"""

from __future__ import annotations

import random
import re
import threading

from tools.core.result import ToolResult

DICE_RE = re.compile(r"(\d*)\s*(?:个)?\s*(?:d6|骰子)")
NUMBER_RANGE = (1, 100)


class GameHub:
    """每个会话一份猜数字状态，重启就重来（不写盘）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._guesses: dict[int, dict] = {}

    # ── 单项游戏 ────────────────────────────────────────────────────
    @staticmethod
    def dice(count: int = 1) -> ToolResult:
        count = max(1, min(6, int(count or 1)))
        rolls = [random.randint(1, 6) for _ in range(count)]
        total = sum(rolls)
        text = f"🎲 {rolls[0]}" if count == 1 else f"🎲 {' + '.join(str(item) for item in rolls)} = {total}"
        return ToolResult(name="game", ok=True, text=text, data={"rolls": rolls, "total": total})

    @staticmethod
    def coin() -> ToolResult:
        side = random.choice(("正面", "反面"))
        return ToolResult(name="game", ok=True, text=f"🪙 {side}", data={"side": side})

    @staticmethod
    def guess_hand(text: str) -> ToolResult:
        mine = random.choice(("石头", "剪刀", "布"))
        theirs = next((item for item in ("石头", "剪刀", "布") if item in text), "")
        if not theirs:
            return ToolResult(name="game", ok=True, text=f"我出{mine}。你出什么？", data={"mine": mine})
        beats = {"石头": "剪刀", "剪刀": "布", "布": "石头"}
        if theirs == mine:
            verdict = "平了"
        elif beats[mine] == theirs:
            verdict = "我赢"
        else:
            verdict = "你赢"
        return ToolResult(
            name="game", ok=True, text=f"我出{mine}，你出{theirs}，{verdict}。",
            data={"mine": mine, "theirs": theirs, "verdict": verdict},
        )

    # ── 猜数字（带状态）────────────────────────────────────────────
    def start_number(self, chat_id: int) -> ToolResult:
        target = random.randint(*NUMBER_RANGE)
        with self._lock:
            self._guesses[int(chat_id)] = {"target": target, "tries": 0}
        return ToolResult(
            name="game", ok=True,
            text=f"猜一个 {NUMBER_RANGE[0]}~{NUMBER_RANGE[1]} 的数，我数着次数。",
            data={"action": "start"},
        )

    def guess(self, chat_id: int, number: int) -> ToolResult:
        key = int(chat_id)
        with self._lock:
            state = self._guesses.get(key)
            if not state:
                return ToolResult(name="game", ok=True, text="我们还没开始猜数字，说「猜数字」我就开始。")
            state["tries"] += 1
            target = int(state["target"])
            tries = int(state["tries"])
            if number == target:
                self._guesses.pop(key, None)
                return ToolResult(
                    name="game", ok=True, text=f"就是 {target}。你猜了 {tries} 次。",
                    data={"action": "win", "tries": tries},
                )
            hint = "小了" if number < target else "大了"
            return ToolResult(
                name="game", ok=True, text=f"{hint}。再猜。",
                data={"action": "hint", "tries": tries},
            )

    def active(self, chat_id: int) -> bool:
        with self._lock:
            return int(chat_id) in self._guesses


def handle_text(text: str, *, chat_id: int = 0, hub: GameHub | None = None) -> ToolResult:
    """规则入口：从一句话里判断要玩哪个。"""
    raw = (text or "").strip()
    engine = hub or GameHub()
    if any(word in raw for word in ("猜数字", "猜个数")):
        return engine.start_number(chat_id)
    if engine.active(chat_id):
        numbers = re.findall(r"\d+", raw)
        if numbers:
            return engine.guess(chat_id, int(numbers[0]))
    if any(word in raw for word in ("抛硬币", "扔硬币", "正反面")):
        return engine.coin()
    if any(word in raw for word in ("石头剪刀布", "猜拳")):
        return engine.guess_hand(raw)
    match = DICE_RE.search(raw)
    if match or "骰子" in raw:
        count = int(match.group(1)) if match and match.group(1) else 1
        return engine.dice(count)
    return ToolResult.failure("game", "看不出要玩什么")
