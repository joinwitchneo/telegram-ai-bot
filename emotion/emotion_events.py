"""情绪事件定义：文本 → 事件 → 数值变化。

设计约束（Phase 4）：
- 情绪只能由事件驱动，**不允许随机数**；
- 所有维度统一量纲 0.0 ~ 1.0；
- 变化量由这里集中定义，方便调参与测试。
"""

from __future__ import annotations

import re

EMOTION_NAMES = (
    "joy", "interest", "affection", "trust", "calm", "excitement",
    "sadness", "anger", "anxiety", "loneliness", "embarrassment", "fatigue",
)

EMOTION_LABELS = {
    "joy": "开心", "interest": "兴趣", "affection": "亲近", "trust": "信任",
    "calm": "平静", "excitement": "兴奋", "sadness": "低落", "anger": "生气",
    "anxiety": "焦虑", "loneliness": "寂寞", "embarrassment": "害羞", "fatigue": "疲惫",
}

# 每个情绪的基线（安静状态下会回到这里）
BASELINE = {
    "joy": 0.45, "interest": 0.50, "affection": 0.35, "trust": 0.40,
    "calm": 0.55, "excitement": 0.28, "sadness": 0.12, "anger": 0.08,
    "anxiety": 0.15, "loneliness": 0.22, "embarrassment": 0.08, "fatigue": 0.30,
}

# 事件 → 各维度增减（同一条消息可能有多个事件）
EVENT_DELTAS: dict[str, dict[str, float]] = {
    "USER_PRAISE": {"joy": 0.10, "affection": 0.06, "excitement": 0.05, "embarrassment": 0.05},
    "USER_CARE": {"joy": 0.08, "affection": 0.08, "trust": 0.03, "loneliness": -0.06},
    "USER_JOKE": {"joy": 0.06, "interest": 0.04, "excitement": 0.04},
    "USER_TEASE": {"embarrassment": 0.06, "joy": 0.03},
    "USER_CHAT": {"interest": 0.02, "calm": 0.01, "loneliness": -0.02},
    "USER_COLD": {"loneliness": 0.06, "sadness": 0.04, "joy": -0.03},
    "USER_INSULT_MILD": {"anger": 0.10, "sadness": 0.06, "calm": -0.05},
    "USER_INSULT": {"anger": 0.20, "sadness": 0.12, "trust": -0.05, "calm": -0.10},
    "USER_APOLOGY": {"anger": -0.12, "sadness": -0.08, "trust": 0.03, "calm": 0.05},
    "USER_RETURNS": {"joy": 0.10, "loneliness": -0.12, "interest": 0.05},
    "USER_TIRED": {"fatigue": 0.05, "anxiety": 0.03, "interest": 0.02},
    "USER_SAD": {"sadness": 0.05, "anxiety": 0.04, "affection": 0.03},
    "TOPIC_INTERESTING": {"interest": 0.08, "excitement": 0.06},
    "TOPIC_BORING": {"interest": -0.05, "fatigue": 0.03},
    "LONG_SILENCE": {"loneliness": 0.05, "interest": -0.02},
    "INTERACTION_TIMEOUT": {"loneliness": 0.03},
    "PROACTIVE_IGNORED": {"loneliness": 0.04, "sadness": 0.03},
    "AGREEMENT_COMPLETED": {"trust": 0.05, "joy": 0.06},
    "RELATIONSHIP_NEGATIVE": {"trust": -0.06, "sadness": 0.05},
}

BORED_WORDS = ("无聊", "没意思", "好闷")
TIRED_WORDS = ("累", "困", "熬", "加班", "撑不住")
SAD_WORDS = (
    "难过", "委屈", "想哭", "失落", "烦死", "心情差", "不开心",
    "考砸", "搞砸", "没考好", "被骂", "吵架", "分手",
)

# 规则表：不依赖外部模块，纯关键词（0 token）
PRAISE_WORDS = ("可爱", "厉害", "好棒", "真棒", "喜欢你", "谢谢你", "辛苦了", "聪明",
                "不错", "爱你", "好看", "优秀", "贴心", "温柔", "靠谱")
CARE_WORDS = ("你还好吗", "还好吗", "累不累", "注意身体", "早点睡", "该睡了", "吃饭了吗",
              "吃了吗", "别太累", "多喝水", "休息一下", "别熬夜", "照顾好自己", "冷不冷")
APOLOGY_WORDS = ("对不起", "抱歉", "我错了", "别生气", "原谅我", "不是故意")
INSULT_HARD = ("傻逼", "废物", "去死", "垃圾", "智障", "脑残")
INSULT_MILD = ("真蠢", "好蠢", "笨蛋", "傻子", "滚", "闭嘴", "烦人", "讨厌你", "有病")
JOKE_WORDS = ("哈哈", "笑死", "嘻嘻", "嘿嘿", "开玩笑", "逗你")
RETURN_WORDS = ("我回来了", "回来了", "在吗", "好久不见", "好久没", "想你了")
COLD_WORDS = {"哦", "嗯", "行", "好", "知道了", "随便", "呵呵", "哦哦", "嗯嗯"}


def map_text_to_events(text: str) -> list[str]:
    """把一句用户消息映射成情绪事件名（纯规则，不调用模型）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    events: list[str] = []
    lowered = raw.lower()
    if any(word in lowered for word in INSULT_HARD):
        events.append("USER_INSULT")
    elif any(word in raw for word in INSULT_MILD):
        events.append("USER_INSULT_MILD")
    if any(word in raw for word in APOLOGY_WORDS):
        events.append("USER_APOLOGY")
    if any(word in raw for word in PRAISE_WORDS):
        events.append("USER_PRAISE")
    if any(word in raw for word in CARE_WORDS):
        events.append("USER_CARE")
    if any(word in raw for word in JOKE_WORDS):
        events.append("USER_JOKE")
    if any(word in raw for word in RETURN_WORDS):
        events.append("USER_RETURNS")
    if raw in COLD_WORDS:
        events.append("USER_COLD")
    if any(word in raw for word in BORED_WORDS):
        events.append("TOPIC_BORING")
    if any(word in raw for word in TIRED_WORDS):
        events.append("USER_TIRED")
    if any(word in raw for word in SAD_WORDS):
        events.append("USER_SAD")
    if not events:
        events.append("USER_CHAT")
    return events


def summarise_emotion(state: dict) -> str:
    """把数值状态压成一句自然语言（给 L2 用，不暴露数字）。"""
    joy = state.get("joy", 0.45)
    interest = state.get("interest", 0.5)
    calm = state.get("calm", 0.55)
    fatigue = state.get("fatigue", 0.3)
    loneliness = state.get("loneliness", 0.2)
    sadness = state.get("sadness", 0.12)
    anger = state.get("anger", 0.08)

    if joy >= 0.7:
        mood = "今天心情很好"
    elif joy >= 0.55:
        mood = "心情不错"
    elif joy <= 0.3:
        mood = "心情有点低落"
    else:
        mood = "心情一般"
    parts = [f"当前情绪：{mood}"]
    if interest >= 0.65:
        parts.append("对他讲的东西挺有兴趣")
    elif interest <= 0.35:
        parts.append("这会儿有点提不起兴趣")
    if fatigue >= 0.6:
        parts.append("有点累，话会少一些")
    if sadness >= 0.35:
        parts.append("情绪偏低")
    if anger >= 0.35:
        parts.append("还有点不爽")
    if calm <= 0.35:
        parts.append("不太平静")
    if loneliness >= 0.5:
        parts.append("有点寂寞，想让他多陪一会儿")
    return "；".join(parts)
