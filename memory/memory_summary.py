"""记忆提取与结构化摘要。

两条路径都遵守 Phase 3 规范：
- 先规则：明显的事实/偏好/习惯/约定由 Python 直接建 Memory，不烧 token；
- 再模型：规则判断不了、但看起来有信息量的，才走 EXTRACTION_MODEL，且必须先过 LLM Policy；
- 摘要：累计 30~50 条或到达 token 阈值才调用 SUMMARY_MODEL，输出结构化 JSON。
"""

from __future__ import annotations

import json
import logging
import re

from core import llm_policy

RULE_PATTERNS = (
    ("preference", r"我(?:很|挺|最|特别)?喜欢([^。！？\n]{1,20})", "用户喜欢{0}", 0.6, False),
    ("preference", r"我(?:不太|不)喜欢([^。！？\n]{1,20})", "用户不喜欢{0}", 0.6, False),
    ("habit", r"我(?:最近|每天|经常|总是|习惯)([^。！？\n]{1,20})", "用户{0}", 0.55, False),
    # 进展类：同一个话题的延续（"我已经开始学爬虫了"）
    ("topic", r"我(?:已经)?开始([^。！？\n]{1,20})", "用户开始{0}", 0.6, False),
    ("agreement", r"(?:记住|别忘了|提醒我)([^。！？\n]{1,30})", "用户要求记住：{0}", 0.8, True),
    ("shared_event", r"我(?:明天|下周|周末|下个月|马上|就要|准备|打算)([^。！？\n]{1,24})", "用户将要做的事：{0}", 0.7, False),
    ("fact", r"我(?:是|在)([^。！？\n]{2,20})(?:工作|上班|上学|读书)", "用户在{0}工作或上学", 0.6, False),
)

MAJOR_EVENT_WORDS = ("考试", "面试", "生日", "毕业", "搬家", "手术", "体检", "答辩", "婚礼", "入职", "离职")

SUMMARY_PROMPT = """以下是角色和用户最近的一段对话。请输出结构化 JSON（不要解释、不要 Markdown）：
{{"summary": "一句话概括", "new_facts": [], "preferences": [], "shared_events": [],
 "topics": [], "relationship_changes": [], "emotion_events": []}}
规则：只写值得长期记住的内容；寒暄和一次性闲聊留空数组；每条不超过 30 字；没把握就留空。

对话：
{CONVERSATION}"""

TYPE_BY_FIELD = {
    "new_facts": "fact",
    "preferences": "preference",
    "shared_events": "shared_event",
    "relationship_changes": "relationship_event",
    "emotion_events": "emotion_event",
}

JSON_FALLBACK_PROMPT = """从下面这句话里找出值得长期记住的信息，输出 JSON 数组，没有就输出 []：
[{{"type": "fact/preference/habit/agreement/shared_event", "content": "一句话", "importance": 0.5, "topic": "话题"}}]
只记录稳定事实、偏好、习惯、约定、重要事件；每条不超过 30 字。

句子：{TEXT}"""


class MemoryExtractor:
    """规则优先；规则命中不了才考虑调用便宜模型。"""

    def __init__(self, store, *, topics=None, client=None, budget=None, bus=None, model_enabled: bool = True) -> None:
        self.store = store
        self.topics = topics
        self.client = client
        self.budget = budget
        self.bus = bus
        self.model_enabled = model_enabled

    def rule_extract(self, text: str) -> list[dict]:
        found: list[dict] = []
        for memory_type, pattern, template, importance, protected in RULE_PATTERNS:
            match = re.search(pattern, text or "")
            if not match:
                continue
            captured = match.group(1).strip("，,。. ")
            if not captured:
                continue
            protected_flag = protected or any(word in captured for word in MAJOR_EVENT_WORDS)
            found.append(
                {
                    "type": memory_type,
                    "content": template.format(captured),
                    "importance": 0.85 if protected_flag else importance,
                    "protected": protected_flag,
                    "topic": captured,
                    "source": "rule",
                }
            )
        return found

    def is_worth_extracting(self, text: str) -> bool:
        raw = (text or "").strip()
        if len(raw) < 20 or "我" not in raw:
            return False
        return not llm_policy.is_filler(raw)

    def model_extract(self, text: str) -> list[dict]:
        if not (self.model_enabled and self.client is not None):
            return []
        if not self.is_worth_extracting(text):
            return []
        budget_state = self.budget.state() if self.budget is not None else None
        decision = llm_policy.decide(text, category="extraction", budget_state=budget_state)
        if not decision.use_llm:
            return []
        prompt = JSON_FALLBACK_PROMPT.replace("{TEXT}", text[:400])
        result = self.client.chat(
            [{"role": "user", "content": prompt}], tier=decision.tier, category="extraction", max_tokens=400
        )
        if not result.ok:
            logging.warning("记忆抽取失败：%s", result.error)
            return []
        start, end = result.content.find("["), result.content.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            items = json.loads(result.content[start : end + 1])
        except (ValueError, TypeError):
            return []
        found: list[dict] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            importance = float(item.get("importance", 0.5) or 0.5)
            if not content or importance < 0.4:
                continue
            found.append(
                {
                    "type": str(item.get("type", "fact")),
                    "content": content[:60],
                    "importance": importance,
                    "protected": False,
                    "topic": str(item.get("topic", "") or "").strip(),
                    "source": "model",
                }
            )
        return found

    def process(self, text: str) -> list[dict]:
        items = self.rule_extract(text)
        if not items:
            items = self.model_extract(text)
        written: list[dict] = []
        for item in items:
            record, action = self.store.create(
                type=str(item.get("type", "fact")),
                content=str(item.get("content", "")),
                tags=[str(item.get("topic", ""))] if item.get("topic") else [],
                importance=float(item.get("importance", 0.5)),
                protected=bool(item.get("protected")),
                source=str(item.get("source", "")),
            )
            written.append({"memory": record, "action": action})
            if self.bus is not None and action in ("created", "superseded"):
                self.bus.publish("MemoryCreated", memory_id=record.get("id"), type=record.get("type"))
            topic = str(item.get("topic", "") or "").strip()
            if self.topics is not None and topic:
                self.topics.upsert(topic, memory_id=str(record.get("id")), importance=float(item.get("importance", 0.5)))
        return written


class MemorySummarizer:
    """按阈值触发的结构化摘要；调用前必须过 LLM Policy。"""

    def __init__(
        self,
        store,
        *,
        topics=None,
        client=None,
        budget=None,
        bus=None,
        min_messages: int = 30,
        max_messages: int = 50,
        token_threshold: int = 1500,
    ) -> None:
        self.store = store
        self.topics = topics
        self.client = client
        self.budget = budget
        self.bus = bus
        self.min_messages = max(4, int(min_messages))
        self.max_messages = max(self.min_messages, int(max_messages))
        self.token_threshold = max(200, int(token_threshold))
        self.calls = 0

    def should_run(self, *, message_count: int, token_count: int) -> bool:
        if message_count >= self.min_messages:
            return True
        return token_count >= self.token_threshold and message_count >= 8

    def run(self, history: list[dict]) -> dict:
        if self.client is None:
            return {"ran": False, "reason": "未配置模型"}
        conversation = "\n".join(
            f"{'用户' if item.get('role') == 'user' else '角色'}：{str(item.get('content', ''))[:200]}"
            for item in history[-self.max_messages :]
        )
        if not conversation.strip():
            return {"ran": False, "reason": "没有对话内容"}
        prompt = SUMMARY_PROMPT.replace("{CONVERSATION}", conversation[:6000])
        budget_state = self.budget.state() if self.budget is not None else None
        decision = llm_policy.decide(prompt, category="summary", budget_state=budget_state)
        if not decision.use_llm:
            return {"ran": False, "reason": decision.reason}
        result = self.client.chat(
            [{"role": "user", "content": prompt}], tier=decision.tier, category="summary", max_tokens=800
        )
        if not result.ok:
            return {"ran": False, "reason": result.error}
        self.calls += 1
        data = self._parse(result.content)
        return {"ran": True, "model": result.model, "data": data, "written": self._persist(data)}

    @staticmethod
    def _parse(raw: str) -> dict:
        default = {
            "summary": "", "new_facts": [], "preferences": [], "shared_events": [],
            "topics": [], "relationship_changes": [], "emotion_events": [],
        }
        text = (raw or "").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return default
        try:
            data = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return default
        if not isinstance(data, dict):
            return default
        for key, value in default.items():
            if key not in data:
                data[key] = value
            elif isinstance(value, list) and not isinstance(data[key], list):
                data[key] = [data[key]]
        return data

    def _persist(self, data: dict) -> list[dict]:
        written: list[dict] = []
        for field, memory_type in TYPE_BY_FIELD.items():
            for content in data.get(field, []) or []:
                text = str(content).strip()
                if not text:
                    continue
                record, action = self.store.create(
                    type=memory_type,
                    content=text[:60],
                    importance=0.6 if memory_type != "fact" else 0.5,
                    protected=memory_type == "relationship_event",
                    source="summary",
                )
                written.append({"memory": record, "action": action})
                if self.bus is not None and action in ("created", "superseded"):
                    self.bus.publish("MemoryCreated", memory_id=record.get("id"), type=record.get("type"))
        for topic in data.get("topics", []) or []:
            text = str(topic).strip()
            if text and self.topics is not None:
                self.topics.upsert(text, importance=0.6)
        return written
