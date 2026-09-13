"""Tool Policy：决定这条消息"要不要用工具、用哪一层、要不要叫主 LLM"。

三层（Phase 7 定稿）：
    L0  Python 直接处理 -> 0 token
    L1  本地模型处理    -> 0 AI API
    L2  主 LLM          -> 需要自然语言理解 / 角色表达时才进入
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tools.core.registry import ToolRegistry

URL_RE = re.compile(r"https?://\S+")

# 关键词 -> 工具名（顺序即优先级）
KEYWORD_RULES: tuple = (
    ("clock", ("几点", "现在时间", "现在几点", "今天几号", "几月几号", "星期几", "礼拜几", "过了多久", "倒计时")),
    ("game", ("骰子", "掷骰", "猜数字", "石头剪刀布", "玩个游戏", "陪我玩", "抛硬币")),
    ("weather", ("天气", "下雨", "气温", "冷不冷", "热不热", "要不要带伞", "多少度")),
    ("web_search", ("搜索", "搜一下", "帮我查", "查一下", "百度一下", "谷歌一下", "查资料")),
    ("location", ("这是哪", "在哪里", "在哪儿", "什么地方", "怎么走")),
)


@dataclass
class ToolDecision:
    level: str = "L2"
    tool: str = ""
    reason: str = ""
    args: dict = field(default_factory=dict)
    use_llm_after: bool = True     # 工具跑完后还要不要主 LLM 说话

    @property
    def is_direct(self) -> bool:
        """L0 且不需要模型 -> 结果可以直接发出去。"""
        return self.level == "L0" and not self.use_llm_after

    def as_log(self) -> str:
        return f"level={self.level} tool={self.tool or '-'} llm_after={self.use_llm_after} reason={self.reason}"


class ToolPolicy:
    def __init__(self, registry: ToolRegistry | None = None, *, enabled: bool = True) -> None:
        self.registry = registry
        self.enabled = bool(enabled)

    def _spec_level(self, name: str) -> str:
        if self.registry is None:
            return "L0"
        tool = self.registry.get(name)
        return tool.spec.level if tool else "L0"

    # -- 文本分流 -----------------------------------------------------
    def decide(self, text: str) -> ToolDecision:
        raw = (text or "").strip()
        if not raw:
            return ToolDecision(level="L2", reason="空消息")
        if not self.enabled:
            return ToolDecision(level="L2", reason="工具层已关闭")

        url = URL_RE.search(raw)
        if url:
            return ToolDecision(
                level=self._spec_level("web_reader"),
                tool="web_reader",
                reason="消息里有链接",
                args={"url": url.group(0)},
                use_llm_after=True,
            )

        for name, keywords in KEYWORD_RULES:
            hit = next((word for word in keywords if word in raw), "")
            if not hit:
                continue
            return ToolDecision(
                level=self._spec_level(name),
                tool=name,
                reason=f"命中关键词：{hit}",
                args={"text": raw, "keyword": hit},
                use_llm_after=name not in ("clock", "game"),
            )
        return ToolDecision(level="L2", reason="普通聊天")

    # -- 感知内容分流（图片/语音/视频/文档）--------------------------
    def decide_perception(self, source_type: str) -> ToolDecision:
        tool = {
            "image": "image",
            "video": "video",
            "voice": "audio",
            "audio": "audio",
            "document": "document",
        }.get(str(source_type), "")
        if not tool:
            return ToolDecision(level="L2", reason=f"未知媒体类型 {source_type}")
        return ToolDecision(
            level=self._spec_level(tool),
            tool=tool,
            reason=f"收到{source_type}",
            use_llm_after=True,
        )
