"""ThoughtValidator：LLM 产出的候选念头必须过这一关才能落库。"""

from __future__ import annotations

from v3.thought.models import THOUGHT_TYPES

MAX_CONTENT_CHARS = 200
MIN_EVIDENCE = 1

# Phase 6：认知器官可以**提议**这些数值，Core 之后再决定采纳多少
HINT_KEYS = ("importance_hint", "curiosity_hint", "urgency_hint", "novelty_hint",
             "confidence_hint", "valence_hint", "relationship_relevance_hint")

COT_MARKERS = (
    "chain of thought", "思维链", "let me think step by step", "分析过程如下",
    "system prompt", "系统提示词", "assistant:", "user:", "【prompt】",
)


class ThoughtValidator:
    def __init__(self, *, max_chars: int = MAX_CONTENT_CHARS, min_evidence: int = MIN_EVIDENCE) -> None:
        self.max_chars = max(20, int(max_chars))
        self.min_evidence = max(0, int(min_evidence))

    def validate(self, candidate: dict) -> tuple:
        """返回 (是否通过, 拒绝原因, 规范化后的候选)。"""
        if not isinstance(candidate, dict):
            return False, "候选不是对象", {}
        content = str(candidate.get("content", "")).strip()
        if not content:
            return False, "内容为空", {}
        if len(content) > self.max_chars:
            return False, f"内容超过 {self.max_chars} 字", {}
        lowered = content.lower()
        for marker in COT_MARKERS:
            if marker.lower() in lowered:
                return False, f"疑似思维链或提示词痕迹：{marker}", {}

        thought_type = str(candidate.get("type", "") or "observation").strip().lower()
        if thought_type not in THOUGHT_TYPES:
            thought_type = "observation"

        evidence = [str(item)[:120] for item in (candidate.get("evidence") or []) if str(item).strip()]
        if len(evidence) < self.min_evidence:
            return False, "缺少证据", {}

        normalized = {
            "type": thought_type,
            "content": content,
            "evidence": evidence[:5],
            "is_unfinished": bool(candidate.get("is_unfinished")),
            "topic": str(candidate.get("topic", "") or "")[:40],
        }
        for key in HINT_KEYS:
            if key in candidate:
                try:
                    normalized[key] = round(max(0.0, min(1.0, float(candidate[key]))), 4)
                except (TypeError, ValueError):
                    continue
        return True, "", normalized

    def validate_many(self, candidates) -> tuple:
        accepted = []
        rejected = []
        for candidate in candidates or []:
            ok, reason, normalized = self.validate(candidate)
            if ok:
                accepted.append(normalized)
            else:
                rejected.append(reason)
        return accepted, rejected
