"""农历/公历互转（1900-2100，数据表来自公开的万年历算法）。"""

from __future__ import annotations

import datetime

# 农历 1900-2100 的闰月/大小月信息表（每个年份一个十六进制数）
LUNAR_INFO = [
    0x04BD8, 0x04AE0, 0x0A570, 0x054D5, 0x0D260, 0x0D950, 0x16554, 0x056A0, 0x09AD0, 0x055D2,
    0x04AE0, 0x0A5B6, 0x0A4D0, 0x0D250, 0x1D255, 0x0B540, 0x0D6A0, 0x0ADA2, 0x095B0, 0x14977,
    0x04970, 0x0A4B0, 0x0B4B5, 0x06A50, 0x06D40, 0x1AB54, 0x02B60, 0x09570, 0x052F2, 0x04970,
    0x06566, 0x0D4A0, 0x0EA50, 0x06E95, 0x05AD0, 0x02B60, 0x186E3, 0x092E0, 0x1C8D7, 0x0C950,
    0x0D4A0, 0x1D8A6, 0x0B550, 0x056A0, 0x1A5B4, 0x025D0, 0x092D0, 0x0D2B2, 0x0A950, 0x0B557,
    0x06CA0, 0x0B550, 0x15355, 0x04DA0, 0x0A5B0, 0x14573, 0x052B0, 0x0A9A8, 0x0E950, 0x06AA0,
    0x0AEA6, 0x0AB50, 0x04B60, 0x0AAE4, 0x0A570, 0x05260, 0x0F263, 0x0D950, 0x05B57, 0x056A0,
    0x096D0, 0x04DD5, 0x04AD0, 0x0A4D0, 0x0D4D4, 0x0D250, 0x0D558, 0x0B540, 0x0B6A0, 0x195A6,
    0x095B0, 0x049B0, 0x0A974, 0x0A4B0, 0x0B27A, 0x06A50, 0x06D40, 0x0AF46, 0x0AB60, 0x09570,
    0x04AF5, 0x04970, 0x064B0, 0x074A3, 0x0EA50, 0x06B58, 0x055C0, 0x0AB60, 0x096D5, 0x092E0,
    0x0C960, 0x0D954, 0x0D4A0, 0x0DA50, 0x07552, 0x056A0, 0x0ABB7, 0x025D0, 0x092D0, 0x0CAB5,
    0x0A950, 0x0B4A0, 0x0BAA4, 0x0AD50, 0x055D9, 0x04BA0, 0x0A5B0, 0x15176, 0x052B0, 0x0A930,
    0x07954, 0x06AA0, 0x0AD50, 0x05B52, 0x04B60, 0x0A6E6, 0x0A4E0, 0x0D260, 0x0EA65, 0x0D530,
    0x05AA0, 0x076A3, 0x096D0, 0x04AFB, 0x04AD0, 0x0A4D0, 0x1D0B6, 0x0D250, 0x0D520, 0x0DD45,
    0x0B5A0, 0x056D0, 0x055B2, 0x049B0, 0x0A577, 0x0A4B0, 0x0AA50, 0x1B255, 0x06D20, 0x0ADA0,
    0x14B63, 0x09370, 0x049F8, 0x04970, 0x064B0, 0x168A6, 0x0EA50, 0x06B20, 0x1A6C4, 0x0AAE0,
    0x0A2E0, 0x0D2E3, 0x0C960, 0x0D557, 0x0D4A0, 0x0DA50, 0x05D55, 0x056A0, 0x0A6D0, 0x055D4,
    0x052D0, 0x0A9B8, 0x0A950, 0x0B4A0, 0x0B6A6, 0x0AD50, 0x055A0, 0x0ABA4, 0x0A5B0, 0x052B0,
    0x0B273, 0x06930, 0x07337, 0x06AA0, 0x0AD50, 0x14B55, 0x04B60, 0x0A570, 0x054E4, 0x0D160,
    0x0E968, 0x0D520, 0x0DAA0, 0x16AA6, 0x056D0, 0x04AE0, 0x0A9D4, 0x0A2D0, 0x0D150, 0x0F252,
    0x0D520,
]

BASE_DATE = datetime.date(1900, 1, 31)  # 公历 1900-01-31 = 农历 1900-01-01


def leap_month(year: int) -> int:
    return LUNAR_INFO[year - 1900] & 0xF


def leap_days(year: int) -> int:
    if leap_month(year):
        return 30 if (LUNAR_INFO[year - 1900] & 0x10000) else 29
    return 0


def month_days(year: int, month: int) -> int:
    if month < 1 or month > 12:
        return 0
    return 30 if (LUNAR_INFO[year - 1900] & (0x10000 >> month)) else 29


def year_days(year: int) -> int:
    total = 348
    bit = 0x8000
    while bit > 0x8:
        if LUNAR_INFO[year - 1900] & bit:
            total += 1
        bit >>= 1
    return total + leap_days(year)


def solar_to_lunar(day: datetime.date) -> dict:
    """公历日期 -> 农历 {year, month, day, leap}。"""
    offset = (day - BASE_DATE).days
    if offset < 0:
        raise ValueError("不支持 1900-01-31 之前的日期")
    year = 1900
    while offset >= year_days(year):
        offset -= year_days(year)
        year += 1
    leap_m = leap_month(year)
    months: list[tuple[int, bool]] = []
    for m in range(1, 13):
        months.append((m, False))
        if leap_m == m:
            months.append((m, True))
    for m, is_leap in months:
        days = leap_days(year) if is_leap else month_days(year, m)
        if offset < days:
            return {"year": year, "month": m, "day": offset + 1, "leap": is_leap}
        offset -= days
    raise ValueError("日期超出农历可转换范围")


def lunar_to_solar(year: int, month: int, day: int, leap: bool = False) -> datetime.date:
    """农历日期 -> 公历日期。闰月时 leap=True。"""
    offset = sum(year_days(y) for y in range(1900, year))
    leap_m = leap_month(year)
    if leap:
        for m in range(1, month + 1):
            offset += month_days(year, m)
    else:
        for m in range(1, month):
            offset += month_days(year, m)
            if leap_m and m == leap_m:
                offset += leap_days(year)
    offset += day - 1
    return BASE_DATE + datetime.timedelta(days=offset)


MONTH_CN = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"]
DAY_CN = [
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
]


def lunar_name(month: int, day: int) -> str:
    prefix = "闰" if False else ""
    return f"{prefix}{MONTH_CN[month - 1]}月{DAY_CN[day - 1]}"


def lunar_date_name(day: datetime.date) -> str:
    info = solar_to_lunar(day)
    prefix = "闰" if info["leap"] else ""
    return f"{prefix}{MONTH_CN[info['month'] - 1]}月{DAY_CN[info['day'] - 1]}"


def is_lunar_new_year_eve(day: datetime.date) -> bool:
    """判断今天是不是农历腊月的最后一天（除夕）。"""
    info = solar_to_lunar(day)
    if info["month"] != 12 or info["leap"]:
        return False
    return info["day"] == month_days(info["year"], 12)
