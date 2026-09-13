"""关系事件定义：五维变化量 + 基线 + 衰减率 + 自然语言摘要。

关键设计：快变量（interaction_heat）可以明显波动，
慢变量（familiarity / trust / intimacy / shared_experience）涨幅极小、几乎不衰减，
这样聊几十条不会把亲密度刷满。
"""

from __future__ import annotations

DIMENSIONS = ("familiarity", "trust", "intimacy", "interaction_heat", "shared_experience")

DIMENSION_LABELS = {
    "familiarity": "熟悉度",
    "trust": "信任",
    "intimacy": "亲密度",
    "interaction_heat": "互动热度",
    "shared_experience": "共同经历",
}

BASELINE = {
    "familiarity": 0.05,
    "trust": 0.30,
    "intimacy": 0.05,
    "interaction_heat": 0.20,
    "shared_experience": 0.02,
}

# 每小时衰减率：热度掉得快，长期维度几乎不动
DECAY_RATES = {
    "interaction_heat": 0.06,
    "intimacy": 0.0005,
    "familiarity": 0.0,
    "trust": 0.0,
    "shared_experience": 0.0,
}

EVENT_DELTAS: dict[str, dict[str, float]] = {
    "USER_CHAT": {"interaction_heat": 0.05, "familiarity": 0.004},
    "USER_CARE": {"interaction_heat": 0.06, "intimacy": 0.010, "trust": 0.010},
    "USER_PRAISE": {"interaction_heat": 0.05, "intimacy": 0.008},
    "USER_JOKE": {"interaction_heat": 0.05, "intimacy": 0.005},
    "USER_TEASE": {"interaction_heat": 0.04, "intimacy": 0.004},
    "USER_RETURNS": {"interaction_heat": 0.08},
    "USER_COLD": {"interaction_heat": -0.03},
    "USER_INSULT_MILD": {"interaction_heat": -0.04, "trust": -0.02},
    "USER_INSULT": {"interaction_heat": -0.06, "trust": -0.04, "intimacy": -0.02},
    "USER_APOLOGY": {"interaction_heat": 0.03, "trust": 0.02},
    "AGREEMENT_COMPLETED": {"trust": 0.03, "shared_experience": 0.02, "intimacy": 0.01},
    "SHARED_EVENT": {"shared_experience": 0.04, "interaction_heat": 0.03, "intimacy": 0.01},
    "RELATIONSHIP_NEGATIVE": {"trust": -0.05, "intimacy": -0.02},
    "LONG_SILENCE": {"interaction_heat": -0.05},
}


def describe(state: dict) -> str:
    """把五维压成一句自然语言（给 L2 用，不暴露数字）。"""
    familiarity = float(state.get("familiarity", 0.05))
    trust = float(state.get("trust", 0.3))
    intimacy = float(state.get("intimacy", 0.05))
    heat = float(state.get("interaction_heat", 0.2))
    shared = float(state.get("shared_experience", 0.02))

    if familiarity >= 0.7:
        familiar_text = "已经很熟了"
    elif familiarity >= 0.4:
        familiar_text = "算是熟人"
    elif familiarity >= 0.2:
        familiar_text = "有点熟了"
    else:
        familiar_text = "还不算熟"
    parts = [f"关系：{familiar_text}"]
    if trust >= 0.6:
        parts.append("比较信任他")
    elif trust <= 0.25:
        parts.append("还没完全信任")
    if intimacy >= 0.5:
        parts.append("关系亲近")
    elif intimacy <= 0.1:
        parts.append("保持自然距离")
    if shared >= 0.3:
        parts.append("有不少共同经历")
    if heat >= 0.6:
        parts.append("最近互动很频繁")
    elif heat <= 0.2:
        parts.append("最近互动偏少")
    return "；".join(parts)


def behavior_hints(state: dict) -> list[str]:
    familiarity = float(state.get("familiarity", 0.05))
    intimacy = float(state.get("intimacy", 0.05))
    heat = float(state.get("interaction_heat", 0.2))
    trust = float(state.get("trust", 0.3))
    hints: list[str] = []
    if familiarity >= 0.5:
        hints.append("称呼和语气可以更随意")
    elif familiarity < 0.2:
        hints.append("别太自来熟，保持一点距离")
    if intimacy >= 0.4:
        hints.append("可以自然开玩笑、偶尔亲近一点")
    if heat >= 0.6:
        hints.append("可以顺势延续刚才的话题")
    elif heat <= 0.2:
        hints.append("别表现得太亲密，先自然聊")
    if trust <= 0.25:
        hints.append("避免过度打探他的私事")
    return hints[:3]
