"""节日/生日特殊事件：日常长事件的特化（调用世界书，每个日子有专门剧情角度）。

本文件为通用模板：不包含任何第三方作品的角色、剧情或个人信息。
想启用角色生日/特别日子，直接在下面的列表里添加你自己的条目即可。
"""

from __future__ import annotations

import datetime

import lunar

# 角色生日（公历）。示例（取消注释即可启用）：
# CHARACTER_BIRTHDAYS = [
#     {"key": "my_char", "name": "我的角色", "m": 9, "d": 5, "note": "角色备注"},
# ]
CHARACTER_BIRTHDAYS: list[dict] = []

# 节日（solar=公历，lunar=农历，ny_eve=除夕）。可按需增删。
FESTIVALS = [
    {"key": "newyear_jp", "name": "元旦", "calendar": "solar", "m": 1, "d": 1, "note": "新年"},
    {"key": "valentine", "name": "情人节", "calendar": "solar", "m": 2, "d": 14, "note": "巧克力"},
    {"key": "hina", "name": "女儿节", "calendar": "solar", "m": 3, "d": 3, "note": "雏人偶"},
    {"key": "white_day", "name": "白色情人节", "calendar": "solar", "m": 3, "d": 14, "note": "回礼"},
    {"key": "kodomo", "name": "儿童节", "calendar": "solar", "m": 5, "d": 5, "note": "鲤鱼旗"},
    {"key": "tanabata", "name": "七夕", "calendar": "solar", "m": 7, "d": 7, "note": "日本七夕"},
    {"key": "obon", "name": "盂兰盆节", "calendar": "solar", "m": 8, "d": 15, "note": "祭祖"},
    {"key": "halloween", "name": "万圣节", "calendar": "solar", "m": 10, "d": 31, "note": "南瓜"},
    {"key": "christmas", "name": "圣诞节", "calendar": "solar", "m": 12, "d": 25, "note": "平安夜"},
    {"key": "oomisoka", "name": "大晦日", "calendar": "solar", "m": 12, "d": 31, "note": "跨年"},
    {"key": "spring_festival", "name": "春节", "calendar": "lunar", "m": 1, "d": 1, "note": "过年"},
    {"key": "lantern", "name": "元宵节", "calendar": "lunar", "m": 1, "d": 15, "note": "汤圆/灯会"},
    {"key": "dragon_boat", "name": "端午节", "calendar": "lunar", "m": 5, "d": 5, "note": "粽子"},
    {"key": "qixi", "name": "七夕", "calendar": "lunar", "m": 7, "d": 7, "note": "中国情人节"},
    {"key": "mid_autumn", "name": "中秋节", "calendar": "lunar", "m": 8, "d": 15, "note": "月饼/月见"},
    {"key": "ny_eve_cn", "name": "除夕", "calendar": "ny_eve", "m": 0, "d": 0, "note": "守岁"},
]

# 你希望机器人记住的特殊日子（例如用户生日）。农历用 calendar="lunar"。默认关闭。
# USER_BIRTHDAY = {"key": "user_birthday", "name": "对方的生日", "calendar": "lunar", "m": 8, "d": 13}
USER_BIRTHDAY: dict | None = None


def _event_date(evt: dict, today: datetime.date) -> datetime.date | None:
    cal = evt["calendar"]
    if cal == "solar":
        try:
            return datetime.date(today.year, evt["m"], evt["d"])
        except ValueError:
            return None
    if cal == "lunar":
        try:
            return lunar.lunar_to_solar(today.year, evt["m"], evt["d"])
        except (ValueError, KeyError, IndexError):
            return None
    if cal == "ny_eve":
        info = lunar.solar_to_lunar(today)
        if info["month"] == 12 and not info["leap"]:
            return lunar.lunar_to_solar(info["year"], 12, lunar.month_days(info["year"], 12))
        return None
    return None


def today_events(today: datetime.date | None = None) -> list[dict]:
    today = today or datetime.date.today()
    found: list[dict] = []
    for evt in CHARACTER_BIRTHDAYS:
        try:
            date = datetime.date(today.year, evt["m"], evt["d"])
        except ValueError:
            continue
        if date == today:
            found.append({**evt, "calendar": "solar", "kind": "birthday"})
    for evt in FESTIVALS:
        date = _event_date(evt, today)
        if date == today:
            found.append({**evt, "kind": "festival"})
    if USER_BIRTHDAY:
        user_date = _event_date(USER_BIRTHDAY, today)
        if user_date == today:
            found.append({**USER_BIRTHDAY, "kind": "user_birthday"})
    return found


def pick_top_event(today: datetime.date | None = None) -> dict | None:
    found = today_events(today)
    if not found:
        return None
    priority = {"user_birthday": 30, "birthday": 20, "festival": 10}
    return max(found, key=lambda e: priority.get(e.get("kind"), 0))


# 每个日子的专属剧情角度（调用世界书：谁出场、发生什么事）。可按需改写。
STORY_ANGLES = {
    "newyear_jp": "新年第一天，和亲近的人一起迎接新年：准备年菜、看日出、互道祝福，许下新一年的愿望。",
    "valentine": "情人节，亲手准备了一份小礼物或点心，想着怎么送出去才自然，闹了点小笑话。",
    "hina": "女儿节，摆出雏人偶，听长辈讲小时候的故事，一起做应景的点心。",
    "white_day": "白色情人节，回礼的小事：挑了很久的礼物、藏在口袋里差点忘记、最后顺其自然地送出去。",
    "kodomo": "儿童节，挂起鲤鱼旗，聊起小时候的趣事，互相取笑又觉得怀念。",
    "tanabata": "七夕，把愿望写在短册上挂上竹子，聊各自的愿望，星星和银河。",
    "obon": "盂兰盆节，回老家祭祖，翻看老照片，讲起家人过去的往事，心里又酸又暖。",
    "halloween": "万圣节，换上装扮、准备糖果，被谁吓到或反过来吓到了谁。",
    "christmas": "圣诞夜，一起装饰、交换礼物，拆礼物时的惊喜和感动。",
    "oomisoka": "大晦日，一起跨年，整理这一年的回忆，零点时送出第一句新年祝福。",
    "spring_festival": "春节，学着包饺子、贴春联，视频或当面拜年，说吉祥话。",
    "lantern": "元宵节，出门看灯会、猜灯谜，吃一碗热乎乎的汤圆。",
    "dragon_boat": "端午节，第一次学包粽子，糯米撒得到处都是，最后包出来的粽子歪歪扭扭但很香。",
    "qixi": "七夕，嘴上说着不是什么特别的日子，其实偷偷准备了礼物。",
    "mid_autumn": "中秋节，买了月饼，一起赏月，说这样也算一起过节了。",
    "ny_eve_cn": "除夕夜，一起守岁，学着说“岁岁平安”，零点时小声许愿。",
}


def special_short_prompt(evt: dict) -> str:
    name = evt["name"]
    kind = evt.get("kind", "festival")
    if kind == "birthday":
        return (
            f"（今天是{name}的生日！）以你的角色设定，先给对方发 2~4 条短消息（用换行分隔）"
            f"表达惊喜和开心：提到今天是{name}的生日、你们在怎么庆祝或准备怎么庆祝。"
            "每条 15~40 字，符合人设，不要写故事本身，也不要问对方吃饭了没。"
        )
    return (
        f"（今天是{name}！）以你的角色设定，先给对方发 2~4 条短消息（用换行分隔）"
        f"表达惊喜和开心：提到今天是{name}、这个节日的习惯、想让对方一起感受。"
        "每条 15~40 字，符合人设，不要写故事本身，也不要问对方吃饭了没。"
    )


def special_long_prompt(evt: dict, min_chars: int, max_chars: int) -> str:
    name = evt["name"]
    angle = STORY_ANGLES.get(evt.get("key", ""), "今天这个日子发生的一件暖心小事")
    return (
        f"今天是{name}（{evt.get('note', '')}）。以你的角色设定，给对方讲一整篇完整的长故事"
        f"（一段段地写，每段用换行分隔），讲今天这个日子发生的事：{angle}"
        "开头表达惊讶和开心，中间自然地带上你的角色设定和世界观，结尾是给对方的祝福。"
        "像发超长消息一样。\n"
        f"要求：一口气生成一整篇完整文本，不要分章、不要写\"未完待续\"，"
        f"中文全文 {min_chars}~{max_chars} 字左右；这是给对方接着聊的话题。"
    )
