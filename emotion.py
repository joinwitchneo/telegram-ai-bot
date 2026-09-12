"""情绪引擎：角色的内部情绪状态。

设计原则（对应 V2 架构文档）：
- 情绪不是单个变量：主情绪 + 强度 + 次要情绪 + 若干连续数值；
- 情绪由**事件**驱动，Python 决定最终数值，模型只能影响"怎么表达"；
- 情绪有**惯性**（单次变化有上限）和**衰减**（随时间回落）。
"""

from __future__ import annotations

import datetime
import json
import logging
import random
from pathlib import Path

# 第一版只用这 12 种情绪
EMOTION_NAMES = [
    "neutral", "happy", "excited", "playful", "shy", "affectionate",
    "annoyed", "angry", "hurt", "sad", "tired", "surprised",
]

EMOTION_LABELS = {
    "neutral": "平静", "happy": "开心", "excited": "兴奋", "playful": "调皮",
    "shy": "害羞", "affectionate": "亲近", "annoyed": "不爽", "angry": "生气",
    "hurt": "委屈", "sad": "低落", "tired": "疲惫", "surprised": "惊讶",
}

# 连续数值的默认值
DEFAULT_FIELDS = {
    "mood": 0.05,        # -1.0 ~ +1.0 整体心情
    "energy": 0.75,      # 0.0 ~ 1.0 精神
    "social_need": 0.50,  # 0.0 ~ 1.0 聊天欲望
    "irritation": 0.0,   # 0.0 ~ 1.0 烦躁
    "hurt": 0.0,         # 0.0 ~ 1.0 委屈
    "affection": 0.35,   # 0.0 ~ 1.0 对当前用户的亲近
}

# 事件 → 情绪变化（对应文档第七节）
# 说明：带 "e:" 前缀的是情绪强度；不带前缀的是连续数值（mood/energy/...）
EVENT_EFFECTS: dict[str, dict[str, float]] = {
    "USER_PRAISE": {"mood": 0.15, "affection": 0.08, "e:happy": 0.25, "e:shy": 0.10},
    "USER_CARE": {"mood": 0.10, "affection": 0.12, "e:shy": 0.08, "social_need": 0.08},
    "USER_JOKE": {"mood": 0.05, "e:playful": 0.15, "social_need": 0.03},
    "USER_TEASE": {"mood": 0.02, "e:playful": 0.10, "irritation": 0.05},
    "USER_CHAT": {"mood": 0.01, "social_need": 0.05},
    "USER_COLD": {"social_need": -0.10, "hurt": 0.05, "mood": -0.03, "e:hurt": 0.10},
    "USER_INSULT_MILD": {"irritation": 0.15, "hurt": 0.08, "mood": -0.12, "e:annoyed": 0.25},
    "USER_INSULT": {
        "irritation": 0.30, "hurt": 0.20, "affection": -0.05, "mood": -0.25,
        "e:angry": 0.25, "e:annoyed": 0.30,
    },
    "USER_APOLOGY": {
        "irritation": -0.20, "hurt": -0.25, "affection": 0.05, "mood": 0.05,
        "e:angry": -0.25, "e:annoyed": -0.20,
    },
    "USER_LEAVES": {"social_need": -0.05},
    "USER_RETURNS": {"social_need": 0.15, "mood": 0.05, "e:happy": 0.12},
    "TOPIC_INTERESTING": {"mood": 0.08, "e:excited": 0.15, "social_need": 0.08},
    "TOPIC_BORING": {"mood": -0.03, "social_need": -0.05, "e:tired": 0.08},
    "BOT_MISTAKE": {"mood": -0.05, "hurt": 0.03, "e:hurt": 0.08},
    "BOT_CORRECTED": {"mood": -0.02},
    "LONG_SILENCE": {"social_need": -0.05, "e:tired": 0.05},
}

# 每分钟衰减率（1.0 = 不衰减）
DECAY_PER_MINUTE: dict[str, float] = {
    "surprised": 0.950,
    "excited": 0.970,
    "happy": 0.985,
    "playful": 0.980,
    "shy": 0.980,
    "annoyed": 0.985,
    "angry": 0.990,
    "hurt": 0.992,
    "sad": 0.990,
    "tired": 0.995,
    "affectionate": 0.995,
    "neutral": 1.0,
}

# 单次变化上限（情绪惯性，对应文档第八/三十一节）
MAX_DELTA = 0.30


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class EmotionEngine:
    def __init__(self, params: dict | None = None) -> None:
        params = params or {}
        self.decay_rates = {**DECAY_PER_MINUTE, **params.get("decay", {})}
        self.effects = {**EVENT_EFFECTS, **params.get("events", {})}
        self.max_delta = float(params.get("max_delta", MAX_DELTA))
        self.mood_recovery = float(params.get("mood_recovery_per_hour", 0.06))

    # ── 状态 ────────────────────────────────────────────────────────
    @staticmethod
    def new_state(now: float | None = None) -> dict:
        state = {k: float(v) for k, v in DEFAULT_FIELDS.items()}
        state["emotions"] = {name: 0.0 for name in EMOTION_NAMES}
        state["primary"] = "neutral"
        state["intensity"] = 0.5
        state["secondary"] = ""
        state["secondary_intensity"] = 0.0
        state["updated_at"] = float(now if now is not None else datetime.datetime.now().timestamp())
        return state

    @staticmethod
    def _bump(state: dict, name: str, delta: float, max_delta: float) -> None:
        emotions = state.setdefault("emotions", {})
        current = float(emotions.get(name, 0.0))
        emotions[name] = _clamp(current + _clamp(delta, -max_delta, max_delta), 0.0, 1.0)

    def apply_event(self, state: dict, event: str, weight: float = 1.0) -> dict:
        """按事件规则更新情绪。模型不能直接改这些数，只有这里能改。"""
        effects = self.effects.get(event)
        if not effects:
            return state
        for key, delta in effects.items():
            scaled = delta * weight
            if key.startswith("e:"):
                self._bump(state, key[2:], scaled, self.max_delta)
            else:
                low, high = (-1.0, 1.0) if key == "mood" else (0.0, 1.0)
                state[key] = _clamp(float(state.get(key, 0.0)) + _clamp(scaled, -self.max_delta, self.max_delta), low, high)
        self._refresh_primary(state)
        state["updated_at"] = datetime.datetime.now().timestamp()
        return state

    def apply_events(self, state: dict, events: list) -> dict:
        for item in events or []:
            if isinstance(item, dict):
                self.apply_event(state, str(item.get("event", "")), float(item.get("weight", 1.0)))
            else:
                self.apply_event(state, str(item))
        return state

    def decay(self, state: dict, minutes: float) -> dict:
        """随时间回落：不同情绪衰减速度不同。"""
        if minutes <= 0:
            return state
        steps = min(minutes, 60 * 24 * 7)
        for name, rate in self.decay_rates.items():
            if name in EMOTION_NAMES:
                current = float(state.get("emotions", {}).get(name, 0.0))
                state.setdefault("emotions", {})[name] = _clamp(current * (rate ** steps), 0.0, 1.0)
        # 心情缓慢回到中性，精力恢复，社交需求缓慢回升
        mood = float(state.get("mood", 0.0))
        drift = self.mood_recovery * steps / 60.0
        state["mood"] = _clamp(mood - drift * (1 if mood > 0 else -1) if abs(mood) > drift else 0.0, -1.0, 1.0)
        state["energy"] = _clamp(float(state.get("energy", 0.7)) + 0.02 * steps / 60.0, 0.0, 1.0)
        state["social_need"] = _clamp(float(state.get("social_need", 0.5)) + 0.01 * steps / 60.0, 0.0, 1.0)
        self._refresh_primary(state)
        state["updated_at"] = datetime.datetime.now().timestamp()
        return state

    def _refresh_primary(self, state: dict) -> None:
        """主情绪 = 强度最高的那个；背景数值可以额外抬高某些情绪。"""
        scores = dict(state.get("emotions", {}))
        scores["annoyed"] = max(scores.get("annoyed", 0.0), float(state.get("irritation", 0.0)) * 0.9)
        scores["angry"] = max(scores.get("angry", 0.0), max(0.0, float(state.get("irritation", 0.0)) - 0.45) * 1.6)
        scores["hurt"] = max(scores.get("hurt", 0.0), float(state.get("hurt", 0.0)))
        scores["affectionate"] = max(
            scores.get("affectionate", 0.0),
            max(0.0, float(state.get("affection", 0.0)) - 0.55) * 1.2,
        )
        scores["tired"] = max(scores.get("tired", 0.0), max(0.0, 0.45 - float(state.get("energy", 0.7))) * 1.5)
        # neutral 是基线，不参与竞争：没有明显情绪时才是平静
        scores.pop("neutral", None)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        primary, primary_score = ordered[0] if ordered else ("neutral", 0.0)
        if primary_score < 0.25:
            primary, primary_score = "neutral", max(0.3, 0.55 - primary_score)
        secondary, secondary_score = ("", 0.0)
        for name, score in ordered[1:]:
            if name != primary and score >= 0.25:
                secondary, secondary_score = name, score
                break
        state["primary"] = primary
        state["intensity"] = round(float(primary_score), 3)
        state["secondary"] = secondary
        state["secondary_intensity"] = round(float(secondary_score), 3)

    # ── 输出给模型看的描述 ──────────────────────────────────────────
    def describe(self, state: dict) -> str:
        primary = EMOTION_LABELS.get(state.get("primary", "neutral"), "平静")
        secondary = EMOTION_LABELS.get(state.get("secondary", ""), "")
        bits = [f"当前情绪：{primary}（强度 {state.get('intensity', 0):.2f}）"]
        if secondary:
            bits.append(f"带一点{secondary}（{state.get('secondary_intensity', 0):.2f}）")
        mood = float(state.get("mood", 0.0))
        bits.append(f"心情 {'+' if mood >= 0 else ''}{mood:.2f}")
        bits.append(f"精力 {float(state.get('energy', 0.7)):.2f}")
        bits.append(f"烦躁 {float(state.get('irritation', 0.0)):.2f}")
        bits.append(f"委屈 {float(state.get('hurt', 0.0)):.2f}")
        bits.append(f"亲近 {float(state.get('affection', 0.0)):.2f}")
        return "；".join(bits)

    def behavior_hints(self, state: dict) -> list[str]:
        """把情绪翻译成行为倾向（给模型的行为指引，避免它自己乱猜）。"""
        hints: list[str] = []
        irritation = float(state.get("irritation", 0.0))
        hurt = float(state.get("hurt", 0.0))
        affection = float(state.get("affection", 0.0))
        energy = float(state.get("energy", 0.7))
        mood = float(state.get("mood", 0.0))
        if irritation >= 0.6:
            hints.append("你现在很烦躁：话短、容易回怼、可能直接结束话题，语气冲但不人身攻击")
        elif irritation >= 0.3:
            hints.append("你有点不爽：比平时更毒舌，但还没到翻脸")
        if hurt >= 0.5:
            hints.append("你有点委屈：话变少、语气变冷，可以说“算了”“随便你”，但别大哭大闹")
        elif hurt >= 0.25:
            hints.append("你有一点不舒服，会拐着弯提一嘴，不会直接说")
        if affection >= 0.7 and mood > 0:
            hints.append("你现在挺亲近他：可以主动一点、开点玩笑，但不要肉麻")
        if energy <= 0.35:
            hints.append("你现在没什么精神：回复短、少反问、别展开话题")
        if float(state.get("intensity", 0)) >= 0.7 and state.get("primary") in ("excited", "happy", "angry", "hurt"):
            hints.append("你现在情绪很强：可以连发几条，像真的憋不住")
        return hints


def load_personality(path: Path) -> dict:
    """读取人格与情绪参数（personality.json），缺什么用默认值。"""
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("could not read personality file %s: %s", path, exc)
            data = {}
    data.setdefault("name", "角色")
    data.setdefault("sarcasm", 0.75)
    data.setdefault("tsundere", 0.8)
    data.setdefault("playfulness", 0.7)
    data.setdefault("warmth", 0.55)
    data.setdefault("assertiveness", 0.7)
    data.setdefault("vulgarity", 0.25)
    data.setdefault("teasing", 0.7)
    data.setdefault("patience", 0.45)
    data.setdefault("expressiveness", 0.8)
    return data
