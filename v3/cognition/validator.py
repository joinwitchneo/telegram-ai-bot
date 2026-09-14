"""把 LLM 的自由输出收成结构化 Proposal（Phase 6 §十六/§十七）。

铁律：
  - 这里不做任何执行，只做"收窄"：未知意图降级、数值夹紧、超量丢弃、
    思维链/无证据的候选一律拒收。
  - 输出永远是 Proposal，DecisionEngine 才决定要不要行动。
"""

from __future__ import annotations

import json
import logging
import re

from v3.cognition.base import INTENTION_TYPES, Intention
from v3.thought.validator import ThoughtValidator

MAX_NEW_THOUGHTS = 3
MAX_CONTENT_CHARS = 200

HINT_KEYS = ("importance", "curiosity", "urgency", "confidence", "valence",
             "relationship_relevance", "novelty")

CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*|```\s*$")

PROPOSAL_EXAMPLE = (
    '{"thought_update": {"importance": 0.8}, '
    '"new_thoughts": [{"type": "curiosity", "content": "一句不超过40字的中文念头", '
    '"topic": "两到六个字", "is_unfinished": true, "evidence": ["引用原话片段"]}], '
    '"intention": {"type": "none|journal|message", "reason": "为什么", '
    '"confidence": 0.0~1.0, "content": "如果打算说话，写要说的话"}}'
)


def extract_json(text: str) -> dict | None:
    raw = CODE_FENCE.sub("", (text or "").strip()).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        logging.info("[v3] proposal JSON 解析失败，本轮按 none 处理")
        return None
    return data if isinstance(data, dict) else None


def _clamp(value, *, default=0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def validate_proposal(data: dict, *, validator: ThoughtValidator | None = None,
                      max_new: int = MAX_NEW_THOUGHTS) -> tuple:
    """返回 (accepted_new_thoughts, thought_update, intention, rejected_reasons)。"""
    rejected: list[str] = []
    validator = validator or ThoughtValidator()
    data = data if isinstance(data, dict) else {}

    # 1) 新念头：走和 Phase 0-5 完全相同的校验器（含 CoT 拒收、证据要求）
    raw_new = data.get("new_thoughts") or data.get("thoughts") or []
    if not isinstance(raw_new, list):
        rejected.append("new_thoughts 不是数组")
        raw_new = []
    accepted, inner_rejected = validator.validate_many(raw_new)
    accepted = accepted[: max(1, int(max_new))]
    rejected.extend(inner_rejected)

    # 2) 对已有念头的更新：只允许"提示值"，且必须夹到 0~1
    update: dict = {}
    raw_update = data.get("thought_update")
    if isinstance(raw_update, dict):
        for key in HINT_KEYS:
            if key in raw_update:
                update[key] = round(_clamp(raw_update.get(key)), 4)
        for key in raw_update:
            if key not in HINT_KEYS and key not in ("id", "thought_id", "reason"):
                rejected.append(f"thought_update 不允许的字段：{key}")
    elif raw_update is not None:
        rejected.append("thought_update 不是对象")

    # 3) 意图：未知类型降级为 none，绝不放行
    raw_intention = data.get("intention") if isinstance(data.get("intention"), dict) else {}
    kind = str(raw_intention.get("type", "none") or "none").strip().lower()
    if kind not in INTENTION_TYPES:
        rejected.append(f"未知意图类型 {kind}，降级为 none")
        kind = "none"
    intention = Intention(
        type=kind,
        reason=str(raw_intention.get("reason", ""))[:200],
        confidence=round(_clamp(raw_intention.get("confidence", 0.0)), 4),
        content=str(raw_intention.get("content", "") or "").strip()[:MAX_CONTENT_CHARS],
    )
    if intention.type == "message" and not intention.content:
        rejected.append("打算说话却没给内容，降级为 none")
        intention = Intention(type="none", reason=intention.reason,
                              confidence=intention.confidence)
    return accepted, update, intention, rejected
