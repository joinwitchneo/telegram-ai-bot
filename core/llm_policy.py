"""LLM Policy：所有模型调用的总闸门。

它只回答六个问题（V2 方案定稿的职责边界）：
1. 这一轮到底需不需要调用 LLM？
2. Python 能不能直接解决？
3. 需不需要等用户把话说完再合并？
4. 该用哪一档模型（cheap / main / strong）？
5. 需不需要外部工具？
6. 当前预算允不允许？需不需要降档？

它**不**决定消息怎么发（条数、长度、停顿、表情包）——那是 Response Planner 的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORIES = ("chat", "summary", "extraction", "proactive", "tool", "retry", "rule")

FILLER_WORDS = {
    "哈哈", "哈哈哈", "哈哈哈哈", "嗯", "嗯嗯", "哦", "哦哦", "好", "好的", "行", "行吧",
    "知道", "知道了", "6", "666", "……", "...", "?", "？",
}

TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "weather": ("天气", "下雨", "气温", "冷不冷", "热不热", "穿什么"),
    "search": ("搜索", "查一下", "帮我查", "搜一下", "百度", "谷歌"),
    "reminders": ("提醒", "记一下", "记个", "待办", "别忘了", "别忘"),
    "memos": ("备忘", "记住", "存一下", "资料", "密码", "账号"),
}

_URL_RE = re.compile(r"https?://|\bwww\.")
_QUESTION_RE = re.compile(r"[?？]")


@dataclass
class PolicyDecision:
    use_llm: bool
    tier: str = "none"              # cheap / main / strong / none
    category: str = "chat"
    reason: str = ""
    tools_needed: list[str] = field(default_factory=list)
    merge_needed: bool = False
    is_filler: bool = False
    budget_level: str = "ok"
    notes: list[str] = field(default_factory=list)

    def as_log(self) -> str:
        tools = ",".join(self.tools_needed) or "-"
        return (
            f"use_llm={self.use_llm} tier={self.tier} category={self.category} "
            f"merge={self.merge_needed} tools={tools} budget={self.budget_level} reason={self.reason}"
        )


def is_filler(text: str) -> bool:
    """纯确认词/拟声词，不需要动用主模型。"""
    stripped = (text or "").strip().strip("。！!~～ ")
    if not stripped or len(stripped) > 8:
        return False
    return stripped in FILLER_WORDS


def detect_tools(text: str) -> list[str]:
    """按关键词判断这一轮可能需要哪些外部工具（懒加载用）。"""
    found: list[str] = []
    lowered = (text or "").lower()
    for tool, keywords in TOOL_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            found.append(tool)
    return found


def decide(
    text: str,
    *,
    category: str = "chat",
    budget_state: dict | None = None,
    rules_handled: bool = False,
    is_command: bool = False,
    has_attachment: bool = False,
) -> PolicyDecision:
    """总闸门判定。纯函数，方便单测。"""
    category = category if category in CATEGORIES else "chat"
    budget_level = str((budget_state or {}).get("level", "ok"))
    tools = detect_tools(text)
    decision = PolicyDecision(
        use_llm=True,
        tier="main",
        category=category,
        tools_needed=tools,
        budget_level=budget_level,
    )

    # 1) 命令 / 规则已处理 → 完全不调模型
    if is_command or rules_handled or category == "rule":
        decision.use_llm = False
        decision.tier = "none"
        decision.reason = "命令或规则可处理"
        return decision

    # 2) 摘要与抽取一律走便宜模型
    if category in ("summary", "extraction"):
        decision.tier = "cheap"
        decision.reason = "摘要/抽取用便宜模型"
        return decision

    if category == "retry":
        decision.tier = "main"
        decision.reason = "重试沿用主模型"
        return decision

    # 3) 预算：硬超限只做规则回复，软超限降档
    if budget_level == "hard":
        decision.use_llm = False
        decision.tier = "none"
        decision.reason = "当日预算已达硬上限，改为规则回复"
        return decision
    if budget_level == "soft":
        decision.notes.append("预算偏紧，降档处理")
        decision.tier = "cheap" if category != "proactive" else "main"
        decision.reason = "预算偏紧已降档"
        return decision

    # 4) 纯拟声词/确认词 → 便宜模型就够
    if is_filler(text):
        decision.is_filler = True
        decision.tier = "cheap"
        decision.reason = "纯确认词，用便宜模型"
        return decision

    # 5) 正式聊天 → 主模型
    decision.tier = "main"
    decision.reason = "正式聊天用主模型"
    if has_attachment:
        decision.notes.append("带附件，需要结合图片/文件上下文")
    if _URL_RE.search(text or ""):
        decision.notes.append("消息里有链接")
    # 短句且不是提问 → 建议先等一会儿，可能还有后续消息
    stripped = (text or "").strip()
    decision.merge_needed = len(stripped) <= 20 and not _QUESTION_RE.search(stripped)
    return decision
