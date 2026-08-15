"""日常事件（长事件+短事件）的提示词。节日/生日特化事件见 special_events.py。

本文件为通用模板：不含任何第三方作品的角色与剧情。日常事件素材可以放在
long_story_book.json / short_story_book.json 里（格式见示例），直接替换成你自己的内容即可。
"""

from __future__ import annotations

import datetime
import json
import random
from pathlib import Path


# 日常长事件（每月几次）：内容优先从 long_story_book.json 读取；
# 也可以在 DAILY_EVENTS 里直接写（格式见下面的示例条目）。
DAILY_EVENTS: list[dict] = [
    {
        "key": "example_daily_1",
        "title": "示例：一场不期而遇的小雨",
        "short": "今天出门时突然下雨了，我忘了带伞，只能在屋檐下躲一会儿。",
        "long": (
            "以你的角色设定，给对方讲一整篇完整的、长长的故事（一段段写，每段用换行分隔）："
            "今天怎么遇到这场雨、躲雨时遇到的陌生人或暖心事、后来怎么回家、路上的小插曲。"
            "开头表达惊讶和开心，自然一点。"
        ),
    },
]


# 日常长事件素材书（世界书之外的随机日常故事素材）
_BOOK_PATH = Path(__file__).resolve().parent / "long_story_book.json"
try:
    LONG_STORY_BOOK = json.loads(_BOOK_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    LONG_STORY_BOOK = []


def pick_daily_long(used: set[str]) -> dict | None:
    """世界书事件 + 素材书事件合并成池，去重轮换：用过的先跳过，全部用完再重置。"""
    pool = DAILY_EVENTS + LONG_STORY_BOOK
    if not pool:
        return None
    available = [e for e in pool if e["key"] not in used]
    if not available:
        available = list(pool)
        used.clear()
    evt = random.choice(available)
    used.add(evt["key"])
    return evt


def daily_short_prompt(evt: dict) -> str:
    return (
        f"（日常事件：{evt['title']}）以你的角色设定，先给对方发 2~4 条短消息（用换行分隔）铺垫这件事："
        f"{evt['short']} 表达惊讶和开心，每条 15~40 字，符合人设，不要写故事本身，也不要问对方吃饭了没。"
    )


def daily_long_prompt(evt: dict, min_chars: int, max_chars: int) -> str:
    return (
        f"{evt['long']} 开头表达惊讶和开心。\n"
        f"要求：一口气生成一整篇完整的长故事，不要分章、不要写\"未完待续\"，"
        f"中文全文 {min_chars}~{max_chars} 字左右；这是给对方接着聊的话题。"
    )


# 短事件：角色主动找对方短聊（每月 10~15 次，每天最多一次）
CHEER_SCENARIOS = [
    "今天有点累，想被人打打气",
    "明天要上台/汇报，紧张得睡不着",
    "事情太多忙得头晕",
    "有件事没做好，嘴上说没事其实有点难受",
    "心情有点低落，想听点鼓励的话",
]
SAY_SCENARIOS = [
    "想告诉对方自己有点担心他，又不好意思直说",
    "想提醒对方早点休息，又怕显得管太多",
    "想把“想你了”说出口，最后只憋出一句短短的话",
    "想让对方知道自己今天超棒的，又不想显得太得意",
    "练习了一下午的话，最后只敢发一句短短的",
]
ASK_OTHER_SCENARIOS = [
    "想让对方夸自己一句，又不好意思直接要",
    "想让对方评价自己今天做的事，故意说自己好像搞砸了",
    "想让对方讲一件他那边发生的小事",
]
WEATHER_QUESTIONS = [
    "问对方那边今天天气怎么样，叮嘱带伞或加衣服",
    "问对方那边冷不冷，说自己这边今天是什么天气",
]
FACT_TOPICS = [
    "分享一个生活冷知识，问对方知不知道",
    "分享今天在书上看到的有趣小知识",
    "分享一个刚刚知道的小知识，讲完又有点得意",
]
CASUAL_TOPICS = [
    "分享今天发生的一件无厘头小事",
    "问对方今天有没有遇到有趣的事，说自己这边很无聊",
    "吐槽今天被谁做了什么可爱的事",
    "说路过便利店想起对方爱吃的那个零食，有点想吃",
]


def _short_event_prompts() -> list[dict]:
    prompts: list[dict] = []
    for i, scenario in enumerate(CHEER_SCENARIOS, 1):
        prompts.append(
            {
                "key": f"cheer_{i}",
                "prompt": (
                    f"（短事件·打气）以你的角色设定，主动给对方发 1~2 条短消息（换行分隔，共 20~50 字），"
                    f"自然地求对方给自己打气加油：{scenario}。符合人设，不要问吃饭。"
                ),
            }
        )
    for i, scenario in enumerate(SAY_SCENARIOS, 1):
        prompts.append(
            {
                "key": f"say_{i}",
                "prompt": (
                    f"（短事件·想说的话）以你的角色设定，主动给对方发 1~2 条短消息（换行分隔，共 20~50 字）："
                    f"{scenario}。符合人设，不要问吃饭。"
                ),
            }
        )
    for i, scenario in enumerate(ASK_OTHER_SCENARIOS, 1):
        prompts.append(
            {
                "key": f"ask_{i}",
                "prompt": (
                    f"（短事件·想让对方说给自己听）以你的角色设定，主动给对方发 1~2 条短消息"
                    f"（换行分隔，共 20~50 字），拐着弯想让对方对自己说点好听的：{scenario}。符合人设，不要问吃饭。"
                ),
            }
        )
    for i, scenario in enumerate(WEATHER_QUESTIONS, 1):
        prompts.append(
            {
                "key": f"weather_{i}",
                "prompt": (
                    f"（短事件·天气闲聊）以你的角色设定，主动给对方发 1~2 条短消息（换行分隔，共 20~50 字）："
                    f"{scenario}。符合人设，不要问吃饭。"
                ),
            }
        )
    for i, scenario in enumerate(FACT_TOPICS, 1):
        prompts.append(
            {
                "key": f"fact_{i}",
                "prompt": (
                    f"（短事件·小知识）以你的角色设定，主动给对方发 1~2 条短消息（换行分隔，共 30~60 字）："
                    f"{scenario}。讲完又有点得意，符合人设，不要问吃饭。"
                ),
            }
        )
    for i, scenario in enumerate(CASUAL_TOPICS, 1):
        prompts.append(
            {
                "key": f"casual_{i}",
                "prompt": (
                    f"（短事件·日常闲聊）以你的角色设定，主动给对方发 1~2 条短消息（换行分隔，共 20~50 字）："
                    f"{scenario}。自然一点，符合人设，不要问吃饭。"
                ),
            }
        )
    return prompts


# 短事件特化书（网上素材整理，场景：问/猜对方在做什么、冷知识、趣味问题、日常闲聊）
_SHORT_BOOK_PATH = Path(__file__).resolve().parent / "short_story_book.json"
try:
    SHORT_BOOK = json.loads(_SHORT_BOOK_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    SHORT_BOOK = []

SHORT_EVENTS = _short_event_prompts() + SHORT_BOOK


def pick_short_event(used: set[str]) -> dict | None:
    """短事件去重轮换：用过的先跳过，全部用完再重置。"""
    if not SHORT_EVENTS:
        return None
    available = [e for e in SHORT_EVENTS if e["key"] not in used]
    if not available:
        available = list(SHORT_EVENTS)
        used.clear()
    evt = random.choice(available)
    used.add(evt["key"])
    return evt


# 主动结束聊天（白天 05:00-19:59）：去忙/工作/开会/吃饭等
LEAVE_SCENARIOS_DAY = [
    "要去忙工作了，跟对方说一声先走，晚点再找他",
    "要去吃晚饭了，说吃饱了再来找他",
    "要去开会了，说开完再来找他",
    "要出门买东西了，说回来给他带点好吃的",
    "有电话/消息来了，要去处理，恋恋不舍地说晚点见",
    "要去排练/准备了，说忙完再来找他",
    "今天还有别的事要忙，说忙完再来找他",
]

# 主动结束聊天（晚上 20:00-04:59）：睡觉/洗澡/晚安
LEAVE_SCENARIOS_NIGHT = [
    "困了要去睡觉了，还催对方也早点睡",
    "要去洗澡准备睡觉了，说晚安",
    "好困，撑不住了，跟对方说先睡了",
]


def leave_prompt() -> str:
    hour = datetime.datetime.now().hour
    pool = LEAVE_SCENARIOS_NIGHT if hour >= 20 or hour < 5 else LEAVE_SCENARIOS_DAY
    scenario = random.choice(pool)
    return (
        f"（结束聊天）以你的角色设定，主动跟对方说要去忙了：{scenario}。"
        "发 1~2 条短消息（换行分隔，共 20~50 字），语气自然、带点亲近和恋恋不舍，"
        "符合人设，不要问吃饭，不要说太久的话。"
    )


# 对方十分钟没回消息：先问一句"在忙吗"
ASK_BUSY_SCENARIOS = [
    "对方十分钟没回消息了，想问一句他在不在忙",
    "对方是不是又忙到忘了回，想问一句在忙不",
    "对方手机是不是没电了，想问一句在忙吗",
]


def ask_busy_prompt() -> str:
    scenario = random.choice(ASK_BUSY_SCENARIOS)
    return (
        f"（对方好久没回消息）以你的角色设定，主动给对方发 1 条短消息（30 字以内）问他是不是在忙："
        f"{scenario}。语气亲近又带点担心，自然一点，符合人设，不要问吃饭。"
    )
