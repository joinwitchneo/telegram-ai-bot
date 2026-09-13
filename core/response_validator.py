"""Response Validator（Phase 5）：Python 拥有最终裁决权。

模型返回什么都不能直接执行：
- message_count 夹到 1~4
- pause 夹到 0.5~8 秒
- 单条长度按回复长度档限制，超长重新拆分 / 截断
- sticker 只能由 Planner 先允许，模型不能自己打开
- 危险内部字段（system / prompt / debug / tokens …）一律剔除
- 空回复、坏 JSON → fallback 文案（来自配置，不写死）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from style.message_renderer import RenderLimits
from style.message_splitter import render_messages

DANGEROUS_KEYS = (
    "system", "prompt", "system_prompt", "internal", "debug", "raw", "tokens",
    "state", "memory", "memories", "score", "trace", "log", "policy",
)

DEFAULT_FALLBACKS = ("等下，我刚刚有点卡。", "嗯…我这边抽了一下，你刚说什么？")

DEFAULT_LENGTH_CAPS = {
    "ULTRA_SHORT": 30,
    "SHORT": 60,
    "NORMAL": 200,
    "LONG": 800,
}

FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    text = FENCE_RE.sub("", text)
    return text.strip()


def strip_internal(plan: dict | None) -> dict:
    """剔除不该出现在渲染/日志里的内部字段。"""
    clean: dict = {}
    for key, value in (plan or {}).items():
        if str(key).lower() in DANGEROUS_KEYS:
            continue
        clean[key] = value
    return clean


@dataclass
class Candidate:
    """模型原始输出解析结果（还没经过硬限制）。"""

    messages: list[str] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass
class ValidatedReply:
    ok: bool
    messages: list[str]
    plan: dict
    issues: list[str] = field(default_factory=list)
    fallback_used: bool = False

    def to_render_dict(self) -> dict:
        return {**self.plan, "messages": list(self.messages)}


def parse_candidate(raw: object) -> Candidate:
    """把模型输出解析成 (messages, plan)。支持纯文本与 JSON 两种。"""
    issues: list[str] = []
    if isinstance(raw, dict):
        plan = strip_internal({k: v for k, v in raw.items() if k != "messages"})
        messages = [_clean_text(item) for item in (raw.get("messages") or [])]
        messages = [item for item in messages if item]
        if not messages:
            content = _clean_text(raw.get("content", ""))
            messages = [line for line in content.split("\n") if line.strip()]
        return Candidate(messages=messages, plan=plan, issues=issues)

    text = _clean_text(raw)
    if not text:
        return Candidate(messages=[], plan={}, issues=["empty_raw"])

    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            issues.append("bad_json")
        else:
            if isinstance(data, dict):
                parsed = parse_candidate(data)
                parsed.issues.extend(issues)
                return parsed
            issues.append("json_not_object")

    messages = [line.strip() for line in text.split("\n") if line.strip()]
    return Candidate(messages=messages, plan={}, issues=issues)


class ResponseValidator:
    def __init__(
        self,
        *,
        limits: RenderLimits | None = None,
        fallback_texts: tuple[str, ...] | list[str] | None = None,
        length_caps: dict | None = None,
        default_length: str = "NORMAL",
        split_min_chars: int = 20,
    ) -> None:
        self.limits = limits or RenderLimits()
        texts = [str(item).strip() for item in (fallback_texts or DEFAULT_FALLBACKS) if str(item).strip()]
        self.fallback_texts = texts or list(DEFAULT_FALLBACKS)
        self.length_caps = {**DEFAULT_LENGTH_CAPS, **(length_caps or {})}
        self.default_length = default_length
        self.split_min_chars = max(10, int(split_min_chars))
        self._fallback_index = 0

    # ── 长度上限 ────────────────────────────────────────────────────
    @staticmethod
    def parse_candidate(raw: object) -> Candidate:
        """模块级 parse_candidate 的方法别名，方便调用方持有实例。"""
        return parse_candidate(raw)

    def cap_for(self, length: str | None) -> int:
        key = str(length or self.default_length).upper()
        cap = self.length_caps.get(key, self.length_caps.get(self.default_length, 200))
        return max(10, min(int(cap), int(self.limits.max_chars)))

    # ── 主校验 ──────────────────────────────────────────────────────
    def validate(
        self,
        messages: list[str] | None,
        plan: dict | None = None,
        *,
        sticker_allowed: bool = False,
        max_count: int | None = None,
    ) -> ValidatedReply:
        issues: list[str] = []
        plan = strip_internal(plan)
        cleaned = [_clean_text(item) for item in (messages or [])]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return self.fallback("empty_response")

        length = str(plan.get("length", self.default_length) or self.default_length).upper()
        cap = self.cap_for(length)

        # 超长：先尝试重新拆分，实在不行再截断
        expanded: list[str] = []
        for index, item in enumerate(cleaned):
            if len(item) <= cap:
                expanded.append(item)
                continue
            issues.append(f"too_long:{index}")
            pieces = render_messages(
                item, target_count=len(cleaned), max_messages=self.limits.max_messages
            )
            pieces = [piece[:cap] for piece in pieces]
            if not pieces:
                pieces = [item[:cap]]
            expanded.extend(pieces)
        cleaned = expanded

        # 条数：Planner 决定"应该拆几条"，模型自己的分行也照发——
        # 模型给了一整段时按标点拆开，模型已经分成几条时就保持分开（上限 4 条）。
        proposed = len(cleaned)
        try:
            planned = int(plan.get("message_count") or proposed or 1)
        except (TypeError, ValueError):
            planned = proposed or 1
        if max_count is not None:
            try:
                planned = max(planned, max(1, int(max_count)))
            except (TypeError, ValueError):
                pass
        count = max(1, min(max(planned, proposed), int(self.limits.max_messages)))
        # 模型把好几句挤成一整段（计划也说 1 条）时，仍然按标点拆成连发
        if count == 1 and proposed == 1 and len(cleaned[0]) >= 24:
            sentence_ends = sum(cleaned[0].count(mark) for mark in "。！？!?")
            if sentence_ends >= 2 or (sentence_ends >= 1 and "，" in cleaned[0]):
                count = min(3, int(self.limits.max_messages))
                issues.append("auto_split_paragraph")
        if len(cleaned) > count:
            issues.append("message_count_merged")
            head = cleaned[: count - 1]
            tail = "".join(cleaned[count - 1 :])
            cleaned = head + [tail[:cap]]
        elif len(cleaned) < count:
            # 只有一条长消息、而计划允许拆 → 按标点拆成几条连发
            # 计划明确要求多条时不留长度门槛（那是 Planner 的决定）；
            # 只有"计划 1 条但内容很长"才用 split_min_chars 兜底自动拆。
            if len(cleaned) == 1 and count > 1:
                pieces = render_messages(
                    cleaned[0], target_count=count, max_messages=self.limits.max_messages
                )
                pieces = [piece[:cap] for piece in pieces if piece.strip()]
                if len(pieces) > 1:
                    cleaned = pieces
                    issues.append("message_count_split")
                else:
                    count = len(cleaned)
            else:
                issues.append("message_count_trimmed")
                count = len(cleaned)

        cleaned = [item[:cap] for item in cleaned if item.strip()]
        if not cleaned:
            return self.fallback("empty_after_validate")

        # 停顿
        pause = self._normalize_pause(plan.get("pause"), issues)

        # 表情包：只能由 Planner 允许，模型不能自己打开
        wants_sticker = bool(plan.get("sticker"))
        final_sticker = bool(sticker_allowed) and wants_sticker
        if wants_sticker and not final_sticker:
            issues.append("sticker_blocked")

        final_plan = {
            "message_count": len(cleaned),
            "length": length,
            "tone": str(plan.get("tone", "NEUTRAL") or "NEUTRAL").upper(),
            "reply_mode": str(plan.get("reply_mode", "CASUAL") or "CASUAL").upper(),
            "split": len(cleaned) > 1,
            "pause": [round(pause[0], 2), round(pause[1], 2)],
            "sticker": final_sticker,
        }
        return ValidatedReply(ok=True, messages=cleaned, plan=final_plan, issues=issues)

    def _normalize_pause(self, value: object, issues: list[str]) -> tuple[float, float]:
        floor = float(self.limits.min_pause)
        ceiling = float(self.limits.max_pause)
        low, high = floor, min(1.0, ceiling)
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                low, high = float(value[0]), float(value[1])
            except (TypeError, ValueError):
                low, high = floor, min(1.0, ceiling)
        elif isinstance(value, (int, float)):
            low = high = float(value)
        if low > high:
            low, high = high, low
        clamped_low = max(floor, min(low, ceiling))
        clamped_high = max(floor, min(high, ceiling))
        if (clamped_low, clamped_high) != (low, high):
            issues.append("pause_clamped")
        if clamped_low > clamped_high:
            clamped_low = clamped_high
        return clamped_low, clamped_high

    # ── 兜底 ────────────────────────────────────────────────────────
    def fallback(self, reason: str = "") -> ValidatedReply:
        text = self.fallback_texts[self._fallback_index % len(self.fallback_texts)]
        self._fallback_index += 1
        plan = {
            "message_count": 1,
            "length": "SHORT",
            "tone": "NEUTRAL",
            "reply_mode": "CASUAL",
            "split": False,
            "pause": [0.5, 1.0],
            "sticker": False,
        }
        return ValidatedReply(
            ok=False,
            messages=[text],
            plan=plan,
            issues=[reason] if reason else ["fallback"],
            fallback_used=True,
        )
