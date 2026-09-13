"""Response Planner（Phase 5）：决定"怎么回"，不决定"回什么"。

职责边界（与 LLM Policy 严格分开）：
    LLM Policy      —— 这一轮要不要调模型、调哪一档（总闸门）
    Response Planner —— 既然要回，回几条、多长、什么语气、停顿多久

三条硬规则：
1. 纯 Python、0 token、**无随机数**（随机只允许出现在 Renderer 的停顿微扰里）；
2. 提问 / 明确请求 / 明显负面情绪 属于"必须回"，任何分数都不能覆盖；
3. "不回复"必须有明确原因 + 冷却限制，绝不允许随机消失。
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

LENGTHS = ("ULTRA_SHORT", "SHORT", "NORMAL", "LONG")
TONES = ("PLAYFUL", "WARM", "GENTLE", "CLIPPED", "QUIET", "NEUTRAL")

# 每种长度档的停顿范围（秒），Renderer 只允许在这个区间里微扰
PAUSE_RANGES: dict[str, tuple[float, float]] = {
    "ULTRA_SHORT": (0.4, 1.0),
    "SHORT": (0.5, 1.5),
    "NORMAL": (0.8, 3.0),
    "LONG": (1.0, 4.5),
}

# 每种长度档最多拆成几条
MAX_MESSAGES_BY_LENGTH = {"ULTRA_SHORT": 1, "SHORT": 2, "NORMAL": 2, "LONG": 3}

FILLER_WORDS = (
    "嗯", "嗯嗯", "嗯呐", "哦", "噢", "哦哦", "好", "好的", "行", "收到",
    "哈哈", "哈哈哈", "嘿嘿", "呵", "……", "...", "。。",
)

QUESTION_WORDS = (
    "吗", "呢", "怎么", "为什么", "为啥", "啥", "什么", "哪", "谁", "是不是",
    "好不好", "行不行", "能不能", "可以吗", "有没有", "多少", "几点", "干嘛",
    "该不该", "要不要", "怎么样", "如何", "啥时候", "对吧",
)

REQUEST_WORDS = (
    "帮我", "提醒我", "提醒", "记一下", "记个", "查一下", "帮我查", "搜索", "搜一下",
    "翻译", "算一下", "列一下", "写个", "生成", "总结一下", "定个", "安排好",
)

NEGATIVE_WORDS = (
    "难过", "委屈", "烦", "难受", "生气", "想哭", "崩溃", "焦虑", "压力", "撑不住",
    "emo", "心态炸", "失眠", "孤独", "寂寞", "失落", "累死", "好累", "不开心", "倒霉",
    "考砸", "搞砸", "没考好", "被骂", "吵架", "分手", "失业", "扣钱", "不顺",
)

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def cjk_length(text: str) -> int:
    return len(CJK_RE.findall(text or ""))


def is_question(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.endswith(("?", "？")):
        return True
    if "?" in raw or "？" in raw:
        return True
    return any(word in raw for word in QUESTION_WORDS)


def is_request(text: str) -> bool:
    raw = (text or "").strip()
    return any(word in raw for word in REQUEST_WORDS)


def has_negative_emotion(text: str) -> bool:
    raw = (text or "").strip().lower()
    return any(word in raw for word in NEGATIVE_WORDS)


def is_low_value(text: str) -> bool:
    """低价值消息：纯语气词/单字回应，没有新信息。"""
    raw = re.sub(r"[\s!?！？。，,~～]+", "", (text or "").strip())
    if not raw:
        return True
    if raw in FILLER_WORDS:
        return True
    return len(raw) <= 2 and cjk_length(raw) <= 2


def _signal(signals: dict | None, group: str, name: str, default: float = 0.0) -> float:
    if not signals:
        return default
    block = signals.get(group) or {}
    try:
        return float(block.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class ReplyPlan:
    """内部回复计划：由 Python 生成，再由 Validator 复核。"""

    should_reply: bool = True
    score: float = 0.5
    reason: str = "normal"
    mode: str = "CASUAL"
    length: str = "NORMAL"
    tone: str = "NEUTRAL"
    message_count: int = 1
    split: bool = False
    pause: tuple[float, float] = (0.5, 1.5)
    sticker_allowed: bool = False
    must_reply: bool = False
    tool_needed: bool = False
    parts: dict = field(default_factory=dict)

    # ── 给上下文用的自然语言倾向（L2）──
    def tone_hint(self) -> str:
        return TONE_HINTS.get(self.tone, "")

    def length_hint(self) -> str:
        return LENGTH_HINTS.get(self.length, "")

    def to_plan_dict(self) -> dict:
        return {
            "should_reply": self.should_reply,
            "reply_mode": self.mode,
            "message_count": self.message_count,
            "length": self.length,
            "tone": self.tone,
            "split": self.split,
            "pause": [round(self.pause[0], 2), round(self.pause[1], 2)],
            "sticker": self.sticker_allowed,
            "score": round(self.score, 3),
            "reason": self.reason,
        }


TONE_HINTS = {
    "PLAYFUL": "心情不错，可以轻松一点、开个小玩笑，但别没完。",
    "WARM": "关系挺近，语气自然亲近一些，不用客套。",
    "GENTLE": "对方现在不太顺，先接住情绪，别讲大道理、别急着给方案。",
    "CLIPPED": "她现在有点不高兴，话短、嘴硬、但别真的伤人。",
    "QUIET": "她有点累，回复短一点，别硬撑着长篇大论。",
    "NEUTRAL": "正常聊天语气，别客套。",
}

LENGTH_HINTS = {
    "ULTRA_SHORT": "只回一句话，十个字上下，像随手打的，别解释、别追问太多。",
    "SHORT": "回一两句短话，总共三四十字以内，别展开。",
    "NORMAL": "可以正常聊几句，别写作文。",
    "LONG": "这个话题值得展开，但依然要像聊天，不要分点、不要总结体。",
}


class ResponsePlanner:
    """把"用户这条消息 + 当前状态 + 用户风格"算成一个可解释的回复计划。"""

    def __init__(
        self,
        *,
        score_normal: float = 0.75,
        score_short: float = 0.50,
        score_low: float = 0.30,
        allow_silence: bool = True,
        silence_cooldown_minutes: int = 10,
        max_consecutive_replies: int = 3,
        max_messages: int = 4,
        sticker_enabled: bool = True,
        now_fn=None,
    ) -> None:
        self.score_normal = float(score_normal)
        self.score_short = float(score_short)
        self.score_low = float(score_low)
        self.allow_silence = bool(allow_silence)
        self.silence_cooldown = max(1, int(silence_cooldown_minutes)) * 60
        self.max_consecutive_replies = max(1, int(max_consecutive_replies))
        self.max_messages = max(1, int(max_messages))
        self.sticker_enabled = bool(sticker_enabled)
        self._now = now_fn or datetime.datetime.now
        self._last_skip_at: datetime.datetime | None = None

    # ── 打分 ────────────────────────────────────────────────────────
    def score(
        self,
        text: str,
        *,
        mode: str = "CASUAL",
        signals: dict | None = None,
        style: dict | None = None,
        gap_minutes: float = 0.0,
        consecutive_replies: int = 0,
        last_user_text: str = "",
    ) -> tuple[float, dict, dict]:
        """返回 (score, 明细, 硬规则)。纯函数、可解释、无随机。"""
        raw = (text or "").strip()
        hard = {
            "question": is_question(raw),
            "request": is_request(raw),
            "negative": has_negative_emotion(raw) or mode == "EMOTIONAL",
            "low_value": is_low_value(raw),
        }
        parts: dict[str, float] = {"base": 0.5}

        if hard["question"]:
            parts["question"] = 0.25
        if hard["request"]:
            parts["request"] = 0.30
        if hard["negative"]:
            parts["negative_emotion"] = 0.30
        if cjk_length(raw) >= 60:
            parts["long_message"] = 0.15
        elif cjk_length(raw) >= 25:
            parts["medium_message"] = 0.08
        if mode == "DEEP":
            parts["deep_talk"] = 0.10
        if gap_minutes >= 30:
            parts["long_gap"] = 0.05
        if _signal(signals, "relationship", "interaction_heat", 0.2) >= 0.5:
            parts["heat"] = 0.05
        if _signal(signals, "relationship", "intimacy", 0.05) >= 0.4:
            parts["intimacy"] = 0.05
        if _signal(signals, "emotion", "interest", 0.5) >= 0.65:
            parts["her_interest"] = 0.04

        if hard["low_value"]:
            parts["low_value"] = -0.35
        if last_user_text and raw and raw == last_user_text.strip():
            parts["duplicate"] = -0.25
        if consecutive_replies >= self.max_consecutive_replies:
            parts["saturated"] = -0.10
        if _signal(signals, "emotion", "fatigue", 0.3) >= 0.6:
            parts["her_fatigue"] = -0.05
        if mode == "TASK":
            parts["task"] = 0.05

        total = _clamp(sum(parts.values()))
        return round(total, 3), parts, hard

    # ── 主入口 ──────────────────────────────────────────────────────
    def plan_proactive(
        self,
        *,
        candidate=None,
        signals: dict | None = None,
        style: dict | None = None,
        mode: str = "CASUAL",
        length: str | None = None,
        tone: str | None = None,
    ) -> ReplyPlan:
        """主动消息的形态：复用同一套长度/语气/停顿规则，不做打分。"""
        score = float(getattr(candidate, "score", 0.6) or 0.6)
        special = bool(getattr(candidate, "special", False))
        reason = str(getattr(candidate, "reason", "character_thought") or "character_thought")
        if length is None:
            length = "NORMAL" if (score >= self.score_normal or special) else "SHORT"
        if length not in LENGTHS:
            length = "SHORT"
        count = 1
        try:
            bursts = float((style or {}).get("consecutive_message_count", 0.0))
        except (TypeError, ValueError):
            bursts = 0.0
        if length == "NORMAL" and bursts >= 2.2:
            count = 2
        plan = ReplyPlan(
            should_reply=True,
            score=score,
            reason=f"proactive:{reason}",
            mode=mode,
            length=length,
            tone=tone or self._decide_tone(mode=mode, signals=signals, hard={"negative": False}),
            message_count=min(count, self.max_messages),
            split=count > 1,
            pause=PAUSE_RANGES.get(length, (0.5, 1.5)),
            must_reply=True,
        )
        plan.sticker_allowed = self._decide_sticker(plan, signals=signals)
        return plan

    def plan(
        self,
        text: str,
        *,
        mode: str = "CASUAL",
        signals: dict | None = None,
        style: dict | None = None,
        gap_minutes: float = 0.0,
        consecutive_replies: int = 0,
        last_user_text: str = "",
        now: datetime.datetime | None = None,
    ) -> ReplyPlan:
        moment = now or self._now()
        score, parts, hard = self.score(
            text,
            mode=mode,
            signals=signals,
            style=style,
            gap_minutes=gap_minutes,
            consecutive_replies=consecutive_replies,
            last_user_text=last_user_text,
        )
        must_reply = bool(hard["question"] or hard["request"] or hard["negative"])
        tool_needed = bool(hard["request"])

        plan = ReplyPlan(
            should_reply=True,
            score=score,
            mode=mode,
            must_reply=must_reply,
            tool_needed=tool_needed,
            parts=parts,
        )

        # ── 不回复：必须有明确原因，且不能是硬规则场景 ──
        if not must_reply and score < self.score_low:
            if self._can_stay_silent(consecutive_replies=consecutive_replies, gap_minutes=gap_minutes):
                self._last_skip_at = moment
                plan.should_reply = False
                plan.reason = "low_value_saturated" if hard["low_value"] else "low_signal"
                plan.length = "ULTRA_SHORT"
                plan.message_count = 0
                plan.split = False
                plan.sticker_allowed = False
                return plan
            if consecutive_replies <= 0 or gap_minutes >= 30:
                plan.reason = "first_message_always_reply"
            else:
                plan.reason = "silence_cooldown"
        elif must_reply:
            plan.reason = "hard_rule"
        elif score >= self.score_normal:
            plan.reason = "high_score"
        elif score >= self.score_short:
            plan.reason = "medium_score"
        else:
            plan.reason = "low_score"

        plan.length = self._decide_length(score=score, mode=mode, hard=hard, style=style, text=text)
        # 提问再短也要像在回答，不能只回一个字
        if hard["question"] and plan.length == "ULTRA_SHORT":
            plan.length = "SHORT"
        plan.message_count = self._decide_count(
            length=plan.length, mode=mode, style=style, hard=hard, text=text
        )
        plan.split = plan.message_count > 1
        plan.pause = PAUSE_RANGES.get(plan.length, (0.8, 3.0))
        plan.tone = self._decide_tone(mode=mode, signals=signals, hard=hard)
        plan.sticker_allowed = self._decide_sticker(plan, signals=signals)
        return plan

    # ── 子决策 ──────────────────────────────────────────────────────
    def _can_stay_silent(self, *, consecutive_replies: int, gap_minutes: float) -> bool:
        if not self.allow_silence:
            return False
        # 第一条消息一定回；久别重逢一定回
        if consecutive_replies <= 0 or gap_minutes >= 30:
            return False
        if self._last_skip_at is None:
            return True
        return (self._now() - self._last_skip_at).total_seconds() >= self.silence_cooldown

    def _decide_length(self, *, score: float, mode: str, hard: dict, style: dict | None, text: str) -> str:
        if mode == "EMOTIONAL" or hard["negative"]:
            length = "NORMAL" if score >= self.score_normal else "SHORT"
        elif hard["request"]:
            length = "NORMAL"
        elif score >= self.score_normal:
            length = "LONG" if (mode == "DEEP" or cjk_length(text) >= 60) else "NORMAL"
        elif score >= self.score_short:
            length = "SHORT"
        else:
            length = "ULTRA_SHORT"

        # 对方习惯短消息 → 降一档，但不再往下降
        if style and self._short_style_user(style):
            order = list(LENGTHS)
            index = order.index(length)
            if index > 0 and length != "SHORT":
                length = order[index - 1]
        return length

    @staticmethod
    def _short_style_user(style: dict) -> bool:
        try:
            if float(style.get("average_message_length", 999)) <= 12 and float(
                style.get("short_message_ratio", 0.0)
            ) >= 0.6:
                return True
        except (TypeError, ValueError):
            return False
        return False

    def _decide_count(self, *, length: str, mode: str, style: dict | None, hard: dict, text: str) -> int:
        bursts = 0.0
        try:
            bursts = float((style or {}).get("consecutive_message_count", 0.0))
        except (TypeError, ValueError):
            bursts = 0.0
        base = MAX_MESSAGES_BY_LENGTH.get(length, 1)
        count = base
        if length == "SHORT":
            # 短回复也要能连发：他习惯连发、或者这句本身有好几个小句时，拆两条
            if bursts >= 2.0 or hard["question"] or cjk_length(text) >= 12:
                count = 2
            else:
                count = 1
        elif length == "NORMAL":
            count = 3 if (bursts >= 2.2 or mode in ("EMOTIONAL", "DEEP")) else 2
        elif length == "LONG":
            count = 3
        return max(1, min(count, self.max_messages))

    @staticmethod
    def _decide_tone(*, mode: str, signals: dict | None, hard: dict) -> str:
        anger = _signal(signals, "emotion", "anger", 0.08)
        fatigue = _signal(signals, "emotion", "fatigue", 0.30)
        sadness = _signal(signals, "emotion", "sadness", 0.12)
        anxiety = _signal(signals, "emotion", "anxiety", 0.15)
        joy = _signal(signals, "emotion", "joy", 0.45)
        excitement = _signal(signals, "emotion", "excitement", 0.28)
        intimacy = _signal(signals, "relationship", "intimacy", 0.05)
        if hard["negative"] or mode == "EMOTIONAL":
            return "GENTLE"
        if anger >= 0.35:
            return "CLIPPED"
        if fatigue >= 0.6:
            return "QUIET"
        if sadness >= 0.4 or anxiety >= 0.45:
            return "GENTLE"
        if joy >= 0.6 or excitement >= 0.5:
            return "PLAYFUL"
        if intimacy >= 0.4:
            return "WARM"
        return "NEUTRAL"

    def _decide_sticker(self, plan: ReplyPlan, *, signals: dict | None) -> bool:
        if not self.sticker_enabled or plan.tool_needed:
            return False
        if plan.length == "LONG" or plan.mode == "TASK":
            return False
        joy = _signal(signals, "emotion", "joy", 0.45)
        return plan.tone in ("PLAYFUL", "WARM") or joy >= 0.55
