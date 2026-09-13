"""Style Adapter（Phase 5）：把用户风格统计翻译成自然语言参考。

两条纪律：
1. 只输出"参考"，不是"复制"——绝不能建议逐字模仿、复读用户；
2. 不把数字写进 Prompt（数字属于程序），只给定性描述。
"""

from __future__ import annotations

MAX_LINES = 2

MIRROR_WARNING = "参考他的习惯调整自己的长短就好，别逐字模仿、别复读他说过的话。"


def guidance(profile: dict | None, *, max_lines: int = MAX_LINES) -> list[str]:
    if not profile or not profile.get("ready"):
        return []
    lines: list[str] = []
    try:
        average = float(profile.get("average_message_length", 0.0))
        short_ratio = float(profile.get("short_message_ratio", 0.0))
        long_ratio = float(profile.get("long_message_ratio", 0.0))
        bursts = float(profile.get("consecutive_message_count", 0.0))
        emoji = float(profile.get("emoji_frequency", 0.0))
        questions = float(profile.get("question_frequency", 0.0))
    except (TypeError, ValueError):
        return []

    if average and average <= 12 and short_ratio >= 0.5:
        lines.append("对方习惯发很短的消息，别长篇大论")
    elif long_ratio >= 0.3:
        lines.append("对方偶尔会一次说很长一段，能接住细节")
    elif questions >= 0.35:
        lines.append("对方常反问，回答可以带一点自己的态度")

    if bursts >= 2.2:
        lines.append("对方经常连发几条，可以顺着这个节奏")
    elif emoji >= 0.25:
        lines.append("对方会用表情符号")
    elif profile.get("punctuation_style") == "极少标点":
        lines.append("对方很少用标点，语气偏随意")

    lines = lines[:max_lines]
    lines.append(MIRROR_WARNING)
    return lines


def render(profile: dict | None) -> str:
    lines = guidance(profile)
    if not lines:
        return ""
    return "；".join(lines)
