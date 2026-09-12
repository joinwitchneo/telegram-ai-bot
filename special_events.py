"""节日/特别日子：到日子发一条真心话式的短消息（不写长故事）。

只用真实日期：公历节日、农历节日、除夕，以及用户自己的生日。
"""

from __future__ import annotations

import datetime

import lunar

# 用户生日（示例，默认关闭）。想启用就填成：
# USER_BIRTHDAY = {"key": "user_birthday", "name": "你的生日", "calendar": "lunar", "m": 1, "d": 1, "note": "农历正月初一"}
USER_BIRTHDAY: dict | None = None

# 其他想记住的日子（角色生日之类）可以往这里加
CHARACTER_BIRTHDAYS: list[dict] = []

FESTIVALS = [
    {"key": "newyear", "name": "元旦", "calendar": "solar", "m": 1, "d": 1, "note": "新年的第一天"},
    {"key": "valentine", "name": "情人节", "calendar": "solar", "m": 2, "d": 14, "note": "情人节"},
    {"key": "white_day", "name": "白色情人节", "calendar": "solar", "m": 3, "d": 14, "note": "回礼的日子"},
    {"key": "children", "name": "儿童节", "calendar": "solar", "m": 6, "d": 1, "note": "儿童节"},
    {"key": "qixi_solar", "name": "七夕", "calendar": "solar", "m": 7, "d": 7, "note": "七夕"},
    {"key": "mid_autumn_solar", "name": "中秋", "calendar": "solar", "m": 9, "d": 25, "note": "中秋前后"},
    {"key": "national", "name": "国庆", "calendar": "solar", "m": 10, "d": 1, "note": "国庆假期"},
    {"key": "halloween", "name": "万圣节", "calendar": "solar", "m": 10, "d": 31, "note": "万圣节"},
    {"key": "christmas", "name": "圣诞节", "calendar": "solar", "m": 12, "d": 25, "note": "圣诞"},
    {"key": "newyear_eve", "name": "跨年夜", "calendar": "solar", "m": 12, "d": 31, "note": "一年的最后一天"},
    {"key": "spring_festival", "name": "春节", "calendar": "lunar", "m": 1, "d": 1, "note": "农历新年"},
    {"key": "lantern", "name": "元宵节", "calendar": "lunar", "m": 1, "d": 15, "note": "元宵"},
    {"key": "dragon_boat", "name": "端午节", "calendar": "lunar", "m": 5, "d": 5, "note": "端午"},
    {"key": "qixi", "name": "七夕", "calendar": "lunar", "m": 7, "d": 7, "note": "农历七夕"},
    {"key": "mid_autumn", "name": "中秋节", "calendar": "lunar", "m": 8, "d": 15, "note": "中秋"},
    {"key": "ny_eve", "name": "除夕", "calendar": "ny_eve", "m": 0, "d": 0, "note": "一年最后一晚"},
]


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
        if _event_date(evt, today) == today:
            found.append({**evt, "kind": "festival"})
    if USER_BIRTHDAY:
        if _event_date(USER_BIRTHDAY, today) == today:
            found.append({**USER_BIRTHDAY, "kind": "user_birthday"})
    return found


def pick_top_event(today: datetime.date | None = None) -> dict | None:
    found = today_events(today)
    if not found:
        return None
    priority = {"user_birthday": 30, "birthday": 20, "festival": 10}
    return max(found, key=lambda e: priority.get(e.get("kind"), 0))


def special_short_prompt(evt: dict) -> str:
    """节日/生日：一条真心话，不是任务式的祝福。"""
    name = evt.get("name", "今天")
    note = evt.get("note", "")
    kind = evt.get("kind", "festival")
    prefix = f"（今天是{name}{'（' + note + '）' if note else ''}）"
    if kind == "user_birthday":
        return (
            prefix
            + "以你的性格，给他发 1~2 条短消息（用换行分隔，总共 30~80 字）。"
            "不要写“生日祝福模板”那种客套话（什么幸福快乐万事如意都别提），"
            "就说你真正想说的：可以嘴硬、可以翻旧账、可以说你其实一直记着这个日子；"
            "可以提一句以前聊过的事。绝对不要提你自己的生日或你没有的生活。不要摆表格。"
        )
    return (
        prefix
        + "以你的性格，给他发 1~2 条短消息（用换行分隔，总共 30~80 字）。"
        "不要念祝福词、不要写小作文，就说一句你真正想说的：可以损他、"
        "可以催他趁今天歇歇、可以提一句你记得他的事。"
        "不许编造你自己的节日经历（你没有生活），也不要摆表格。"
    )
