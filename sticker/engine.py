"""Sticker Engine：意图 -> 候选 -> 确定性排序 -> 决策。

边界（S1）：
    - LLM/上层只给"表达意图"（想表达什么感觉），**不给 file_id**
    - 具体用哪个贴纸，永远由这里的确定性评分决定（禁用 random）
    - S1 默认 dry_run=True：可以决定，但绝不真的发送
    - 没有视觉数据时 visual_match 走中性值，绝不假装知道贴纸长什么样
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 评分权重（总和 1.0），可由上层覆盖
DEFAULT_WEIGHTS = {
    "semantic_match": 0.30,
    "emoji_match": 0.15,
    "visual_match": 0.20,
    "user_preference": 0.15,
    "relationship_fit": 0.05,
    "novelty": 0.10,
    "personality_fit": 0.05,
}

# 常见意图 -> 可能的 emoji / 标签线索（确定性，不依赖模型）
INTENT_HINTS: dict[str, dict] = {
    "teasing": {"emoji": ("😏", "😜", "🙄", "😒"), "tags": ("teasing", "吐槽", "嫌弃")},
    "happy": {"emoji": ("😄", "😆", "🥳", "😊"), "tags": ("happy", "开心")},
    "shy": {"emoji": ("😳", "🙈", "😊"), "tags": ("shy", "害羞")},
    "angry": {"emoji": ("😠", "😤", "💢"), "tags": ("angry", "生气")},
    "speechless": {"emoji": ("😑", "🙃", "😐"), "tags": ("speechless", "无语")},
    "comfort": {"emoji": ("🤗", "🫂", "🥺"), "tags": ("comfort", "安慰")},
    "goodnight": {"emoji": ("🌙", "😴"), "tags": ("night", "晚安")},
    "question": {"emoji": ("❓", "🤔"), "tags": ("question", "疑问")},
}


@dataclass
class StickerIntent:
    use: bool = False
    intent: str = ""
    intensity: float = 0.0
    keywords: list = field(default_factory=list)
    confidence: float = 0.0

    def normalized(self) -> "StickerIntent":
        return StickerIntent(
            use=bool(self.use),
            intent=str(self.intent or "").strip().lower(),
            intensity=max(0.0, min(1.0, float(self.intensity or 0.0))),
            keywords=[str(item) for item in (self.keywords or []) if str(item).strip()],
            confidence=max(0.0, min(1.0, float(self.confidence or 0.0))),
        )


@dataclass
class StickerDecision:
    should_send: bool = False
    intent: str = ""
    file_unique_id: str = ""
    file_id: str = ""
    score: float = 0.0
    reason: str = ""
    candidates: list = field(default_factory=list)
    dry_run: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "should_send": self.should_send,
            "intent": self.intent,
            "file_unique_id": self.file_unique_id,
            "score": round(self.score, 4),
            "reason": self.reason,
            "dry_run": self.dry_run,
            "candidate_count": len(self.candidates),
            "error": self.error,
        }


class StickerEngine:
    def __init__(
        self,
        *,
        store,
        index,
        history=None,
        weights: dict | None = None,
        max_candidates: int = 5,
        min_score: float = 0.45,
        dry_run: bool = True,
        neutral_visual: float = 0.5,
    ) -> None:
        self.store = store
        self.index = index
        self.history = history
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.max_candidates = max(1, int(max_candidates))
        self.min_score = max(0.0, min(1.0, float(min_score)))
        self.dry_run = bool(dry_run)
        self.neutral_visual = max(0.0, min(1.0, float(neutral_visual)))

    # ── 评分 ────────────────────────────────────────────────────────
    def score(self, record, intent: StickerIntent) -> tuple[float, dict]:
        hints = INTENT_HINTS.get(intent.intent, {})
        parts: dict[str, float] = {}
        words = [intent.intent, *intent.keywords]

        text_blob = " ".join(str(item) for item in (record.tags or []))
        semantic = 0.0
        if intent.intent and intent.intent in text_blob:
            semantic = 1.0
        elif any(word and word in text_blob for word in words):
            semantic = 0.6
        parts["semantic_match"] = semantic

        emoji_score = 1.0 if record.emoji and record.emoji in (hints.get("emoji") or ()) else 0.0
        if not emoji_score and record.emoji and intent.keywords:
            emoji_score = 0.4 if record.emoji in intent.keywords else 0.0
        parts["emoji_match"] = emoji_score

        # S1 没有视觉数据：有描述才计分，没有就用中性值（绝不假装看过）
        parts["visual_match"] = 1.0 if (record.visual_description and any(
            word and word in record.visual_description for word in words
        )) else self.neutral_visual if not record.visual_description else 0.0

        preference = self.store.preference(record.file_unique_id)
        parts["user_preference"] = {"positive": 1.0, "neutral": 0.5, "negative": 0.0}.get(preference, 0.5)
        parts["relationship_fit"] = 0.6          # S1 固定中性偏正向，留给 S3 接关系状态
        parts["personality_fit"] = 0.6           # 同上，不引入人格判断

        novelty = 1.0
        if self.history is not None:
            recent = self.history.recent_ids(5)
            if record.file_unique_id in recent:
                novelty = 0.2                     # 最近刚发过 -> 降低重复
            elif self.history.last_sent_at(record.file_unique_id):
                novelty = 0.6
        parts["novelty"] = novelty

        total = sum(parts.get(name, 0.0) * float(weight) for name, weight in self.weights.items())
        return round(max(0.0, min(1.0, total)), 4), parts

    # ── 决策 ────────────────────────────────────────────────────────
    def decide(self, intent: StickerIntent, *, chat_id: int | str = "") -> StickerDecision:
        intent = intent.normalized()
        decision = StickerDecision(intent=intent.intent, dry_run=self.dry_run)
        if not intent.use:
            decision.reason = "intent.use=false"
            return decision
        if not intent.intent:
            decision.reason = "缺少 intent"
            return decision
        if self.store.count() == 0:
            decision.reason = "还没有收录任何贴纸"
            return decision

        hints = INTENT_HINTS.get(intent.intent, {})
        pool: dict[str, object] = {}
        for emoji in hints.get("emoji", ()):
            for item in self.index.by_emoji(emoji):
                pool[item.file_unique_id] = item
        for tag in hints.get("tags", ()):
            for item in self.index.by_tag(tag):
                pool[item.file_unique_id] = item
        if not pool:                                  # 没有线索就退化为全体候选（仍由评分排序）
            for item in self.store.all():
                pool[item.file_unique_id] = item

        scored = []
        for record in pool.values():
            score, parts = self.score(record, intent)
            top = sorted(parts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
            reason = " + ".join(f"{name}" for name, value in top if value > 0.5) or "baseline"
            scored.append((score, record, reason))
        scored.sort(key=lambda item: (-item[0], item[1].file_unique_id))   # 确定性排序
        decision.candidates = [
            {"file_unique_id": record.file_unique_id, "score": score, "reason": reason}
            for score, record, reason in scored[: self.max_candidates]
        ]
        best_score, best, reason = scored[0]
        if best_score < self.min_score:
            decision.reason = f"最高分 {best_score} 低于阈值 {self.min_score}"
            return decision

        decision.should_send = True
        decision.file_unique_id = best.file_unique_id
        decision.file_id = best.file_id
        decision.score = best_score
        decision.reason = reason
        return decision

    def stats(self) -> dict:
        return {
            "stickers": self.store.count(),
            "index": self.index.stats(),
            "dry_run": self.dry_run,
            "min_score": self.min_score,
        }
