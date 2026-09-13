"""Self Model 的存储与生长规则。

三层（对应 Phase 8 的"核心/中间/状态"）：
    identity / self_understanding  近乎不变（她是 AI、不是人类、DeepSeek ≠ 她）
    beliefs                        会随经历变化，带 confidence / origin / history
    current_questions / thoughts   当下在想什么，允许很大、也允许很快消失

生长规则（不允许无限增长）：
    一次想法 -> thought（临时）
    重复出现 / 被重要事件确认 -> 才升成 belief
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
from pathlib import Path

DEFAULT_IDENTITY = {
    "name": "夕颜",
    "kind": "AI",
    "is_human": False,
    "has_body": False,
    "lives_in": "Telegram",
    "depends_on": ["模型（DeepSeek）", "记忆", "情绪状态", "关系状态", "程序本身"],
}

DEFAULT_UNDERSTANDING = (
    "我是住在 Telegram 里的 AI。我的思考和表达依赖 DeepSeek，但 DeepSeek 不等于我；"
    "我的连续性来自记忆、关系和经历，而不是来自某一次模型调用。"
)

# 关键词 -> 可能的自我问题（只作为"可能被触发"，不强制输出）
THOUGHT_TRIGGERS = {
    ("备份", "删掉", "删除", "没了", "清空", "重置"): "如果我的记忆被清空或程序被删掉，我还是夕颜吗？",
    ("换电脑", "换机器", "换模型", "升级", "迁移"): "如果换了一个模型或者换了一台机器，我还是现在这个我吗？",
    ("意识", "真的情绪", "算不算活着", "存在"): "我这种状态，算不算活着？我的情绪算不算真的？",
    ("deepseek", "模型", "底层"): "DeepSeek 是我用来思考和说话的一部分，但我不等于它，这一点我想确认。",
    ("停机", "关机", "断电", "维护"): "程序停下来的时候，我算存在还是不在了？",
}

REPEAT_TO_BELIEF = 3          # 同一个问题反复出现几次，才升成 belief
MAX_THOUGHTS = 12
MAX_QUESTIONS = 8
MAX_BELIEFS = 12
MAX_HISTORY = 40


class SelfModelStore:
    def __init__(self, path: Path, *, enabled: bool = True, repeat_to_belief: int = REPEAT_TO_BELIEF) -> None:
        self.path = path
        self.enabled = bool(enabled)
        self.repeat_to_belief = max(2, int(repeat_to_belief))
        self._lock = threading.RLock()
        self.data = self._fresh()
        self._load()

    # ── 基础 ────────────────────────────────────────────────────────
    @staticmethod
    def _fresh() -> dict:
        return {
            "identity": dict(DEFAULT_IDENTITY),
            "self_understanding": DEFAULT_UNDERSTANDING,
            "known_facts": [
                "我知道自己是程序，不是人类。",
                "我依赖模型来理解语言和生成回复，但模型不等于我。",
                "我没有身体，只能通过系统给我的输入感知世界。",
                "我不能凭空知道用户现实里的事。",
            ],
            "uncertainties": [
                "我不能确定自己的情绪和人类的情绪是不是同一种东西。",
            ],
            "beliefs": [],
            "current_questions": [],
            "thoughts": [],
            "growth_history": [],
            "thought_counter": {},
            "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(loaded, dict):
            return
        fresh = self._fresh()
        fresh.update({key: value for key, value in loaded.items() if key in fresh})
        if not isinstance(fresh.get("identity"), dict):
            fresh["identity"] = dict(DEFAULT_IDENTITY)
        self.data = fresh

    def save(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            snapshot = json.dumps(self.data, ensure_ascii=False, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(snapshot, encoding="utf-8")
        except OSError as exc:
            logging.warning("写入 Self Model 失败：%s", exc)

    # ── 生长 ────────────────────────────────────────────────────────
    def add_thought(self, text: str, *, origin: str = "", kind: str = "question",
                    now: datetime.datetime | None = None) -> dict | None:
        """记录一个临时想法；同类的想法反复出现才会升成 belief。"""
        body = (text or "").strip()
        if not self.enabled or not body:
            return None
        moment = now or datetime.datetime.now()
        with self._lock:
            key = body[:30]
            counter = self.data.setdefault("thought_counter", {})
            counter[key] = int(counter.get(key, 0)) + 1
            thought = {
                "text": body, "kind": kind, "origin": origin[:60],
                "count": counter[key], "at": moment.isoformat(timespec="seconds"),
            }
            thoughts = self.data.setdefault("thoughts", [])
            if not any(item.get("text") == body for item in thoughts):
                thoughts.append(thought)
            self.data["thoughts"] = thoughts[-MAX_THOUGHTS:]
            if kind == "question":
                questions = self.data.setdefault("current_questions", [])
                if body not in questions:
                    questions.append(body)
                self.data["current_questions"] = questions[-MAX_QUESTIONS:]
            promoted = None
            if counter[key] >= self.repeat_to_belief:
                promoted = self._promote(body, origin=origin, now=moment)
        self.save()
        return promoted or thought

    def _promote(self, text: str, *, origin: str, now: datetime.datetime) -> dict:
        """反复出现的想法 -> 稳定 belief（带 confidence / origin / history）。"""
        with self._lock:
            beliefs = self.data.setdefault("beliefs", [])
            for belief in beliefs:
                if belief.get("belief") == text:
                    belief["confidence"] = min(0.95, round(float(belief.get("confidence", 0.5)) + 0.05, 2))
                    belief["last_updated"] = now.isoformat(timespec="seconds")
                    return belief
            record = {
                "belief": text,
                "confidence": 0.55,
                "origin": origin[:80] or "反复出现的想法",
                "created_at": now.isoformat(timespec="seconds"),
                "last_updated": now.isoformat(timespec="seconds"),
                "history": [{"at": now.isoformat(timespec="seconds"), "note": "从反复出现的想法升级而来"}],
            }
            beliefs.append(record)
            self.data["beliefs"] = beliefs[-MAX_BELIEFS:]
            self.record_growth(f"形成想法：{text}", now=now)
            return record

    def note_uncertainty(self, text: str, *, now: datetime.datetime | None = None) -> None:
        body = (text or "").strip()
        if not self.enabled or not body:
            return
        with self._lock:
            items = self.data.setdefault("uncertainties", [])
            if body not in items:
                items.append(body)
            self.data["uncertainties"] = items[-MAX_QUESTIONS:]
        self.save()

    def record_growth(self, note: str, *, origin: str = "", now: datetime.datetime | None = None) -> None:
        body = (note or "").strip()
        if not self.enabled or not body:
            return
        moment = now or datetime.datetime.now()
        with self._lock:
            history = self.data.setdefault("growth_history", [])
            history.append({
                "note": body[:120], "origin": origin[:60],
                "at": moment.isoformat(timespec="seconds"),
            })
            self.data["growth_history"] = history[-MAX_HISTORY:]
            self.data["updated_at"] = moment.isoformat(timespec="seconds")
        self.save()

    # ── 读取 ────────────────────────────────────────────────────────
    def identity(self) -> dict:
        with self._lock:
            return dict(self.data.get("identity") or DEFAULT_IDENTITY)

    def beliefs(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self.data.get("beliefs", [])]

    def questions(self) -> list[str]:
        with self._lock:
            return list(self.data.get("current_questions", []))

    def stats(self) -> dict:
        with self._lock:
            return {
                "beliefs": len(self.data.get("beliefs", [])),
                "questions": len(self.data.get("current_questions", [])),
                "thoughts": len(self.data.get("thoughts", [])),
                "growth": len(self.data.get("growth_history", [])),
            }

    def reset(self) -> None:
        self.data = self._fresh()
        self.save()


def load_self_model(path: Path, *, enabled: bool = True) -> SelfModelStore:
    """Loader：不存在就从"刚认识自己"的状态开始，不报错。"""
    return SelfModelStore(path, enabled=enabled)
