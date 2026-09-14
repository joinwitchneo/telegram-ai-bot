"""ThoughtFormer：Observation → Thought（Phase 6 §一 的廉价一步）。

为什么需要它：Phase 6 把"生成内容"放进了认知器官（LLM），而认知又要求
"已有念头达到阈值"。若念头库是空的，就永远不触发、永远没有念头——系统空转。
设计文档的闭环里 `Observation → Thought Formation` 本来就是 Core 的便宜一步，
所以这里用**纯 Python 规则**把"用户刚说过的话"变成候选念头：

  - 只记录用户说过什么（不改写、不脑补），证据就是原话；
  - importance / curiosity / urgency / novelty 全部由规则算；
  - 够不够格去深思由检查点决定（这里不做任何判断）。

这一步绝不调用模型。
"""

from __future__ import annotations

import datetime
import re

from v3.thought.models import STATE_ACTIVE, Thought
from v3.thought.novelty import compute_information_gain, compute_novelty, tokens

QUESTION_MARKERS = ("吗", "?", "？", "怎么", "为什么", "要不要", "是不是", "可以吗")
UNFINISHED_MARKERS = ("还没", "没有说", "回头", "等下", "待会", "再说", "以后", "到时候")
PUNCT_RE = re.compile(r"[\s，。！？!?,、~…“”\"'（）()\[\]【】]+")
PARTICLES = set("的了着过还有是在就也都不要会能和我你他她它们吗呢吧啊把被给对跟与及或之前后里上下个这那种些点")

MAX_TOPIC_CHARS = 6


class ThoughtFormer:
    def __init__(self, *, thoughts, observations, interests=None, max_per_tick: int = 5,
                 now_fn=None) -> None:
        self.thoughts = thoughts
        self.observations = observations
        self.interests = interests
        self.max_per_tick = max(1, int(max_per_tick))
        self._now = now_fn or datetime.datetime.now

    def form(self, *, now: datetime.datetime | None = None,
             cycle_id: str = "") -> list:
        """把最近未消费的观测变成候选念头；返回本轮新建的念头。"""
        if self.thoughts is None or self.observations is None:
            return []
        moment = now or self._now()
        rows = self.observations.recent(limit=self.max_per_tick, unconsumed_only=True)
        if not rows:
            return []
        recent = self.thoughts.recent_contents(limit=20)
        known_topics = self._known_topics()
        created = []
        for item in rows:
            content = str(item.get("user_message", "") or "").strip()
            if not content:
                continue
            thought = self._make(content, item, moment=moment, cycle_id=cycle_id,
                                 recent_contents=recent, known_topics=known_topics)
            saved, action = self.thoughts.add(thought)
            if action == "created":
                created.append(saved)
                recent.append(saved.content)
        self.observations.mark_consumed(cycle_id or "former", limit=len(rows))
        return created

    # ── 规则（透明、可测）────────────────────────────────────────
    def _make(self, content: str, observation: dict, *, moment: datetime.datetime,
              cycle_id: str, recent_contents: list, known_topics: list) -> Thought:
        text = content[:200]
        is_question = any(marker in text for marker in QUESTION_MARKERS)
        is_unfinished = is_question or any(marker in text for marker in UNFINISHED_MARKERS)
        novelty = compute_novelty(text, recent_contents=recent_contents)
        gain = compute_information_gain(text, recent_contents=recent_contents,
                                        topic_seen_count=1, observation_is_new=True)
        importance = 0.35 + (0.15 if len(text) >= 12 else 0.0) \
            + (0.15 if is_question else 0.0) + (0.10 if is_unfinished else 0.0)
        curiosity = 0.5 + (0.2 if is_question else 0.0) + 0.2 * novelty
        urgency = 0.15 if is_unfinished else 0.05
        thought = Thought(
            id="", type="curiosity" if is_question else "observation", content=text,
            evidence=[text[:120]], source="observation",
            topic=self._topic(text, known_topics),
            is_unfinished=is_unfinished,
            novelty=novelty, information_gain=gain,
            score=round(0.6 * gain + 0.4 * novelty, 4),
            importance=round(min(1.0, importance), 4),
            curiosity=round(min(1.0, curiosity), 4),
            urgency=round(min(1.0, urgency), 4),
            confidence=0.5, valence=0.0,
            motivation=0.0,
            activation=0.5, persistence=0.4,
            relationship_relevance=0.3,
            created_at=moment.isoformat(timespec="seconds"),
            last_seen_at=moment.isoformat(timespec="seconds"),
            updated_at=moment.isoformat(timespec="seconds"),
            cycle_id=cycle_id,
            metadata={"observation_timestamp": str(observation.get("timestamp", ""))},
        )
        if is_unfinished:
            thought.set_state(STATE_ACTIVE)
        return thought

    def _known_topics(self) -> list:
        """已有的话题（兴趣 + 念头），用来让"同一件事"聚到同一个话题上。"""
        rows: list = []
        if self.interests is not None:
            try:
                rows += [str(item.topic) for item in self.interests.all()]
            except Exception:  # noqa: BLE001
                pass
        try:
            rows += [str(thought.topic) for thought in self.thoughts.all() if thought.topic]
        except Exception:  # noqa: BLE001
            pass
        return sorted({row for row in rows if row}, key=len, reverse=True)

    @staticmethod
    def _topic(text: str, known_topics=(None,)) -> str:
        """话题标签：优先复用已有话题（同一件事要聚到一起），否则取最长关键词。"""
        cleaned = PUNCT_RE.sub("", text)
        for topic in (known_topics or []):
            if topic and topic in text:
                return topic
        candidates = sorted((tok for tok in tokens(text) if len(tok) >= 2),
                            key=len, reverse=True)
        meaningful = [tok for tok in candidates if not (set(tok) & PARTICLES)]
        if meaningful:
            meaningful.sort(key=lambda tok: (-len(tok), text.find(tok)))
            return meaningful[0][:MAX_TOPIC_CHARS]
        if candidates:
            return candidates[0][:MAX_TOPIC_CHARS]
        return cleaned[:MAX_TOPIC_CHARS] or "闲聊"
