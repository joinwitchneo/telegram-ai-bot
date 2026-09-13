"""Context Manager：按模式、预算和相关性动态组装上下文（Phase 2）。

结构固定为五层：
    L0 System Contract  —— 身份、输出协议、硬性边界（永不裁剪，静态前缀的一部分）
    L1 Persona          —— 核心人格（永不裁剪，静态前缀的一部分）
    L2 Current State    —— 当前状态摘要（Phase 2 只留接口，可空）
    L3 Relevant Memory  —— 相关记忆（Phase 2 只留接口，默认空）
    L4 Recent Conversation —— 最近对话（按模式给窗口，再按预算裁剪）

动态内容一律排在静态前缀之后，保证 prompt cache 前缀稳定。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

from core.token_budget import estimate_messages, estimate_tokens, trim_blocks

MODES = ("CASUAL", "DEEP", "EMOTIONAL", "TASK")

DEFAULT_WINDOWS = {"CASUAL": 12, "DEEP": 25, "EMOTIONAL": 25, "TASK": 12}

EMOTION_WORDS = (
    "难过", "委屈", "好烦", "很烦", "烦死", "难受", "生气", "想哭", "崩溃", "焦虑",
    "压力", "撑不住", "不想活", "emo", "心态炸", "失眠", "孤独", "寂寞", "失落",
    "考砸", "搞砸", "没考好", "被骂", "吵架", "分手",
)
TASK_WORDS = (
    "帮我", "提醒我", "记一下", "记个", "查一下", "帮我查", "搜索", "搜一下", "翻译",
    "算一下", "列一下", "整理", "写个", "生成", "总结一下", "怎么办", "怎么做", "定个",
)
DEEP_MARKERS = (
    "其实我", "我想跟你说", "我说实话", "说实话", "我最近", "你觉得", "你怎么想",
    "为什么", "我在想", "认真", "纠结", "犹豫",
)

# Phase 8：只有这类话题才召回 Self Model（正常聊天绝不加载她的存在主义思考）
SELF_TRIGGERS = (
    "你是谁", "你是什么", "你是程序", "你是ai", "你算不算", "你有没有意识", "你有意识",
    "你会害怕", "你害怕", "你难过吗", "删除", "删掉", "清空", "备份", "重置", "换电脑",
    "换模型", "deepseek", "模型", "存在", "活着", "死", "消失", "记忆", "真不真", "真的情绪",
    "人和ai", "人类", "你自己", "自我",
    "自己是", "自己是什么", "觉得自己是", "是不是人", "程序吗",
    "情绪是真的", "ai的情绪", "ai的情绪", "有没有感情", "有感情",
)


def wants_self_model(text: str) -> bool:
    """这一轮要不要把 Self Model 放进上下文（0 token 的纯规则判断）。"""
    lowered = (text or "").lower()
    return any(word in lowered for word in SELF_TRIGGERS)


def detect_mode(text: str, history: list[dict] | None = None) -> str:
    """用简单规则判断对话模式（不调用模型，判断不了就 CASUAL）。"""
    raw = (text or "").strip()
    lowered = raw.lower()
    cjk_len = len(re.findall(r"[\u4e00-\u9fff]", raw))
    if any(word in lowered for word in EMOTION_WORDS):
        return "EMOTIONAL"
    if any(word in lowered for word in TASK_WORDS):
        return "TASK"
    if cjk_len >= 80 or any(marker in lowered for marker in DEEP_MARKERS):
        return "DEEP"
    if history:
        long_user_messages = 0
        for item in history[-6:]:
            if item.get("role") == "user" and len(str(item.get("content", ""))) >= 60:
                long_user_messages += 1
        if long_user_messages >= 3:
            return "DEEP"
    return "CASUAL"


@dataclass
class ContextBlock:
    name: str
    text: str
    priority: int          # 0 = 永不裁剪
    layer: str = ""        # L0 / L1 / L2 / L3 / L4
    dynamic: bool = False

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class BuiltContext:
    messages: list[dict]
    mode: str
    blocks: list[ContextBlock] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def static_prefix(self) -> str:
        return "".join(b.text for b in self.blocks if not b.dynamic)

    @property
    def dynamic_text(self) -> str:
        return "".join(b.text for b in self.blocks if b.dynamic)


class ContextManager:
    def __init__(
        self,
        *,
        contract: str,
        persona: str,
        token_budget=None,
        windows: dict | None = None,
        max_context_tokens: int = 6000,
        min_history: int = 2,
    ) -> None:
        self.contract = contract
        self.persona = persona
        self.budget = token_budget
        self.windows = {**DEFAULT_WINDOWS, **(windows or {})}
        self.max_context_tokens = max(500, int(max_context_tokens))
        self.min_history = max(0, int(min_history))

    # ── 静态前缀 ────────────────────────────────────────────────────
    def static_prefix(self) -> str:
        """L0 + L1，字节稳定，不含时间/随机值/动态状态。"""
        parts = [self.contract.strip()]
        if self.persona.strip():
            parts.append(f"【人格】\n{self.persona.strip()}")
        return "\n\n".join(part for part in parts if part)

    # ── 组装 ────────────────────────────────────────────────────────
    def build(
        self,
        *,
        user_text: str,
        history: list[dict] | None = None,
        state: dict | None = None,
        memory_blocks: list[dict] | None = None,
        mode: str | None = None,
        now: datetime.datetime | None = None,
        extra_system: str | None = None,
    ) -> BuiltContext:
        history = list(history or [])
        mode = (mode or detect_mode(user_text, history)).upper()
        if mode not in MODES:
            mode = "CASUAL"
        max_context = self.max_context_tokens
        if self.budget is not None:
            max_context = min(max_context, int(getattr(self.budget, "max_context_tokens", max_context)))

        persona_text = f"【人格】\n{self.persona.strip()}" if self.persona.strip() else ""
        blocks: list[ContextBlock] = [
            ContextBlock(name="L0", text=self.contract.strip(), priority=0, layer="L0"),
            ContextBlock(name="L1", text=persona_text, priority=0, layer="L1"),
        ]
        state_text = self._render_state(state, mode)
        if state_text:
            blocks.append(ContextBlock(name="L2", text=state_text, priority=1, layer="L2", dynamic=True))
        memory_text = self._render_memory(memory_blocks, mode)
        if memory_text:
            blocks.append(
                ContextBlock(name="L3", text=memory_text, priority=2, layer="L3", dynamic=True)
            )

        window = int(self.windows.get(mode, 12))
        recent = history[-window:] if window > 0 else []
        static_tokens = sum(b.tokens for b in blocks)
        budget_left = max(0, max_context - static_tokens - estimate_tokens(user_text))

        # 先按优先级裁剪 L2/L3（使用 Phase 1 的 trim_blocks）
        kept, dropped = trim_blocks(
            [{"name": b.name, "tokens": b.tokens, "priority": b.priority, "ref": b} for b in blocks],
            max(0, budget_left),
        )
        kept_names = {entry["name"] for entry in kept}
        blocks = [b for b in blocks if b.name in kept_names]

        # L4 历史：按"越旧越先裁"逐条裁，至少保留最近 min_history 条
        history_tokens = estimate_messages(recent)
        while recent and static_tokens + history_tokens > max_context - estimate_tokens(user_text):
            if len(recent) <= self.min_history:
                break
            removed = recent.pop(0)
            dropped.append(f"L4:{str(removed.get('content', ''))[:12]}")
            history_tokens = estimate_messages(recent)

        # 动态消息 = 各动态层（L2 状态 / L3 记忆）+ 模式与时间
        dynamic_parts = [block.text for block in blocks if block.dynamic and block.text.strip()]
        dynamic_parts.append(self._render_dynamic(mode, now))
        dynamic_text = "\n\n".join(part for part in dynamic_parts if part.strip())
        messages: list[dict] = [
            {"role": "system", "content": self.static_prefix()},
        ]
        if dynamic_text:
            messages.append({"role": "system", "content": dynamic_text})
        messages.extend(recent)
        if (user_text or "").strip():
            messages.append({"role": "user", "content": user_text})
        # 感知/主动消息这类"这一轮要干什么"的指令必须放在最后：
        # 夹在历史和当前消息中间时，模型容易被更早的历史带偏。
        if extra_system and str(extra_system).strip():
            messages.append({"role": "system", "content": str(extra_system).strip()})

        stats = {
            "mode": mode,
            "window": window,
            "history_count": len(recent),
            "history_dropped": sum(1 for d in dropped if d.startswith("L4:")),
            "tokens": {
                "L0": estimate_tokens(self.contract.strip()),
                "L1": estimate_tokens(persona_text),
                "L2": next((b.tokens for b in blocks if b.name == "L2"), 0),
                "L3": next((b.tokens for b in blocks if b.name == "L3"), 0),
                "L4": history_tokens,
                "user": estimate_tokens(user_text),
            },
            "total_context_tokens": (
                estimate_tokens(self.contract.strip())
                + estimate_tokens(persona_text)
                + next((b.tokens for b in blocks if b.name == "L2"), 0)
                + next((b.tokens for b in blocks if b.name == "L3"), 0)
                + history_tokens
                + estimate_tokens(user_text)
                + estimate_tokens(dynamic_text)
            ),
            "max_context_tokens": max_context,
            "dropped": dropped,
        }
        return BuiltContext(messages=messages, mode=mode, blocks=blocks, dropped=dropped, stats=stats)

    # ── 各层渲染 ────────────────────────────────────────────────────
    def _render_state(self, state: dict | None, mode: str) -> str:
        """L2：当前状态（情绪 / 关系 / 话题 / 行为倾向 / 回复倾向 / 对方风格）。

        只给自然语言，数字留在程序里；空状态不占 token。
        """
        lines: list[str] = []
        if state:
            emotion = str(state.get("emotion") or "").strip()
            relationship = str(state.get("relationship") or "").strip()
            topic = str(state.get("topic") or "").strip()
            behavior = str(state.get("behavior") or "").strip()
            tone = str(state.get("tone") or "").strip()
            style = str(state.get("style") or "").strip()
            self_view = str(state.get("self_model") or "").strip()
            if emotion:
                lines.append(emotion if emotion.startswith("当前情绪") else f"当前情绪：{emotion}")
            if relationship:
                lines.append(relationship if relationship.startswith("关系") else f"当前关系：{relationship}")
            if topic:
                lines.append(f"当前话题：{topic}")
            if behavior:
                lines.append(f"行为影响：{behavior}")
            if tone:
                lines.append(f"回复倾向：{tone}")
            if style:
                lines.append(f"对方风格：{style}")
            if self_view:
                # Self Model 是"她自己怎么看自己"，只在相关话题里出现，且限长
                lines.append(f"【我自己】{self_view[:220]}")
        if not lines:
            return ""
        return "【当前状态】\n" + "\n".join(lines)

    def _render_memory(self, memory_blocks: list[dict] | None, mode: str) -> str:
        """L3：相关记忆。Phase 2 只接收调用方传入的内容，不自己做检索。"""
        if not memory_blocks:
            return ""
        lines = []
        for item in memory_blocks:
            text = str(item.get("content", "")).strip()
            if not text:
                continue
            tag = str(item.get("type", "") or "").strip()
            lines.append(f"- {text}" + (f"（{tag}）" if tag else ""))
        if not lines:
            return ""
        return "【相关记忆】\n" + "\n".join(lines)

    def _render_dynamic(self, mode: str, now: datetime.datetime | None) -> str:
        """动态尾部：模式 + 时间。放在静态前缀之后，不污染 cache。"""
        moment = now or datetime.datetime.now()
        return (
            f"【当前模式】{mode}\n"
            f"【当前时间】{moment.strftime('%Y-%m-%d %H:%M')}"
            f"（星期{'一二三四五六日'[moment.weekday()]}）——要提时间就用这个，不要猜。"
        )
