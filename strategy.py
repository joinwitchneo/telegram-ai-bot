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
    elif intent == "long_talk" or intent == "question":
        mode = "LONG"
        min_messages, max_messages = 2, 4
        delay_first = (2.0, 5.0)
        delay_between = (0.8, 2.2)
        notes.append("他在认真聊：可以说 2~4 条，但别写成小作文，也不要给一堆建议。")
    else:
        notes.append("正常聊天：1~3 条，像随手回消息。")

    if conflict and mode != "ANGRY" and mode != "HURT":
        notes.append("他刚骂了你：可以顶回去，也可以冷处理，别装没事。")

    burst = False
    # 只有这些模式才允许"刷屏"；生气/委屈/没精神时不允许被打断成连发
    if mode in ("PLAYFUL", "AFFECTIONATE", "NORMAL", "LONG") and primary in (
        "excited", "happy", "playful", "affectionate", "surprised",
    ):
        probability = _burst_probability(intensity, level, energy)
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

    return {
        "mode": mode,
        "min_messages": min_messages,
        "max_messages": max_messages,
        "delay_first": delay_first,
        "delay_between": delay_between,
        "allow_sticker": allow_sticker,
        "burst": burst,
        "notes": notes,
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
