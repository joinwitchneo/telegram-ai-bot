"""Stage 1：消息分析（规则版）。

只做一件事：把用户这句话翻译成"情绪事件 + 意图"，交给 Python 更新状态。
不用额外调用模型，保证速度和稳定；以后要换成模型判断，只要保持返回结构不变。
"""

from __future__ import annotations

import re

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]"
)
PUNCT_RE = re.compile(r"[，。！？!?,.;；：:]")


def analyze_style(messages: list[str]) -> dict:
    """提取用户这几条消息的聊天风格（对应文档第二节的 ChatStyle）。

    只描述"怎么发"，不判断"说什么"。
    """
    texts = [m.strip() for m in messages if m and m.strip()]
    if not texts:
        return {
            "message_count": 0, "average_length": 0.0, "current_length": 0,
            "sentence_count": 0, "punctuation_style": "normal", "emoji_usage": False,
            "emoji_count": 0, "newline_usage": False, "burst_style": False,
            "question_style": False, "energy": "normal", "formality": "normal",
            "length_class": "normal",
        }
    joined = "\n".join(texts)
    lengths = [len(re.sub(r"\s", "", t)) for t in texts]
    avg_length = sum(lengths) / len(lengths)
    current_length = lengths[-1]
    emoji_count = len(EMOJI_RE.findall(joined))
    punct_count = len(PUNCT_RE.findall(joined))
    sentence_count = len(re.findall(r"[。！？!?…]+", joined)) or len(texts)
    high_energy_words = ("哈哈", "卧槽", "操", "笑死", "救命", "草", "!!", "！！")
    casual_words = ("哈哈", "卧槽", "草", "兄弟", "老哥", "嘛", "呗", "啦", "啊这")
    return {
        "message_count": len(texts),
        "average_length": round(avg_length, 1),
        "current_length": current_length,
        "sentence_count": sentence_count,
        "punctuation_style": "sparse" if punct_count <= len(texts) else "normal",
        "emoji_usage": emoji_count > 0,
        "emoji_count": emoji_count,
        "newline_usage": len(texts) > 1,
        "burst_style": len(texts) >= 2,
        "question_style": any(("?" in t or "？" in t) for t in texts),
        "energy": "high" if (emoji_count or any(w in joined for w in high_energy_words)) else "normal",
        "formality": "low" if (emoji_count or any(w in joined for w in casual_words) or punct_count == 0) else "normal",
        "length_class": (
            "ultra_short" if avg_length <= 6
            else "long" if current_length >= 120 or avg_length >= 80
            else "normal"
        ),
    }

PRAISE_WORDS = [
    "可爱", "厉害", "好棒", "真棒", "喜欢你", "谢谢你", "辛苦了", "真聪明", "聪明",
    "不错", "爱你", "好看", "牛", "优秀", "贴心", "温柔", "好厉害", "真行", "靠谱",
]
CARE_WORDS = [
    "你还好吗", "还好吗", "累不累", "注意身体", "早点睡", "该睡了", "吃饭了吗", "吃了吗",
    "别太累", "多喝水", "休息一下", "别熬夜", "照顾好自己", "冷不冷", "怎么了",
]
APOLOGY_WORDS = ["对不起", "抱歉", "我错了", "别生气", "原谅我", "不是故意", "我反省", "我改"]
INSULT_HARD = ["傻逼", "废物", "去死", "滚远", "sb", "垃圾", "没用的东西", "智障", "脑残"]
INSULT_MILD = ["真蠢", "好蠢", "笨蛋", "傻子", "笨死", "滚", "闭嘴", "烦人", "讨厌你", "有病"]
TEASE_WORDS = ["开玩笑", "逗你", "骗你的", "哈哈", "笑死", "嘻嘻", "嘿嘿", "哈哈哈"]
COLD_WORDS = {"哦", "嗯", "行", "好", "知道了", "随便", "呵呵", "哦哦", "嗯嗯", "。"}
RETURN_WORDS = ["我回来了", "回来了", "在吗", "好久不见", "好久没", "想你了", "在么", "我回来了"]
QUESTION_WORDS = ["为什么", "怎么办", "你觉得", "你说", "是不是", "如何", "吗？", "吗?", "？", "?"]
INTERESTING_WORDS = [
    "游戏", "电影", "番", "动漫", "音乐", "旅行", "学习", "考试", "工作", "加班",
    "难过", "开心", "烦", "累", "生气", "喜欢", "讨厌", "梦想", "以后", "未来",
]
BORING_WORDS = ["在干嘛", "在吗", "哦", "嗯", "行", "好的", "test", "测试"]

# 收尾信号：他说完这些就是要结束了
CLOSING_WORDS = [
    "晚安", "睡了", "睡觉去", "去睡", "我睡", "先这样", "不聊了", "改天聊", "晚点聊",
    "拜拜", "再见", "我走了", "出门了", "我出门", "去忙了", "先去忙", "下班了", "开会去",
]

# 开始讲自己的事（进入深聊）
DEEP_WORDS = [
    "我想跟你说", "我跟你说个事", "其实我", "说实话", "我最近", "我心里", "我有点难过",
    "我很难受", "纠结", "迷茫", "压力好大", "我该怎么办", "我不知道怎么",
]


def is_closing(text: str) -> bool:
    """他是不是在收尾（说完就准备撤）。"""
    raw = (text or "").strip()
    if not raw or len(raw) > 40:
        return False
    return any(word in raw for word in CLOSING_WORDS)


def is_deep_talk(text: str) -> bool:
    """他是不是开始认真讲自己的事。"""
    raw = (text or "").strip()
    cjk = len(re.findall(r"[\u4e00-\u9fff]", raw))
    if any(word in raw for word in DEEP_WORDS):
        return True
    return cjk >= 80


def _has_any(text: str, words) -> bool:
    return any(word in text for word in words)


def _count(text: str, words) -> int:
    return sum(text.count(word) for word in words)


def describe_style(style: dict) -> str:
    """把 ChatStyle 说成一句给模型看的话。"""
    if not style or not style.get("message_count"):
        return ""
    parts = [
        f"他这次发了 {style.get('message_count')} 条消息",
        f"平均每条 {style.get('average_length')} 字",
        f"最新一条 {style.get('current_length')} 字",
        "连着发的" if style.get("burst_style") else "单条发的",
        "几乎不打标点" if style.get("punctuation_style") == "sparse" else "会用标点",
    ]
    parts.append(f"用了 {style.get('emoji_count')} 个 emoji" if style.get("emoji_usage") else "没用 emoji")
    if style.get("energy") == "high":
        parts.append("情绪很高")
    if style.get("formality") == "low":
        parts.append("语气随意")
    if style.get("question_style"):
        parts.append("在向你提问")
    return "；".join(parts)


def analyze(text: str, recent_context: list[dict] | None = None, cold_streak: int = 0) -> dict:
    """把一句话解析成情绪事件与意图。"""
    raw = (text or "").strip()
    lowered = raw.lower()
    cjk_len = len(re.findall(r"[\u4e00-\u9fff]", raw))
    style = analyze_style(raw.split("\n"))
    events: list[dict] = []
    cold = raw in COLD_WORDS or (cjk_len <= 2 and lowered in {"ok", "okay", "嗯", "哦"})

    # 1) 骂 / 怼（先判断，优先级最高）
    if _has_any(lowered, INSULT_HARD):
        events.append({"event": "USER_INSULT", "weight": 1.0})
        conflict = True
    elif _has_any(raw, INSULT_MILD):
        events.append({"event": "USER_INSULT_MILD", "weight": 1.0})
        conflict = True
    else:
        conflict = False

    # 2) 道歉
    if _has_any(raw, APOLOGY_WORDS):
        events.append({"event": "USER_APOLOGY", "weight": 1.0})

    # 3) 夸奖 / 关心
    if _has_any(raw, PRAISE_WORDS):
        events.append({"event": "USER_PRAISE", "weight": min(1.5, 0.8 + cjk_len / 40)})
    if _has_any(raw, CARE_WORDS):
        events.append({"event": "USER_CARE", "weight": 1.0})

    # 4) 玩笑 / 调侃
    if _has_any(raw, TEASE_WORDS):
        events.append({"event": "USER_JOKE", "weight": 1.0})
        if "你" in raw:
            events.append({"event": "USER_TEASE", "weight": 0.8})

    # 5) 冷淡（连续出现会更明显）
    if cold:
        weight = 1.0 + min(1.5, cold_streak * 0.5)
        events.append({"event": "USER_COLD", "weight": weight})

    # 6) 回来 / 主动找
    if _has_any(raw, RETURN_WORDS):
        events.append({"event": "USER_RETURNS", "weight": 1.0})

    # 7) 话题有没有意思
    if cjk_len >= 12 or _has_any(raw, INTERESTING_WORDS):
        events.append({"event": "TOPIC_INTERESTING", "weight": 0.8})
    elif cjk_len <= 6 and _has_any(raw, BORING_WORDS):
        events.append({"event": "TOPIC_BORING", "weight": 0.6})

    # 8) 兜底：正常聊天
    if not events:
        events.append({"event": "USER_CHAT", "weight": 1.0})

    if conflict:
        user_intent = "conflict"
    elif _has_any(raw, APOLOGY_WORDS):
        user_intent = "apology"
    elif _has_any(raw, QUESTION_WORDS):
        user_intent = "question"
    elif cjk_len >= 40:
        user_intent = "long_talk"
    elif cold:
        user_intent = "cold"
    else:
        user_intent = "casual_chat"

    if conflict:
        user_emotion = "angry"
    elif cold:
        user_emotion = "bored"
    elif _has_any(raw, ["累", "困", "加班", "睡不着"]):
        user_emotion = "tired"
    elif _has_any(raw, ["难过", "烦", "不开心", "委屈"]):
        user_emotion = "sad"
    elif _has_any(raw, TEASE_WORDS):
        user_emotion = "happy"
    else:
        user_emotion = "neutral"

    # 关系变化：正常聊天慢慢涨，冲突扣分（幅度都很小）
    if conflict:
        relationship_effect = -0.02
    elif _has_any(raw, APOLOGY_WORDS):
        relationship_effect = 0.004
    elif cjk_len >= 8:
        relationship_effect = 0.003
    else:
        relationship_effect = 0.001

    return {
        "raw": raw,
        "style": style,
        "user_emotion": user_emotion,
        "user_intent": user_intent,
        "events": events,
        "relationship_effect": relationship_effect,
        "conflict": conflict,
        "cold": cold,
        "cjk_len": cjk_len,
        "memory_candidates": [],
    }
