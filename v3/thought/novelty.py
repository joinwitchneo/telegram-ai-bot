"""novelty / information_gain：由 Python 规则计算，LLM 不参与。

一个念头有没有带来新东西，取决于它与近期念头/兴趣主题的重合度，
以及它是否引入了此前没出现过的关键词。
"""

from __future__ import annotations

import re

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z0-9_]{3,}")

STOP_WORDS = {
    "用户", "自己", "觉得", "好像", "有点", "最近", "这个", "那个", "什么", "怎么",
    "因为", "所以", "然后", "还是", "可能", "应该",
}


def tokens(text: str, *, limit: int = 24) -> set:
    """粗粒度关键词集合：中文二元组 + 英文单词（确定性，无模型）。"""
    raw = (text or "").strip()
    if not raw:
        return set()
    result = set()
    for word in LATIN_RE.findall(raw):
        result.add(word.lower())
    chinese = CJK_RE.findall(raw)
    for index in range(len(chinese) - 1):
        pair = chinese[index] + chinese[index + 1]
        if pair not in STOP_WORDS:
            result.add(pair)
    if not result:
        result.add(raw[:2])
    return set(sorted(result)[:limit])


def _overlap(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def compute_novelty(content: str, *, recent_contents=None) -> float:
    """与最近 N 条念头的重合度越低，novelty 越高（0~1）。"""
    mine = tokens(content)
    if not mine:
        return 0.0
    history = [tokens(item) for item in (recent_contents or []) if item]
    if not history:
        return 1.0
    worst = max(_overlap(mine, other) for other in history)
    return round(max(0.0, min(1.0, 1.0 - worst)), 4)


def compute_information_gain(
    content: str,
    *,
    recent_contents=None,
    topic_seen_count: int = 1,
    observation_is_new: bool = True,
) -> float:
    """信息增益 = novelty x 新鲜度 x 边际衰减。

    topic_seen_count 越大（同一主题反复出现），增益越低 —— 这是防止
    "反复想同一件事还以为很有收获" 的关键，也保证兴趣增量单调递减。
    """
    base = compute_novelty(content, recent_contents=recent_contents)
    freshness = 1.0 if observation_is_new else 0.5
    decay = 1.0 / max(1, int(topic_seen_count))
    gain = base * freshness * decay
    return round(max(0.0, min(1.0, gain)), 4)
