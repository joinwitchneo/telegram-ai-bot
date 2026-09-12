"""Stage 2：消息策略选择。

决定"这一轮角色准备怎么说"：说几句、多快回、要不要连发、要不要带刺。
数值全部由 Python 决定，模型只负责把内容写出来。
"""

from __future__ import annotations

import random

MODES = ["SHORT", "NORMAL", "LONG", "BURST", "COLD", "PLAYFUL", "ANGRY", "HURT", "AFFECTIONATE"]


def _burst_probability(intensity: float, relationship: float, energy: float) -> float:
    if intensity > 0.75:
        probability = 0.35
    elif intensity > 0.55:
        probability = 0.15
    else:
        probability = 0.03
    probability *= 0.6 + 0.8 * relationship      # 越熟越容易刷屏
    probability *= 0.7 + 0.5 * energy            # 越有精神越容易刷屏
    return max(0.0, min(0.6, probability))


def _vulgarity_hint(irritation: float, personality: dict) -> str:
    """脏话概率（文档第三十五节）：平时几乎不用，生气时才允许。"""
    base = float(personality.get("vulgarity", 0.25))
    if irritation >= 0.75:
        limit = "30%~45%"
    elif irritation >= 0.5:
        limit = "15%~30%"
    elif irritation >= 0.25:
        limit = "5%~10%"
    else:
        limit = "0~2%"
    if base <= 0.1:
        return "不要用脏话（人格参数里脏话倾向很低）。"
    return (
        f"脏话倾向：{limit} 的句子可以带一个轻微的脏字（比如“靠”“妈的”这种量级），"
        "其余时候不要用；绝对不要连着骂人。"
    )


def choose(
    emotion_state: dict,
    relationship: dict,
    analysis: dict,
    personality: dict,
    *,
    energy_bias: float = 0.0,
    user_style: dict | None = None,
    long_term_style: dict | None = None,
    phase: str = "",
    dependence: float = 0.0,
    playfulness: float = 0.0,
) -> dict:
    """选出本轮的说话策略。"""
    irritation = float(emotion_state.get("irritation", 0.0))
    hurt = float(emotion_state.get("hurt", 0.0))
    affection = float(emotion_state.get("affection", 0.0))
    mood = float(emotion_state.get("mood", 0.0))
    energy = max(0.0, min(1.0, float(emotion_state.get("energy", 0.7)) + energy_bias))
    intensity = float(emotion_state.get("intensity", 0.0))
    primary = emotion_state.get("primary", "neutral")
    level = float(relationship.get("level", 0.05))
    intent = analysis.get("user_intent", "casual_chat")
    conflict = bool(analysis.get("conflict"))
    style = user_style or analysis.get("style") or {}
    long_style = long_term_style or {}

    # 长期习惯：这个人平时就发很短的消息 → 整体偏向短回复（文档第六节）
    habitual_short = float(long_style.get("avg_length", 99) or 99) <= 8 and int(
        long_style.get("samples", 0) or 0
    ) >= 5
    this_short = style.get("length_class") == "ultra_short"
    this_long = style.get("length_class") == "long"

    mode = "NORMAL"
    min_messages, max_messages = 1, 3
    delay_first = (0.8, 2.5)
    delay_between = (0.6, 2.0)
    allow_sticker = True
    notes: list[str] = []

    if irritation >= 0.6:
        mode = "ANGRY"
        min_messages, max_messages = 1, 2
        delay_first = (0.3, 1.2)       # 生气的时候回得快
        delay_between = (0.4, 1.2)
        allow_sticker = False
        notes.append("可以直接回怼、话短、别解释太多；他要是继续惹你，可以冷一句“行，你说了算”。")
    elif hurt >= 0.5:
        mode = "HURT"
        min_messages, max_messages = 1, 2
        delay_first = (1.5, 4.0)
        delay_between = (1.0, 2.5)
        allow_sticker = False
        notes.append("话少、语气淡，可以“哦”“算了”“随你”，但不要哭闹；等对方哄你再慢慢软下来。")
    elif (this_short or habitual_short) and not this_long and intent not in ("long_talk", "question"):
        mode = "SHORT"
        min_messages, max_messages = 1, 1
        delay_first = (0.8, 2.5)
        delay_between = (0.6, 1.5)
        notes.append("对方发得很短：你也只回一句，几个字到十几个字，别展开。")
    elif energy <= 0.35:
        mode = "SHORT"
        min_messages, max_messages = 1, 1
        delay_first = (1.5, 3.5)
        delay_between = (1.0, 2.0)
        notes.append("没精神：就一句，短、平，不展开话题。")
    elif affection >= 0.6 and mood > 0.2 and random.random() < 0.35:
        mode = "AFFECTIONATE"
        min_messages, max_messages = 1, 3
        notes.append("可以亲近一点、主动一点，但不要腻；记住你是傲娇，别把好意说得太直白。")
    elif primary in ("happy", "excited", "playful") and intensity >= 0.5:
        mode = "PLAYFUL"
        min_messages, max_messages = 1, 3
        notes.append("可以开玩笑、接梗、损他一句。")
    elif intent == "long_talk" or intent == "question" or this_long:
        mode = "LONG"
        min_messages, max_messages = 2, 4
        delay_first = (2.0, 5.0)
        delay_between = (0.8, 2.2)
        notes.append("他在认真聊：可以说 2~4 条，但别写成小作文，也不要给一堆建议。")
    else:
        notes.append("正常聊天：1~3 条，像随手回消息。")

    if conflict and mode != "ANGRY" and mode != "HURT":
        notes.append("他刚骂了你：可以顶回去，也可以冷处理，别装没事。")

    # 模仿对方的表达习惯（标点 / emoji / 连发），但只学节奏不抄内容（文档第二、七节）
    if style:
        if style.get("punctuation_style") == "sparse":
            notes.append("对方几乎不打标点：你也少打，句末可以不用句号。")
        if not style.get("emoji_usage"):
            notes.append("对方不用 emoji：你也别用。")
        elif int(style.get("emoji_count", 0) or 0) >= 2:
            notes.append("对方爱用 emoji：你可以跟着用一点，但别堆。")
        if int(style.get("message_count", 1) or 1) >= 3:
            notes.append("对方刚才是连着发几条的：你也可以分条回应，不要合并成一大段。")
        if style.get("question_style"):
            notes.append("对方在问你问题：先回答，再顺口反问他一句。")

    burst = False
    # 只有这些模式才允许"刷屏"；生气/委屈/没精神时不允许被打断成连发
    if mode in ("PLAYFUL", "AFFECTIONATE", "NORMAL", "LONG") and primary in (
        "excited", "happy", "playful", "affectionate", "surprised",
    ):
        probability = _burst_probability(intensity, level, energy)
        if style.get("burst_style") or float(long_style.get("burst_rate", 0) or 0) >= 0.5:
            probability *= 1.3  # 对方习惯连发，你也更容易连发
        if random.random() < probability:
            burst = True
            mode = "BURST"
            min_messages, max_messages = 2, 4
            delay_first = (1.0, 2.0)
            delay_between = (0.8, 2.0)
            notes.append("情绪上来了：可以连发 2~4 条，第一条短一点，像脱口而出。")

    if level > 0.75:
        notes.append("你们已经很熟：说话可以更随意、更直接。")
    elif level < 0.25:
        notes.append("你们还不算熟：别太自来熟，别撒娇，话留一点。")

    if random.random() < float(personality.get("tsundere", 0.8)) * 0.35:
        notes.append("这轮可以带一点傲娇：嘴上否认，行为上关心。")

    # 聊天阶段（文档第十四节）：只做最有用的几个
    if phase == "ENDING":
        mode = "SHORT"
        min_messages, max_messages = 1, 1
        delay_first = (0.8, 2.5)
        delay_between = (0.6, 1.2)
        allow_sticker = True
        notes.append("他在收尾：回一句就够。不追问、不留钩子、不说“我等你”。")
    elif phase == "STARTING" and mode in ("NORMAL", "PLAYFUL", "AFFECTIONATE"):
        max_messages = min(max_messages, 2)
        notes.append("刚开场：别一上来倒一堆话，也别太自来熟。")
    elif phase == "DEEP":
        mode = "LONG"
        min_messages, max_messages = 2, 4
        delay_first = (2.0, 5.0)
        notes.append("他在认真讲自己的事：认真听、可以追问细节，但别给建议、别说教、别总结。")
    elif phase == "CONFLICT" and mode not in ("ANGRY", "HURT"):
        max_messages = min(max_messages, 2)
        notes.append("你们刚闹过：语气还没完全回去，别立刻热络。")

    # 依赖度上来之后允许粘人（但仍是傲娇式表达）
    if dependence >= 0.6 and affection >= 0.45 and mood > 0.0 and random.random() < 0.4:
        notes.append("你其实想让他多陪一会儿：可以要求他留下（“再聊五分钟”“别急着走”），但嘴上要否认自己在粘人。")

    if playfulness >= 0.6:
        notes.append("你们平时互相整活：这轮可以更皮一点，玩笑可以开大一点，但别伤人。")

    # 跑题（Topic Drift）：概率随关系上升，但认真聊/收尾/闹情绪时不许跑
    topic_drift = False
    if phase not in ("DEEP", "ENDING") and mode not in ("ANGRY", "HURT", "SHORT"):
        drift_probability = 0.15 + 0.10 * max(0.0, min(1.0, (level - 0.3) / 0.5))
        if random.random() < drift_probability * (0.8 + 0.4 * energy):
            topic_drift = True
            notes.append(
                "可以顺口扯到一个相关的小话题（比如突然想起另一件事、或反问一句别的），"
                "但要先把他的话接住，别丢下他在说的事、别硬转。"
            )

    return {
        "mode": mode,
        "min_messages": min_messages,
        "max_messages": max_messages,
        "delay_first": delay_first,
        "delay_between": delay_between,
        "allow_sticker": allow_sticker,
        "burst": burst,
        "notes": notes,
        "phase": phase,
        "topic_drift": topic_drift,
        "vulgarity_hint": _vulgarity_hint(irritation, personality),
    }


def describe(strategy: dict) -> str:
    """给模型的策略说明（不要暴露内部数值给用户，只用于生成）。"""
    lines = [
        f"本轮说话模式：{strategy['mode']}",
        f"条数：{strategy['min_messages']}~{strategy['max_messages']} 条",
        "要求：" + " ".join(strategy.get("notes", [])),
        str(strategy.get("vulgarity_hint", "")),
    ]
    return "\n".join(line for line in lines if line.strip())
