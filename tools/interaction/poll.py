"""投票：解析"发起投票 主题 A B C"，由 Telegram 原生投票承载（0 API）。"""

from __future__ import annotations

import re

from tools.core.result import ToolResult

PREFIXES = ("发起投票", "搞个投票", "投票：", "投票:")


def parse(text: str) -> ToolResult:
    raw = (text or "").strip()
    for prefix in PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    else:
        return ToolResult.failure("poll", "看不出要投票的内容")
    parts = [item.strip() for item in re.split(r"[、,，/|]+", raw) if item.strip()]
    if len(parts) < 2:
        parts = [item.strip() for item in re.split(r"\s+", raw) if item.strip()]
    if len(parts) < 2:
        return ToolResult.failure("poll", "至少要有一个问题和两个选项，比如：发起投票 吃什么 面 饭")
    question, options = parts[0], parts[1:11]
    return ToolResult(
        name="poll",
        ok=True,
        text=f"发起投票：{question}（选项：{'、'.join(options)}）",
        data={"question": question, "options": options, "source_type": "poll"},
        source_type="poll", confidence=1.0,
    )
