"""时间：100% 本地，0 API、0 token。"""

from __future__ import annotations

import datetime

WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


def now_text(*, tz_offset_hours: float | None = None, city: str = "") -> str:
    moment = datetime.datetime.now()
    if tz_offset_hours is not None:
        moment = datetime.datetime.utcnow() + datetime.timedelta(hours=float(tz_offset_hours))
    elif city:
        try:
            from zoneinfo import ZoneInfo

            moment = datetime.datetime.now(ZoneInfo(city))
        except Exception:  # noqa: BLE001 - 时区名不认识就用本机时间
            moment = datetime.datetime.now()
    weekday = WEEKDAYS[moment.weekday()]
    return f"现在是 {moment.strftime('%Y-%m-%d %H:%M')}（星期{weekday}）"


def weekday_text(*, offset_days: int = 0) -> str:
    moment = datetime.datetime.now() + datetime.timedelta(days=int(offset_days))
    return f"{moment.strftime('%Y-%m-%d')} 是星期{WEEKDAYS[moment.weekday()]}"


def countdown_text(target: str) -> str:
    """target 支持 2026-09-20 或 2026-09-20 14:00。"""
    raw = (target or "").strip()
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            parsed = datetime.datetime.strptime(raw, pattern)
        except ValueError:
            continue
        if "%Y" not in pattern:
            parsed = parsed.replace(year=datetime.datetime.now().year)
        delta = parsed - datetime.datetime.now()
        total = int(delta.total_seconds())
        if total < 0:
            return f"{parsed.strftime('%m-%d %H:%M')} 已经过去了"
        days, rest = divmod(total, 86400)
        hours = rest // 3600
        return f"距离 {parsed.strftime('%m-%d %H:%M')} 还有 {days} 天 {hours} 小时"
    return ""


def handle_text(text: str, *, city: str = "") -> str:
    """规则入口：从一句话里回答时间类问题。"""
    raw = text or ""
    if any(word in raw for word in ("倒计时", "还有多久", "还有几天")):
        digits = []
        for token in raw.replace("年", "-").replace("月", "-").replace("日", " ").split():
            if any(char.isdigit() for char in token) or "-" in token:
                digits.append(token)
        for token in digits:
            cleaned = token.strip("，,。.!！?？的")
            if any(char.isdigit() for char in cleaned):
                text_out = countdown_text(cleaned)
                if text_out:
                    return text_out
    if any(word in raw for word in ("星期几", "礼拜几")):
        offset = 1 if "明天" in raw else -1 if "昨天" in raw else 0
        return weekday_text(offset_days=offset)
    return now_text(city=city)
