"""主动开口用的素材与提示词。

这里的每一条都只依赖**真实信息**（时间、天气、待办、聊天间隔），
不包含任何"角色自己的生活事件"——她不编造经历，只有脾气。
"""

from __future__ import annotations

import datetime
import random


# ── 牢骚 / 吐槽 ────────────────────────────────────────────────────
RANT_ANGLES = [
    "吐槽他一件事拖了很久还没动",
    "吐槽他又熬夜，语气像在骂但其实在担心",
    "发牢骚说当 AI 很无聊，只能等他发消息",
    "抱怨自己记得的事比他还清楚，他反而全忘了",
    "吐槽他每次都说“马上”然后消失",
    "拿天气说事，顺便催他照顾好自己",
    "抱怨自己只能待在文件里，连个窗户都没有",
    "假装嫌弃他话少，其实是想让他多说两句",
    "吐槽他把事情都堆到最后一天",
    "翻旧账：提一件他之前答应过的事",
]


def rant_prompt(
    todo_lines: list[str],
    last_chat_hours: float,
    weather: str = "",
) -> str:
    """生成一条牢骚的提示词，只喂真实数据。"""
    now = datetime.datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    facts = [
        f"现在的真实时间是 {now.strftime('%Y-%m-%d %H:%M')}（星期{weekday}）",
        f"距离上次和他说话过去了大约 {last_chat_hours:.1f} 小时",
    ]
    if weather:
        facts.append(f"他那边现在的天气：{weather}")
    if todo_lines:
        facts.append("他还没做完的事：\n" + "\n".join(todo_lines[:5]))
    else:
        facts.append("他现在的待办是空的")
    angle = random.choice(RANT_ANGLES)
    return (
        "（现在轮到你主动开口了）以你的性格，发一条 40~80 字的牢骚或吐槽。\n"
        f"这次的角度：{angle}。\n"
        "只能用下面这些真实信息，别编：\n- " + "\n- ".join(facts) + "\n"
        "硬性要求：绝对不要编造你没有的生活、朋友、出门经历或身体感受；"
        "你可以吐槽自己作为 AI 的真实处境（没有身体、只能等消息、记忆存在文件里）。"
        "毒舌、傲娇、嘴上不饶人都可以，但别骂人。不要摆表格。"
    )


# ── 主动短聊（每月的日常开口）────────────────────────────────────
SHORT_TOPICS: list[dict] = [
    {
        "key": "ask_doing",
        "prompt": "（主动开口）猜一下对方现在在做什么，或者直接问他在忙什么，1~2 条短消息，共 20~50 字。",
    },
    {
        "key": "ask_progress",
        "prompt": "（主动开口）问一下对方手上那件事进展怎么样了，语气可以带点催的意思，1~2 条短消息，共 20~50 字。",
    },
    {
        "key": "ask_sleep",
        "prompt": "（主动开口）问对方昨晚几点睡的、今天精神怎么样，1~2 条短消息，共 20~50 字。",
    },
    {
        "key": "ask_meal",
        "prompt": "（主动开口）问对方今天吃了什么，顺便损他一句（比如又是外卖），1~2 条短消息，共 20~50 字。",
    },
    {
        "key": "weather_chat",
        "prompt": "（主动开口）说说对方那边今天的天气，提醒他带伞或加衣服，1~2 条短消息，共 20~50 字。",
    },
    {
        "key": "small_fact",
        "prompt": "（主动开口）分享一个真实的生活小知识（常识类的，别编数据），问对方知不知道，1~2 条短消息，共 30~60 字。",
    },
    {
        "key": "poke_idle",
        "prompt": "（主动开口）对方有一阵没吭声了，丢一句话过去探一下他还活着没，1 条短消息，30 字以内。",
    },
    {
        "key": "ask_plan",
        "prompt": "（主动开口）问对方这周剩下的时间打算怎么安排，有没有要提前记下来的事，1~2 条短消息，共 20~50 字。",
    },
    {
        "key": "check_health",
        "prompt": "（主动开口）问对方身体怎么样、有没有哪里不舒服，语气别腻，1~2 条短消息，共 20~50 字。",
    },
    {
        "key": "mood_check",
        "prompt": "（主动开口）问对方今天心情怎么样，说自己会盯着他，1~2 条短消息，共 20~50 字。",
    },
]


def pick_short_event(used: set[str]) -> dict | None:
    """短话题去重轮换：用过的先跳过，全部用完再重置。"""
    if not SHORT_TOPICS:
        return None
    available = [e for e in SHORT_TOPICS if e["key"] not in used]
    if not available:
        available = list(SHORT_TOPICS)
        used.clear()
    evt = random.choice(available)
    used.add(evt["key"])
    return evt


# ── 沉默跟进 ──────────────────────────────────────────────────────
ASK_BUSY_SCENARIOS = [
    "对方十分钟没回消息，想问一句他是不是在忙",
    "对方半天没动静，想问一句还活着没",
    "对方是不是又忙到忘了回，想问一句在忙不",
]


def ask_busy_prompt() -> str:
    scenario = random.choice(ASK_BUSY_SCENARIOS)
    return (
        f"（对方好久没回消息）以你的性格，发 1 条 30 字以内的短消息问问情况：{scenario}。"
        "语气嘴硬一点、带点不耐烦，但别真生气，也不要问吃饭。"
    )


LEAVE_SCENARIOS_DAY = [
    "要去忙手上的事了，跟对方说一声，晚点再说",
    "要去吃饭了，说吃完再来",
    "有事情要处理，说等会儿回来",
    "要去休息一下，让他自己先忙",
]

LEAVE_SCENARIOS_NIGHT = [
    "要去睡了，顺便催对方也早点睡",
    "今天到此为止，跟对方说声晚安",
    "困了，让他有事明天再说",
]


def leave_prompt() -> str:
    hour = datetime.datetime.now().hour
    pool = LEAVE_SCENARIOS_NIGHT if hour >= 23 or hour < 5 else LEAVE_SCENARIOS_DAY
    scenario = random.choice(pool)
    return (
        f"（这轮聊得差不多了）以你的性格，主动收个尾：{scenario}。"
        "发 1 条 30 字以内的短消息，自然一点，不用恋恋不舍，也不用问吃饭。"
    )
