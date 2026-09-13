"""记忆召回：关键词/标签 + 重要性 + 新近度 + 使用频率 + 情绪权重，取 Top-K。

权重全部来自配置（`MEMORY_WEIGHT_*`），不硬编码。
只有真正进入 L3 的记忆才 mark_used——"被搜到"不等于"被用到"。
"""

from __future__ import annotations

import datetime
import re

DEFAULT_WEIGHTS = {
    "keyword": 0.45,
    "importance": 0.25,
    "recency": 0.15,
    "frequency": 0.10,
    "emotion": 0.05,
}

STOPWORDS = {
    "我", "你", "他", "她", "的", "了", "是", "在", "有", "和", "就", "都", "也", "这", "那",
    "吗", "呢", "吧", "啊", "呀", "着", "过", "很", "太", "不", "没", "会", "要", "把", "被",
}


def tokenize(text: str) -> set[str]:
    """轻量分词：中文字（+二元组）+ 英文单词，去掉停用词。"""
    raw = (text or "").strip().lower()
    if not raw:
        return set()
    words = set(re.findall(r"[a-z0-9_]{2,}", raw))
    cjk = re.findall(r"[\u4e00-\u9fff]", raw)
    words |= {char for char in cjk if char not in STOPWORDS}
    for index in range(len(cjk) - 1):
        pair = cjk[index] + cjk[index + 1]
        if not (cjk[index] in STOPWORDS and cjk[index + 1] in STOPWORDS):
            words.add(pair)
    return words


class MemoryRetriever:
    def __init__(
        self,
        store,
        *,
        weights: dict | None = None,
        top_k: int = 5,
        cold_keyword_min: float = 0.3,
        recency_half_life_days: float = 14.0,
        frequency_saturation: int = 5,
    ) -> None:
        self.store = store
        merged = {**DEFAULT_WEIGHTS, **(weights or {})}
        total = sum(max(0.0, float(v)) for v in merged.values())
        # 归一化，保证 score 落在 0~1，便于阈值判断
        self.weights = {key: max(0.0, float(value)) / total for key, value in merged.items()} if total else DEFAULT_WEIGHTS
        self.top_k = max(1, min(8, int(top_k)))
        self.cold_keyword_min = max(0.0, float(cold_keyword_min))
        self.recency_half_life_days = max(1.0, float(recency_half_life_days))
        self.frequency_saturation = max(1, int(frequency_saturation))

    # ── 打分 ────────────────────────────────────────────────────────
    def score_parts(self, memory: dict, query_tokens: set[str], now: datetime.datetime) -> dict:
        memory_tokens = tokenize(str(memory.get("content", ""))) | {
            str(tag).lower() for tag in memory.get("tags", [])
        }
        overlap = len(query_tokens & memory_tokens)
        keyword = overlap / max(1, min(len(query_tokens), len(memory_tokens))) if query_tokens else 0.0

        importance = float(memory.get("importance", 0.5))

        last_used = str(memory.get("last_used_at") or memory.get("created_at") or "")
        try:
            moment = datetime.datetime.fromisoformat(last_used)
            age_days = max(0.0, (now - moment).total_seconds() / 86400)
        except ValueError:
            age_days = 999.0
        recency = 1.0 / (1.0 + age_days / self.recency_half_life_days)

        frequency = min(1.0, int(memory.get("use_count", 0)) / self.frequency_saturation)
        emotion = float(memory.get("emotion_weight", 0.0))
        total = (
            keyword * self.weights["keyword"]
            + importance * self.weights["importance"]
            + recency * self.weights["recency"]
            + frequency * self.weights["frequency"]
            + emotion * self.weights["emotion"]
        )
        return {
            "keyword": round(keyword, 4),
            "importance": round(importance, 4),
            "recency": round(recency, 4),
            "frequency": round(frequency, 4),
            "emotion": round(emotion, 4),
            "total": round(total, 4),
        }

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        now: datetime.datetime | None = None,
        min_score: float = 0.0,
    ) -> list[dict]:
        """返回按分数排序的候选：每项 {memory, score, parts}。"""
        moment = now or datetime.datetime.now()
        limit = max(1, min(8, int(top_k or self.top_k)))
        query_tokens = tokenize(query)
        candidates = []
        for memory in self.store.list():           # 已排除失效/归档
            temperature = str(memory.get("temperature", "WARM"))
            if temperature == "ARCHIVED":
                continue
            parts = self.score_parts(memory, query_tokens, moment)
            # COLD 默认不注入：只有明确相关（关键词命中）才考虑
            if temperature == "COLD" and parts["keyword"] < self.cold_keyword_min:
                continue
            # 关键词为 0 时只允许 HOT 记忆靠重要性和新近度进来
            if parts["keyword"] == 0 and temperature != "HOT" and float(memory.get("importance", 0)) < 0.8:
                continue
            if parts["total"] < min_score:
                continue
            candidates.append({"memory": memory, "score": parts["total"], "parts": parts})
        candidates.sort(key=lambda item: (-item["score"], str(item["memory"].get("id"))))
        return candidates[:limit]

    def retrieve_blocks(
        self,
        query: str,
        *,
        top_k: int | None = None,
        now: datetime.datetime | None = None,
        mark_used: bool = True,
    ) -> list[dict]:
        """给 Context Manager 用的 L3 结构：只有这些才会被记入 use_count。"""
        picked = self.retrieve(query, top_k=top_k, now=now)
        blocks = []
        for item in picked:
            memory = item["memory"]
            if mark_used:
                self.store.mark_used(str(memory.get("id")), now)
            blocks.append(
                {
                    "id": memory.get("id"),
                    "content": memory.get("content"),
                    "type": memory.get("type"),
                    "score": item["score"],
                    "parts": item["parts"],
                    "tags": memory.get("tags", []),
                }
            )
        return blocks
